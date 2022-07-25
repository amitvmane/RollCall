from exceptions import *
import logging

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