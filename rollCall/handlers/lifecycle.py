"""
Lifecycle handlers: /src, /erc, /set_title, /panel, inline keyboards, panel machinery,
notify_proxy_owner_wait_to_in, and the btn_* callback handler.
"""
import asyncio
import logging
import os
from datetime import datetime

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_state import (
    bot, _panel_msg_ids, _pending_deletes, _pending_overrides,
    _log_task_exc,
    _is_rate_limited, _get_display_name, format_mention_with_name,
    format_mention_with_name_md, _esc_md,
    warn_no_username, _dm_promoted_real_user, get_rc_db_id, reply_error,
)
from config import ADMINS
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    duplicateProxy, repeatlyName, amountOfRollCallsReached, alreadyInList,
)
from functions import admin_rights, roll_call_not_started
from models import User
from rollcall_manager import manager
from db import update_rollcall
from services import rollcalls as rollcalls_svc
from services import voting as voting_svc


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _build_panel_text(rc, rc_number: int) -> str:
    """Panel text with optional web voting link appended."""
    text = rc.allList().replace("__RCID__", str(rc_number))
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    if not base:
        return text
    # Prefer permanent group URL (bookmarkable); fall back to per-rollcall URL
    try:
        from db import get_or_create_chat
        chat = get_or_create_chat(rc.chat_id)
        group_token = chat.get("group_web_token")
    except Exception:
        group_token = None
    if group_token:
        text += f"\n\n🔗 Web: {base}/web/group/{group_token}"
    else:
        token = getattr(rc, "web_token", None)
        if token:
            text += f"\n\n🔗 Web: {base}/web/join/{token}"
    return text


# ── Inline keyboards ──────────────────────────────────────────────────────────

async def get_status_keyboard(rc_number: int = 0) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("✅ IN",       callback_data=f"btn_in_{rc_number}"),
        InlineKeyboardButton("❌ OUT",      callback_data=f"btn_out_{rc_number}"),
        InlineKeyboardButton("❓ MAYBE",    callback_data=f"btn_maybe_{rc_number}"),
    )
    markup.add(
        InlineKeyboardButton("📋 Lists",   callback_data=f"btn_lists_{rc_number}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"btn_refresh_{rc_number}"),
    )
    markup.add(
        InlineKeyboardButton(f"🛑 End RollCall #{rc_number}", callback_data=f"btn_end_{rc_number}")
    )
    return markup


async def get_lists_keyboard(rc_number: int = 0) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Who's IN",    callback_data=f"btn_wi_{rc_number}"),
        InlineKeyboardButton("❌ Who's OUT",   callback_data=f"btn_wo_{rc_number}"),
    )
    markup.add(
        InlineKeyboardButton("❓ Who's Maybe", callback_data=f"btn_wm_{rc_number}"),
        InlineKeyboardButton("⏳ Waiting",     callback_data=f"btn_ww_{rc_number}"),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"btn_status_{rc_number}"))
    return markup


async def get_end_confirm_keyboard(rc_number: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Yes",    callback_data=f"btn_endconfirm_{rc_number}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"btn_endcancel_{rc_number}"),
    )
    return markup


# ── Panel machinery ───────────────────────────────────────────────────────────

def _persist_panel_msg_id(rc, msg_id: int) -> None:
    rc_db_id = getattr(rc, 'db_id', None) or getattr(rc, 'id', None)
    if rc_db_id:
        rc.panel_msg_id = msg_id
        update_rollcall(rc_db_id, panel_msg_id=msg_id)


async def _update_panel(cid: int, rc_number: int, rc, force_new: bool = False) -> None:
    """Update the status panel for a rollcall.

    Always edits the existing panel in-place immediately so every vote is
    reflected without delay. Falls back to sending a new message if the
    original panel was deleted or has not been sent yet.
    force_new — always sends a fresh message (used by /panel command).
    """
    key = (cid, rc_number)

    if key not in _panel_msg_ids and getattr(rc, 'panel_msg_id', None):
        _panel_msg_ids[key] = rc.panel_msg_id

    text = _build_panel_text(rc, rc_number)
    markup = await get_status_keyboard(rc_number)

    if not force_new:
        existing_msg_id = _panel_msg_ids.get(key)
        if existing_msg_id:
            try:
                await bot.edit_message_text(text, cid, existing_msg_id, reply_markup=markup)
                return
            except Exception as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    return
                logging.debug(f"Panel edit failed for ({cid}, {rc_number}): {e}")

    sent = await bot.send_message(cid, text, reply_markup=markup)
    _panel_msg_ids[key] = sent.message_id
    _persist_panel_msg_id(rc, sent.message_id)


async def show_panel_for_rollcall(chat_id: int, rc_number: int, force_new: bool = False):
    rollcalls = manager.get_rollcalls(chat_id)
    index = rc_number - 1
    if index < 0 or index >= len(rollcalls):
        return
    rc = rollcalls[index]
    await _update_panel(chat_id, rc_number, rc, force_new=force_new)


# ── Proxy owner notification ──────────────────────────────────────────────────

async def notify_proxy_owner_wait_to_in(rc, moved_user: User, cid, title: str, rc_number: int):
    try:
        proxy_owners = getattr(rc, "proxy_owners", None)
        if not proxy_owners:
            return
        user_key = moved_user.user_id
        owner_id = proxy_owners.get(user_key)
        if not owner_id:
            return
        try:
            member = await bot.get_chat_member(cid, owner_id)
            if member.user.username:
                owner_mention = f"@{_esc_md(member.user.username)}"
            else:
                owner_mention = f"[{_esc_md(member.user.first_name)}](tg://user?id={owner_id})"
        except Exception:
            owner_mention = "Proxy owner"

        txt = f"{owner_mention}, your proxy {format_mention_with_name_md(moved_user)} → IN for '{_esc_md(title)}' (#{rc_number})"
        await bot.send_message(cid, txt, parse_mode="Markdown")

        try:
            await bot.send_message(
                owner_id,
                f"🎉 Your proxy *{_esc_md(moved_user.name)}* got promoted from WAITING → IN for *{_esc_md(title)}* (#{rc_number})!",
                parse_mode="Markdown",
            )
        except Exception:
            pass
    except Exception:
        logging.exception("Failed to notify proxy owner for WAITING→IN")


# ── /start_roll_call ──────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):
    cid = message.chat.id
    msg = message.text

    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        arr = msg.split(" ")
        title = ' '.join(arr[1:]) if len(arr) > 1 else None

        result = await rollcalls_svc.start_rollcall(
            cid, title,
            message.from_user.id, message.from_user.first_name,
            message.from_user.username,
        )
        rc_number_1based = result["number"]
        rc = manager.get_rollcall(cid, result["rc_index"])
        markup = await get_status_keyboard(rc_number_1based)
        text = _build_panel_text(rc, rc_number_1based)
        sent = await bot.send_message(message.chat.id, text, reply_markup=markup)
        _panel_msg_ids[(cid, rc_number_1based)] = sent.message_id
        _persist_panel_msg_id(rc, sent.message_id)

    except Exception as e:
        await reply_error(cid, e)


# ── /end_roll_call ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        pmts = message.text.split(" ")[1:]
        rc_number = 0
        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The RollCallnumber must be a positive integer")

        async with manager.get_erc_lock(cid):
            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
            rc = manager.get_rollcall(cid, rc_number)
            ended_number = rc_number + 1
            ended_by = message.from_user.first_name or message.from_user.username or "someone"

            # Capture finish text before the service removes the rollcall
            finish_text = rc.finishList().replace("__RCID__", str(ended_number))
            finish_text = f"{finish_text}\n\n🎉 Ended by {ended_by}"

            result = await rollcalls_svc.end_rollcall(
                cid, rc_number,
                message.from_user.id, message.from_user.first_name,
                message.from_user.username,
            )

            await bot.send_message(cid, finish_text)

            # Update panel IDs: remove ended slot, shift renumbered ones down
            _panel_msg_ids.pop((cid, ended_number), None)
            for entry in sorted(result["renumbered"], key=lambda x: x["old"]):
                old_key = (cid, entry["old"])
                if old_key in _panel_msg_ids:
                    _panel_msg_ids[(cid, entry["new"])] = _panel_msg_ids.pop(old_key)

            if result["ghost_eligible"]:
                ghost_markup = InlineKeyboardMarkup(row_width=2)
                ghost_markup.add(
                    InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{result['ghost_rc_db_id']}"),
                    InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{result['ghost_rc_db_id']}")
                )
                await bot.send_message(cid, "👻 Did anyone ghost today's session?", reply_markup=ghost_markup)

            updated_rollcalls = manager.get_rollcalls(cid)
            if updated_rollcalls:
                lines = [f"⚠️ Rollcall #{ended_number} ended. IDs updated:"]
                for entry in result["renumbered"]:
                    lines.append(f"  #{entry['old']} '{entry['title']}' → #{entry['new']}")
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, "\n".join(lines))
                    for idx, rollcall in enumerate(updated_rollcalls):
                        new_id = idx + 1
                        text = f"Rollcall number {new_id}\n\n" + _build_panel_text(rollcall, new_id)
                        await bot.send_message(cid, text)
    except Exception as e:
        await reply_error(message, e)


# ── /set_title ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_title")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")[1:]
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        title = " ".join(pmts)
        user = message.from_user.first_name

        if title == "":
            title = "<Empty>"

        rc = manager.get_rollcall(cid, rc_number)
        rc.title = title
        rc.save()

        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, 'The roll call title is set to: ' + title)

        await _update_panel(cid, rc_number + 1, rc)
        logging.info(f"[{_ts()}] Title changed: {user} -> {title}")

    except Exception as e:
        await reply_error(message, e)


# ── /panel ────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/panel")
async def show_panel(message):
    try:
        cid = message.chat.id

        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        pmts = message.text.split(" ")[1:]
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        text = _build_panel_text(rc, rc_number + 1)
        markup = await get_status_keyboard(rc_number + 1)

        sent = await bot.send_message(cid, text, reply_markup=markup)
        _panel_msg_ids[(cid, rc_number + 1)] = sent.message_id
        _persist_panel_msg_id(rc, sent.message_id)

    except Exception as e:
        await reply_error(message, e)


# ── btn_* callback handler ────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("btn_"))
async def callback_handler(call):
    try:
        raw_data = call.data or ""
        cid = call.message.chat.id

        data = raw_data.split("_")
        if len(data) != 3 or data[0] != "btn":
            await bot.answer_callback_query(call.id, "Invalid action")
            return

        action = data[1]

        try:
            rc_number = int(data[2])
        except ValueError:
            await bot.answer_callback_query(call.id, "Invalid rollcall number")
            return

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 1 or rc_number > len(rollcalls):
            await bot.answer_callback_query(call.id, "Invalid rollcall!")
            return

        rc = rollcalls[rc_number - 1]

        # ── IN / OUT / MAYBE ──────────────────────────────────────────────────
        if action in ("in", "out", "maybe"):
            if _is_rate_limited(cid, call.from_user.id):
                await bot.answer_callback_query(call.id, "You're voting too fast — please wait a moment.")
                return
            _username = call.from_user.username or None
            if not _username:
                asyncio.create_task(warn_no_username(cid, call.from_user.first_name)).add_done_callback(_log_task_exc)
            _first_name = _get_display_name(call.from_user)

            # Ghost reconfirmation check for IN votes (uses service helper)
            if action == "in":
                from bot_state import _pending_reconf
                reconf = voting_svc.check_ghost_reconfirmation_needed(cid, call.from_user.id, rc_number - 1)
                if reconf["needed"]:
                    if (cid, call.from_user.id) in _pending_reconf:
                        await bot.answer_callback_query(call.id, "You already have a pending confirmation — please use the earlier buttons.")
                        return
                    _pending_reconf[(cid, call.from_user.id)] = {
                        'rc_number': rc_number - 1,
                        'comment': '',
                        '_ts': datetime.now().timestamp(),
                    }
                    ghost_markup = InlineKeyboardMarkup(row_width=2)
                    ghost_markup.add(
                        InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=f"reconf_in_{rc_number - 1}_{call.from_user.id}"),
                        InlineKeyboardButton("❌ I'm out", callback_data=f"reconf_out_{rc_number - 1}_{call.from_user.id}"),
                    )
                    _user_obj = User(_first_name, _username, call.from_user.id, [])
                    await bot.send_message(
                        cid,
                        f"👻 *Warning:* {format_mention_with_name_md(_user_obj)}, you've ghosted *{reconf['ghost_count']}* session(s) before.\n"
                        f"⚠️ Absent Limit: *{reconf['absent_limit']}*\n\n"
                        f"Are you committing to be at *{_esc_md(reconf['rollcall_title'])}*?",
                        parse_mode="Markdown",
                        reply_markup=ghost_markup
                    )
                    return

            try:
                if action == "in":
                    svc_result = await voting_svc.vote_in(cid, call.from_user.id, _first_name, _username, rc_number=rc_number - 1)
                elif action == "out":
                    svc_result = await voting_svc.vote_out(cid, call.from_user.id, _first_name, _username, rc_number=rc_number - 1)
                else:
                    svc_result = await voting_svc.vote_maybe(cid, call.from_user.id, _first_name, _username, rc_number=rc_number - 1)
            except alreadyInList as e:
                await bot.answer_callback_query(call.id, str(e) or "You're already in this status!")
                return

            # Re-fetch rc after service mutation for panel update
            rc = manager.get_rollcall(cid, rc_number - 1)
            user_d = svc_result["user"]
            user_id_v = user_d["user_id"]
            user_name_v = user_d["name"]
            user_uname_v = user_d.get("username")

            if svc_result["action"] == "waitlisted":
                await bot.answer_callback_query(call.id, "Event max limit reached, added to waitlist")
                if not manager.get_shh_mode(cid):
                    if isinstance(user_id_v, int):
                        _u = User(user_name_v, user_uname_v, user_id_v, [])
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(_u)} → WAITING for '{_esc_md(rc.title)}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{user_name_v} → WAITING for '{rc.title}' (#{rc_number})")
            else:
                await bot.answer_callback_query(call.id, "Status updated")
                if not manager.get_shh_mode(cid):
                    label = {"in": "IN", "out": "OUT", "maybe": "MAYBE"}[action]
                    if isinstance(user_id_v, int):
                        _u = User(user_name_v, user_uname_v, user_id_v, [])
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(_u)} → {label} for '{_esc_md(rc.title)}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{user_name_v} → {label} for '{rc.title}' (#{rc_number})")

            promoted = svc_result.get("promoted")
            if promoted and action in ("out", "maybe"):
                p_id = promoted["user_id"]
                p_name = promoted["name"]
                p_uname = promoted.get("username")
                if not manager.get_shh_mode(cid):
                    if isinstance(p_id, int):
                        _p = User(p_name, p_uname, p_id, [])
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(_p)} → IN (from WAITING) for '{_esc_md(rc.title)}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{p_name} → IN (from WAITING) for '{rc.title}' (#{rc_number})")
                if isinstance(p_id, int):
                    asyncio.create_task(_dm_promoted_real_user(p_id, rc.title, rc_number)).add_done_callback(_log_task_exc)
                _p_obj = User(p_name, p_uname, p_id, [])
                await notify_proxy_owner_wait_to_in(rc, _p_obj, cid, rc.title, rc_number)

            text = _build_panel_text(rc, rc_number)
            markup = await get_status_keyboard(rc_number)
            try:
                await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
                _panel_msg_ids[(cid, rc_number)] = call.message.message_id
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── Lists submenu ─────────────────────────────────────────────────────
        if action == "lists":
            await bot.answer_callback_query(call.id)
            markup = await get_lists_keyboard(rc_number)
            try:
                await bot.edit_message_text("Select list:", cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        if action in ("wi", "wo", "wm", "ww"):
            await bot.answer_callback_query(call.id)
            if action == "wi":
                text = rc.inListText()
            elif action == "wo":
                text = rc.outListText()
            elif action == "wm":
                text = rc.maybeListText()
            else:
                text = rc.waitListText()
            try:
                await bot.edit_message_text(
                    text if text.strip() else "List is empty.",
                    cid, call.message.message_id,
                    reply_markup=await get_lists_keyboard(rc_number),
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── Back to main panel ────────────────────────────────────────────────
        if action == "status":
            await bot.answer_callback_query(call.id)
            text = _build_panel_text(rc, rc_number)
            markup = await get_status_keyboard(rc_number)
            try:
                await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── Refresh ───────────────────────────────────────────────────────────
        if action == "refresh":
            await bot.answer_callback_query(call.id, "Refreshed")
            text = _build_panel_text(rc, rc_number)
            markup = await get_status_keyboard(rc_number)
            try:
                await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
                _panel_msg_ids[(cid, rc_number)] = call.message.message_id
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── Show end confirmation ─────────────────────────────────────────────
        if action == "end":
            member = await bot.get_chat_member(cid, call.from_user.id)
            admin_mode = manager.get_admin_rights(cid)
            if admin_mode and member.status not in ["administrator", "creator"]:
                await bot.answer_callback_query(call.id, "⛔ Only admins can end rollcalls", show_alert=True)
                return
            await bot.answer_callback_query(call.id)
            markup = await get_end_confirm_keyboard(rc_number)
            # Auto-started rollcalls + /src with no args can leave rc.title empty
            # or "<Empty>". Show a sensible label instead of "''" in the prompt.
            if rc.title and rc.title != "<Empty>":
                rc_label = f"'{rc.title}'"
            else:
                rc_label = "this rollcall"
            try:
                await bot.edit_message_text(
                    f"Are you sure you want to end {rc_label} (#{rc_number})?",
                    cid, call.message.message_id, reply_markup=markup,
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ── Confirm end rollcall ──────────────────────────────────────────────
        if action == "endconfirm":
            admin_mode = manager.get_admin_rights(cid)
            if admin_mode:
                member = await bot.get_chat_member(cid, call.from_user.id)
                if member.status not in ["administrator", "creator"]:
                    await bot.answer_callback_query(call.id, "⛔ Only admins can end rollcalls", show_alert=True)
                    return

            async with manager.get_erc_lock(cid):
                rc = manager.get_rollcall(cid, rc_number - 1)
                if rc is None:
                    await bot.answer_callback_query(call.id, "Rollcall already ended.")
                    return

                ended_by = call.from_user.first_name or call.from_user.username or "someone"
                ended_number = rc_number

                # Capture finish text before the service removes the rollcall.
                try:
                    final_text = rc.finishList().replace("__RCID__", str(rc_number))
                    final_text = f"{final_text}\n\n🎉 Ended by {ended_by}"
                except Exception:
                    logging.exception(f"Failed to build finish list for rollcall #{rc_number}")
                    final_text = f"Rollcall #{rc_number} ended by {ended_by}."

                result = await rollcalls_svc.end_rollcall(
                    cid, rc_number - 1,
                    call.from_user.id, ended_by,
                    getattr(call.from_user, "username", None),
                )

                await bot.answer_callback_query(call.id, "Rollcall ended")

                # Replace the "Are you sure?" prompt in-place with the finish list.
                try:
                    await bot.edit_message_text(
                        final_text, cid, call.message.message_id, reply_markup=None,
                    )
                except Exception:
                    logging.exception(f"Failed to edit end-confirm message for chat {cid}")
                    try:
                        await bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)
                    except Exception:
                        pass
                    try:
                        await bot.send_message(cid, final_text)
                    except Exception:
                        logging.exception(f"Failed to send finish list for chat {cid}")

                # Update panel IDs
                _panel_msg_ids.pop((cid, ended_number), None)
                for entry in sorted(result["renumbered"], key=lambda x: x["old"]):
                    old_key = (cid, entry["old"])
                    if old_key in _panel_msg_ids:
                        _panel_msg_ids[(cid, entry["new"])] = _panel_msg_ids.pop(old_key)

                if result["ghost_eligible"]:
                    ghost_markup = InlineKeyboardMarkup(row_width=2)
                    ghost_markup.add(
                        InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{result['ghost_rc_db_id']}"),
                        InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{result['ghost_rc_db_id']}")
                    )
                    await bot.send_message(cid, "👻 Did anyone ghost today's session?", reply_markup=ghost_markup)

                updated_rollcalls = manager.get_rollcalls(cid)
                if updated_rollcalls and not manager.get_shh_mode(cid):
                    lines = [f"⚠️ Rollcall #{ended_number} ended. IDs updated:"]
                    for entry in result["renumbered"]:
                        lines.append(f"  #{entry['old']} '{entry['title']}' → #{entry['new']}")
                    await bot.send_message(cid, "\n".join(lines))
                    for idx, rollcall in enumerate(updated_rollcalls):
                        new_id = idx + 1
                        text = _build_panel_text(rollcall, new_id)
                        panel_markup = await get_status_keyboard(new_id)
                        sent = await bot.send_message(cid, text, reply_markup=panel_markup)
                        _panel_msg_ids[(cid, new_id)] = sent.message_id
                        _persist_panel_msg_id(rollcall, sent.message_id)
            return

        # ── Cancel end rollcall ───────────────────────────────────────────────
        if action == "endcancel":
            await bot.answer_callback_query(call.id, "Cancelled")
            text = _build_panel_text(rc, rc_number)
            markup = await get_status_keyboard(rc_number)
            try:
                await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        await bot.answer_callback_query(call.id, "Unknown action")

    except Exception as e:
        err_str = str(e)
        if "query is too old" in err_str or "query ID is invalid" in err_str:
            logging.warning(f"[{_ts()}] Stale callback ignored ({call.data[:50]}): {err_str[:80]}")
            return
        logging.exception("Error in callback_handler")
        try:
            await bot.answer_callback_query(call.id, "⚠️ Something went wrong.")
        except Exception:
            pass
