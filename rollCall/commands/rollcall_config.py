from models.Database import db
from exceptions.exceptions import rollCallNotStarted, parameterMissing, rollCallNoExists, timeError, incorrectParameter

import re
import datetime
import traceback

import pytz

async def set_rollcall_time(bot, message):
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

async def set_rollcall_reminder(bot, message):

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

async def event_fee(bot, message):

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

async def set_location(bot, message):

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

async def wait_limit(bot, message):
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

# SET TITLE COMMAND
async def set_title(bot, message):
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
