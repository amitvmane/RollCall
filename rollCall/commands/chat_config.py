from models.Database import db
from exceptions.exceptions import parameterMissing, rollCallNotStarted
from utils.functions import auto_complete_timezone

import traceback

# SET ADMIN RIGHTS TO TRUE
async def set_admins(bot, message):
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
async def unset_admins(bot, message):

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

async def config_timezone(bot, message):
    print(message)
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

async def shh(bot, message):
    try:
        cid = message.chat.id
        
        # DESACTIVE THE MINIMUM OUTPUT FEATURE
        db.chat_collection.update_one({"_id": cid}, {"$set": {"config.shh": True}})
        await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

    except rollCallNotStarted as e:
        await bot.send_message(message.chat.id, "Roll call is not active")

# NON RESUME NOTIFICATIONS
async def louder(bot, message):
    try:
        cid = message.chat.id
        
        # DESACTIVE THE MINIMUM OUTPUT FEATURE
        db.chat_collection.update_one(
            {"_id": cid}, {"$set": {"config.shh": False}})
        await bot.send_message(message.chat.id, "Ok, i can hear you!")

    except rollCallNotStarted as e:
        await bot.send_message(message.chat.id, "Roll call is not active")
