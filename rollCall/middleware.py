from telebot.asyncio_handler_backends import BaseMiddleware
from models import Database
from config import CONN_DB
from datetime import datetime
import re
import traceback


class MyMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message']

    async def pre_process(self, update, data):
        print("Middleware working")
        db = Database(CONN_DB).db

        if not db['users'].find_one({"_id": update.from_user.id}):
            db['users'].insert_one({"_id": update.from_user.id, "last_interaction": datetime.now()})
        
        if not db['chats'].find_one({"_id":update.chat.id}):
            chat = {
                "_id": update.chat.id,
                "config":{
                    "adminRights": False,
                    "shh": False,
                    "timezone": "Asia/Calcutta",
                    "adminList": []
                    },
                "createdAt":datetime.now()
            }
            rollCalls = {
                "_id": update.chat.id,
                'rollCalls': []
            }
            db['chats'].insert_one(chat)
            db['rollCalls'].insert_one(rollCalls)

        rcNumber = re.findall(r' ::\d+$', update.text)
        if rcNumber: 
            rcNumber = rcNumber[0].replace(":","").strip()
        
        update.data = {"rcNumber": rcNumber or 1}
        