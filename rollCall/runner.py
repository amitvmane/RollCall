import logging

import telebot

from telegram_helper import bot

def main():
 
    logging.info("Bot started!")

    try:
        bot.infinity_polling()

    except Exception as e:
        logging.error(f"Something went wrong! {e}")

    logging.info("Bot shutting down.")

if __name__=="__main__":
    main()