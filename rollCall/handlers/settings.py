"""
Settings handlers: /srt, /srr, /event_fee, /individual_fee, /when, /location, /set_limit, /shh, /louder
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta

import pytz

from bot_state import (
    bot, _log_task_exc, _dm_promoted_real_user,
    format_mention_with_name, format_mention_with_name_md, _esc_md, get_rc_db_id,
)
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    timeError,
)
from functions import admin_rights, roll_call_not_started
from rollcall_manager import manager
from db import log_admin_action, increment_user_stat, increment_rollcall_stat


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        if len(message.text.split(" ")) == 1:
            raise parameterMissing("invalid datetime format, refer help section for details")
        cid = message.chat.id
        msg = message.text
        rc_number = 0
        pmts = msg.split(" ")[1:]

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

        if (pmts[0]).lower() == 'cancel':
            rc.finalizeDate = None
            rc.reminder = None
            rc.save()
            if not manager.get_shh_mode(cid):
                await bot.send_message(message.chat.id, "Reminder time is canceled.")
            return

        input_datetime = " ".join(pmts).strip()
        tz = pytz.timezone(rc.timezone)
        date = datetime.strptime(input_datetime, "%d-%m-%Y %H:%M")
        date = tz.localize(date)
        now_date_string = datetime.now(pytz.timezone(rc.timezone)).strftime("%d-%m-%Y %H:%M")
        now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
        now_date = tz.localize(now_date)

        if now_date > date:
            raise timeError("Please provide valid future datetime.")

        rc.finalizeDate = date
        changed = False
        if rc.reminder is not None:
            rc.reminder = None
            changed = True

        rc.save()
        if not manager.get_shh_mode(cid):
            backslash = '\n'
            await bot.send_message(cid, f"Event notification time is set to {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')} {rc.timezone} for '{rc.title}' (ID: {rc_number + 1}).{backslash*2+'Reminder has been reset!' if changed else ''}")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)
        from check_reminders import start
        rollcalls = manager.get_rollcalls(cid)
        asyncio.create_task(start(rollcalls, rc.timezone, cid)).add_done_callback(_log_task_exc)

    except Exception as e:
        logging.exception("[set_rollcall_time] Unexpected error")
        await bot.send_message(message.chat.id, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_reminder")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srr")
async def reminder(message):
    cid = message.chat.id
    msg = message.text
    rc_number = 0
    pmts = msg.split(" ")[1:]

    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        if len(pmts) > 0 and pmts[0] != 'cancel' and len(pmts[0]) == 2:
            if pmts[0][0] == "0":
                pmts[0] = pmts[0][1]

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

        if rc.finalizeDate is None:
            raise parameterMissing('First you need to set a finalize time for the current rollcall')

        if len(pmts) > 0 and pmts[0].lower() == 'cancel':
            rc.reminder = None
            rc.save()
            if not manager.get_shh_mode(cid):
                await bot.send_message(message.chat.id, "Reminder Notification is canceled.")
            from handlers.lifecycle import _update_panel
            await _update_panel(cid, rc_number + 1, rc)
            return

        if len(pmts) == 0 or not pmts[0].isdigit():
            raise parameterMissing("The format is /set_rollcall_reminder hours")

        if int(pmts[0]) < 1:
            raise incorrectParameter("Hours must be higher than 1")

        hour = pmts[0]

        finalize = rc.finalizeDate
        if finalize.tzinfo is None:
            finalize = pytz.timezone(rc.timezone).localize(finalize)
        if finalize - timedelta(hours=int(hour)) < datetime.now(pytz.timezone(rc.timezone)):
            raise incorrectParameter("Reminder notification time is less than current time, please set it correctly.")

        rc.reminder = int(hour) if int(hour) != 0 else None
        rc.save()
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f'I will remind {hour}hour/s before the event! Thank you!')

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except ValueError as e:
        logging.exception("[reminder] Invalid value")
        await bot.send_message(cid, 'The correct format is /set_rollcall_reminder HH')
    except Exception as e:
        await bot.send_message(cid, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/event_fee")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/ef")
async def event_fee(message):
    cid = message.chat.id
    pmts = message.text.split(" ")[1:]
    rc_number = 0

    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

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
        event_price = " ".join(pmts)
        event_price_number = re.findall('[0-9]+', event_price)

        if len(event_price_number) == 0 or int(event_price_number[0]) <= 0:
            raise incorrectParameter("The correct format is '/event_fee Integer' Where 'Integer' it's up to 0 number")

        rc.event_fee = event_price
        rc.save()

        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"Event Fee set to {event_price}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(cid, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/individual_fee")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/if")
async def individual_fee(message):
    cid = message.chat.id
    pmts = message.text.split(" ")[1:]
    rc_number = 0

    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

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
        if rc.event_fee is None:
            raise parameterMissing("No event fee set. Use /event_fee to set one first.")
        in_list = len(rc.inList)
        event_price = int(re.sub(r'[^0-9]', "", str(rc.event_fee)))

        if in_list > 0:
            ind_fee = round(event_price / in_list, 2)
        else:
            ind_fee = 0

        await bot.send_message(cid, f'Individual fee is {ind_fee}')

    except Exception as e:
        await bot.send_message(cid, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/when")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/w")
async def when(message):
    cid = message.chat.id
    pmts = message.text.split(" ")
    rc_number = 0

    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

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
        if rc.finalizeDate is None:
            raise incorrectParameter("There is no start time for the event")

        await bot.send_message(cid, f"The event with title {rc.title} will start at {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')}!")

    except Exception as e:
        await bot.send_message(cid, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):
    cid = message.chat.id
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        if len(message.text.split(" ")) < 2:
            raise incorrectParameter("The correct format is /location <place>")

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

        place = " ".join(pmts)
        rc = manager.get_rollcall(cid, rc_number)
        rc.location = place
        rc.save()

        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"Location updated for '{rc.title}' (ID: {rc_number + 1}) → {place}.")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(cid, str(e))


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_limit")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sl")
async def wait_limit(message):
    if not await admin_rights(message, manager):
        await bot.send_message(message.chat.id, "You don't have permission to use this command.")
        return
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        msg = message.text
        cid = message.chat.id
        pmts = msg.split(" ")[1:]
        rc_number = 0

        if len(pmts) == 0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        if "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        if len(pmts) == 0 or not str(pmts[0]).isdigit() or int(pmts[0]) <= 0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        limit = int(pmts[0])

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        old_limit = rc.inListLimit
        was_full = old_limit is not None and len(rc.inList) >= int(old_limit)

        rc.inListLimit = limit
        rc.save()
        logging.info(f"[{_ts()}] Max limit of attendees is set to {limit}")
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"Max limit of attendees is set to {limit}")

        moved_from_in_to_wait = []
        moved_from_wait_to_in = []

        if len(rc.inList) > limit:
            moved_from_in_to_wait = rc.inList[limit:]
            rc.waitList.extend(rc.inList[limit:])
            rc.inList = rc.inList[:limit]
            for u in moved_from_in_to_wait:
                rc._save_user_to_db(u, 'waitlist')
            rc.save()
        elif len(rc.inList) < limit:
            available_slots = limit - len(rc.inList)
            moved_from_wait_to_in = rc.waitList[:available_slots]
            rc.inList.extend(rc.waitList[:available_slots])
            rc.waitList = rc.waitList[available_slots:]
            for u in moved_from_wait_to_in:
                rc._save_user_to_db(u, 'in')
            rc.save()

        if moved_from_in_to_wait:
            if not manager.get_shh_mode(cid):
                names = ", ".join(u.name for u in moved_from_in_to_wait)
                await bot.send_message(
                    cid,
                    f"{names} moved from IN to WAITING because limit was set to {limit} for '{rc.title}' (ID: {rc_number + 1})."
                )

        if moved_from_wait_to_in:
            if not manager.get_shh_mode(cid):
                for u in moved_from_wait_to_in:
                    if isinstance(u.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name_md(u)} → IN (from WAITING) for '{_esc_md(rc.title)}' (#{rc_number + 1})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(
                            cid,
                            f"{u.name} → IN (from WAITING) for '{rc.title}' (#{rc_number + 1})",
                        )

            rc_db_id = get_rc_db_id(rc)
            from handlers.lifecycle import notify_proxy_owner_wait_to_in
            for u in moved_from_wait_to_in:
                if isinstance(u.user_id, int):
                    asyncio.create_task(_dm_promoted_real_user(u.user_id, rc.title, rc_number + 1)).add_done_callback(_log_task_exc)
                await notify_proxy_owner_wait_to_in(rc, u, cid, rc.title, rc_number + 1)

                if rc_db_id is not None and isinstance(u.user_id, int):
                    increment_user_stat(cid, u.user_id, "total_waiting_to_in")
                    increment_user_stat(cid, u.user_id, "total_in")
                    increment_rollcall_stat(rc_db_id, "total_in")

        if len(rc.inList) == limit and not was_full:
            if not manager.get_shh_mode(cid):
                await bot.send_message(
                    cid,
                    f"Rollcall '{rc.title}' (ID: {rc_number + 1}) has reached its max limit ({limit}). New IN will go to WAITING."
                )

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except parameterMissing as e:
        await bot.send_message(message.chat.id, str(e))
    except rollCallNotStarted as e:
        await bot.send_message(message.chat.id, str(e))
    except Exception as e:
        logging.exception("[wait_limit] Unexpected error")
        await bot.send_message(message.chat.id, str(e))


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/shh")
async def shh(message):
    if not await admin_rights(message, manager):
        await bot.send_message(message.chat.id, "You don't have permission to use this command.")
        return
    manager.set_shh_mode(message.chat.id, True)
    log_admin_action(message.chat.id, message.from_user.id, message.from_user.first_name, "shh_on")
    await bot.send_message(message.chat.id, "Ok, i will keep quiet!")


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/louder")
async def louder(message):
    if not await admin_rights(message, manager):
        await bot.send_message(message.chat.id, "You don't have permission to use this command.")
        return
    manager.set_shh_mode(message.chat.id, False)
    log_admin_action(message.chat.id, message.from_user.id, message.from_user.first_name, "shh_off")
    await bot.send_message(message.chat.id, "Ok, i can hear you!")
