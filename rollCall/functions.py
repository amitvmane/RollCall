from exceptions import *
import logging
import traceback

import Levenshtein
import pytz
from telebot import TeleBot

from config import TELEGRAM_TOKEN

bot=TeleBot(TELEGRAM_TOKEN)

#COMPLETE ROLLCALL ID
def get_rc_number(chat_roll_calls, pmts):

    if len(pmts)>1 and "::" in pmts[-1]:
        
        try:

            rc_number=int(pmts[-1].replace("::",""))
            del pmts[-1]

            if rc_number not in [i['rcId'] for i in chat_roll_calls]:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

            return rc_number

        except:
            print(traceback.format_exc())
            raise incorrectParameter("The rollcall number must be a positive integer")
    
    else:
        return 1
  
#FUNCTION TO RAISE NO ADMIN RIGHTS ERROR
def admin_rights(message):
    
    if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
        logging.error("Error - user does not have sufficient permissions for this operation")
        return False

    return True
    
def auto_complete_timezone(timezone):
    continent=timezone.split("/")[0].lower()
    place=timezone.split("/")[1].lower().replace(" ","_")

    if place=='india':
        place='calcutta'
    if place=='argentina':
        place='buenos_aires'

    for tz in pytz.all_timezones:
        if tz.split("/")[0].lower()==continent:
            if len(tz.split("/"))==2:
                diff=Levenshtein.distance(place, tz.split("/")[1].lower(), score_cutoff=int((len(place)*0.35)))

            elif len(tz.split("/"))==3:
                diff=Levenshtein.distance(place, tz.split("/")[2].lower(), score_cutoff=int((len(place)*0.35)))

            if diff<=int((len(place)*0.35)):
                return tz
            