import asyncio
import logging
import traceback
from datetime import datetime, timedelta

import pytz
from telebot.async_telebot import AsyncTeleBot

from config import TELEGRAM_TOKEN
from db import end_rollcall

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)


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

                tz = pytz.timezone(timezone)
                now_date_string = datetime.now(tz).strftime("%d-%m-%Y %H:%M")
                now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
                now_date = tz.localize(now_date)

                if rollcall.reminder is not None:
                    reminder_time = rollcall.finalizeDate - timedelta(hours=int(rollcall.reminder))
                    if now_date >= reminder_time:
                        await bot.send_message(
                            chat_id,
                            f"Gentle reminder! event with title - {rollcall.title} is {rollcall.reminder} hour/s away"
                        )
                        rollcall.reminder = None
                        continue

                if rollcall.finalizeDate is not None and rollcall.reminder is None:
                    if now_date >= rollcall.finalizeDate:
                        await bot.send_message(
                            chat_id,
                            f"Event with title - {rollcall.title} is started ! Have a good time. Cheers!\n\n"
                            f"{rollcall.finishList().replace('__RCID__', str(rollcalls.index(rollcall) + 1))}"
                        )

                        rc_db_id = getattr(rollcall, "db_id", None) or getattr(rollcall, "id", None)
                        if rc_db_id is not None:
                            end_rollcall(rc_db_id)

                        if rollcall in rollcalls:
                            rollcalls.remove(rollcall)

                        rollcall.finalizeDate = None
                        continue

            except Exception as e:
                logging.error(f"[check_reminders] Error processing rollcall reminder: {traceback.format_exc()}")

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
        await check(rollcalls, timezone, chat_id)
    except Exception as e:
        logging.error(f"[check_reminders] Unexpected error in reminder loop: {traceback.format_exc()}")