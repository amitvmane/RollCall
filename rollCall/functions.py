from exceptions import *
import logging

import Levenshtein
import pytz

from telebot import TeleBot
from pymongo import MongoClient

from config import TELEGRAM_TOKEN


bot=TeleBot(TELEGRAM_TOKEN)


#CONNECT DB
def get_database_chats(CONN_DB):

    client = MongoClient(CONN_DB)
    db_base = client['rollCallDatabase']
    db = db_base['chats']

    return db

def get_database(CONN_DB):

    client = MongoClient(CONN_DB)
    db_base = client['rollCallDatabase']

    print(db_base['rollCalls'])

    return db_base, db_base['chats'], db_base['rollCalls']

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
# def roll_call_not_started(cid):

#     if len(db['rollCalls'])==0:
#         logging.error("Roll call is not active")
#         return False

#     return True
        

#FUNCTION TO RAISE NO ADMIN RIGHTS ERROR
def admin_rights(message):
    
    if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
        logging.error("Error - user does not have sufficient permissions for this operation")
        return False

    return True
    

#FUNCTION TO CHECK IF SHH/LOUDER IS ACTIVE
def send_list(message, chat):
    if chat[message.chat.id]["shh"]:
        return False
    else:
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
            