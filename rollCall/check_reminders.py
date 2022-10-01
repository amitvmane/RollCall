import pytz
from datetime import datetime, timedelta
import asyncio
from config import TELEGRAM_TOKEN
from telebot.async_telebot import AsyncTeleBot

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

async def check(rollcalls, timezone, chat_id):

    current_sec = int(datetime.now().strftime("%S"))
    delay=0

    if current_sec!=0:
        delay=60-current_sec
    
    await asyncio.sleep(delay)

    while True:
        noReminderRollcalls=0

        if len(rollcalls)==0 or noReminderRollcalls==len(rollcalls):
            break

        for rollcall in rollcalls:
            try:

                #IF ROLLCALL HASN'T FINALIZE DATE, SKIP
                if rollcall.finalizeDate==None:
                    noReminderRollcalls+=1
                    continue

                #GET NOW TIME
                tz=pytz.timezone(timezone)
                now_date_string=datetime.now(pytz.timezone(timezone)).strftime("%d-%m-%Y %H:%M")
                now_date=datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
                now_date=tz.localize(now_date)
               
                #CHECK ROLLCALL REMINDER
                if rollcall.reminder!=None:

                    reminder_time=rollcall.finalizeDate-timedelta(hours=int(rollcall.reminder))

                    if now_date>=reminder_time:
                        await bot.send_message(chat_id, f'Gentle reminder! event with title - {rollcall.title} is {rollcall.reminder} hour/s away')   
                        rollcall.reminder=None

                #CHECK ROLLCALL FINALIZE DATE
                if rollcall.finalizeDate!=None and rollcall.reminder==None:

                    print(now_date, rollcall.finalizeDate)
                    if now_date>=rollcall.finalizeDate:
                        await bot.send_message(chat_id, f' Event with title - {rollcall.title} is started ! Have a good time. Cheers!\n\n{rollcall.allList()}')
                        rollcalls.remove(rollcall)
                        rollcall.finalizeDate=None
                        
            except Exception as e:
                print(e)

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
        print(e)
 