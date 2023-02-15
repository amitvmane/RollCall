import logging
import asyncio

from bot import bot
from models.Database import Database
from config.config import CONN_DB
from reminders.check_reminders import check

from datetime import datetime

async def main():
    try:
        Database(CONN_DB).db['system'].update_one({"_id": "63d4cc4378e1ae99cb9f329e"}, {"$set": {"last_login": datetime.now()}}, upsert=True)
        await asyncio.gather(bot.polling(timeout=100, non_stop=True), check())
        
    except Exception as e:
        logging.error(f"Something went wrong! {e}")

    logging.info("Bot shutting down.")
    Database(CONN_DB).db['system'].update_one({"name": "logs"}, {"$set": {"last_logout": datetime.now()}}, upsert=True)

if __name__ == "__main__":
    asyncio.run(main())
