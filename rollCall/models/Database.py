import logging
import re
import traceback

from config.config import CONN_DB, TELEGRAM_TOKEN

from pymongo import MongoClient
from telebot import TeleBot

bot = TeleBot(TELEGRAM_TOKEN)

#CLASS TO MANAGE MONGO DATABASE
class Database:
    def __init__(self, CONN_DB):

        #CONNECTION
        self.client = MongoClient(CONN_DB)
        self.db = self.client['rollCallDatabase']

        #COLLECTIONS
        self.chat_collection = self.db['chats']
        self.rc_collection = self.db['rollCalls']
 
    #RETURN INLIST
    def inListText(self, cid, rcId):

        rc = self.getRollCallById(cid, rcId)

        txt=f'In:\n'
        i=0

        for user in rc['inList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n"

        return txt+'\n' if len(rc['inList'])>0 else "In:\nNobody\n\n"

    #RETURN OUTLIST
    def outListText(self, cid, rcId):

        rc = self.getRollCallById(cid, rcId)

        txt=f'Out:\n'
        i=0

        for user in rc['outList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n" 

        return txt+'\n' if len(rc['outList'])>0 else "Out:\nNobody\n\n"
    
    #RETURN MAYBELIST
    def maybeListText(self, cid, rcId):

        rc = self.getRollCallById(cid, rcId)

        txt=f'Maybe:\n'
        i=0

        for user in rc['maybeList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n" 

        return txt+'\n' if len(rc['maybeList'])>0 else "Maybe:\nNobody\n\n"

    #RETURN WAITLIST
    def waitListText(self, cid, rcId):

        rc = self.getRollCallById(cid, rcId)

        txt=f'Waiting:\n'
        i=0

        for user in rc['waitList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n" 

        return (txt+'\n' if len(rc['waitList'])>0 else "Waiting:\nNobody") if rc['inListLimit']!=None else ""

    #RETURN ALL THE STATES
    def allList(self, cid, rcId):

        rc = self.getRollCallById(cid, rcId)
        chat_config = self.getChatConfigById(cid)

        createdDate = 'Yet to decide'

        if rc['finalizeDate'] != None:
            createdDate = rc['finalizeDate'].strftime('%d-%m-%Y %H:%M')

        return "Title: "+rc["title"]+f'\nID: {rc["rcId"]}'+f"\nEvent time: {createdDate} {' '+chat_config['timezone'] if createdDate !='Yet to decide' else ''}\nLocation: {rc['location'] if rc['location']!=None else 'Yet to decide'}\n\n"+(self.inListText(cid, rcId) if self.inListText(cid, rcId)!='In:\nNobody\n\n' else '')+(self.outListText(cid,rcId) if self.outListText(cid,rcId)!='Out:\nNobody\n\n' else '')+(self.maybeListText(cid, rcId) if self.maybeListText(cid, rcId)!='Maybe:\nNobody\n\n' else '')+(self.waitListText(cid, rcId) if self.waitListText(cid, rcId)!='Waiting:\nNobody' else '')+'Max limit: '+('♾' if rc['inListLimit']==None else str(rc['inListLimit']))
        
    #RETURN THE FINISH LIST (ONLY IN ERC COMMAND)
    def finishList(self, cid, rcId):

        try:
            rc = self.getRollCallById(cid, rcId)

            createdDate = 'Yet to decide'

            if rc['finalizeDate'] != None:
                createdDate = rc['finalizeDate'].strftime('%d-%m-%Y %H:%M')

            backslash='\n'

            inList = self.inListText(cid, rcId)
            outList = self.outListText(cid, rcId)
            maybeList = self.maybeListText(cid, rcId)
            waitList = self.waitListText(cid, rcId)

            txt = f"Title: {rc['title']}\nID: {rc['rcId']}{(backslash+'Event time: '+createdDate) if createdDate != 'Yet to decide' else ''}{(backslash+'Location: '+rc['location']) if rc['location'] != None else ''}{(backslash+'Event fee: '+str(rc['event_fee'])+backslash + 'Individual Fee: ' + str((round(int(re.sub(r'[^0-9]', '', str(rc['event_fee'])))/len(rc['inList']), 2)) if len(rc['inList'])>0 else '0') + backslash*2 + 'Additional unknown/penalty fees are not included and needs to be handled separately.' + backslash*2) if rc['event_fee'] != None else backslash*2+'In case of paid event - reach out to organiser for payment contribution'}\n\n{inList}{outList}{maybeList}{waitList if waitList !='Waiting:'+backslash+'Nobody' else ''}Max limit: {('♾' if rc['inListLimit']==None else str(rc['inListLimit']))}"

            return txt
        except:
            print(traceback.format_exc())

    #ADD A NEW USER TO IN LIST
    def addIn(self, user, cid, rcId):

        rc = self.getRollCallById(cid, rcId)
        user = user.__dict__
        exists_on_allNames = False

        #ERROR FOR REPEATLY NAME IN SET COMMANDS
        for us in rc['allNames']:

            if user["user_id"] == us["user_id"]:
                user['last_state'] = us['last_state']
                exists_on_allNames = True
                pos = rc['allNames'].index(us)
    
            if user['name']==us['name'] and user['user_id']!=us['user_id']:

                if user['username'] != None:
                    user['name'] += f' ({user["username"]})'
                else:
                    bot.send_message(cid, 'That name already exists!')
                    return

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in rc['inList']:

            #IF HAS A NEW COMMENT, REPLACE IT
            if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                us['comment']=user['comment']
                return
            
            if us['user_id']==user['user_id']:
                bot.send_message(cid, 'No duplicate proxy please :-), Thanks!')
                return 'Error'

        #ERROR FOR DUPLICATE USER ON WAIT LIST STATE
        if rc['inListLimit']!=None:
            for us in rc['waitList']:
                if us==user:
                    bot.send_message(cid, 'No duplicate proxy please :-), Thanks!')
                    return 'Error'

                #IF HAS A NEW COMMENT, REPLACE IT
                if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                    us['comment']=user['comment']
                    return

        #REMOVE THE USER FROM OTHER STATE
        if user['last_state'] != None:

            lastState = user['last_state']

            for us in rc[lastState]:
                if us['user_id'] == user['user_id']:
                    rc[lastState].remove(us)

        #ADD USER TO WAITLIST IF IN LIST IS FULL
        if rc['inListLimit']!=None:
            if len(rc['inList'])==int(rc['inListLimit']):
                user['last_state'] = 'waitList'
                rc['waitList'].append(user)

                if not exists_on_allNames:
                    rc['allNames'].append(user)
                else:
                    rc['allNames'][pos] = user

                self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rcId}, {"$set":{"rollCalls.$":rc}})
                self.db['users'].update_one({"_id":cid, "users.user_id":user['user_id']}, {"$inc":{"users.$.responses":1}})
                bot.send_message(cid, f"Event max limit is reached, {user['name']} was added in waitlist")
                return

        #ADD THE USER TO THE STATE
        user['last_state'] = 'inList'

        if not exists_on_allNames:
            rc['allNames'].append(user)
        else:
            rc['allNames'][pos] = user 

        rc['inList'].append(user)
        self.db['users'].update_one({"_id":cid, "users.user_id":user['user_id']}, {"$inc":{"users.$.responses":1}})
        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rcId}, {"$set":{"rollCalls.$":rc}})

    #ADD A NEW USER TO OUT LIST
    def addOut(self, user, cid, rcId):

        rc = self.getRollCallById(cid, rcId)
        user = user.__dict__
        exists_on_allNames = False
        result = None

        #ERROR FOR REPEATLY NAME
        for us in rc['allNames']:

            if user["user_id"] == us["user_id"]:
                user['last_state'] = us['last_state']
                exists_on_allNames = True
                pos = rc['allNames'].index(us)

            if user['name']==us['name'] and user['user_id']!=us['user_id']:

                if user['username'] != None:
                    user['name'] += f' ({user["username"]})'
                else:
                    bot.send_message(cid, 'That name already exists!')
                    return 'Error'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in rc['outList']:

            #IF HAS A NEW COMMENT, REPLACE IT
            if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                us['comment']=user['comment']
                return

            if us['user_id']==user['user_id']:
                bot.send_message(cid, 'No duplicate proxy please :-), Thanks!')
                return 'Error'

        #REMOVE THE USER FROM LAST STATE
        if user['last_state'] != None:
            lastState = user['last_state']

            if lastState == 'inList':
                self.db['users'].update_one({"_id":cid, "users.user_id":user['user_id']}, {"$inc":{"users.$.in_to_out_count":1}})

            for us in rc[lastState]:
                if us['user_id'] == user['user_id']:
                    rc[lastState].remove(us)

        #IF LAST STATE WAS IN LIST AND WAIT LIST FEATURE ITS ACTIVE, THEN MOVE THE FIRST USER ON WAITLIST TO INLIST
        if rc['inListLimit']!=None and user['last_state'] == 'inList':

            if len(rc['inList'])<int(rc['inListLimit']) and len(rc['waitList'])>0:

                result=rc['waitList'][0]
                rc['inList'].append(rc['waitList'][0])
                rc['waitList'].pop(0)  

        #ADD THE USER TO THE STATE
        user['last_state'] = 'outList'

        if not exists_on_allNames:
            rc['allNames'].append(user)
        else:
            rc['allNames'][pos] = user 

        rc['outList'].append(user)  
        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rcId}, {"$set":{"rollCalls.$":rc}})
        self.db['users'].update_one({"_id":cid, "users.user_id":user['user_id']}, {"$inc":{"users.$.responses":1}})
        if result!=None:
            return result
        
    #ADD A NEW USER TO MAYBE LIST
    def addMaybe(self, user, cid, rcId):

        rc = self.getRollCallById(cid, rcId)
        user = user.__dict__
        exists_on_allNames = False
        result = None

        #ERROR FOR REPEATLY NAME IN SET COMMANDS
        for us in rc['allNames']:

            if user["user_id"] == us["user_id"]:
                user['last_state'] = us['last_state']
                exists_on_allNames = True
                pos = rc['allNames'].index(us)

            if user['name']==us['name'] and user['user_id']!=us['user_id']:
                if user['username'] != None:
                    user['name'] += f' ({user["username"]})'
                else:
                    bot.send_message(cid, 'That name already exists!')
                    return 'Error'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in rc['maybeList']:
            
            #IF HAS A NEW COMMENT, REPLACE IT
            if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                us['comment']=user['comment']
                return

            if us['user_id']==user['user_id']:
                bot.send_message(cid, 'No duplicate proxy please :-), Thanks!')
                return 'Error'

        #REMOVE THE USER FROM OTHER STATE
        if user['last_state']!=None:

            lastState = user['last_state']

            for us in rc[lastState]:
                if us['user_id'] == user['user_id']:
                    rc[lastState].remove(us)

        #IF LAST STATE WAS IN LIST AND WAIT LIST FEATURE ITS ACTIVE, THEN MOVE THE FIRST USER ON WAITLIST TO INLIST
        if rc['inListLimit']!=None and user['last_state'] == 'inList':

            if len(rc['inList'])<int(rc['inListLimit']) and len(rc['waitList'])>0:

                result=rc['waitList'][0]
                rc['inList'].append(rc['waitList'][0])
                rc['waitList'].pop(0)

        #ADD THE USER TO THE STATE
        user['last_state'] = 'maybeList'

        if not exists_on_allNames:
            rc['allNames'].append(user)
        else:
            rc['allNames'][pos] = user 

        rc['maybeList'].append(user)
        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rcId}, {"$set":{"rollCalls.$":rc}})
        self.db['users'].update_one({"_id":cid, "users.user_id":user['user_id']}, {"$inc":{"users.$.responses":1}})
        if result:
            return result

    #RETURN A LIST WITH A TEXT OF EACH ROLLCALL WITH ALL HIS INFO
    def allRollCallsInfo(self, cid):
        txt = []
        chat_config = self.getChatConfigById(cid)

        for rollCall in self.rc_collection.find_one({"_id":cid})['rollCalls']:

            createdDate = 'Yet to decide'
            

            if rollCall['finalizeDate'] != None:
                createdDate = rollCall['finalizeDate'].strftime('%d-%m-%Y %H:%M')

            txt.append("Title: "+rollCall["title"]+f'\nID: {rollCall["rcId"]}'+f"\nEvent time: {createdDate} {' '+chat_config['timezone'] if createdDate !='Yet to decide' else ''}\nLocation: {rollCall['location'] if rollCall['location']!=None else 'Yet to decide'}\n\n"+(self.inListText(cid, rollCall['rcId']) if self.inListText(cid, rollCall['rcId'])!='In:\nNobody\n\n' else '')+(self.outListText(cid, rollCall['rcId']) if self.outListText(cid, rollCall['rcId'])!='Out:\nNobody\n\n' else '')+(self.maybeListText(cid, rollCall['rcId']) if self.maybeListText(cid, rollCall['rcId'])!='Maybe:\nNobody\n\n' else '')+(self.waitListText(cid, rollCall['rcId']) if self.waitListText(cid, rollCall['rcId'])!='Waiting:\nNobody' else '')+'Max limit: '+('♾' if rollCall['inListLimit']==None else str(rollCall['inListLimit'])))

        return txt

    #FINISH A ROLLCALL
    def finishRollCall(self, cid, rcId):
        rollCalls = self.rc_collection.find_one({"_id":cid})['rollCalls']
        for rollCall in rollCalls:
            if rollCall['rcId'] == rcId:
                self.db['endedRollCalls'].update_one({"_id":cid}, {"$push":{"endedRollCalls":rollCall}})
        self.rc_collection.update_one({"_id":cid}, {"$pull":{"rollCalls":{"rcId":rcId}}})

    #DELETE A USER FROM A STATE
    def deleteUser(self, name, cid, rcId):

        rc = self.getRollCallById(cid, rcId)
        user_removed = False

        #FOUND USER
        for us in rc['allNames']:
            if us['name'] == name:
                user_removed = True
                rc[us['last_state']].remove(us)

        if not user_removed:
            False

        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rcId}, {"$set":{"rollCalls.$":rc}})
        return True

    #GET ALL ROLLCALLS
    def getAllRollCalls(self, cid):
        return self.rc_collection.find_one({"_id":cid})['rollCalls']

    #GET ROLLCALL BY ID
    def getRollCallById(self, cid, rcId):
        for rollCall in self.rc_collection.find_one({"_id":cid})['rollCalls']:
            if rollCall['rcId'] == rcId:
                return rollCall
        
        return None

    #GET CHAT CONFIG
    def getChatConfigById(self, cid):
        return self.chat_collection.find_one({"_id":cid})['config']

db = Database(CONN_DB)