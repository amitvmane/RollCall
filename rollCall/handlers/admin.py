"""
Admin handlers: /delete_user, /set_status, /audit_log, /gentoken, audit_pagination_callback
"""
import html
import logging
import os
from datetime import datetime, timedelta, timezone

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_state import bot, _pending_deletes, _pending_overrides, _esc_md, _prune_pending, reply_error
from db import generate_api_token, insert_api_token, _hash_token
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
)
from functions import admin_rights, roll_call_not_started
from models import User
from rollcall_manager import manager
from services import admin as admin_svc


_AUDIT_PER_PAGE = 15

_AUDIT_LABELS = {
    "new_rollcall":          "📋 New Rollcall",
    "end_rollcall":          "🏁 End Rollcall",
    "set_admins":            "🔧 Admin Mode ON",
    "unset_admins":          "🔧 Admin Mode OFF",
    "delete_template":       "🗑️ Delete Template",
    "create_template":       "📝 Create/Update Template",
    "schedule_template":     "📅 Schedule Template",
    "start_template":        "▶️ Start Template",
    "sif":                   "➡️ Set IN For",
    "sof":                   "➡️ Set OUT For",
    "smf":                   "➡️ Set MAYBE For",
    "delete_user":           "🗑️ Delete User",
    "set_status":            "🔄 Set Status",
    "toggle_ghost_tracking": "👻 Ghost Tracking",
    "buzz":                  "📢 Buzz",
    "shh_on":                "🤫 Quiet Mode ON",
    "shh_off":               "🔊 Quiet Mode OFF",
    "timezone":              "🕐 Timezone",
}


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _fmt_audit_entry(r: dict) -> str:
    ts = html.escape(str(r.get("created_at", ""))[:16].replace("T", " "))
    admin = html.escape(r.get("admin_name") or "—")
    label = html.escape(_AUDIT_LABELS.get(r.get("action_type", ""), r.get("action_type", "")))
    target = html.escape(str(r["target_name"])) if r.get("target_name") else ""
    details = html.escape(str(r["details"])) if r.get("details") else ""
    suffix = (f"  {target}  ·  {details}" if target and details
              else f"  {target}" if target
              else f"  {details}" if details
              else "")
    return f"• <code>{ts}</code>  <b>{admin}</b>  {label}{suffix}"


def _build_audit_keyboard(page: int, total_pages: int, per_page: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    prev = InlineKeyboardButton("← Prev", callback_data=f"audit_pg_{page-1}_{per_page}") \
        if page > 1 else InlineKeyboardButton("·", callback_data="audit_noop")
    label = InlineKeyboardButton(f"{page}/{total_pages}", callback_data="audit_noop")
    nxt = InlineKeyboardButton("Next →", callback_data=f"audit_pg_{page+1}_{per_page}") \
        if page < total_pages else InlineKeyboardButton("·", callback_data="audit_noop")
    markup.add(prev, label, nxt)
    return markup


async def _send_audit_page(cid: int, page: int, per_page: int, edit_msg_id: int = None):
    data = admin_svc.get_audit_log(cid, page=page, per_page=per_page)
    total = data["total"]
    if total == 0:
        text = "No commands recorded yet."
        if edit_msg_id:
            await bot.edit_message_text(text, cid, edit_msg_id)
        else:
            await bot.send_message(cid, text)
        return

    page = data["page"]
    total_pages = data["total_pages"]
    records = data["records"]

    lines = [f"<b>🔍 Audit Log — Page {page}/{total_pages}  ({total} total)</b>", ""]
    lines += [_fmt_audit_entry(r) for r in records]
    text = "\n".join(lines)
    markup = _build_audit_keyboard(page, total_pages, per_page) if total_pages > 1 else None

    if edit_msg_id:
        try:
            await bot.edit_message_text(text, cid, edit_msg_id, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            if "message is not modified" not in str(e):
                raise
    else:
        await bot.send_message(cid, text, parse_mode="HTML", reply_markup=markup)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
async def delete_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        msg = message.text
        cid = message.chat.id
        arr = msg.split(" ")
        rc_number = 0

        if len(arr) > 1 and "::" in arr[-1]:
            try:
                rc_number = int(arr[-1].replace("::", "")) - 1
                del arr[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        name = " ".join(arr[1:])
        admin_id = message.from_user.id

        _prune_pending(_pending_deletes)
        _prune_pending(_pending_overrides)
        _pending_deletes[(cid, admin_id)] = {
            'name': name,
            'rc_number': rc_number,
            '_ts': datetime.now().timestamp(),
        }
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"delconf_yes_{rc_number}_{admin_id}"),
            InlineKeyboardButton("❌ Cancel",      callback_data=f"delconf_no_{rc_number}_{admin_id}"),
        )
        await bot.send_message(
            cid,
            f"⚠️ Remove *{_esc_md(name)}* from rollcall #{rc_number + 1}?",
            parse_mode="Markdown",
            reply_markup=markup,
        )

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/audit_log")
async def audit_log_command(message):
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split()
        per_page = _AUDIT_PER_PAGE
        if len(parts) > 1:
            try:
                per_page = max(5, min(50, int(parts[1])))
            except ValueError:
                pass
        await _send_audit_page(cid, page=1, per_page=per_page)
    except (insufficientPermissions,) as e:
        await reply_error(message, e)
    except Exception:
        logging.exception("Error in /audit_log")
        await bot.send_message(message.chat.id, "Error fetching audit log.")


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/set_status")
async def set_status_override(message):
    cid = message.chat.id
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        parts = message.text.strip().split()
        rc_number = 0

        if len(parts) > 1 and "::" in parts[-1]:
            try:
                rc_number = int(parts[-1].replace("::", "")) - 1
                parts = parts[:-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        if len(parts) < 3:
            await bot.send_message(
                cid,
                "Usage: /set_status <name> <in|out|maybe> [::N]\n"
                "Example: /set_status Alice in"
            )
            return

        new_status = parts[-1].lower()
        if new_status not in ("in", "out", "maybe"):
            await bot.send_message(cid, "Status must be one of: in, out, maybe")
            return

        name = " ".join(parts[1:-1])

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        candidates = []
        bucket_map = (
            (rc.inList, 'in'), (rc.outList, 'out'), (rc.maybeList, 'maybe'), (rc.waitList, 'waitlist'),
        )
        wanted = name.lstrip("@").lower()
        for lst, status_name in bucket_map:
            for u in lst:
                if (u.username and u.username.lower() == wanted) or u.name.lower() == name.lower():
                    candidates.append((u, status_name))

        if not candidates:
            await bot.send_message(cid, f"⚠️ User '{name}' not found in rollcall #{rc_number + 1}.")
            return
        if len(candidates) > 1:
            distinct = {(u.user_id, s) for u, s in candidates}
            if len(distinct) > 1:
                hint = ", ".join(sorted({u.name for u, _ in candidates}))
                await bot.send_message(
                    cid,
                    f"⚠️ '{name}' matches multiple users ({hint}). Use the exact @username to disambiguate.",
                )
                return
        found_user, current_status = candidates[0]

        if current_status == new_status:
            await bot.send_message(
                cid,
                f"ℹ️ *{_esc_md(found_user.name)}* is already {new_status.upper()} in rollcall #{rc_number + 1}.",
                parse_mode="Markdown",
            )
            return

        admin_id = message.from_user.id
        _pending_overrides[(cid, admin_id)] = {
            'user': found_user,
            'new_status': new_status,
            'rc_number': rc_number,
            '_ts': datetime.now().timestamp(),
        }

        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton(f"✅ Move to {new_status.upper()}", callback_data=f"ovrd_yes_{rc_number}_{admin_id}"),
            InlineKeyboardButton("❌ Cancel",                         callback_data=f"ovrd_no_{rc_number}_{admin_id}"),
        )
        await bot.send_message(
            cid,
            f"Move *{_esc_md(found_user.name)}* → *{new_status.upper()}* in rollcall #{rc_number + 1}?",
            parse_mode="Markdown",
            reply_markup=markup,
        )

    except (rollCallNotStarted, incorrectParameter, insufficientPermissions, parameterMissing) as e:
        await reply_error(cid, e)
    except Exception:
        logging.exception("Error in /set_status")
        await bot.send_message(cid, "Error processing /set_status, please try again.")


@bot.message_handler(commands=["gentoken"])
async def gentoken_command(message):
    cid = message.chat.id
    uid = message.from_user.id

    if message.chat.type not in ("group", "supergroup"):
        await bot.send_message(cid, "⛔ /gentoken only works in group chats.")
        return

    # Always require Telegram admin status — independent of the bot's admin_rights setting.
    try:
        member = await bot.get_chat_member(cid, uid)
    except Exception:
        logging.exception("gentoken: get_chat_member failed")
        await bot.send_message(cid, "⚠️ Could not verify your admin status. Please try again.")
        return

    if member.status not in ("administrator", "creator"):
        await bot.send_message(cid, "⛔ Only Telegram group admins can generate API tokens.")
        return

    token = generate_api_token()
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365)
    requester_name = message.from_user.first_name or message.from_user.username or str(uid)
    chat_title = message.chat.title or str(cid)

    insert_api_token(
        token_hash=_hash_token(token),
        chat_id=cid,
        scopes="admin,read,vote",
        label=f"{chat_title} — /gentoken by {requester_name}",
        issued_by_user_id=uid,
        expires_at=expires_at,
    )

    web_base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    dashboard_line = (
        f"\n🖥 Admin dashboard: {web_base}/admin/\n"
        if web_base else ""
    )

    dm_text = (
        f"🔑 *API Token — {_esc_md(chat_title)}*\n\n"
        f"`{token}`\n\n"
        f"⚠️ _Save this now — it won't be shown again\\._\n\n"
        f"*Chat ID:* `{cid}`\n"
        f"*Scopes:* read, vote, admin\n"
        f"*Expires:* {expires_at.strftime('%d %b %Y')}\n"
        f"{dashboard_line}\n"
        f"When it expires, run /gentoken in the group again\\."
    )

    try:
        await bot.send_message(uid, dm_text, parse_mode="Markdown")
        await bot.send_message(
            cid,
            f"✅ Token sent to you via DM, {requester_name}.\n"
            f"Expires: {expires_at.strftime('%d %b %Y')}.",
        )
    except Exception as e:
        err = str(e).lower()
        if "forbidden" in err or "initiate conversation" in err or "bot was blocked" in err:
            try:
                me = await bot.get_me()
                start_link = f"\n\n👉 t.me/{me.username}"
            except Exception:
                start_link = ""
            await bot.send_message(
                cid,
                f"⚠️ {requester_name}, I couldn't DM you — please start a private chat with me "
                f"first, then run /gentoken again in this group.{start_link}",
            )
        else:
            logging.exception("gentoken: failed to send DM")
            await bot.send_message(cid, "⚠️ Token was generated but I couldn't send it via DM. Please try again.")


@bot.callback_query_handler(func=lambda call: call.data and (
    call.data.startswith("audit_pg_") or call.data == "audit_noop"
))
async def audit_pagination_callback(call):
    try:
        if call.data == "audit_noop":
            await bot.answer_callback_query(call.id)
            return
        parts = call.data.split("_")  # audit_pg_<page>_<per_page>
        page = int(parts[2])
        per_page = int(parts[3])
        cid = call.message.chat.id
        await bot.answer_callback_query(call.id)
        await _send_audit_page(cid, page=page, per_page=per_page, edit_msg_id=call.message.message_id)
    except Exception as e:
        err_str = str(e)
        if "query is too old" in err_str or "query ID is invalid" in err_str:
            logging.warning(f"[{_ts()}] Stale audit callback ignored")
            return
        logging.exception("Error in audit_pagination_callback")
        try:
            await bot.answer_callback_query(call.id, "Error loading page")
        except Exception:
            pass
