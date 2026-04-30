import asyncio
import logging
import traceback
from datetime import datetime, timedelta

import pytz
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import TELEGRAM_TOKEN
from db import end_rollcall, update_streak_on_checkin

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


async def check(rollcalls, timezone, chat_id):
    current_sec = int(datetime.now().strftime("%S"))
    delay = 0
    if current_sec != 0:
        delay = 60 - current_sec
    await asyncio.sleep(delay)

    while True:
        if len(rollcalls) == 0:
            break

        no_reminder_rollcalls = 0

        for rollcall in list(rollcalls):
            try:
                if rollcall.finalizeDate is None:
                    no_reminder_rollcalls += 1
                    continue

                # Store rc_number before any modifications (safer than using index later)
                rc_number = rollcalls.index(rollcall) + 1

                tz = pytz.timezone(timezone)
                now_date_string = datetime.now(tz).strftime("%d-%m-%Y %H:%M")
                now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
                now_date = tz.localize(now_date)

                if rollcall.reminder is not None:
                    reminder_time = rollcall.finalizeDate - timedelta(hours=int(rollcall.reminder))
                    if now_date >= reminder_time:
                        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sending reminder for rollcall #{rc_number}: {rollcall.title}")
                        await bot.send_message(
                            chat_id,
                            f"Gentle reminder! event with title - {rollcall.title} is {rollcall.reminder} hour/s away"
                        )
                        rollcall.reminder = None
                        continue

                if rollcall.finalizeDate is not None and rollcall.reminder is None:
                    if now_date >= rollcall.finalizeDate:
                        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Rollcall #{rc_number} started: {rollcall.title}")
                        await bot.send_message(
                            chat_id,
                            f"Event with title - {rollcall.title} is started ! Have a good time. Cheers!\n\n"
                            f"{rollcall.finishList().replace('__RCID__', str(rc_number))}"
                        )

                        rc_db_id = getattr(rollcall, "db_id", None) or getattr(rollcall, "id", None)

                        # Update attendance streaks for real users who were IN
                        for u in rollcall.inList:
                            if isinstance(u.user_id, int):
                                try:
                                    update_streak_on_checkin(chat_id, u.user_id)
                                except Exception:
                                    pass

                        if rc_db_id is not None:
                            end_rollcall(rc_db_id)

                            # Fire ghost prompt if tracking is enabled and rollcall had IN users
                            try:
                                from rollcall_manager import manager
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

                        rollcall.finalizeDate = None
                        continue

            except Exception as e:
                logging.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Error processing rollcall reminder: {traceback.format_exc()}")

        if len(rollcalls) == 0 or no_reminder_rollcalls == len(rollcalls):
            break

        await asyncio.sleep(60)


async def start(rollcalls, timezone, chat_id):
    try:
        current_sec = int(datetime.now().strftime("%S"))
        delay = 60 - current_sec
        if delay == 60:
            delay = 0
        await asyncio.sleep(delay)
        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting reminder check for chat {chat_id}")
        await check(rollcalls, timezone, chat_id)
    except Exception as e:
        logging.error(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Unexpected error in reminder loop: {traceback.format_exc()}")