import logging

import asyncio

from telegram_helper import bot
from check_reminders import start


async def main():
 
    logging.info("Bot started!")

    try: 
        await bot.polling(non_stop=True, timeout=40)

    except Exception as e:
        logging.error(f"Something went wrong! {e}")

    logging.info("Bot shutting down.")

if __name__=="__main__":
    asyncio.run(main())