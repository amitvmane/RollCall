from datetime import datetime
import re

from telebot.asyncio_handler_backends import BaseMiddleware

from models.Database import Database
from config.config import CONN_DB


db = Database(CONN_DB).db

class MyMiddleware(BaseMiddleware):
    def __init__(self):
        self.update_types = ['message']

    async def pre_process(self, update, data):

        if '/' in update.text:
            
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
                endedRollcalls = {
                    "_id": update.chat.id,
                    'endedRollCalls':[]
                }
                users = {
                    "_id": update.chat.id,
                    "users": []
                }
                
                db['chats'].insert_one(chat)
                db['rollCalls'].insert_one(rollCalls)
                db['endedRollCalls'].insert_one(endedRollcalls)
                db['users'].insert_one(users)

            #GET RC NUMBER IF EXIST
            rcNumber = re.findall(r' ::\d+$', update.text)
            if rcNumber:
                update.text = update.text.replace(rcNumber[0], "")
                rcNumber = rcNumber[0].replace(":","").strip()    
            update.data = {"rcNumber": rcNumber or 1}
    
    async def post_process(self, update, data, exception):
        
        if '/' in update.text:
            #SAVE INTERACTION
            if not db['users'].find_one({"_id": update.chat.id, "users.user_id": update.from_user.id}):
                db['users'].update_one({"_id":update.chat.id},{"$push":{"users":{"user_id": update.from_user.id, 'name':update.from_user.first_name, 'command':update.text, "last_interaction": datetime.now(), "in_to_out_count":0, "responses":0,'registeredAt':datetime.now()}}})
            else:
                db['users'].update_one({"_id": update.chat.id, "users.user_id": update.from_user.id}, {"$set":{'users.$.command':update.text, "users.$.last_interaction": datetime.now()}})

        return await super().post_process(update, data, exception)
        