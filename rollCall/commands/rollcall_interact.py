from models.Database import db
from exceptions.exceptions import rollCallNotStarted, rollCallNoExists, incorrectParameter, parameterMissing
from utils.functions import admin_rights

import traceback
import re


async def rollCalls(bot, message):
    try:

        # DEFINING VARIABLES
        cid = message.chat.id
        chat_roll_calls = db.rc_collection.find_one(
            {"_id": message.chat.id})['rollCalls']

        # ERROR IF NOT EXISTS ANY ROLLCALLS
        if len(chat_roll_calls) == 0:
            await bot.send_message(cid, "There are not rollcalls yet")

        rollCalls = db.allRollCallsInfo(cid)
        for rollCallInfo in rollCalls:
            await bot.send_message(cid, rollCallInfo)

    except Exception as e:
        print(traceback.format_exc())


async def freeze(bot, message):
    try:
        cid = message.chat.id

        rcNumber = int(message.data['rcNumber'])
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        db.db['rollCalls'].update_one({"_id": cid, "rollCalls.rcId": rcNumber}, {
                                    "$set": {"rollCalls.$.freeze": True}})
        await bot.send_message(cid, 'Freezing')
    except Exception as e:
        print(traceback.format_exc())


async def unfreeze(bot, message):
    try:
        cid = message.chat.id

        rcNumber = int(message.data['rcNumber'])
        chatRollCalls = db.getAllRollCalls(cid)
        rc = db.getRollCallById(cid, rcNumber)

        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # ASSIGN ROLLCALL ID
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        db.db['rollCalls'].update_one({"_id": cid, "rollCalls.rcId": rcNumber}, {
                                    "$set": {"rollCalls.$.freeze": False}})
        await bot.send_message(cid, 'Unfreezing')
    except Exception as e:
        print(traceback.format_exc())


async def individual_fee(bot, message):

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


async def when(bot, message):

    cid = message.chat.id
    rcNumber = int(message.data['rcNumber'])

    chatRollCalls = db.getAllRollCalls(cid)
    chatConfig = db.getChatConfigById(cid)
    rc = db.getRollCallById(cid, rcNumber)

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


async def delete_user(bot, message):
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

        if db.deleteUser(name, cid, rcNumber):
            await bot.send_message(cid, "The user was deleted!")

        else:
            await bot.send_message(cid, "That user wasn't found")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# SEE WHOS IN ON A ROLLCALL
async def whos_in(bot, message):
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
async def whos_out(bot, message):
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
async def whos_maybe(bot, message):
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
async def whos_waiting(bot, message):
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
