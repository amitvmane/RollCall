"""
Voting handlers: /in, /out, /maybe
"""
import asyncio
import logging

from bot_state import (
    bot, _log_task_exc, _pending_reconf, _is_rate_limited, _get_display_name,
    format_mention_with_name, format_mention_with_name_md, _esc_md,
    warn_no_username, _dm_promoted_real_user, get_rc_db_id,
)
from exceptions import (
    rollCallNotStarted, incorrectParameter, duplicateProxy,
)
from functions import roll_call_not_started
from models import User
from rollcall_manager import manager
from db import (
    increment_user_stat, increment_rollcall_stat, get_ghost_count, upsert_chat_member,
)
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return
        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        _username = message.from_user.username or None
        _display_name = _get_display_name(message.from_user)
        if not _username:
            asyncio.create_task(warn_no_username(cid, _display_name)).add_done_callback(_log_task_exc)
        user = User(_display_name, _username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        if isinstance(user.user_id, int):
            upsert_chat_member(cid, user.user_id, _display_name, _username)

        if isinstance(user.user_id, int) and manager.get_ghost_tracking_enabled(cid):
            ghost_count = get_ghost_count(cid, user.user_id)
            absent_limit = manager.get_absent_limit(cid)
            if ghost_count >= absent_limit:
                _pending_reconf[(cid, user.user_id)] = {'rc_number': rc_number, 'comment': comment}
                markup = InlineKeyboardMarkup(row_width=2)
                markup.add(
                    InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=f"reconf_in_{rc_number}_{user.user_id}"),
                    InlineKeyboardButton("❌ I'm out", callback_data=f"reconf_out_{rc_number}_{user.user_id}"),
                )
                await bot.send_message(
                    cid,
                    f"👻 *Warning:* You've ghosted *{ghost_count}* session(s) before.\n"
                    f"⚠️ Absent Limit: *{absent_limit}*\n\n"
                    f"Are you committing to be at *{_esc_md(rc.title)}*?",
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                return

        result = rc.addIn(user)
        rc.save()

        rc_db_id = get_rc_db_id(rc)
        if result not in ('AB', 'AC', 'AU') and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_in")
            increment_rollcall_stat(rc_db_id, "total_in")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif result == 'AC':
            if not manager.get_shh_mode(cid):
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
        elif result is None:
            if not manager.get_shh_mode(cid):
                if isinstance(user.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(user)} is now IN!",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{user.name} is now IN!")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(message.chat.id, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return
        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        _username = message.from_user.username or None
        _display_name = _get_display_name(message.from_user)
        if not _username:
            asyncio.create_task(warn_no_username(cid, _display_name)).add_done_callback(_log_task_exc)
        user = User(_display_name, _username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        if isinstance(user.user_id, int):
            upsert_chat_member(cid, user.user_id, _display_name, _username)

        was_in = any(u.user_id == user.user_id for u in rc.inList)

        result = rc.addOut(user)
        rc.save()

        rc_db_id = get_rc_db_id(rc)
        if result not in ('AB', 'AU') and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_out")
            increment_rollcall_stat(rc_db_id, "total_out")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if not manager.get_shh_mode(cid):
                if isinstance(result.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(result)} → IN",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{result.name} → IN")

            if isinstance(result.user_id, int):
                asyncio.create_task(_dm_promoted_real_user(result.user_id, rc.title, rc_number + 1)).add_done_callback(_log_task_exc)

            from handlers.lifecycle import notify_proxy_owner_wait_to_in
            await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number + 1)

            if rc_db_id is not None and isinstance(result.user_id, int):
                increment_user_stat(cid, result.user_id, "total_waiting_to_in")
                increment_user_stat(cid, result.user_id, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")

        if not manager.get_shh_mode(cid) and result not in ('AB', 'AU') and not isinstance(result, User):
            in_outlist_now = any(u.user_id == user.user_id for u in rc.outList)
            if in_outlist_now:
                if isinstance(user.user_id, int):
                    if was_in:
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(user)} → OUT for '{_esc_md(rc.title)}' (#{rc_number + 1})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(user)} is now OUT!",
                            parse_mode="Markdown",
                        )
                else:
                    if was_in:
                        await bot.send_message(cid, f"{user.name} → OUT for '{rc.title}' (#{rc_number + 1})")
                    else:
                        await bot.send_message(cid, f"{user.name} is now OUT!")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(message.chat.id, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return
        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        _username = message.from_user.username or None
        _display_name = _get_display_name(message.from_user)
        if not _username:
            asyncio.create_task(warn_no_username(cid, _display_name)).add_done_callback(_log_task_exc)
        user = User(_display_name, _username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        if isinstance(user.user_id, int):
            upsert_chat_member(cid, user.user_id, _display_name, _username)

        result = rc.addMaybe(user)
        rc.save()

        rc_db_id = get_rc_db_id(rc)
        if result not in ('AB', 'AU') and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_maybe")
            increment_rollcall_stat(rc_db_id, "total_maybe")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if not manager.get_shh_mode(cid):
                if isinstance(result.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(result)} → IN (from WAITING) for '{_esc_md(rc.title)}' (#{rc_number + 1})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{result.name} → IN (from WAITING) for '{rc.title}' (#{rc_number + 1})")

            if isinstance(result.user_id, int):
                asyncio.create_task(_dm_promoted_real_user(result.user_id, rc.title, rc_number + 1)).add_done_callback(_log_task_exc)

            from handlers.lifecycle import notify_proxy_owner_wait_to_in
            await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number + 1)

            if rc_db_id is not None and isinstance(result.user_id, int):
                increment_user_stat(cid, result.user_id, "total_waiting_to_in")
                increment_user_stat(cid, result.user_id, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")

        elif result is None:
            if not manager.get_shh_mode(cid):
                if isinstance(user.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(user)} is now MAYBE!",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{user.name} is now MAYBE!")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(message.chat.id, str(e))
