import logging
import datetime
import re
import asyncio
import json
import traceback

from telebot.async_telebot import AsyncTeleBot
import pytz

from config import TELEGRAM_TOKEN, ADMINS, CONN_DB
from exceptions import *
from models import  User, Database
from functions import *
from check_reminders import start


bot = AsyncTeleBot(token=TELEGRAM_TOKEN)
logging.info("Bot already started")

# START COMMAND, SIMPLE TEXT
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message):
    try:
        cid = message.chat.id
        db = Database(CONN_DB, cid)
        resp = db.chat_collection.find_one({"_id":cid})
   
        if resp==None:

            chat={

                "_id":cid,

                "config":{
                    "adminRights":False,
                    "shh":False,
                    "timezone":"Asia/Calcutta",
                    "adminList":[]}

            }

            rc = {
                "_id":cid,

                'rollCalls':[]
            }

            db.chat_collection.insert_one(chat)
            db.rc_collection.insert_one(rc)
            
        # # CHECK FOR ADMIN RIGHTS
        if resp['config']['adminRights']==True:

            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        # START MSG
        await bot.send_message(message.chat.id, 'Hi! im RollCall!\n\nType /help to see all the commands')

    except Exception as e:
        print(traceback.format_exc())

# HELP COMMAND WITH ALL THE COMMANDS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/help")
async def help_commands(message):
    #HELP MSG
    await bot.send_message(message.chat.id, '''The commands are:\n-/start  - To start the bot\n-/help - To see the commands\n-/start_roll_call - To start a new roll call (optional title)\n-/in - To let everybody know you will be attending (optional comment)\n-/out - To let everybody know you wont be attending (optional comment)\n-/maybe - To let everybody know you dont know (optional comment)\n-/whos_in - List of those who will go\n-/whos_out - List of those who will not go\n-/whos_maybe - List of those who maybe will go\n-/set_title - To set a title for the current roll call\n-/set_in_for - Allows you to respond for another user\n-/set_out_for - Allows you to respond for another user\n-/set_maybe_for - Allows you to respond for another user\n-/shh - to apply minimum output for each command\n-/louder - to disable minimum output for each command\n-/set_limit - To set a limit to IN state\n-/end_roll_call - To end a roll call\n-/set_rollcall_time - To set a finalize time to the current rc. Accepts 2 parameters date (DD-MM-YYYY) and time (H:M). Write cancel to delete it\n-/set_rollcall_reminder - To set a reminder before the ends of the rc. Accepts 1 parameter, hours as integers. Write 'cancel' to delete the reminder\n-/timezone - To set your timezone, accepts 1 parameter (Continent/Country) or (Continent/State)\n-/when - To check the start time of a roll call\n-/location - To check the location of a roll call''')

# SET ADMIN RIGHTS TO TRUE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):
    try:

        #DEFINING VARIABLES
        cid=message.chat.id
        db = Database(CONN_DB, cid)
        
        #ONLY ADMIN USERS CAN USE THIS COMMAND
        user_permissions = await bot.get_chat_member(cid, message.from_user.id) 

        if user_permissions not in ['admin', 'creator']:
            await bot.send_message(cid, "You don't have permissions to use this command :(")
            return

        # DEFINING NEW STATE OF ADMIN RIGTS
        db.chat_collection.update_one({"_id":cid}, {"$set":{"config.adminRights":True}})

        await bot.send_message(cid, 'Admin permissions activated')
    
    except:
        print(traceback.format_exc())
    
# SET ADMIN RIGHTS TO FALSE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):

    #DEFINING VARIABLES
    cid = message.chat.id
    db = Database(CONN_DB, cid)

    #ONLY ADMIN USERS CAN USE THIS COMMAND
    user_permissions = await bot.get_chat_member(cid, message.from_user.id) 

    if user_permissions not in ['admin', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return

    # DEFINING NEW STATE OF ADMIN RIGTS
    db.chat_collection.update_one({"_id":cid}, {"$set":{"config.adminRights":False}})

    await bot.send_message(cid, 'Admin permissions disabled')

# SEND ANNOUNCEMENTS TO ALL GROUPS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):

    db = Database(CONN_DB)

    if len(message.text.split(" ")) < 1:
        await bot.send_message(message.chat.id, "Message is missing")

    msg = message.text.split(" ")[1:]

    ids = db.chat_collection.distinct("_id")

    for k in ids:
        try:
            await bot.send_message(int(k), " ".join(msg))
        except:
            pass

# ADJUST TIMEZONE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg = message.text
        cid = message.chat.id
        db = Database(CONN_DB, cid)

        if len(msg.split(" ")) < 2:
            raise parameterMissing(
                "The correct format is: /timezone continent/country or continent/state")
        if len(msg.split(" ")[1].split("/")) != 2:
            raise parameterMissing(
                "The correct format is: /timezone continent/country or continent/state")

        #Formating timezone
        timezone=auto_complete_timezone(" ".join(msg.split(" ")[1:]))

        if timezone != None:
            db.chat_collection.update_one({"_id":cid}, {"$set":{"config.timezone":timezone}})
            await bot.send_message(message.chat.id, f"Your timezone has been set to {timezone}")

        else:
            await bot.send_message(message.chat.id, f"Your timezone doesn't exists, if you can't found your timezone, check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode = 'HTML')

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
@ bot.message_handler(func = lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/r")
async def rollCalls(message):
    try:

        #DEFINING VARIABLES
        cid = message.chat.id
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        #ERROR IF NOT EXISTS ANY ROLLCALLS
        if len(chat_roll_calls)==0:
            await bot.send_message(cid, "There are not rollcalls yet")

        res = db.allRollCallsInfo(cid)

        for r in res:
            await bot.send_message(cid, r)
    
    except Exception as e:
        print(traceback.format_exc())
        print(e)

# START A ROLL CALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):


    try:
        # DEFINING VARIABLES
        cid = message.chat.id
        msg = message.text
        title='<Empty>'
        db = Database(CONN_DB, cid)
        active_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config
        ids_to_use = [1,2,3]

        #MAXIMUM ROLLCALLS ERROR
        if len(active_roll_calls)>=3:
            raise amountOfRollCallsReached("Allowed Maximum number of active roll calls per group is 3.")

        # CHECK FOR ADMIN RIGHTS
        if chat_config['adminRights']==True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        #GET AVAILABLE ROLLCALLS ID'S
        ids_used=[i['rcId'] for i in active_roll_calls]
        ids_to_use=list(set(ids_to_use)-set(ids_used))

        # SET THE RC TITLE
        arr=msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            title=' '.join(arr)
       
       
        #CREATING ROLLCALL OBJECT
        roll_call = {

            "rcId": ids_to_use[0],
            "title": title,
            "inList": [],
            "outList": [],
            "maybeList": [],
            "waitList": [],
            "allNames": [],
            "inListLimit": None,
            "reminder": None,
            "finalizeDate": None,
            "location": None,
            "event_fee": None,
            "createdDate": datetime.datetime.utcnow()

        }

        #UPDATING RC TO DB
        db.rc_collection.update_one({"_id":cid}, {"$push":{"rollCalls":roll_call}})

        await bot.send_message(message.chat.id, f"Roll call with title: {title} started!")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

#SET A ROLLCALL START TIME
@ bot.message_handler(func = lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:

        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")[1:]
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        if len(message.text.split(" ")) <= 2 and pmts[0]!='cancel':
            raise parameterMissing("invalid datetime format, refer help section for details")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)

        #GET ROLLCALL
        rc = db.rollCallInfo(rc_number)

        #CANCEL THE CURRENT REMINDER TIME
        if (pmts[0]).lower() == 'cancel':
            db.rc_collection.update_one({'_id':1873806303, 'rollCalls.rcId':1}, {"$set":{"rollCalls.$.finalizeDate":None, "rollCalls.$.reminder":None}})
            await bot.send_message(message.chat.id, f"Title: {rc['title']}\nID: {rc['rcId']}\n\nReminder time has been canceled.")
            return

        #PARSING INPUT DATETIME
        input_datetime=" ".join(pmts).strip()

        tz=pytz.timezone(chat_config['timezone'])
        date=datetime.datetime.strptime(input_datetime, "%d-%m-%Y %H:%M")

        now_date_string=datetime.datetime.now(tz).strftime("%d-%m-%Y %H:%M")
        now_date=datetime.datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
        now_date=tz.localize(now_date)

        #ERROR FOR INVALID DATETIME
        if now_date.replace(tzinfo=None) > date:
            raise timeError("Please provide valid future datetime.")
            
        db.rc_collection.update_one({'_id':1873806303, 'rollCalls.rcId':1}, {"$set":{"rollCalls.$.finalizeDate":date, "rollCalls.$.reminder":None}})

        await bot.send_message(cid, f"Title: {rc['title']}\nID: {rc['rcId']}\n\nEvent notification time is set to {date}. Reminder has been reset!")
        
        if all(i['finalizeDate'] == None for i in chat_roll_calls):
            asyncio.create_task(start(cid))
        
    except Exception as e:
        await bot.send_message(message.chat.id, e)

#SET A ROLLCALL REMINDER
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_reminder")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srr")
async def reminder(message):
    
    #DEFINING VARIABLES
    cid=message.chat.id
    msg=message.text
    rc_number=1 #RC NUMBER DEFAULT
    pmts=msg.split(" ")[1:]
    db = Database(CONN_DB, cid)
    chat_roll_calls = db.chat_roll_calls
    chat_config = db.chat_config

    try:
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        #IF NUMBER HAS 00:00 FORMAT REPLACE IT TO 0:0
        if pmts[0]!='cancel' and len(pmts[0])==2:
            if pmts[0][0]=="0":
                pmts[0]=pmts[0][1]

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        #GET ROLLCALL
        rc = db.rollCallInfo(rc_number)

        #IF NOT EXISTS A FINALIZE DATE, RAISE ERROR
        if rc['finalizeDate'] == None:
            raise parameterMissing('First you need to set a finalize time for the current rollcall')

        #CANCEL REMINDER
        if pmts[0].lower() == 'cancel':
            db.rc_collection.update_one({'_id':1873806303, 'rollCalls.rcId':1}, {"$set":{"rollCalls.$.reminder":None}})
            await bot.send_message(message.chat.id, "Reminder Notification is canceled.")
            return
            
        #IF THERE ARE NOT PARAMETERS RAISE ERROR
        if len(pmts) == 0 or not pmts[0].isdigit(): 
            raise parameterMissing("The format is /set_rollcall_reminder hours")

        #IF HOUR IS NOT POSITIVE
        if int(pmts[0]) < 0 or int(pmts[0]) < 1:
            raise incorrectParameter("Hours must be higher than 1")

        hour=pmts[0]
        
        #Reminder notification time is less than current time
        if rc['finalizeDate'] - datetime.timedelta(hours=int(hour)) < datetime.datetime.now(pytz.timezone(chat_config['timezone'])).replace(tzinfo=None ):
            raise incorrectParameter("Reminder notification time is less than current time, please set it correctly.")

        db.rc_collection.update_one({'_id':1873806303, 'rollCalls.rcId':1}, {"$set":{"rollCalls.$.reminder":hour}})
        await bot.send_message(cid, f'I will remind {hour} hour/s before the event! Thank you!')
        
    except ValueError as e:
        print(traceback.format_exc())
        await bot.send_message(cid, 'The correct format is /set_rollcall_reminder HH')
    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

#SET AN EVENT_FEE
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/event_fee")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/ef")
async def event_fee(message):
    
    #DEFINING VARIABLES
    cid=message.chat.id
    pmts=message.text.split(" ")[1:]
    rc_number=1
    db = Database(CONN_DB, cid)
    chat_roll_calls = db.chat_roll_calls

    try:
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        event_price=" ".join(pmts)
        event_price_number=re.findall('[0-9]+', event_price)
        
        if len(event_price_number)==0 or int(event_price_number[0])<=0:
            raise incorrectParameter("The correct format is '/event_fee Integer' Where 'Integer' it's up to 0 number")

        db.rc_collection.update_one({'_id':cid, 'rollCalls.rcId':rc_number}, {"$set":{"rollCalls.$.event_fee":int(event_price)}})
        await bot.send_message(cid, f"Event Fee set to {event_price}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

    except Exception as e:
        await bot.send_message(cid, e)

#CHECK HOW MUCH IS INDIVIDUAL FEE
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/individual_fee")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/if")
async def individual_fee(message):
    
    #DEFINING VARIABLES
    cid=message.chat.id
    pmts=message.text.split(" ")[1:]
    rc_number=1
    db = Database(CONN_DB, cid)
    chat_roll_calls = db.chat_roll_calls
    
    try:
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        rc = db.rollCallInfo(rc_number)

        in_list=len(rc['inList'])
        event_price=int(re.sub(r'[^0-9]', "", str(rc['event_fee'])))

        if in_list>0:
            individual_fee=round(event_price/in_list, 2)
        else:
            individual_fee=0

        await bot.send_message(cid, f'Individual fee is {individual_fee}')
          
    except Exception as e:
        await bot.send_message(cid, e)

#CHECK WHEN A ROLLCALL WILL BE START
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/when")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/w")
async def when(message):
    
    cid=message.chat.id
    pmts=message.text.split(" ")
    rc_number=1
    db = Database(CONN_DB, cid)
    chat_roll_calls = db.chat_roll_calls
    chat_config = db.chat_config

    try:
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)

        rc = db.rollCallInfo(rc_number)

        if rc['finalizeDate'] == None:
            raise incorrectParameter("There is no start time for the event")

        await bot.send_message(cid, f"The event with title {rc['title']} will start at {rc['finalizeDate'].strftime('%d-%m-%Y %H:%M')} {chat_config['timezone']}!")

    except Exception as e:
        await bot.send_message(cid, e)

#SET A LOCATION FOR A ROLLCALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):
    
    try:
        cid=message.chat.id
        msg=message.text
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
          
        if len(message.text.split(" ")) < 2:
            raise incorrectParameter("The correct format is /location <place>")
       
        pmts=msg.split(" ")[1:]
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)

        place=" ".join(pmts)
        rc = db.rollCallInfo(rc_number)

        db.rc_collection.update_one({'_id':cid, 'rollCalls.rcId':rc_number}, {"$set":{"rollCalls.$.location":place}})
        await bot.send_message(cid, f"The rollcall with title - {rc['title']} has a new location!")

    except Exception as e:
        print(e)
        await bot.send_message(cid, e)

# SET A LIMIT FOR IN LIST
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_limit")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sl")
async def wait_limit(message):
    try:
        cid=message.chat.id
        msg=message.text
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1 or int(message.text.split(" ")[1])<0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        # DEFINING VARIABLES
        pmts=msg.split(" ")[1:]
        limit=int(pmts[0])

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
    
        #GET ROLLCALL
        rc = db.rollCallInfo(rc_number)
        
        # SETTING THE LIMIT TO INLIST
        rc['inListLimit']=limit
        logging.info(f"Max limit of attendees is set to {limit}")
        await bot.send_message(cid, f'Max limit of attendees is set to {limit}')

        # MOVING USERS IF IN LIST HAS ALREADY REACH THE LIMIT
        if len(rc['inList']) > limit:
            rc['waitList'].extend(rc['inList'][limit:])
            rc['inList']=rc['inList'][:limit]
        elif len(rc['inList']) < limit:
            a=int(limit-len(rc['inList']))
            rc['inList'].extend(
                rc['waitList'][: limit-len(rc['inList'])])
            rc['waitList']=rc['waitList'][a:]

        db.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rc_number}, {"$set":{"rollCalls.$":rc}})

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#DELETE AN USER OF A ROLLCALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
async def delete_user(message):
    try:

        # DEFINE VARIABLES
        msg=message.text
        cid=message.chat.id
        arr=msg.split(" ")
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETER MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        # CHECK FOR ADMIN RIGHTS
        if chat_config['adminRights']==True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # DELETE THE USER
        name=" ".join(arr[1:])

        if db.delete_user(name, cid, rc_number):
            await bot.send_message(cid, "The user was deleted!")

        else:
            await bot.send_message(cid, "That user wasn't found")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#RESUME NOTIFICATIONS
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0] =="/shh")
async def shh(message):
    try:
        cid = message.chat.id
        db = Database(CONN_DB, cid)

        # DESACTIVE THE MINIMUM OUTPUT FEATURE
        db.chat_collection.update_one({"_id":cid}, {"$set":{"config.shh":True}})
        await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

#NON RESUME NOTIFICATIONS
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0] =="/louder")
async def louder(message):
    try:
        cid = message.chat.id
        db = Database(CONN_DB, cid)


        # DESACTIVE THE MINIMUM OUTPUT FEATURE
        db.chat_collection.update_one({"_id":cid}, {"$set":{"config.shh":False}})
        await bot.send_message(message.chat.id, "Ok, i can hear you!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

#CHANGE STATE TO IN
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        # DEFINING VARIABLES
        msg= message.text
        cid= message.chat.id
        pmts= msg.split(" ")
        comment=""
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config
        
        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)

        user =User(message.from_user.first_name, message.from_user.username if message.from_user.username != "" else "None", message.from_user.id)
        
        # DEFINING THE USER COMMENT
        arr= msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment= ' '.join(arr)
            user.comment=comment

        # ADDING THE USER TO THE LIST
        result=db.addIn(user, cid, rc_number)

        if result == 'Error 1':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        # PRINTING THE LIST
        if not chat_config['shh']:
            await bot.send_message(cid, db.allList(cid, rc_number))

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

#CHANGE STATE TO OUT
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:

        # DEFINING VARIABLES
        msg= message.text
        pmts = msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        user= User(message.from_user.first_name, message.from_user.username, message.from_user.id)

        # DEFINING THE USER COMMENT
        arr= msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment= ' '.join(arr)
            user.comment=comment

        # ADDING THE USER TO THE LIST
        result = db.addOut(user, cid, rc_number)

        if result == 'Error 1':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if type(result.user_id) == int:
                await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        # PRINTING THE LIST
        if not chat_config['shh']:
            await bot.send_message(cid, db.allList(cid, rc_number))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#CHANGE STATE TO MAYBE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:

        # DEFINING VARIABLES
        msg= message.text
        pmts = msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
    
        user= User(message.from_user.first_name, message.from_user.username, message.from_user.id)

        # DEFINING THE USER COMMENT
        arr= msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment= ' '.join(arr)
            user.comment=comment

        # ADDING THE USER TO THE LIST
        result=db.addMaybe(user, cid, rc_number)

        if result == 'Error 1':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if type(result.user_id) == int:
                await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        # PRINTING THE LIST
        if not chat_config['shh']:
            await bot.send_message(cid, db.allList(cid, rc_number))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#CHANGE STATE TO IN BUT FOR SOMEONE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 1
        arr = msg.split(" ")
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        elif len(arr) <= 1:
            raise parameterMissing("Input username is missing")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
    
        # CREATING THE USER OBJECT
        user = User(arr[1], None, arr[1])
        comment = " ".join(arr[2: ]) if len(arr) > 2 else ""
        user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addIn(user, cid, rc_number)

        #DUPLICATE USER IN SAME STATE ERROR
        if result == 'Error 1':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")

        #INLIST REACHED MAX USERS LIMIT ERROR
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        #NAME ALREADY EXIST ERROR
        elif result == 'Error 2':
            raise repeatlyName("That name already exists!")

        #NOTIFY USER THAT WAS MOVED FROM WAITLIST TO INLIST
        elif type(result) == dict:

            if type(result['user_id']) == int:
                
                name, username, user_id= result['name'], result['username'], result['user_id'] 
                await bot.send_message(cid, f"{'@'+ username if username == None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")
            
            else:
                await bot.send_message(cid, f"{result['name']} now you are in!")

        # PRINTING THE LIST
        if not chat_config['shh']:
            await bot.send_message(cid, db.allList(cid, rc_number))

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

#CHANGE STATE TO OUT BUT FOR SOMEONE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 1
        arr = msg.split(" ")
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config
        
        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # CREATING THE USER OBJECT
        user = User(arr[1], None, arr[1])
        comment = " ".join(arr[2: ]) if len(arr) > 2 else ""
        user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addOut(user, cid, rc_number)

        #DUPLICATE USER IN SAME STATE ERROR
        if result == 'Error 1':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")

        #INLIST REACHED MAX USERS LIMIT ERROR
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        #NAME ALREADY EXIST ERROR
        elif result == 'Error 2':
            raise repeatlyName("That name already exists!")

        #NOTIFY USER THAT WAS MOVED FROM WAITLIST TO INLIST
        elif type(result) == dict:

            if type(result['user_id']) == int:

                name, username, user_id= result['name'], result['username'], result['user_id'] 
                await bot.send_message(cid, f"{'@'+username if username !=None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")
            
            else:
                await bot.send_message(cid, f"{result['name']} now you are in!")

        # PRINTING THE LIST
        if not chat_config['shh']:
            await bot.send_message(cid, db.allList(cid, rc_number))

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

#CHANGE STATE TO MAYBE BUT FOR SOMEONE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 1
        arr = msg.split(" ")
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # CREATING THE USER OBJECT
        user = User(arr[1], None, arr[1])
        comment = " ".join(arr[2: ]) if len(arr) > 2 else ""
        user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addMaybe(user, cid, rc_number)

        #DUPLICATE USER IN SAME STATE ERROR
        if result == 'Error 1':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")

        #INLIST REACHED MAX USERS LIMIT ERROR
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        #NAME ALREADY EXIST ERROR
        elif result == 'Error 2':
            raise repeatlyName("That name already exists!")

        #NOTIFY USER THAT WAS MOVED FROM WAITLIST TO INLIST
        elif type(result) == dict:

            if type(result['user_id']) == int:

                name, username, user_id = result['name'], result['username'], result['user_id'] 
                await bot.send_message(cid, f"{'@'+username if username !=None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")

            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        # PRINTING THE LIST
        if not chat_config['shh']:
            await bot.send_message(cid, db.allList(cid, rc_number))
            return

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#SEE WHOS IN ON A ROLLCALL
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_in")
async def whos_in(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rc_number = 1
        pmts=message.text.split(" ")[1:]
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
    
        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # PRINTING LIST
        rc = db.rollCallInfo(rc_number)
        await bot.send_message(cid, f"{rc['title']} {db.inListText(cid, rc_number)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#SEE WHOS OUT ON A ROLLCALL
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_out")
async def whos_out(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rc_number = 1
        pmts=message.text.split(" ")[1:]
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # PRINTING LIST
        rc = db.rollCallInfo(rc_number)
        await bot.send_message(cid, f"{rc['title']} {db.outListText(cid, rc_number)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#SEE WHOS MAYBE ON A ROLLCALL
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_maybe")
async def whos_maybe(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rc_number = 1
        pmts=message.text.split(" ")[1:]
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
       
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # PRINTING LIST
        rc = db.rollCallInfo(rc_number)
        await bot.send_message(cid, f"{rc['title']} {db.maybeListText(cid, rc_number)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#SEE WHOS WAITING ON A ROLLCALL
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_waiting")
async def whos_waiting(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rc_number = 1
        pmts=message.text.split(" ")[1:]
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")
        
        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # PRINTING LIST
        rc = db.rollCallInfo(rc_number)
        await bot.send_message(cid, f"{rc['title']} {db.waitListText(cid, rc_number)}")
        
    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() =="/set_title")  # SET TITLE COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:
        
        # DEFINING VARIABLES
        cid= message.chat.id
        msg= message.text
        pmts=msg.split(" ")[1:]
        title = " ".join(pmts)
        user= message.from_user.first_name
        rc_number=1
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls

        # CHECK FOR RC ALREADY RUNNING
        if len(chat_roll_calls)==0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # DEFINING TITLE FOR RC
        if title=="":
            title="<Empty>"

        db.rc_collection.update_one({'_id':cid, 'rollCalls.rcId':rc_number}, {"$set":{"rollCalls.$.title":title}})
        await bot.send_message(cid, 'The roll call title is set to: ' + title)

        logging.info(user+"-"+"The title has change to "+title)

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")  #END ROLL CALL COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:

        # DEFINING VARIABLES
        cid= message.chat.id
        msg= message.text
        rc_number=1
        pmts = msg.split(" ")[1:]
        db = Database(CONN_DB, cid)
        chat_roll_calls = db.chat_roll_calls
        chat_config = db.chat_config

        # CHECK FOR A RUNNING RC
        if len(chat_roll_calls)<=0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR ADMIN RIGHTS
        if chat_config['adminRights']==True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        #ASSIGN ROLLCALL ID
        rc_number = get_rc_number(chat_roll_calls, pmts)
        
        # SENDING LIST
        await bot.send_message(message.chat.id, "Roll ended!")
        await bot.send_message(cid, db.finishList(cid, rc_number))
        
        # DELETING RC
        db.finishRollCall(cid, rc_number)
        logging.info("The roll call " + db.rollCallInfo(rc_number)['title'] + " has ended")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)





        
        

