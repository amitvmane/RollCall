import logging

import asyncio

from telegram_helper import bot


async def main():
 
    logging.info("Bot started!")

    try:
        await bot.infinity_polling()

    except Exception as e:
        logging.error(f"Something went wrong! {e}")

    logging.info("Bot shutting down.")

if __name__=="__main__":
    asyncio.run(main())
