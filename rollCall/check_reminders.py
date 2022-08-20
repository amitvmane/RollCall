import pytz
from datetime import datetime, timedelta
import asyncio
from config import TELEGRAM_TOKEN
import telebot
from telebot.async_telebot import AsyncTeleBot
import traceback


bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

async def check(rollcall, timezone, chat_id):
    while True:

        if rollcall.finalizeDate==None:
            print('Breaking..')
            break

        tz=pytz.timezone(timezone)
        now_date_string=datetime.now(pytz.timezone(timezone)).strftime("%d-%m-%Y %H:%M")
        now_date=datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
        now_date=tz.localize(now_date)

        now_day=now_date.day
        now_hour=now_date.hour
        now_minute=now_date.minute
        condition=True


        if rollcall.reminder!=None:

            reminder_time=rollcall.finalizeDate-timedelta(hours=int(rollcall.reminder))

            if now_day>=reminder_time.day and now_hour>=reminder_time.hour and now_minute>=reminder_time.minute:
                await bot.send_message(chat_id, f'Gentle reminder! event with title - {rollcall.title} is {rollcall.reminder} hour/s away')   
                rollcall.reminder=None
            else:
                print('Waiting 60 seconds to remind..')
                await asyncio.sleep(60)


        if rollcall.finalizeDate!=None and rollcall.reminder==None:

            if (now_day>=rollcall.finalizeDate.day)and (now_hour>=rollcall.finalizeDate.hour) and (now_minute>=rollcall.finalizeDate.minute):
                await bot.send_message(chat_id, f' Event with title - {rollcall.title} is started ! Have a good time. Cheers!')
                rollcall.finalizeDate=None
                break
            else:
                print("Waiting 60 seconds to finalize..")
                await asyncio.sleep(60)
        
        

async def start(rollcall, timezone, chat_id):
    print("Has set a reminder ",rollcall.title)
    current_sec = int(datetime.now().strftime("%S"))
    delay = 60 - current_sec
    if delay == 60:
        delay = 0
    print('Waiting..')
    await asyncio.sleep(delay)
    await check(rollcall, timezone, chat_id)
 