import asyncio
import logging
import traceback
from datetime import datetime, timedelta

import pytz
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import TELEGRAM_TOKEN
from db import end_rollcall, update_streak_on_checkin, get_all_scheduled_templates, update_template_last_scheduled_date, get_all_chat_ids

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Registry to track active reminder loops per chat_id to prevent duplicates
_active_loops = set()


def _ensure_aware(dt, tz):
    """Return dt as tz-aware; localize naive datetimes using the given tz."""
    if dt is None or dt.tzinfo is not None:
        return dt
    try:
        return tz.localize(dt, is_dst=None)
    except Exception:
        return tz.localize(dt, is_dst=False)


async def check(rollcalls, timezone, chat_id):
    from rollcall_manager import manager
    while True:
        # BUG13: always refresh from manager so additions/removals by /erc are visible
        rollcalls = manager.get_rollcalls(chat_id)
        if len(rollcalls) == 0:
            break

        no_reminder_rollcalls = 0

        # Snapshot the current list and build rc_id_map from it for this pass
        current_rollcalls = list(rollcalls)
        rc_id_map = {id(rc): i + 1 for i, rc in enumerate(current_rollcalls)}

        for rollcall in current_rollcalls:
            try:
                if rollcall.finalizeDate is None:
                    no_reminder_rollcalls += 1
                    continue

                rc_number = rc_id_map.get(id(rollcall))
                if rc_number is None:
                    continue

                tz = pytz.timezone(timezone)
                now_date_string = datetime.now(tz).strftime("%d-%m-%Y %H:%M")
                now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
                now_date = tz.localize(now_date)

                # BUG12: ensure finalizeDate is tz-aware before any comparison
                finalize_dt = _ensure_aware(rollcall.finalizeDate, tz)

                if rollcall.reminder is not None:
                    reminder_time = finalize_dt - timedelta(hours=int(rollcall.reminder))
                    if now_date >= reminder_time:
                        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sending reminder for rollcall #{rc_number}: {rollcall.title}")
                        await bot.send_message(
                            chat_id,
                            f"Gentle reminder! event with title - {rollcall.title} is {rollcall.reminder} hour/s away"
                        )
                        rollcall.reminder = None
                        continue

                if rollcall.finalizeDate is not None and rollcall.reminder is None:
                    if now_date >= finalize_dt:
                        # Take the same lock /erc uses so we don't race with a
                        # concurrent manual end (which would double-send the
                        # finish text and ghost prompt).
                        async with manager.get_erc_lock(chat_id):
                            current_rcs = manager.get_rollcalls(chat_id)
                            if rollcall not in current_rcs:
                                # /erc beat us to it — nothing left to do.
                                continue
                            # Recompute rc_number in case /erc on another
                            # rollcall renumbered the list while we waited.
                            rc_number = current_rcs.index(rollcall) + 1

                            logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Rollcall #{rc_number} started: {rollcall.title}")
                            if not manager.get_shh_mode(chat_id):
                                finish_text = rollcall.finishList().replace('__RCID__', str(rc_number))
                                finish_text = f"{finish_text}\n\n🕐 Auto-closed at scheduled time"
                                await bot.send_message(chat_id, finish_text)

                            rc_db_id = getattr(rollcall, "db_id", None) or getattr(rollcall, "id", None)

                            # Update attendance streaks for real users who were IN
                            for u in rollcall.inList:
                                if isinstance(u.user_id, int):
                                    try:
                                        update_streak_on_checkin(chat_id, u.user_id)
                                    except Exception:
                                        logging.warning(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Failed to update streak for user {u.user_id} in chat {chat_id}: {traceback.format_exc()}")

                            if rc_db_id is not None:
                                end_rollcall(rc_db_id)

                                # Fire ghost prompt if tracking is enabled and rollcall had IN users
                                try:
                                    from db import get_rollcall_in_users
                                    ghost_tracking_on = manager.get_ghost_tracking_enabled(chat_id)
                                    has_users = bool(get_rollcall_in_users(rc_db_id))
                                    absent_already = getattr(rollcall, "absent_marked", False)
                                    if ghost_tracking_on and has_users and not absent_already:
                                        markup = InlineKeyboardMarkup(row_width=2)
                                        markup.add(
                                            InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rc_db_id}"),
                                            InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{rc_db_id}"),
                                        )
                                        await bot.send_message(chat_id, "👻 Did anyone ghost today's session?", reply_markup=markup)
                                except Exception:
                                    logging.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error sending ghost prompt after auto-close: {traceback.format_exc()}")

                            if rollcall in rollcalls:
                                rollcalls.remove(rollcall)

                            # Clean up panel state for the closed rollcall
                            try:
                                from bot_state import _panel_msg_ids
                                _panel_msg_ids.pop((chat_id, rc_number), None)
                                for num in sorted(n for (c, n) in list(_panel_msg_ids) if c == chat_id and n > rc_number):
                                    _panel_msg_ids[(chat_id, num - 1)] = _panel_msg_ids.pop((chat_id, num))
                            except Exception:
                                pass

                            rollcall.finalizeDate = None
                        continue

            except Exception as e:
                logging.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error processing rollcall reminder: {traceback.format_exc()}")

        if len(rollcalls) == 0 or no_reminder_rollcalls == len(rollcalls):
            break

        await asyncio.sleep(60)


async def start(rollcalls, timezone, chat_id):
    """
    Start the reminder/auto-close loop for a specific chat.
    Safe to call multiple times; will not start duplicate loops.
    """
    if chat_id in _active_loops:
        logging.debug(f"Reminder loop already active for chat {chat_id}")
        return

    try:
        _active_loops.add(chat_id)
        current_sec = int(datetime.now().strftime("%S"))
        delay = 60 - current_sec
        if delay == 60:
            delay = 0
        await asyncio.sleep(delay)
        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting reminder check for chat {chat_id}")
        await check(rollcalls, timezone, chat_id)
    except Exception:
        logging.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Unexpected error in reminder loop: {traceback.format_exc()}")
    finally:
        _active_loops.discard(chat_id)
        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Reminder loop finished for chat {chat_id}")


async def resume_reminder_loops():
    """On startup, restart reminder loops for all chats that have active rollcalls with a finalizeDate.
    Called once from runner.py to recover state after a bot restart.
    """
    from rollcall_manager import manager

    chat_ids = get_all_chat_ids()
    resumed = 0
    for chat_id in chat_ids:
        try:
            chat = manager.get_chat(chat_id)
            rollcalls = manager.get_rollcalls(chat_id)
            if any(rc.finalizeDate is not None for rc in rollcalls):
                tzname = chat.get("timezone", "Asia/Calcutta")
                asyncio.create_task(start(rollcalls, tzname, chat_id))
                resumed += 1
                logging.info(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"Resumed reminder loop for chat {chat_id} ({len(rollcalls)} active rollcall(s))"
                )
        except Exception:
            logging.error(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"Error resuming reminder loop for chat {chat_id}: {traceback.format_exc()}"
            )
    logging.info(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Startup: resumed {resumed} reminder loop(s) across {len(chat_ids)} chat(s)"
    )


async def _auto_start_from_template(chat_id: int, tmpl: dict):
    """Create a rollcall from a scheduled template and announce it to the group."""
    from rollcall_manager import manager
    from functions import get_next_weekday_datetime

    chat = manager.get_chat(chat_id)
    tzname = chat.get("timezone", "Asia/Calcutta")
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Calcutta")
        tzname = "Asia/Calcutta"

    rollcalls = manager.get_rollcalls(chat_id)
    if len(rollcalls) >= 3:
        await bot.send_message(
            chat_id,
            f"⚠️ Could not auto-start template '{tmpl['name']}': maximum 3 active rollcalls already open."
        )
        return

    title = tmpl.get("title") or tmpl["name"]
    rc = manager.add_rollcall(chat_id, title)

    if tmpl.get("inlistlimit") is not None:
        rc.inListLimit = tmpl["inlistlimit"]
    if tmpl.get("location"):
        rc.location = tmpl["location"]
    if tmpl.get("eventfee"):
        rc.event_fee = tmpl["eventfee"]

    rc.timezone = tzname
    rc.finalizeDate = None

    event_day = tmpl.get("event_day")
    event_time = tmpl.get("event_time")
    if event_day and event_time:
        dt = get_next_weekday_datetime(tz, event_day, event_time)
        if dt:
            rc.finalizeDate = dt

    rc.save()

    rc_index = len(rollcalls) - 1  # 0-based; rc was just appended by add_rollcall
    rc_number = rc_index + 1       # 1-based display number

    # Send the full rollcall panel with inline vote buttons
    try:
        from handlers.lifecycle import get_status_keyboard
        from bot_state import _panel_msg_ids
        markup = await get_status_keyboard(rc_number)
        text = rc.allList().replace("__RCID__", str(rc_number))
        sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
        _panel_msg_ids[(chat_id, rc_number)] = sent.message_id
    except Exception:
        # Fallback: plain announcement if panel send fails
        close_info = ""
        if rc.finalizeDate:
            close_info = f"\nCloses: {rc.finalizeDate.strftime('%A, %d %b at %H:%M')}"
        from bot_state import _esc_md
        await bot.send_message(
            chat_id,
            f"📋 *{_esc_md(title)}* rollcall is now open!{close_info}\nVote with /in or /out.",
            parse_mode="Markdown"
        )

    logging.info(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Auto-started template '{tmpl['name']}' for chat {chat_id}"
    )

    # Ensure the reminder/auto-close loop is running for this new rollcall
    if rc.finalizeDate:
        def _log_exc(t):
            if not t.cancelled() and t.exception():
                logging.error(f"Reminder loop raised: {t.exception()}")
        asyncio.create_task(start(rollcalls, tzname, chat_id)).add_done_callback(_log_exc)


async def check_template_schedules():
    """Persistent loop that fires scheduled templates at their configured day/time."""
    # Align to the next minute boundary before entering the loop
    current_sec = int(datetime.now().strftime("%S"))
    if current_sec != 0:
        await asyncio.sleep(60 - current_sec)

    while True:
        try:
            scheduled = get_all_scheduled_templates()
            for tmpl in scheduled:
                chat_id = tmpl.get("chatid")
                schedule_day = tmpl.get("schedule_day")
                schedule_time = tmpl.get("schedule_time")
                last_date = tmpl.get("last_scheduled_date")

                if not chat_id or not schedule_day or not schedule_time:
                    continue

                try:
                    from rollcall_manager import manager
                    chat = manager.get_chat(chat_id)
                    tz = pytz.timezone(chat.get("timezone", "Asia/Calcutta"))
                except Exception:
                    tz = pytz.timezone("Asia/Calcutta")

                now = datetime.now(tz)
                today_name = now.strftime("%A").lower()
                now_time_str = now.strftime("%H:%M")
                today_date = now.strftime("%Y-%m-%d")

                recurrence_type = tmpl.get("recurrence_type", "weekly") or "weekly"

                if recurrence_type == "monthly":
                    # schedule_day is a day-of-month integer string (e.g. "15")
                    try:
                        target_day = int(schedule_day)
                    except (ValueError, TypeError):
                        continue
                    if now.day != target_day:
                        continue
                    if now_time_str != schedule_time:
                        continue
                    if last_date == today_date:
                        continue
                elif recurrence_type == "biweekly":
                    if today_name != schedule_day.lower():
                        continue
                    if now_time_str != schedule_time:
                        continue
                    if last_date == today_date:
                        continue
                    if last_date:
                        try:
                            last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
                            if (now.date() - last_dt).days < 14:
                                continue
                        except (ValueError, TypeError):
                            continue
                else:
                    # weekly (default)
                    if today_name != schedule_day.lower():
                        continue
                    if now_time_str != schedule_time:
                        continue
                    if last_date == today_date:
                        continue

                try:
                    await _auto_start_from_template(chat_id, tmpl)
                    update_template_last_scheduled_date(chat_id, tmpl["name"], today_date)
                except Exception:
                    logging.error(
                        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"Failed to auto-start template '{tmpl.get('name')}' for chat {chat_id}: "
                        f"{traceback.format_exc()}"
                    )

        except Exception:
            logging.error(
                f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"Error in template schedule loop: {traceback.format_exc()}"
            )

        await asyncio.sleep(60)