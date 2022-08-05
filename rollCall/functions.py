from exceptions import *
import logging
import Levenshtein
import datetime
import pytz
import asyncio
from check_reminder import check

#FUNCTION TO RAISE RC ALREADY STARTED ERROR
#USELESS IN NEW FEATURE
def roll_call_already_started(message, chat):
    try:
        if len(chat[message.chat.id]["rollCalls"])==1:
            logging.error(f"Roll call with title {chat[message.chat.id]['rollCalls'][0].title} is still in progress")
            return False
        else:
            return True
    except:
        return True

#FUNCTION TO RAISE RC NOT STARTED ERROR
def roll_call_not_started(message, chat):
    try:
        if len(chat[message.chat.id]["rollCalls"])==0:
            logging.error("Roll call is not active")
            return False
        else:
            return True
    except:
        return False

#FUNCTION TO RAISE NO ADMIN RIGHTS ERROR
def admin_rights(message, chat):
    try:
        if chat[message.chat.id]["adminRights"]:
            if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
                logging.error("Error - user does not have sufficient permissions for this operation")
                return False
        else:
            return True
    except:
        return True

#FUNCTION TO CHECK IF SHH/LOUDER IS ACTIVE
def send_list(message, chat):
    if chat[message.chat.id]["shh"]:
        return False
    else:
        return True

def auto_complete_timezone(timezone):
    continent=timezone.text.split("/")[0]
    place=timezone.text.split("/")[1].lower().replace(" ","_")

    print(continent, place)

    for tz in pytz.all_timezones:
        if tz.split("/")[0].lower()==continent:
            if len(tz.split("/"))==2:
                diff=Levenshtein.distance(place, tz.split("/")[1].lower(), score_cutoff=int((len(place)*0.35)))

            elif len(tz.split("/"))==3:
                diff=Levenshtein.distance(place, tz.split("/")[2].lower(), score_cutoff=int((len(place)*0.35)))

            if diff<=int((len(place)*0.35)):
                return tz
            