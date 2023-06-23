from models.Database import db
from models.User import User
from exceptions.exceptions import rollCallNotStarted, rollCallNoExists, parameterMissing

import traceback

# CHANGE STATE TO IN
async def in_user(bot, message):
    try:
        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
        
        # GET ROLLCALL/CHAT INFORMATION FROM DB
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK IF EXIST THE ROLLCALL
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        #CHECK IF THE ROLLCALL IS FREEZED
        if rc['freeze']:
            await bot.send_message(cid, f'The rollcall {rc["title"]} is currently freezed')
            return

        user = User(
            message.from_user.first_name,
            message.from_user.username if message.from_user.username != "" else None, 
            message.from_user.id,
            comment = msg.split(" ", 1)[1] if len(msg.split(" ")) > 1 else ""
            )

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
async def out_user(bot, message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])

        # GET ROLLCALL/CHAT INFORMATION FROM DB
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK IF ROLLCALL EXIST
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        user = User(
            message.from_user.first_name,
            message.from_user.username if message.from_user.username != "" else None, 
            message.from_user.id,
            comment = msg.split(" ", 1)[1] if len(msg.split(" ")) > 1 else ""
        )

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
async def maybe_user(bot, message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
        rcNumber = int(message.data['rcNumber'])
        
        chatRollCalls = db.getAllRollCalls(cid)
        chatConfig = db.getChatConfigById(cid)
        rc = db.getRollCallById(cid, rcNumber)

        # CHECK FOR RC ALREADY RUNNING
        if len(chatRollCalls) == 0:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK IF ROLLCALL EXIST
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        user = User(
            message.from_user.first_name,
            message.from_user.username, 
            message.from_user.id,
            comment = msg.split(" ", 1)[1] if len(msg.split(" ")) > 1 else ""
            )

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
async def set_in_for(bot, message):
    try:

        # DEFINING VARIABLES
        msg = message.text
        cid = message.chat.id
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

        # CHECK IF ROLLCALL EXIST
        if not rc:
            raise rollCallNoExists("The roll call id doesn't exist")

        #CHECK IF ROLLCALL IS FREEZED
        if rc['freeze']:
            await bot.send_message(cid, f'The rollcall {rc["title"]} is currently freezed')
            return

        # CREATING THE USER OBJECT
        user = User(
            arr[1], 
            None, 
            arr[1],
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            )

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
async def set_out_for(bot, message):
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
async def set_maybe_for(bot, message):
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
