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
from models import User, Database
from utils.functions import *
from middleware import MyMiddleware


bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

#CONFIG
db = Database(CONN_DB)
bot.setup_middleware(MyMiddleware())

logging.info("Bot already started")


# START COMMAND, SIMPLE TEXT
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message): 
    cid = message.chat.id

    #CHECK FOR ADMIN RIGHTS
    if not admin_rights(message) and not message.chat.type == 'private':
        await bot.send_message(cid, "Error - user does not have sufficient permissions for this operation")
        return

    # START MSG
    await bot.send_message(cid, 'Hi! im RollCall!\n\nType /help to see all the commands')

# HELP COMMAND WITH ALL THE COMMANDS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/help")
async def help_commands(message):
    # HELP MSG
    await bot.send_message(message.chat.id, '''The commands are:\n-/start  - To start the bot\n-/help - To see the commands\n-/start_roll_call - To start a new roll call (optional title)\n-/in - To let everybody know you will be attending (optional comment)\n-/out - To let everybody know you wont be attending (optional comment)\n-/maybe - To let everybody know you dont know (optional comment)\n-/whos_in - List of those who will go\n-/whos_out - List of those who will not go\n-/whos_maybe - List of those who maybe will go\n-/set_title - To set a title for the current roll call\n-/set_in_for - Allows you to respond for another user\n-/set_out_for - Allows you to respond for another user\n-/set_maybe_for - Allows you to respond for another user\n-/shh - to apply minimum output for each command\n-/louder - to disable minimum output for each command\n-/set_limit - To set a limit to IN state\n-/end_roll_call - To end a roll call\n-/set_rollcall_time - To set a finalize time to the current rc. Accepts 2 parameters date (DD-MM-YYYY) and time (H:M). Write cancel to delete it\n-/set_rollcall_reminder - To set a reminder before the ends of the rc. Accepts 1 parameter, hours as integers. Write 'cancel' to delete the reminder\n-/timezone - To set your timezone, accepts 1 parameter (Continent/Country) or (Continent/State)\n-/when - To check the start time of a roll call\n-/location - To check the location of a roll call''')

# SET ADMIN RIGHTS TO TRUE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):
    try:
        # DEFINING VARIABLES
        cid = message.chat.id

        # ONLY ADMIN USERS CAN USE THIS COMMAND
        user_permissions = await bot.get_chat_member(cid, message.from_user.id)

        if user_permissions not in ['admin', 'creator'] and not message.chat.type == 'private':
            await bot.send_message(cid, "You don't have permissions to use this command :(")
            return

        # DEFINING NEW STATE OF ADMIN RIGTS
        db.chat_collection.update_one({"_id": cid}, {"$set": {"config.adminRights": True}})
        await bot.send_message(cid, 'Admin permissions activated')

    except:
        print(traceback.format_exc())

# SET ADMIN RIGHTS TO FALSE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):

    # DEFINING VARIABLES
    cid = message.chat.id

    # ONLY ADMIN USERS CAN USE THIS COMMAND
    user_permissions = await bot.get_chat_member(cid, message.from_user.id)

    if user_permissions not in ['admin', 'creator'] and not message.chat.type == 'private':
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return

    # DEFINING NEW STATE OF ADMIN RIGTS
    db.chat_collection.update_one({"_id": cid}, {"$set": {"config.adminRights": False}})
    await bot.send_message(cid, 'Admin permissions disabled')

# SEND ANNOUNCEMENTS TO ALL GROUPS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):

    if len(message.text.split(" ")) < 1:
        await bot.send_message(message.chat.id, "Message is missing")

    msg = message.text.split(" ")[1:]
    chats = db.chat_collection.distinct("_id")

    for _id in chats:
        try:
            await bot.send_message(int(_id), " ".join(msg))
        except:
            pass

# ADJUST TIMEZONE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg = message.text
        cid = message.chat.id

        #RAISE ERROR IF TIMEZONE WASNT WROTE
        if len(msg.split(" ")) < 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")

        #GETTING TIMEZONE
        timezone = auto_complete_timezone(msg.replace("/timezone", ''))

        if not timezone:
            await bot.send_message(message.chat.id, f"Your timezone doesn't exists, if you can't found your timezone, check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode='HTML')
            return
            
        db.chat_collection.update_one({"_id": cid}, {"$set": {"config.timezone": timezone}})
        await bot.send_message(message.chat.id, f"Your timezone has been set to {timezone}")
            
    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

# Version command
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/version")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/v")
async def version_command(message):

    file = open('./version.json')
    data = json.load(file)
    for i in range(0, len(data)):

        version = data[-1 - i]

        if version["DeployedOnProd"] == 'Y':
            txt = ''
            txt += f'Version: {version["Version"]}\nDescription: {version["Description"]}\nDeployed: {version["DeployedOnProd"]}\nDeployed datetime: {version["DeployedDatetime"]}'
            await bot.send_message(message.chat.id, txt)
            break

# GET ALL ROLLCALLS OF THE CURRENT CHAT
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/rollcalls")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/r")
async def rollCalls(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        chat_roll_calls = db.rc_collection.find_one({"_id":message.chat.id})['rollCalls']

        # ERROR IF NOT EXISTS ANY ROLLCALLS
        if len(chat_roll_calls) == 0:
            await bot.send_message(cid, "There are not rollcalls yet")

        rollCalls = db.allRollCallsInfo(cid)
        for rollCallInfo in rollCalls:
            await bot.send_message(cid, rollCallInfo)

    except Exception as e:
        print(traceback.format_exc())

# START A ROLL CALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):

    try:
        # DEFINING VARIABLES
        cid = message.chat.id
        msg = message.text
        title = '<Empty>'
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        idsToUse = [1, 2, 3]

        # MAXIMUM ROLLCALLS ERROR
        if len(chatRollCalls) >= 3:
            raise amountOfRollCallsReached("Allowed Maximum number of active roll calls per group is 3.")

        # CHECK FOR ADMIN RIGHTS
        if chatConfig['adminRights'] == True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        # GET AVAILABLE ROLLCALLS ID'S
        idsUsed = [i['rcId'] for i in chatRollCalls]
        idsToUse = list(set(idsToUse) - set(idsUsed))

        # SET THE RC TITLE
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            title = ' '.join(arr)

        # CREATING ROLLCALL OBJECT
        rollCall = {
            "rcId": idsToUse[0],
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

        # UPDATING RC TO DB
        db.rc_collection.update_one({"_id": cid}, {"$push": {"rollCalls": rollCall}})
        await bot.send_message(message.chat.id, f"Roll call with title: {title} started!")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

# SET A ROLLCALL START TIME
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:

        cid = message.chat.id
        msg = message.text
        rcNumber = int(message.data['rcNumber'])
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)

        rc = db.getRollCallById(cid, rcNumber)        
        inputDatetime = re.findall('\d{2}-\d{2}-\d{4} \d{2}:\d{2}', msg)

        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        #RAISE ERROR IF INVALID DATETIME
        if not inputDatetime:
            raise parameterMissing("Invalid datetime format, refer help section for details")
        
        inputDatetime = inputDatetime[0]

        #RAISE ERROR IF NOT EXISTS RCID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # CANCEL THE CURRENT REMINDER TIME
        if 'cancel' in msg:
            db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {"$set": {"rollCalls.$.finalizeDate": None, "rollCalls.$.reminder": None}})
            await bot.send_message(message.chat.id, f"Title: {rc['title']}\nID: {rc['rcId']}\n\nReminder time has been canceled.")
            return

        #GETTING NOW DATETIME AND PARSING INPUT DATETIME
        tz = pytz.timezone(chatConfig['timezone'])
        date = datetime.datetime.strptime(inputDatetime, "%d-%m-%Y %H:%M")  
        nowDate = datetime.datetime.strptime(datetime.datetime.now(tz).strftime("%d-%m-%Y %H:%M"), "%d-%m-%Y %H:%M")

        # ERROR FOR INVALID DATETIME
        if nowDate > date:
            raise timeError("Please provide valid future datetime.")

        db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {"$set": {"rollCalls.$.finalizeDate": date, "rollCalls.$.reminder": None}})
        await bot.send_message(cid, f"Title: {rc['title']}\nID: {rc['rcId']}\n\nEvent notification time is set to {date}. Reminder has been reset!")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# SET A ROLLCALL REMINDER
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_reminder")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srr")
async def reminder(message):

    # DEFINING VARIABLES
    cid = message.chat.id
    msg = message.text
    time = re.findall("\d{2}:\d{2}", msg)
    rcNumber = int(message.data['rcNumber'])

    chatRollCalls = db.getAllRollCalls(cid)
    chatConfig = db.getChatConfigById(cid)
    rc = db.getRollCallById(cid, rcNumber)

    try:
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # IF NOT EXISTS A FINALIZE DATE, RAISE ERROR
        if rc['finalizeDate'] == None:
            raise parameterMissing('First you need to set a finalize time for the current rollcall')

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        if 'cancel' in msg:
            db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {"$set": {"rollCalls.$.reminder": None}})
            await bot.send_message(message.chat.id, "Reminder Notification is canceled.")
            return
            
        if not time:
            raise parameterMissing("The format is /set_rollcall_reminder HH:MM")

        timeObj = datetime.datetime.strptime(time[0], "%H:%M").time()
     
        # Reminder notification time is less than current time
        if rc['finalizeDate'] - datetime.timedelta(hours=timeObj.hour, minutes=timeObj.minute) < datetime.datetime.now(pytz.timezone(chatConfig['timezone'])).replace(tzinfo=None):
            raise incorrectParameter("Reminder notification time is less than current time, please set it correctly.")

        db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {"$set": {"rollCalls.$.reminder": time[0]}})
        await bot.send_message(cid, f'I will remind {time[0]} hour/s before the event! Thank you!')

    except ValueError as e:
        print(traceback.format_exc())
        await bot.send_message(cid, 'The correct format is /set_rollcall_reminder HH')
    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

# SET AN EVENT_FEE
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/event_fee")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/ef")
async def event_fee(message):

    # DEFINING VARIABLES
    cid = message.chat.id
    msg = message.text
    rcNumber = int(message.data['rcNumber'])
    
    chatRollCalls = db.getAllRollCalls(cid)
    rc = db.getRollCallById(cid, rcNumber)

    try:
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        eventPrice = re.findall('[0-9]+', msg)

        if not eventPrice:
            raise incorrectParameter("The correct format is '/event_fee Integer' Where 'Integer' it's up to 0 number")

        db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {"$set": {"rollCalls.$.event_fee": int(eventPrice[0])}})
        await bot.send_message(cid, f"Event Fee set to {eventPrice[0]}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

    except Exception as e:
        await bot.send_message(cid, e)

# CHECK HOW MUCH IS INDIVIDUAL FEE
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/individual_fee")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/if")
async def individual_fee(message):

    # DEFINING VARIABLES
    cid = message.chat.id
    rcNumber = int(message.data['rcNumber'])

    chatRollCalls = db.getAllRollCalls(cid)
    rc = db.getRollCallById(cid, rcNumber)

    try:
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        in_list = len(rc['inList'])
        event_price = int(re.sub(r'[^0-9]', "", str(rc['event_fee'])))

        if in_list > 0:
            individual_fee = round(event_price / in_list, 2)
        else:
            individual_fee = 0

        await bot.send_message(cid, f'Individual fee is {individual_fee}')

    except Exception as e:
        await bot.send_message(cid, e)

# CHECK WHEN A ROLLCALL WILL BE START
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/when")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/w")
async def when(message):

    cid = message.chat.id
    rcNumber = int(message.data['rcNumber'])

    chatRollCalls = db.getAllRollCalls(cid)
    chatConfig = db.getChatConfigById(cid)
    rc = db.getRollCallById(cid)

    try:
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        if rc['finalizeDate'] == None:
            raise incorrectParameter("There is no start time for the event")

        await bot.send_message(cid, f"The event with title {rc['title']} will start at {rc['finalizeDate'].strftime('%d-%m-%Y %H:%M')} {chatConfig['timezone']}!")

    except Exception as e:
        await bot.send_message(cid, e)

# SET A LOCATION FOR A ROLLCALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):

    try:
        cid = message.chat.id
        msg = message.text
        rcNumber = int(message.data['rcNumber'])

        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        if len(message.text.split(" ")) < 2:
            raise incorrectParameter("The correct format is /location <place>")

        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {"$set": {"rollCalls.$.location": msg.split(" ")[1:][0]}})
        await bot.send_message(cid, f"The rollcall with title - {rc['title']} has a new location!")

    except Exception as e:
        await bot.send_message(cid, e)

# SET A LIMIT FOR IN LIST
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_limit")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sl")
async def wait_limit(message):
    try:
        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")
        rcNumber = int(message.data['rcNumber'])

        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # CHECK FOR PARAMETERS MISSING
        if len(pmts) <= 1 or int(pmts[1]) < 0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        limit = int(pmts[1])

        # SETTING THE LIMIT TO INLIST
        rc['inListLimit'] = limit

        # MOVING USERS IF IN LIST HAS ALREADY REACH THE LIMIT
        if len(rc['inList']) > limit:
            rc['waitList'].extend(rc['inList'][limit:])
            rc['inList'] = rc['inList'][:limit]
        elif len(rc['inList']) < limit:
            a = int(limit - len(rc['inList']))
            rc['inList'].extend(
                rc['waitList'][: limit - len(rc['inList'])])
            rc['waitList'] = rc['waitList'][a:]

        db.rc_collection.update_one({"_id": cid, "rollCalls.rcId": rcNumber}, {"$set": {"rollCalls.$": rc}})
        await bot.send_message(cid, f'Max limit of attendees is set to {limit}')

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# DELETE AN USER OF A ROLLCALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
async def delete_user(message):
    try:

        # DEFINE VARIABLES
        msg = message.text
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR ADMIN RIGHTS
        if chatConfig['adminRights'] == True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # CHECK FOR PARAMETER MISSING
        elif len(msg.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        # DELETE THE USER
        name = " ".join(msg.split(" ")[1:])

        if db.delete_user(name, cid, rcNumber):
            await bot.send_message(cid, "The user was deleted!")

        else:
            await bot.send_message(cid, "That user wasn't found")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# RESUME NOTIFICATIONS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/shh")
async def shh(message):
    try:
        cid = message.chat.id
        
        # DESACTIVE THE MINIMUM OUTPUT FEATURE
        db.chat_collection.update_one({"_id": cid}, {"$set": {"config.shh": True}})
        await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

    except rollCallNotStarted as e:
        await bot.send_message(message.chat.id, "Roll call is not active")

# NON RESUME NOTIFICATIONS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/louder")
async def louder(message):
    try:
        cid = message.chat.id
        
        # DESACTIVE THE MINIMUM OUTPUT FEATURE
        db.chat_collection.update_one(
            {"_id": cid}, {"$set": {"config.shh": False}})
        await bot.send_message(message.chat.id, "Ok, i can hear you!")

    except rollCallNotStarted as e:
        await bot.send_message(message.chat.id, "Roll call is not active")

# CHANGE STATE TO IN
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        comment = ""
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        user = User(message.from_user.first_name,
                    message.from_user.username if message.from_user.username != "" else "None", message.from_user.id)

        # DEFINING THE USER COMMENT
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
            user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addIn(user, cid, rcNumber)

        if result == 'Error':
            return

        # PRINTING THE LIST
        if not chatConfig['shh']:
            await bot.send_message(cid, db.allList(cid, rcNumber))

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

# CHANGE STATE TO OUT
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        comment = ""
        rcNumber = int(message.data['rcNumber'])

        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        user = User(message.from_user.first_name,
                    message.from_user.username, message.from_user.id)

        # DEFINING THE USER COMMENT
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
            user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addOut(user, cid, rcNumber)

        if result == 'Error':
            return
        elif type(result) == dict:
            if type(result['user_id']) == int:

                name, username, user_id = result['name'], result['username'], result['user_id']
                await bot.send_message(cid, f"{'@'+username if username !=None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")

            else:
                await bot.send_message(cid, f"{result['name']} now you are in!")

        # PRINTING THE LIST
        if not chatConfig['shh']:
            await bot.send_message(cid, db.allList(cid, rcNumber))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# CHANGE STATE TO MAYBE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        comment = ""
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        user = User(message.from_user.first_name,
                    message.from_user.username, message.from_user.id)

        # DEFINING THE USER COMMENT
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
            user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addMaybe(user, cid, rcNumber)

        if result == 'Error':
            return
        elif type(result) == dict:
            if type(result['user_id']) == int:

                name, username, user_id = result['name'], result['username'], result['user_id']
                await bot.send_message(cid, f"{'@'+username if username !=None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")

            else:
                await bot.send_message(cid, f"{result['name']} now you are in!")
        # PRINTING THE LIST
        if not chatConfig['shh']:
            await bot.send_message(cid, db.allList(cid, rcNumber))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# CHANGE STATE TO IN BUT FOR SOMEONE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        comment = ""
        rcNumber = int(message.data['rcNumber'])
        arr = msg.split(" ")
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        elif len(arr) <= 1:
            raise parameterMissing("Input username is missing")

        # ASSIGN ROLLCALL ID
        if not db.getRollCallById(cid, rcNumber):
            raise rollCallNoExists("The roll call id doesn't exist")

        # CREATING THE USER OBJECT
        user = User(arr[1], None, arr[1])
        comment = " ".join(arr[2:]) if len(arr) > 2 else ""
        user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addIn(user, cid, rcNumber)

        if result == 'Error':
            return

        # NOTIFY USER THAT WAS MOVED FROM WAITLIST TO INLIST
        elif type(result) == dict:

            if type(result['user_id']) == int:

                name, username, user_id = result['name'], result['username'], result['user_id']
                await bot.send_message(cid, f"{'@'+ username if username == None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")

            else:
                await bot.send_message(cid, f"{result['name']} now you are in!")

        # PRINTING THE LIST
        if not chatConfig['shh']:
            await bot.send_message(cid, db.allList(cid, rcNumber))

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

# CHANGE STATE TO OUT BUT FOR SOMEONE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        comment = ""
        rcNumber = int(message.data['rcNumber'])
        arr = msg.split(" ")
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # CREATING THE USER OBJECT
        user = User(arr[1], None, arr[1])
        comment = " ".join(arr[2:]) if len(arr) > 2 else ""
        user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addOut(user, cid, rcNumber)

        if result == 'Error':
            return
        # NOTIFY USER THAT WAS MOVED FROM WAITLIST TO INLIST
        elif type(result) == dict:
            if type(result['user_id']) == int:

                name, username, user_id = result['name'], result['username'], result['user_id']
                await bot.send_message(cid, f"{'@'+username if username !=None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")

            else:
                await bot.send_message(cid, f"{result['name']} now you are in!")

        # PRINTING THE LIST
        if not chatConfig['shh']:
            await bot.send_message(cid, db.allList(cid, rcNumber))

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

# CHANGE STATE TO MAYBE BUT FOR SOMEONE
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        comment = ""
        rcNumber = int(message.data['rcNumber'])
        arr = msg.split(" ")
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc =  db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # CREATING THE USER OBJECT
        user = User(arr[1], None, arr[1])
        comment = " ".join(arr[2:]) if len(arr) > 2 else ""
        user.comment = comment

        # ADDING THE USER TO THE LIST
        result = db.addMaybe(user, cid, rcNumber)

        if result == 'Error':
            return

        # NOTIFY USER THAT WAS MOVED FROM WAITLIST TO INLIST
        elif type(result) == dict:

            if type(result['user_id']) == int:

                name, username, user_id = result['name'], result['username'], result['user_id']
                await bot.send_message(cid, f"{'@'+username if username !=None else f'[{name}](tg://user?id={user_id})'} now you are in!", parse_mode="Markdown")

            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        # PRINTING THE LIST
        if not chatConfig['shh']:
            await bot.send_message(cid, db.allList(cid, rcNumber))
            return

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# SEE WHOS IN ON A ROLLCALL
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_in")
async def whos_in(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
     
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # PRINTING LIST
        await bot.send_message(cid, f"{rc['title']} {db.inListText(cid, rcNumber)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# SEE WHOS OUT ON A ROLLCALL
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_out")
async def whos_out(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # PRINTING LIST
        await bot.send_message(cid, f"{rc['title']} {db.outListText(cid, rcNumber)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# SEE WHOS MAYBE ON A ROLLCALL
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_maybe")
async def whos_maybe(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # PRINTING LIST
        await bot.send_message(cid, f"{rc['title']} {db.maybeListText(cid, rcNumber)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# SEE WHOS WAITING ON A ROLLCALL
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_waiting")
async def whos_waiting(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
       
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not db.getRollCallById(cid, rcNumber):
            raise rollCallNoExists("The roll call id doesn't exist")

        # PRINTING LIST
        await bot.send_message(cid, f"{rc['title']} {db.waitListText(cid, rcNumber)}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)


# SET TITLE COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_title")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        msg = message.text
        user = message.from_user.first_name
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        elif len(msg.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # DEFINING TITLE FOR RC
        title = " ".join(msg.split(" ")[1:])

        db.rc_collection.update_one({'_id': cid, 'rollCalls.rcId': rcNumber}, {
                                    "$set": {"rollCalls.$.title": title}})
        await bot.send_message(cid, 'The roll call title is set to: ' + title)

    except Exception as e:
        await bot.send_message(message.chat.id, e)


# END ROLL CALL COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        msg = message.text
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR A RUNNING RC
        if len(chatRollCalls) <= 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR ADMIN RIGHTS
        if chatConfig['adminRights'] == True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        # SENDING LIST
        await bot.send_message(cid, "Roll ended!")
        await bot.send_message(cid, db.finishList(cid, rcNumber))

        # DELETING RC
        db.finishRollCall(cid, rcNumber)
        logging.info("The roll call " + rc['title'] + " has ended")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)
