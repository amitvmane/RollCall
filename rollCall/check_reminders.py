import pytz
from telebot import TeleBot
from datetime import datetime, timedelta
import asyncio
import traceback

from models import Database
from config import TELEGRAM_TOKEN, CONN_DB

bot = TeleBot(token=TELEGRAM_TOKEN)
db = Database(CONN_DB)


async def check():
    while True:
        try:
            for chat in db.chat_collection.find():
                timezone = chat['config']['timezone']
                chatId = chat['_id']
                rollCalls = db.rc_collection.find_one({"_id":chatId})['rollCalls']
                
                for rollCall in rollCalls:

                        # IF ROLLCALL HASN'T FINALIZE DATE, SKIP
                        if rollCall['finalizeDate'] == None:
                            continue

                        # GET NOW TIME
                        tz = pytz.timezone(timezone)
                        now_date_string = datetime.now(pytz.timezone(timezone)).strftime("%d-%m-%Y %H:%M")
                        now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
                        now_date = tz.localize(now_date)

                        # CHECK ROLLCALL REMINDER
                        if rollCall["reminder"] != None:
                            
                            reminderTime = datetime.datetime.strptime(rollCall['reminder'], "%H:%M").time()

                            timeLeft = rollCall['finalizeDate'] - timedelta(hours=reminderTime.hour, minutes=reminderTime.minutes)

                            if now_date >= timeLeft:
                                await bot.send_message(chatId, f'Gentle reminder! event with title - {rollCall["title"]} is {rollCall["reminder"]} hour/s away')
                                db.rc_collection.update_one({'_id': chat["_id"], 'rollCalls.rcId': rollCall['rcId']}, {"$set": {"rollCalls.$.reminder": None}})

                        # CHECK ROLLCALL FINALIZE DATE
                        if now_date >= tz.localize(rollCall['finalizeDate']):
                            bot.send_message(chatId, f' Event with title - {rollCall["title"]} has started! Have a good time. Cheers!\n\n')
                            bot.send_message(chatId, db.finishList(chatId, rollCall['rcId']))
                            db.finishRollCall(chatId, rollCall['rcId'])

        except Exception as e:
            print(traceback.format_exc())

        await asyncio.sleep(60)
