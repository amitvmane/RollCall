from telebot.asyncio_handler_backends import BaseMiddleware
from models import Database
from config import CONN_DB,commands
from datetime import datetime
import re

db = Database(CONN_DB).db

class MyMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message']

    async def pre_process(self, update, data):

        if update.text.split(" ")[0].split("@")[0] in commands:
            
            #SAVE CHAT ON DATABASE IF NO EXISTS
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
            
            #GET RC NUMBER IF EXIST
            rcNumber = re.findall(r' ::\d+$', update.text)
            if rcNumber:
                update.text = update.text.replace(rcNumber[0], "")
                rcNumber = rcNumber[0].replace(":","").strip()    
            update.data = {"rcNumber": rcNumber or 1}
    

    async def post_process(self, update, data, exception):

        
        if update.text.split(" ")[0].split("@")[0] in commands:
            #SAVE INTERACTION
            if not db['users'].find_one({"_id": update.chat.id, "users._id":update.from_user.id}):
                print("added")
                db['users'].update_one({"_id": update.chat.id}, {"$push":{"users":{'name':update.from_user.first_name, '_id':update.from_user.id,'command':update.text, "last_interaction": datetime.now()}}})
            else:
                db['users'].update_one({"_id": update.chat.id, 'users._id':update.from_user.id}, {"$set":{'users.$.command':update.text, "users.$.last_interaction": datetime.now()}})

        return await super().post_process(update, data, exception)
        