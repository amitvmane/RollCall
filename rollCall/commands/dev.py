from models.Database import db
from pymongo import DESCENDING
import json
from utils.analytics.functions import get_data, get_users, total_downtime_or_uptime, active_rollcalls, get_chat_zones, get_all_rollcalls

async def broadcast(bot, message):

    if len(message.text.split(" ")) < 1:
        await bot.send_message(message.chat.id, "Message is missing")

    msg = message.text.split(" ")[1:]
    chats = db.chat_collection.distinct("_id")

    for _id in chats:
        try:
            await bot.send_message(int(_id), " ".join(msg))
        except:
            pass

async def version_command(bot, message):

    version = next(db.db['versions'].find().sort("_id", DESCENDING).limit(1))
    txt = f'Version: {version["Version"]}\nDescription: {version["Description"]}\nDeployed: {version["DeployedOnProd"]}\nDeployed datetime: {version["DeployedDatetime"]}'
    await bot.send_message(message.chat.id, txt)

async def registered_chats(bot, message):
    img = get_data()
    await bot.send_photo(message.chat.id, img)

async def registered_users(bot, message):
    img = get_users()
    await bot.send_photo(message.chat.id, img)

async def downtime_uptime(bot, message):
    result = total_downtime_or_uptime()
    await bot.send_message(message.chat.id, f"Uptime: {result['last_login']}\nDowntime: {result['last_logout']}")

async def active_rollcalls_count(bot, message):
    result = active_rollcalls()
    await bot.send_message(message.chat.id, f"Total active rollcalls: {result}")

async def chat_zones(bot, message):
    result = get_chat_zones()
    await bot.send_photo(message.chat.id, result)

async def rollcalls_count(bot, message):
    result = get_all_rollcalls()
    await bot.send_message(message.chat.id, f'Total rollcalls: {result}')