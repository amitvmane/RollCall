"""
Proxy handlers: /set_in_for (/sif), /set_out_for (/sof), /set_maybe_for (/smf)
"""
import logging

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from datetime import datetime

from bot_state import (
    bot, format_mention_with_name, format_mention_with_name_md, _esc_md,
    _dm_promoted_real_user, _log_task_exc, get_rc_db_id,
    _pending_proxy_add, _prune_pending, reply_error,
)
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    duplicateProxy, repeatlyName,
)
from functions import admin_rights, roll_call_not_started
from models import User
from rollcall_manager import manager
from db import (
    add_or_update_proxy_user, log_admin_action, get_ghost_count_by_proxy_name,
    increment_user_stat, increment_rollcall_stat,
)
import asyncio


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
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

        arr = msg.split(" ")
        if len(arr) > 1:
            proxy_name = arr[1]

            if len(proxy_name) > 40:
                await bot.send_message(cid, f"⚠️ Proxy name is too long (max 40 characters). Got {len(proxy_name)}.")
                return

            already_present = any(
                u.name == proxy_name and isinstance(u.user_id, str)
                for u in rc.inList + rc.waitList
            )
            if already_present:
                await bot.send_message(cid, f"⚠️ '{proxy_name}' is already IN or WAITING for '{rc.title}'.")
                return

            user = User(proxy_name, None, proxy_name, rc.allNames)
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            user.comment = comment

            ghost_count = get_ghost_count_by_proxy_name(cid, proxy_name)
            if ghost_count > 0:
                limit = manager.get_absent_limit(cid)
                if ghost_count >= limit:
                    _prune_pending(_pending_proxy_add)
                    _pending_proxy_add[(cid, message.from_user.id, proxy_name)] = {
                        'comment': comment,
                        '_ts': datetime.now().timestamp(),
                    }
                    markup = InlineKeyboardMarkup(row_width=2)
                    markup.add(
                        InlineKeyboardButton("✅ Yes, add anyway", callback_data=f"proxy_add_{rc_number}_{proxy_name}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"proxy_cancel_{rc_number}_{proxy_name}"),
                    )
                    await bot.send_message(
                        cid,
                        f"👻 *Warning:* *{_esc_md(proxy_name)}* has ghosted *{ghost_count}* session(s) before.\n"
                        f"⚠️ Absent Limit: *{limit}*\n\n"
                        f"Still add to *{_esc_md(rc.title)}*?",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                    return

            proxy_owner_id = message.from_user.id
            rc.set_proxy_owner(user.user_id, proxy_owner_id)

            # rc.addIn → _save_user_to_db already writes the proxy row with
            # the correct status (in or waitlist), so no separate
            # add_or_update_proxy_user call is needed here.
            result = rc.addIn(user)
            rc.save()

            log_admin_action(cid, message.from_user.id, message.from_user.first_name, "sif", target_name=proxy_name, rollcall_id=rc.id, details=rc.title)

            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif result is None:
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, f"{user.name} is now IN!")

            from handlers.lifecycle import show_panel_for_rollcall
            await show_panel_for_rollcall(cid, rc_number + 1)

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
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
        arr = msg.split(" ")

        if len(arr) > 1:
            user = User(arr[1], None, arr[1], rc.allNames)
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            user.comment = comment

            was_in = any((u.user_id == user.user_id or u.name == user.name) for u in rc.inList)

            result = rc.addOut(user)
            rc.save()

            log_admin_action(cid, message.from_user.id, message.from_user.first_name, "sof", target_name=arr[1], rollcall_id=rc.id, details=rc.title)

            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
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
            elif result is None:
                if not manager.get_shh_mode(cid):
                    if was_in:
                        await bot.send_message(cid, f"{user.name} → OUT for '{rc.title}' (#{rc_number + 1})")
                    else:
                        await bot.send_message(cid, f"{user.name} is now OUT!")

            from handlers.lifecycle import show_panel_for_rollcall
            await show_panel_for_rollcall(cid, rc_number + 1)

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
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
        arr = msg.split(" ")

        if len(arr) > 1:
            user = User(arr[1], None, arr[1], rc.allNames)
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            user.comment = comment

            result = rc.addMaybe(user)
            rc.save()

            log_admin_action(cid, message.from_user.id, message.from_user.first_name, "smf", target_name=arr[1], rollcall_id=rc.id, details=rc.title)

            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
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
            elif result is None:
                if not manager.get_shh_mode(cid):
                    await bot.send_message(cid, f"{user.name} is now MAYBE!")

            from handlers.lifecycle import show_panel_for_rollcall
            await show_panel_for_rollcall(cid, rc_number + 1)

    except Exception as e:
        await reply_error(message, e)
