import logging
import time
import datetime
import re
import asyncio
import json

import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.types import(
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ForceReply
)
import pytz

from config import TELEGRAM_TOKEN, ADMINS
from exceptions import *
from models import RollCall, User
from functions import *
from check_reminders import start
import traceback

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

chat = {}

logging.info("Bot already started")

# START COMMAND, SIMPLE TEXT
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message):

    cid = message.chat.id

    # IF THIS CHAT DOESN'T HAVE A STORAGE, CREATES ONE
    if cid not in chat:
        chat[cid] = {}
        chat[cid]["rollCalls"] = []

    # CHECK FOR ADMIN RIGHTS
    if admin_rights(message, chat) == False:
        await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
        return

    # START MSG
    await bot.send_message(message.chat.id, 'Hi! im RollCall!\n\nType /help to see all the commands')

# HELP COMMAND WITH ALL THE COMMANDS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/help")
async def help_commands(message):
    #HELP MSG
    await bot.send_message(message.chat.id, '''The commands are:\n-/start  - To start the bot\n-/help - To see the commands\n-/start_roll_call - To start a new roll call (optional title)\n-/in - To let everybody know you will be attending (optional comment)\n-/out - To let everybody know you wont be attending (optional comment)\n-/maybe - To let everybody know you dont know (optional comment)\n-/whos_in - List of those who will go\n-/whos_out - List of those who will not go\n-/whos_maybe - List of those who maybe will go\n-/set_title - To set a title for the current roll call\n-/set_in_for - Allows you to respond for another user\n-/set_out_for - Allows you to respond for another user\n-/set_maybe_for - Allows you to respond for another user\n-/shh - to apply minimum output for each command\n-/louder - to disable minimum output for each command\n-/set_limit - To set a limit to IN state\n-/end_roll_call - To end a roll call\n-/set_rollcall_time - To set a finalize time to the current rc. Accepts 2 parameters date (DD-MM-YYYY) and time (H:M). Write cancel to delete it\n-/set_rollcall_reminder - To set a reminder before the ends of the rc. Accepts 1 parameter, hours as integers. Write 'cancel' to delete the reminder\n-/timezone - To set your timezone, accepts 1 parameter (Continent/Country) or (Continent/State)\n-/when - To check the start time of a roll call\n-/location - To check the location of a roll call
    ''')

# SET ADMIN RIGHTS TO TRUE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):

    response=await bot.get_chat_member(message.chat.id, message.from_user.id)

    if response.status not in ['admin', 'creator']:
        await bot.send_message(message.chat.id, "You don't have permissions to use this command :(")


    # IF THIS CHAT DOESN'T HAVE A STORAGE, CREATES ONE
    if message.chat.id not in chat:
        chat[message.chat.id]={}
        chat[message.chat.id]["adminRigts"]=False
        chat[message.chat.id]["rollCalls"]=[]

    # DEFINING NEW STATE OF ADMIN RIGTS
    chat[message.chat.id]["adminRigts"]=True

    await bot.send_message(message.chat.id, 'Admin permissions activated')

# SET ADMIN RIGHTS TO FALSE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):

    response=await bot.get_chat_member(message.chat.id, message.from_user.id)

    if response.status not in ['admin', 'creator']:
        await bot.send_message(message.chat.id, "You don't have permissions to use this command :(")

    # IF THIS CHAT DOESN'T HAVE A STORAGE, CREATES ONE
    if message.chat.id not in chat:
        chat[message.chat.id]={}
        chat[message.chat.id]["adminRigts"]=False
        chat[message.chat.id]["rollCalls"]=[]

    # DEFINING NEW STATE OF ADMIN RIGTS
    chat[message.chat.id]["adminRigts"]=False

    await bot.send_message(message.chat.id, 'Admin permissions disabled')

# SEND ANNOUNCEMENTS TO ALL GROUPS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):
    if len(message.text.split(" ")) > 1:
        msg=message.text.split(" ")[1:]
        try:
            with open('./database.json', 'r') as read_file:
                data=json.load(read_file)
        except Exception as e:
            print(traceback.format_exc())
            print(e)
            return

        for k in data:
            try:
                await bot.send_message(int(k["chat_id"]), " ".join(msg))
            except:
                pass
    else:
        await bot.send_message(message.chat.id, "Message is missing")

# ADJUST TIMEZONE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg=message.text
        cid=message.chat.id

        if len(msg.split(" ")) < 2:
            raise parameterMissing(
                "The correct format is: /timezone continent/country or continent/state")
        if len(msg.split(" ")[1].split("/")) != 2:
            raise parameterMissing(
                "The correct format is: /timezone continent/country or continent/state")

        response=auto_complete_timezone(" ".join(msg.split(" ")[1:]))

        if message.chat.id not in chat:
            chat[message.chat.id]={}

        if response != None:
            await bot.send_message(message.chat.id, f"Your timezone has been set to {response}")
            for rollcall in chat[message.chat.id]['rollCalls']:
                rollcall.timezone=response
        else:
            await bot.send_message(message.chat.id, f"Your timezone doesnt exists, if you can't found your timezone, check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode = 'HTML')

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

# Version command
@ bot.message_handler(func = lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/version")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/v")
async def version_command(message):
    file=open('./version.json')
    data=json.load(file)
    for i in range(0,len(data)):

        version=data[-1-i]
 
        if version["DeployedOnProd"]=='Y':
            txt=''
            txt+=f'Version: {version["Version"]}\nDescription: {version["Description"]}\nDeployed: {version["DeployedOnProd"]}\nDeployed datetime: {version["DeployedDatetime"]}'
            await bot.send_message(message.chat.id, txt)
            break
        



#GET ALL ROLLCALLS OF THE CURRENT CHAT
@ bot.message_handler(func = lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/rollcalls")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/r")
async def show_reminders(message):
    cid = message.chat.id

    if len(chat[cid]["rollCalls"])==0:
        await bot.send_message(cid, "There are not rollcalls yet")

    for rollcall in chat[cid]["rollCalls"]:
        await bot.send_message(cid, f"Rollcall number {chat[cid]['rollCalls'].index(rollcall)+1}\n\n"+rollcall.allList())

# START A ROLL CALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):

    # DEFINING VARIABLES
    cid=message.chat.id
    msg=message.text
    title=''

    with open('./database.json', 'r') as read_file:
        database=json.load(read_file)
        read_file.close()
    
    cond=True
    for i in database:
        if int(i['chat_id']) == message.chat.id:
            cond=False

    if cond == True:
        database.append({'chat_id': cid})
        with open('./database.json', 'w') as write_file:
            json.dump(database, write_file)

    # IF THIS CHAT DOESN'T HAVE A STORAGE, CREATES ONE
    if cid not in chat:
        chat[cid]={}

    if 'rollCalls' not in chat[cid]:
        chat[cid]['rollCalls']=[]

    if len(chat[cid]['rollCalls'])>=3:
        await bot.send_message(cid, "The maximum of rollcalls is 3. Please finish one to create another")
        raise amountOfRollCallsReached("The maximum of rollcalls is 3. Please finish one to create another")

    try:
        # CHECK IF ADMIN_RIGHTS ARE ON
        if admin_rights(message, chat) == False:
            raise insufficientPermissions(
                "Error - user does not have sufficient permissions for this operation")

        else:

            # SET THE RC TITLE
            arr=msg.split(" ")
            if len(arr) > 1:
                arr.pop(0)
                title=' '.join(arr)
            else:
                title='<Empty>'

            ###DEFAULT CONFIG###

            chat[cid]['shh']=False

            if "allNames" not in chat[cid]:
                chat[cid]["allNames"]=[]

            if "adminRights" not in chat[cid]:
                chat[cid]["adminRights"]=False

            if "reminders" not in chat[cid]:
                chat[cid]["reminders"]={}

            if 'waitingRC' not in chat[cid]:
                chat[cid]['tasks']=[]

            chat[cid]["allNames"].append([])

            ###DEFAULT CONFIG###

            # ADD RC TO LIST
            chat[cid]["rollCalls"].append(RollCall(title))
            await bot.send_message(message.chat.id, f"Roll call with title: {title} started!")

    except insufficientPermissions as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)
    except rollCallAlreadyStarted as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

@ bot.message_handler(func = lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        if len(message.text.split(" ")) == 1:
            raise parameterMissing(
                "invalid datetime format, refer help section for details")

        if len(message.text.split(" ")) < 2 and msg!='cancel':
            raise parameterMissing(
                "invalid datetime format, refer help section for details")

        cid=message.chat.id
        msg=message.text
        rc_number=0 #DEFAULT RC NUMBER
        pmts=msg.split(" ")[1:]

        #IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
        if "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")


        
        # if (len(pmts)>2 or msg=='cancel' and len(pmts)>=2) and ":" in pmts[-1]:
        #     rc_number=int(pmts[-1].replace(":",""))-1
        #     pmts=pmts[:len(pmts)-2]
          
        
        #CANCEL THE CURRENT REMINDER TIME
        if (pmts[0]).lower() == 'cancel':
            chat[message.chat.id]['rollCalls'][rc_number].finalizeDate=None
            chat[message.chat.id]['rollCalls'][rc_number].reminder=None
            await bot.send_message(message.chat.id, "Reminder time is canceled.")
            return


        #PARSING INPUT DATETIME
        input_datetime=" ".join(pmts).strip()

        tz=pytz.timezone(chat[message.chat.id]['rollCalls'][rc_number].timezone)
        date=datetime.datetime.strptime(input_datetime, "%d-%m-%Y %H:%M")
        date=tz.localize(date)

        now_date_string=datetime.datetime.now(pytz.timezone(
        chat[message.chat.id]['rollCalls'][rc_number].timezone)).strftime("%d-%m-%Y %H:%M")
        now_date=datetime.datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
        now_date=tz.localize(now_date)

        ###

        if now_date > date:
            raise timeError("Please provide valid future datetime.")

        if chat[cid]['rollCalls'][rc_number].finalizeDate == None:
            chat[cid]['rollCalls'][rc_number].finalizeDate=date
            await bot.send_message(cid, f"Event notification time is set to {chat[cid]['rollCalls'][rc_number].finalizeDate.strftime('%d-%m-%Y %H:%M')} {chat[cid]['rollCalls'][rc_number].timezone}")
            asyncio.create_task(start(chat[cid]['rollCalls'], chat[cid]['rollCalls'][rc_number].timezone, cid))
        
        else:
            chat[cid]['rollCalls'][rc_number].finalizeDate=date
            await bot.send_message(cid, f"Event notification time is set to {date.strftime('%d-%m-%Y %H:%M')} {chat[cid]['rollCalls'][rc_number].timezone}")


    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_reminder")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srr")
async def reminder(message):
    try:

        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        cid=message.chat.id
        msg=message.text
        rc_number=0 #RC NUMBER DEFAULT
        pmts=msg.split(" ")[1:]

        if pmts[0]!='cancel' and len(pmts[0])==2:
            if pmts[0][0]=="0":
                pmts[0]=pmts[0][1]

        #IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
        if "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

        #IF NOT EXISTS A FINALIZE DATE, RAISE ERROR
        if chat[message.chat.id]['rollCalls'][rc_number].finalizeDate == None:
            raise parameterMissing(
                'First you need to set a finalize time for the current rollcall')

        #CANCEL REMINDER
        if pmts[0].lower() == 'cancel':
            chat[message.chat.id]['rollCalls'][rc_number].reminder=None
            await bot.send_message(message.chat.id, "Reminder Notification is canceled.")

        #IF THERE ARE NOT PARAMETERS RAISE ERROR
        if len(pmts) == 0 or not pmts[0].isdigit(): 
            raise parameterMissing(
                "The format is /set_rollcall_reminder hours")

        #IF HOUR IS NOT POSITIVE
        if int(pmts[0]) < 0:
            raise incorrectParameter("Hours must be positive")

        hour=pmts[0]

        if int(hour) < 1:
            raise incorrectParameter("Hours must be higher than 1")
        
        if chat[cid]['rollCalls'][rc_number].finalizeDate - datetime.timedelta(hours=int(hour)) < datetime.datetime.now(pytz.timezone(chat[message.chat.id]['rollCalls'][rc_number].timezone)):
            raise incorrectParameter(
                "Reminder notification time is less than current time, please set it correctly.")

        chat[cid]['rollCalls'][rc_number].reminder=int(hour) if hour != 0 else None
        await bot.send_message(cid, f'I will remind {hour}hour/s before the event! Thank you!')
        

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except incorrectParameter as e:
        await bot.send_message(message.chat.id, e)
    except ValueError as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, 'The correct format is /set_rollcall_reminder HH')

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/event_fee")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/ef")
async def event_fee(message):
    try:
        if roll_call_not_started(message, chat)==False:
            raise rollCallNotStarted("Roll call is not active")
        else:
            cid=message.chat.id
            pmts=" ".join(message.text.split(" ")[1:])

            chat[message.chat.id]['rollCalls'][0].event_fee = pmts

            await bot.send_message(cid, f"Now the Event Fee is {pmts}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

    except rollCallNotStarted as e:
        await bot.send_message(message.chat.id, e)
    except incorrectParameter as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/when")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/w")
async def when(message):
    try:
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        pmts=message.text.split(" ")[1:]
        rc_number=0

        if "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

        if chat[message.chat.id]['rollCalls'][rc_number].finalizeDate == None:
            raise incorrectParameter("There is no start time for the event")
        else:
            cid=message.chat.id

            await bot.send_message(cid, f"The event with title {chat[message.chat.id]['rollCalls'][0].title} will start at {chat[message.chat.id]['rollCalls'][0].finalizeDate.strftime('%d-%m-%Y %H:%M')}!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except incorrectParameter as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):
    try:
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
          
        if len(message.text.split(" ")) < 3:
            raise incorrectParameter("The correct format is /location place")
       
        cid=message.chat.id
        msg=message.text
        rc_number=0
        place=" ".join(msg.split(" ")[1:])

        if ":" in place.split(" ")[-1] and place[-1].split(" ")[1].isdigit():
            rc_number=int(place[-1][1])
            place=place[:len(place)-2]

        chat[cid]['rollCalls'][rc_number].location=place

        await bot.send_message(cid, f"The rollcall with title - {chat[cid]['rollCalls'][0].title} has a new location!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except incorrectParameter as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

# SET A LIMIT FOR IN LIST
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_limit")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sl")
async def wait_limit(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1 or int(message.text.split(" ")[1])<0:
            raise parameterMissing(
                "Input limit is missing or it's not a positive number")

        # DEFINING VARIABLES
        msg= message.text
        cid= message.chat.id
        comment=""
        pmts=msg.split(" ")[1:]
        rc_number=0
        limit=int(pmts[0])

        if len(pmts)>1 and ":" in pmts[-1]:
            rc_number=int(pmts[-1][1])-1

        # SETTING THE LIMIT TO INLIST
        chat[cid]["rollCalls"][rc_number].inListLimit=limit
        logging.info(f"Max limit of attendees is set to {limit}")
        await bot.send_message(cid, f"Max limit of attendees is set to {limit}")

        # MOVING USERS IF IN LIST HAS ALREADY REACH THE LIMIT
        if len(chat[cid]["rollCalls"][rc_number].inList) > limit:
            chat[cid]["rollCalls"][rc_number].waitList.extend(chat[cid]["rollCalls"][rc_number].inList[limit:])
            chat[cid]["rollCalls"][rc_number].inList=chat[cid]["rollCalls"][rc_number].inList[:limit]
        elif len(chat[cid]["rollCalls"][rc_number].inList) < limit:
            a=int(limit-len(chat[cid]["rollCalls"][rc_number].inList))
            chat[cid]["rollCalls"][rc_number].inList.extend(
                chat[cid]["rollCalls"][rc_number].waitList[: limit-len(chat[cid]["rollCalls"][rc_number].inList)])
            chat[cid]["rollCalls"][rc_number].waitList=chat[cid]["rollCalls"][rc_number].waitList[a:]

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
async def delete_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETER MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")
        # CHECK FOR ADMING RIGHTS
        if admin_rights(message, chat) == False:
            raise insufficientPermissions(
                "Error - user does not have sufficient permissions for this operation")

        # DEFINE VARIABLES
        msg=message.text
        cid=message.chat.id
        arr=msg.split(" ")
        rc_number=0

        if len(arr)>2 and ":" in arr[-1]:
            rc_number=int(arr[-1][1])-1
            arr=arr[:len(arr)-1]
        
        print(arr)


        # DELETE THE USER
        name=" ".join(arr[1:])
        if chat[cid]["rollCalls"][rc_number].delete_user(name, chat[cid]["allNames"][rc_number]) == True:
            await bot.send_message(cid, "The user was deleted!")
        else:
            await bot.send_message(cid, "That user wasn't found")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except insufficientPermissions as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except:
        print(traceback.format_exc())


@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0] =="/shh")
async def shh(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DESACTIVE THE MINIMUM OUTPUT FEATURE
            chat[message.chat.id]['shh']=True
            await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")


@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0] =="/louder")
async def louder(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DESACTIVE THE MINIMUM OUTPUT FEATURE
            chat[message.chat.id]['shh']=False
            await bot.send_message(message.chat.id, "Ok, i can hear you!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")


@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:
            # DEFINING VARIABLES
            msg= message.text
            cid= message.chat.id
            comment=""
            rc_number=0

            if len(msg.split(" "))>=2 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])

            user =User(message.from_user.first_name, message.from_user.username if message.from_user.username != "" else "None", message.from_user.id, chat[cid]["allNames"][rc_number])

            # DEFINING THE USER COMMENT
            arr= msg.split(" ")
            if len(arr) > 1:
                arr.pop(0)
                comment= ' '.join(arr)
                user.comment=comment

            # ADDING THE USER TO THE LIST
            result=chat[cid]["rollCalls"][rc_number].addIn(
                user, chat[cid]["allNames"][rc_number])
            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

            # PRINTING THE LIST
            if send_list(message, chat):
                await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList())

    except duplicateProxy as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)


@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DEFINING VARIABLES
            msg= message.text
            cid= message.chat.id
            comment=""
            rc_number=0

            if len(msg.split(" "))==2 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])
            user= User(message.from_user.first_name, message.from_user.username, message.from_user.id, chat[cid]["allNames"][rc_number])

            # DEFINING THE USER COMMENT
            arr= msg.split(" ")
            if len(arr) > 1:
                arr.pop(0)
                comment= ' '.join(arr)
                user.comment=comment

            # ADDING THE USER TO THE LIST
            result=chat[cid]["rollCalls"][rc_number].addOut(
                user, chat[cid]["allNames"][rc_number])
            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif isinstance(result, User):
                if type(result.user_id) == int:
                    await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            # PRINTING THE LIST
            if send_list(message, chat):
                await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList())

    except duplicateProxy as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)


@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DEFINING VARIABLES
            msg= message.text
            cid= message.chat.id
            comment=""
            rc_number=0

            if len(msg.split(" "))==2 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])
            user= User(message.from_user.first_name, message.from_user.username, message.from_user.id, chat[cid]["allNames"][rc_number])

            # DEFINING THE USER COMMENT
            arr= msg.split(" ")
            if len(arr) > 1:
                arr.pop(0)
                comment= ' '.join(arr)
                user.comment=comment

            # ADDING THE USER TO THE LIST
            result=chat[cid]["rollCalls"][rc_number].addMaybe(
                user, chat[cid]["allNames"][rc_number])
            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif isinstance(result, User):
                if type(result.user_id) == int:
                    await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            # PRINTING THE LIST
            if send_list(message, chat):
                await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList())

    except duplicateProxy as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)


@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        else:

            # DEFINING VARIABLES
            msg= message.text
            cid= message.chat.id
            comment=""
            rc_number=0

            if len(msg.split(" "))>=3 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])
            arr=msg.split(" ")

            # CREATING THE USER OBJECT
            if len(arr) > 1:
                user= User(arr[1], None, arr[1], chat[cid]["allNames"][rc_number])
                comment =" ".join(arr[2: ]) if len(arr) > 2 else ""
                user.comment=comment

                # ADDING THE USER TO THE LIST
                result=chat[cid]["rollCalls"][rc_number].addIn(
                    user, chat[cid]["allNames"][rc_number])
                if result == 'AB':
                    raise duplicateProxy(
                        "No duplicate proxy please :-), Thanks!")
                elif result == 'AC':
                    await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
                elif result == 'AA':
                    raise repeatlyName("That name already exists!")
                elif isinstance(result, User):
                    if type(result.user_id) == int:
                        await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                    else:
                        await bot.send_message(cid, f"{result.name} now you are in!")

                # PRINTING THE LIST
                if send_list(message, chat):
                    await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList())

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except duplicateProxy as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except repeatlyName as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)


@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        else:

            # DEFINING VARIABLES
            msg= message.text
            cid= message.chat.id
            comment=""
            rc_number=0

            if len(msg.split(" "))>=3 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])
            arr=msg.split(" ")

            # CREATING THE USER OBJECT
            if len(arr) > 1:
                user= User(arr[1], None, arr[1], chat[cid]["allNames"][rc_number])
                comment =" ".join(arr[2: ]) if len(arr) > 2 else ""
                user.comment=comment

                # ADDING THE USER TO THE LIST
                result=chat[cid]["rollCalls"][rc_number].addOut(
                    user, chat[cid]["allNames"][rc_number])
                if result == 'AB':
                    raise duplicateProxy(
                        "No duplicate proxy please :-), Thanks!")
                elif result == 'AC':
                    await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
                elif result == 'AA':
                    raise repeatlyName("That name already exists!")
                elif isinstance(result, User):
                    if type(result.user_id) == int:
                        await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                    else:
                        await bot.send_message(cid, f"{result.name} now you are in!")

                # PRINTING THE LIST
                if send_list(message, chat):
                    await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList())

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except duplicateProxy as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except repeatlyName as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        else:

            # DEFINING VARIABLES
            msg= message.text
            cid= message.chat.id
            comment=""
            rc_number=0

            if len(msg.split(" "))>=3 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])
            arr=msg.split(" ")

            # CREATING THE USER OBJECT
            if len(arr) > 1:
                user= User(arr[1], None, arr[1], chat[cid]["allNames"][rc_number])
                comment =" ".join(arr[2: ]) if len(arr) > 2 else ""
                user.comment=comment

                # ADDING THE USER TO THE LIST
                result=chat[cid]["rollCalls"][rc_number].addMaybe(
                    user, chat[cid]["allNames"][rc_number])

                if result == 'AB':
                    raise duplicateProxy(
                        "No duplicate proxy please :-), Thanks!")
                elif result == 'AC':
                    await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
                elif result == 'AA':
                    raise repeatlyName("That name already exists!")
                elif isinstance(result, User):
                    if type(result.user_id) == int:
                        await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                    else:
                        await bot.send_message(cid, f"{result.name} now you are in!")


                # PRINTING THE LIST
                if send_list(message, chat):
                    await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList())
                    return

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except duplicateProxy as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except repeatlyName as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_in")  # WHOS IN COMMAND
async def whos_in(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DEFINING VARIABLES
            cid= message.chat.id
            rc_number=0

            pmts=message.text.split(" ")[1:]

            if len(pmts)>=1 and ":" in pmts[-1]:
                rc_number=int(pmts[-1][1])-1

            # PRINTING LIST
            await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].inListText())

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message("Roll call is not active")


@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_out")  # WHOS IN COMMAND
async def whos_out(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DEFINING VARIABLES
            cid= message.chat.id
            rc_number=0

            pmts=message.text.split(" ")[1:]

            if len(pmts)>=1 and ":" in pmts[-1]:
                rc_number=int(pmts[-1][1])-1

            # PRINTING LIST
            await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].outListText())

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message("Roll call is not active")


@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_maybe")  # WHOS IN COMMAND
async def whos_maybe(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DEFINING VARIABLES
            cid= message.chat.id
            rc_number=0

            pmts=message.text.split(" ")[1:]

            if len(pmts)>=1 and ":" in pmts[-1]:
                rc_number=int(pmts[-1][1])-1

            # PRINTING LIST
            await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].maybeListText())

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message("Roll call is not active")


@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_waiting")  # WHOS IN COMMAND
async def whos_waiting(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DEFINING VARIABLES
            cid= message.chat.id
            rc_number=0

            pmts=message.text.split(" ")[1:]

            if len(pmts)>=3 and ":" in pmts[-1]:
                rc_number=int(pmts[-1][1])-1


            # PRINTING LIST
            await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].waitListText())

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message("Roll call is not active")

@ bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() =="/set_title")  # SET TITLE COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:
           # CHECK FOR RC ALREADY RUNNING
        if    roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        else:

            # DEFINING VARIABLES
            cid= message.chat.id
            msg= message.text
            rc_number=0

            if len(msg.split(" "))>=2 and ":" in msg.split(" ")[-1]:
                rc_number=int(msg.split(" ")[-1][1])-1
                msg=" ".join(msg.split(" ")[:len(msg.split(" "))-1])

            arr= msg.split(" ")
            user= message.from_user.first_name

            # DEFINING TITLE FOR RC
            if title=="":
                title="<Empty>"

            title = " ".join(arr[1:])
            chat[cid]["rollCalls"][rc_number].title=title
            await bot.send_message(cid, 'The roll call title is set to: ' + title)

            logging.info(user+"-"+"The title has change to "+title)

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")  #START ROLL CALL COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:
        # CHECK FOR A RUNNING RC
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK IF ADMIN_RIGHTS ARE ON
        if admin_rights(message, chat) == False:
            raise insufficientPermissions(
                "Error - user does not have sufficient permissions for this operation")

        else:

            # DEFINING VARIABLES
            cid=message.chat.id
            pmts=message.text.split(" ")[1:]

            if len(pmts)==3 and ":" in pmts[-1]:
                rc_number=int(pmts[-1][1])-1

            # SENDING LIST
            await bot.send_message(message.chat.id, "Roll ended!")

            await bot.send_message(cid, "Title - "+chat[cid]["rollCalls"][rc_number].title+"\n"+chat[cid]["rollCalls"][rc_number].inListText() + chat[cid]["rollCalls"][rc_number].outListText() + chat[cid]["rollCalls"][rc_number].maybeListText() + chat[cid]["rollCalls"][rc_number].waitListText())

            logging.info("The roll call "+chat[cid]["rollCalls"][rc_number].title+" has ended")

            # DELETING RC
            chat[cid]["rollCalls"].pop(rc_number)

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except insufficientPermissions as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except Exception as e:
        print(traceback.format_exc())




        
        

