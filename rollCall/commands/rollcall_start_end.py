from models.Database import db
from exceptions.exceptions import amountOfRollCallsReached, rollCallNotStarted, rollCallNoExists
from utils.functions import admin_rights

import datetime
import traceback

async def start_roll_call(bot, message):
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
            "freeze": False,
            "createdDate": datetime.datetime.utcnow()
        }

        # UPDATING RC TO DB
        db.rc_collection.update_one({"_id": cid}, {"$push": {"rollCalls": rollCall}})
        await bot.send_message(message.chat.id, f"Roll call with title: {title} started!")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

async def end_roll_call(bot, message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
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

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)
