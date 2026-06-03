"""
Lifecycle handlers: /src, /erc, /set_title, /panel, inline keyboards, panel machinery,
notify_proxy_owner_wait_to_in, and the btn_* callback handler.
"""
import asyncio
import logging
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
    duplicateProxy, repeatlyName, amountOfRollCallsReached,
)
from functions import admin_rights, roll_call_not_started
from models import User
from rollcall_manager import manager
from db import (
    update_rollcall, log_admin_action, increment_user_stat, increment_rollcall_stat,
    update_streak_on_checkin, upsert_chat_member,
)


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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

    text = rc.allList().replace("__RCID__", str(rc_number))
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
    title = ''

    try:
        rollcalls = manager.get_rollcalls(cid)

        if len(rollcalls) >= 3:
            raise amountOfRollCallsReached("Allowed Maximum number of active roll calls per group is 3.")

        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            title = ' '.join(arr)
        else:
            title = '<Empty>'

        rc_index = len(rollcalls)
        rc = manager.add_rollcall(cid, title)
        logging.info(f"[{_ts()}] [CHAT {cid}] Rollcall started: '{title}' (RC #{rc_index+1}) by {message.from_user.first_name} (@{message.from_user.username})")
        log_admin_action(cid, message.from_user.id, message.from_user.first_name, "new_rollcall", target_name=title)
        markup = await get_status_keyboard(rc_index + 1)
        text = rc.allList().replace("__RCID__", str(rc_index + 1))
        sent = await bot.send_message(message.chat.id, text, reply_markup=markup)
        _panel_msg_ids[(cid, rc_index + 1)] = sent.message_id
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
            rc_db_id = rc.id
            ghost_tracking_on = manager.get_ghost_tracking_enabled(cid)
            ended_number = rc_number + 1

            in_users = rc.inList
            has_any_users = len(in_users) > 0

            participants = set(
                u.user_id for u in (rc.inList + rc.outList + rc.maybeList + rc.waitList)
                if isinstance(u.user_id, int)
            )
            for u in in_users:
                if isinstance(u.user_id, int):
                    update_streak_on_checkin(cid, u.user_id)
            for uid in participants:
                increment_user_stat(cid, uid, "total_rollcalls")

            ended_by = message.from_user.first_name or message.from_user.username or "someone"
            finish_text = rc.finishList().replace("__RCID__", str(rc_number + 1))
            finish_text = f"{finish_text}\n\n🎉 Ended by {ended_by}"
            await bot.send_message(cid, finish_text)
            logging.info(f"[{_ts()}] Rollcall ended: '{rc.title}' (RC #{rc_number+1})")
            _panel_msg_ids.pop((cid, rc_number + 1), None)
            manager.remove_rollcall(cid, rc_number)
            for num in sorted(n for (c, n) in list(_panel_msg_ids) if c == cid and n > rc_number + 1):
                _panel_msg_ids[(cid, num - 1)] = _panel_msg_ids.pop((cid, num))
            logging.info(f"[{_ts()}] [CHAT {cid}] Rollcall ended: '{rc.title}' by {message.from_user.first_name} (@{message.from_user.username})")
            log_admin_action(cid, message.from_user.id, message.from_user.first_name, "end_rollcall", target_name=rc.title)

            if ghost_tracking_on and has_any_users and rc_db_id and not rc.absent_marked:
                markup = InlineKeyboardMarkup(row_width=2)
                markup.add(
                    InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rc_db_id}"),
                    InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{rc_db_id}")
                )
                await bot.send_message(cid, "👻 Did anyone ghost today's session?", reply_markup=markup)

            updated_rollcalls = manager.get_rollcalls(cid)
            if len(updated_rollcalls) > 0:
                lines = [f"⚠️ Rollcall #{ended_number} ended. IDs updated:"]
                for idx, rollcall in enumerate(updated_rollcalls):
                    new_id = idx + 1
                    old_id = new_id if new_id < ended_number else new_id + 1
                    if old_id != new_id:
                        lines.append(f"  #{old_id} '{rollcall.title}' → #{new_id}")
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, "\n".join(lines))
                    for idx, rollcall in enumerate(updated_rollcalls):
                        new_id = idx + 1
                        text = f"Rollcall number {new_id}\n\n" + rollcall.allList().replace("__RCID__", str(new_id))
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
        text = rc.allList().replace("__RCID__", str(rc_number + 1))
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
            user = User(_first_name, _username, call.from_user.id, rc.allNames)

            upsert_chat_member(cid, call.from_user.id, _first_name, _username)

            # Ghost reconfirmation check
            if action == "in" and isinstance(user.user_id, int) and manager.get_ghost_tracking_enabled(cid):
                from db import get_ghost_count
                from bot_state import _pending_reconf
                ghost_count = get_ghost_count(cid, user.user_id)
                absent_limit = manager.get_absent_limit(cid)
                if ghost_count >= absent_limit:
                    _pending_reconf[(cid, user.user_id)] = {'rc_number': rc_number - 1, 'comment': ''}
                    markup = InlineKeyboardMarkup(row_width=2)
                    markup.add(
                        InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=f"reconf_in_{rc_number - 1}_{user.user_id}"),
                        InlineKeyboardButton("❌ I'm out", callback_data=f"reconf_out_{rc_number - 1}_{user.user_id}"),
                    )
                    await bot.send_message(
                        cid,
                        f"👻 *Warning:* You've ghosted *{ghost_count}* session(s) before.\n"
                        f"Absent limit: *{absent_limit}*\n\n"
                        f"Are you committing to be at *{_esc_md(rc.title)}*?",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                    return

            if action == "in":
                result = rc.addIn(user)
            elif action == "out":
                result = rc.addOut(user)
            else:
                result = rc.addMaybe(user)

            rc.save()
            rc_db_id = get_rc_db_id(rc)

            if rc_db_id is not None and isinstance(user.user_id, int):
                if action == "in":
                    if result not in ("AB", "AC", "AA", "AU"):
                        increment_user_stat(cid, user.user_id, "total_in")
                        increment_rollcall_stat(rc_db_id, "total_in")
                elif action == "out":
                    if result not in ("AB", "AU"):
                        increment_user_stat(cid, user.user_id, "total_out")
                        increment_rollcall_stat(rc_db_id, "total_out")
                else:
                    if result not in ("AB", "AU"):
                        increment_user_stat(cid, user.user_id, "total_maybe")
                        increment_rollcall_stat(rc_db_id, "total_maybe")

            if result == "AB":
                await bot.answer_callback_query(call.id, "No duplicate proxy please 🙂")
                return
            elif result == "AA":
                await bot.answer_callback_query(call.id, "That name already exists!")
                return
            elif result == "AC":
                await bot.answer_callback_query(call.id, "Event max limit reached, added to waitlist")
                if not manager.get_shh_mode(cid):
                    if isinstance(user.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(user)} → WAITING for '{_esc_md(rc.title)}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{user.name} → WAITING for '{rc.title}' (#{rc_number})")
            else:
                await bot.answer_callback_query(call.id, "Status updated")
                if not manager.get_shh_mode(cid):
                    status_map = {"in": "IN", "out": "OUT", "maybe": "MAYBE"}
                    label = status_map[action]
                    if isinstance(user.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(user)} → {label} for '{_esc_md(rc.title)}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{user.name} → {label} for '{rc.title}' (#{rc_number})")

            if action in ("out", "maybe") and isinstance(result, User):
                if not manager.get_shh_mode(cid):
                    if isinstance(result.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(result)} → IN (from WAITING) for '{_esc_md(rc.title)}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{result.name} → IN (from WAITING) for '{rc.title}' (#{rc_number})")

                if isinstance(result.user_id, int):
                    asyncio.create_task(_dm_promoted_real_user(result.user_id, rc.title, rc_number)).add_done_callback(_log_task_exc)

                await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number)

                if rc_db_id is not None and isinstance(result.user_id, int):
                    increment_user_stat(cid, result.user_id, "total_waiting_to_in")
                    increment_user_stat(cid, result.user_id, "total_in")
                    increment_rollcall_stat(rc_db_id, "total_in")

            text = rc.allList().replace("__RCID__", str(rc_number))
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
            text = rc.allList().replace("__RCID__", str(rc_number))
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
            text = rc.allList().replace("__RCID__", str(rc_number))
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
            try:
                await bot.edit_message_text(
                    f"Are you sure you want to end rollcall '{rc.title}' (#{rc_number})?",
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

                rc_db_id = rc.id
                ghost_tracking_on = manager.get_ghost_tracking_enabled(cid)
                has_any_users = len(rc.inList) > 0
                absent_already_marked = rc.absent_marked
                ended_number = rc_number

                participants = set(
                    u.user_id for u in (rc.inList + rc.outList + rc.maybeList + rc.waitList)
                    if isinstance(u.user_id, int)
                )
                for u in rc.inList:
                    if isinstance(u.user_id, int):
                        update_streak_on_checkin(cid, u.user_id)
                for uid in participants:
                    increment_user_stat(cid, uid, "total_rollcalls")

                await bot.answer_callback_query(call.id, "Rollcall ended")
                ended_by = call.from_user.first_name or call.from_user.username or "someone"
                log_admin_action(cid, call.from_user.id, ended_by, "end_rollcall", target_name=rc.title, rollcall_id=rc_db_id, details="via panel")

                try:
                    final_text = rc.finishList().replace("__RCID__", str(rc_number))
                    final_text = f"{final_text}\n\nRollcall ended by {ended_by}"
                    await bot.send_message(cid, final_text)
                except Exception:
                    pass

                logging.info(f"[{_ts()}] [CHAT {cid}] Rollcall ended: '{rc.title}' by {ended_by} (panel)")
                _panel_msg_ids.pop((cid, rc_number), None)
                manager.remove_rollcall(cid, rc_number - 1)
                for num in sorted(n for (c, n) in list(_panel_msg_ids) if c == cid and n > rc_number):
                    _panel_msg_ids[(cid, num - 1)] = _panel_msg_ids.pop((cid, num))

                if ghost_tracking_on and has_any_users and rc_db_id and not absent_already_marked:
                    markup = InlineKeyboardMarkup(row_width=2)
                    markup.add(
                        InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rc_db_id}"),
                        InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{rc_db_id}")
                    )
                    await bot.send_message(cid, "👻 Did anyone ghost today's session?", reply_markup=markup)

                updated_rollcalls = manager.get_rollcalls(cid)
                if len(updated_rollcalls) > 0 and not manager.get_shh_mode(cid):
                    lines = [f"⚠️ Rollcall #{ended_number} ended. IDs updated:"]
                    for idx, rollcall in enumerate(updated_rollcalls):
                        new_id = idx + 1
                        old_id = new_id if new_id < ended_number else new_id + 1
                        if old_id != new_id:
                            lines.append(f"  #{old_id} '{rollcall.title}' → #{new_id}")
                    await bot.send_message(cid, "\n".join(lines))
                    for idx, rollcall in enumerate(updated_rollcalls):
                        new_id = idx + 1
                        text = rollcall.allList().replace("__RCID__", str(new_id))
                        markup = await get_status_keyboard(new_id)
                        sent = await bot.send_message(cid, text, reply_markup=markup)
                        _panel_msg_ids[(cid, new_id)] = sent.message_id
                        _persist_panel_msg_id(rollcall, sent.message_id)
            return

        # ── Cancel end rollcall ───────────────────────────────────────────────
        if action == "endcancel":
            await bot.answer_callback_query(call.id, "Cancelled")
            text = rc.allList().replace("__RCID__", str(rc_number))
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
