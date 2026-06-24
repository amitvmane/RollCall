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
    reply_error,
)
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    timeError,
)
from functions import admin_rights, roll_call_not_started
from rollcall_manager import manager
from services import settings as settings_svc


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
        input_datetime = " ".join(pmts).strip()
        result = settings_svc.set_rollcall_time(
            cid, rc_number, input_datetime,
            message.from_user.id, message.from_user.first_name,
        )

        if not manager.get_shh_mode(cid):
            if result["cancelled"]:
                await bot.send_message(message.chat.id, "Reminder time is canceled.")
            else:
                suffix = "\n\nReminder has been reset!" if result["reminder_reset"] else ""
                rc_title = result["rollcall"]["title"]
                await bot.send_message(
                    cid,
                    f"Event notification time is set to {result['finalize_str']} {result['timezone']}"
                    f" for '{rc_title}' (ID: {rc_number + 1}).{suffix}"
                )

        rc = manager.get_rollcall(cid, rc_number)
        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)
        if not result["cancelled"]:
            from check_reminders import start
            rollcalls = manager.get_rollcalls(cid)
            asyncio.create_task(start(rollcalls, rc.timezone, cid)).add_done_callback(_log_task_exc)

    except Exception as e:
        from bot_state import _USER_FACING_EXCEPTIONS
        if not isinstance(e, _USER_FACING_EXCEPTIONS):
            logging.exception("[set_rollcall_time] Unexpected error")
        await reply_error(message, e)


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

        if len(pmts) == 0:
            raise parameterMissing("The format is /set_rollcall_reminder hours")

        cancel = pmts[0].lower() == "cancel"
        hours_val = None if cancel else (int(pmts[0]) if pmts[0].isdigit() else None)
        if not cancel and hours_val is None:
            raise parameterMissing("The format is /set_rollcall_reminder hours")

        result = settings_svc.set_reminder(
            cid, rc_number, hours_val,
            message.from_user.id, message.from_user.first_name,
        )

        if not manager.get_shh_mode(cid):
            if result["cancelled"]:
                await bot.send_message(message.chat.id, "Reminder Notification is canceled.")
            else:
                await bot.send_message(cid, f"I will remind {result['hours']}hour/s before the event! Thank you!")

        rc = manager.get_rollcall(cid, rc_number)
        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except ValueError:
        logging.info(f"[{_ts()}] [reminder] invalid value from user")
        await bot.send_message(cid, "The correct format is /set_rollcall_reminder HH")
    except Exception as e:
        await reply_error(cid, e)


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

        event_price = " ".join(pmts)
        event_price_number = re.findall('[0-9]+', event_price)

        if len(event_price_number) == 0 or int(event_price_number[0]) <= 0:
            raise incorrectParameter("The correct format is '/event_fee Integer' Where 'Integer' it's up to 0 number")

        settings_svc.set_event_fee(cid, event_price, message.from_user.id, message.from_user.first_name, rc_number)
        rc = manager.get_rollcall(cid, rc_number)

        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"Event Fee set to {event_price}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await reply_error(cid, e)


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

        result = settings_svc.get_individual_fee(cid, rc_number)
        await bot.send_message(cid, f"Individual fee is {result['individual_fee']}")

    except Exception as e:
        await reply_error(cid, e)


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
        await reply_error(cid, e)


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
        settings_svc.set_location(cid, place, message.from_user.id, message.from_user.first_name, rc_number)
        rc = manager.get_rollcall(cid, rc_number)

        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"Location updated for '{rc.title}' (ID: {rc_number + 1}) → {place}.")

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await reply_error(cid, e)


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

        if len(pmts) == 0 or not str(pmts[0]).isdigit() or int(pmts[0]) < 0:
            raise parameterMissing("Input limit is missing or it's not a valid number (use 0 to remove the cap)")

        limit = int(pmts[0])

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 0 or len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        async with manager.get_chat_write_lock(cid):
            result = settings_svc.set_wait_limit(
                cid, limit,
                message.from_user.id, message.from_user.first_name,
                rc_number=rc_number,
            )

        rc = manager.get_rollcall(cid, rc_number)
        rc_title = result["rollcall"]["title"]
        rc_number_1 = rc_number + 1
        shh = manager.get_shh_mode(cid)

        if limit == 0:
            logging.info(f"[{_ts()}] Attendance limit cleared for rollcall #{rc_number_1}")
            if not shh:
                await bot.send_message(cid, f"Attendance limit cleared for '{rc_title}' (#{rc_number_1}) — no cap.")
        else:
            logging.info(f"[{_ts()}] Max limit of attendees is set to {limit}")
            if not shh:
                await bot.send_message(cid, f"Max limit of attendees is set to {limit}")

        for u in result["demoted"]:
            uid = u["user_id"]
            name = u["name"]
            if not shh:
                await bot.send_message(
                    cid,
                    f"{name} moved from IN to WAITING because limit was set to {limit} for '{rc_title}' (ID: {rc_number_1})."
                )
            async def _dm_demoted(uid=uid, title=rc_title, rcn=rc_number_1):
                try:
                    await bot.send_message(
                        uid,
                        f"⚠️ A new attendance limit was set for *{_esc_md(title)}* (#{rcn}) and you've been moved from IN to the WAITLIST.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logging.warning(f"Could not DM demoted user {uid}")
            asyncio.create_task(_dm_demoted()).add_done_callback(_log_task_exc)

        if result["promoted"]:
            from handlers.lifecycle import notify_proxy_owner_wait_to_in
            for u in result["promoted"]:
                uid = u["user_id"]
                name = u["name"]
                is_proxy = u["is_proxy"]
                if not shh:
                    if not is_proxy:
                        await bot.send_message(
                            cid,
                            f"[{_esc_md(name)}](tg://user?id={uid}) → IN (from WAITING) for '{_esc_md(rc_title)}' (#{rc_number_1})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(
                            cid,
                            f"{name} → IN (from WAITING) for '{rc_title}' (#{rc_number_1})",
                        )
                if not is_proxy:
                    asyncio.create_task(_dm_promoted_real_user(uid, rc_title, rc_number_1)).add_done_callback(_log_task_exc)

                # find the actual User object from the rc for notify_proxy_owner
                rc_user = next((x for x in rc.inList if (x.user_id == uid or x.name == name)), None)
                if rc_user is not None:
                    await notify_proxy_owner_wait_to_in(rc, rc_user, cid, rc_title, rc_number_1)

        if limit > 0 and len(rc.inList) == limit and not result["was_full"]:
            if not shh:
                await bot.send_message(
                    cid,
                    f"Rollcall '{rc_title}' (ID: {rc_number_1}) has reached its max limit ({limit}). New IN will go to WAITING."
                )

        from handlers.lifecycle import _update_panel
        await _update_panel(cid, rc_number_1, rc)

    except parameterMissing as e:
        await reply_error(message, e)
    except rollCallNotStarted as e:
        await reply_error(message, e)
    except Exception as e:
        from bot_state import _USER_FACING_EXCEPTIONS
        if not isinstance(e, _USER_FACING_EXCEPTIONS):
            logging.exception("[wait_limit] Unexpected error")
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/shh")
async def shh(message):
    if not await admin_rights(message, manager):
        await bot.send_message(message.chat.id, "You don't have permission to use this command.")
        return
    settings_svc.set_shh_mode(message.chat.id, True, message.from_user.id, message.from_user.first_name)
    await bot.send_message(message.chat.id, "Ok, i will keep quiet!")


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/louder")
async def louder(message):
    if not await admin_rights(message, manager):
        await bot.send_message(message.chat.id, "You don't have permission to use this command.")
        return
    settings_svc.set_shh_mode(message.chat.id, False, message.from_user.id, message.from_user.first_name)
    await bot.send_message(message.chat.id, "Ok, i can hear you!")
