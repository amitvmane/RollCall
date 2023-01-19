import telebot
from pymongo import MongoClient

from config import TELEGRAM_TOKEN
from exceptions import *
from functions import *

import re
import traceback
import logging

#CLASS TO MANAGE MONGO DATABASE
class Database:
    def __init__(self, CONN_DB, cid = None):

        #CONNECTION
        self.client = MongoClient(CONN_DB)
        self.db = self.client['rollCallDatabase']

        #COLLECTIONS
        self.chat_collection = self.db['chats']
        self.rc_collection = self.db['rollCalls']

        #CHAT INFO
        self.chat_config = self.chat_collection.find_one({"_id":cid})['config']
        self.chat_roll_calls = self.rc_collection.find_one({"_id":cid})['rollCalls']

    #RETURN INLIST
    def inListText(self, cid, rc_id):

        rc = self.rollCallInfo(rc_id)

        txt=f'In:\n'
        i=0

        for user in rc['inList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n"

        return txt+'\n' if len(rc['inList'])>0 else "In:\nNobody\n\n"

    #RETURN OUTLIST
    def outListText(self, cid, rc_id):

        rc = self.rollCallInfo(rc_id)

        txt=f'Out:\n'
        i=0

        for user in rc['outList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n" 

        return txt+'\n' if len(rc['outList'])>0 else "Out:\nNobody\n\n"
    
    #RETURN MAYBELIST
    def maybeListText(self, cid, rc_id):

        rc = self.rollCallInfo(rc_id)

        txt=f'Maybe:\n'
        i=0

        for user in rc['maybeList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n" 

        return txt+'\n' if len(rc['maybeList'])>0 else "Maybe:\nNobody\n\n"

    #RETURN WAITLIST
    def waitListText(self, cid, rc_id):

        rc = self.rollCallInfo(rc_id)

        txt=f'Waiting:\n'
        i=0

        for user in rc['waitList']:
            i+=1
            txt+= f"{i}. {user['name']} {('('+ user['comment'] + ')') if user['comment'] != '' else ''}\n" 

        return (txt+'\n' if len(rc['waitList'])>0 else "Waiting:\nNobody") if rc['inListLimit']!=None else ""

    #RETURN ALL THE STATES
    def allList(self, cid, rc_id):

        rc = self.rollCallInfo(rc_id)

        createdDate = 'Yet to decide'

        if rc['finalizeDate'] != None:
            createdDate = rc['finalizeDate'].strftime('%d-%m-%Y %H:%M')

        return "Title: "+rc["title"]+f'\nID: {rc["rcId"]}'+f"\nEvent time: {createdDate} {' '+self.chat_config['timezone'] if createdDate !='Yet to decide' else ''}\nLocation: {rc['location'] if rc['location']!=None else 'Yet to decide'}\n\n"+(self.inListText(cid, rc_id) if self.inListText(cid, rc_id)!='In:\nNobody\n\n' else '')+(self.outListText(cid,rc_id) if self.outListText(cid,rc_id)!='Out:\nNobody\n\n' else '')+(self.maybeListText(cid, rc_id) if self.maybeListText(cid, rc_id)!='Maybe:\nNobody\n\n' else '')+(self.waitListText(cid, rc_id) if self.waitListText(cid, rc_id)!='Waiting:\nNobody' else '')+'Max limit: '+('♾' if rc['inListLimit']==None else str(rc['inListLimit']))
        
    #RETURN THE FINISH LIST (ONLY IN ERC COMMAND)
    def finishList(self, cid, rc_id):

        try:
            rc = self.rollCallInfo(rc_id)

            createdDate = 'Yet to decide'

            if rc['finalizeDate'] != None:
                createdDate = rc['finalizeDate'].strftime('%d-%m-%Y %H:%M')

            backslash='\n'

            inList = self.inListText(cid, rc_id)
            outList = self.outListText(cid, rc_id)
            maybeList = self.maybeListText(cid, rc_id)
            waitList = self.waitListText(cid, rc_id)

            txt = f"Title: {rc['title']}\nID: {rc['rcId']}{(backslash+'Event time: '+createdDate) if createdDate != 'Yet to decide' else ''}{(backslash+'Location: '+rc['location']) if rc['location'] != None else ''}{(backslash+'Event fee: '+rc['event_fee']+backslash + 'Individual Fee: ' + str((round(int(re.sub(r'[^0-9]', '', rc['event_fee']))/len(rc['inList']), 2)) if len(rc['inList'])>0 else '0') + backslash*2 + 'Additional unknown/penalty fees are not included and needs to be handled separately.' + backslash*2) if rc['event_fee'] != None else backslash*2+'In case of paid event - reach out to organiser for payment contribution'}\n\n{inList}{outList}{maybeList}{waitList if waitList !='Waiting:'+backslash+'Nobody' else ''}Max limit: {('♾' if rc['inListLimit']==None else str(rc['inListLimit']))}"

            return txt
        except:
            print(traceback.format_exc())

    #ADD A NEW USER TO IN LIST
    def addIn(self, user, cid, rc_id):

        rc = self.rollCallInfo(rc_id)
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
                    return 'Error 2'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in rc['inList']:

            #IF HAS A NEW COMMENT, REPLACE IT
            if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                us['comment']=user['comment']
                return
            
            if us['user_id']==user['user_id']:
                return 'Error 1'

        #ERROR FOR DUPLICATE USER ON WAIT LIST STATE
        if rc['inListLimit']!=None:
            for us in rc['waitList']:
                if us==user:
                    return "Error 1"

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
                    rc['waitList'].append(user)
                    logging.info(f"The user {user['name']} has been added to the Wait list")
                    return 'AC'

        #ADD THE USER TO THE STATE
        user['last_state'] = 'inList'

        if not exists_on_allNames:
            rc['allNames'].append(user)
        else:
            rc['allNames'][pos] = user 

        rc['inList'].append(user)
        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rc_id}, {"$set":{"rollCalls.$":rc}})

        logging.info(f"User {user['name']} has change his state to in")

    #ADD A NEW USER TO OUT LIST
    def addOut(self, user, cid, rc_id):

        rc = self.rollCallInfo(rc_id)
        user = user.__dict__
        exists_on_allNames = False

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
                    return 'Error 2'
                    
        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in rc['outList']:

            #IF HAS A NEW COMMENT, REPLACE IT
            if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                us['comment']=user['comment']
                return

            if us['user_id']==user['user_id']:
                return 'Error 1'

        #REMOVE THE USER FROM LAST STATE
        if user['last_state'] != None:

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
                rc['outList'].append(user)  
                logging.info(f"User {user['name']} has change his state to out")
                return result

        #ADD THE USER TO THE STATE
        user['last_state'] = 'outList'

        if not exists_on_allNames:
            rc['allNames'].append(user)
        else:
            rc['allNames'][pos] = user 

        rc['outList'].append(user)  
        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rc_id}, {"$set":{"rollCalls.$":rc}})

        logging.info(f"User {user['name']} has change his state to out")
        
    #ADD A NEW USER TO MAYBE LIST
    def addMaybe(self, user, cid, rc_id):

        rc = self.rollCallInfo(rc_id)
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
                    return 'Error 2'

        #ERROR FOR DUPLICATE USER IN THE SAME STATE
        for us in rc['maybeList']:
            
            #IF HAS A NEW COMMENT, REPLACE IT
            if us['user_id']==user['user_id'] and us['comment'] != user['comment']:
                us['comment']=user['comment']
                return

            if us['user_id']==user['user_id']:
                return 'Error 1'

        #REMOVE THE USER FROM OTHER STATE
        if user['last_state']!=None:

            lastState = user['last_state']

            for us in rc[lastState]:
                print(us['user_id'], user['user_id'])
                if us['user_id'] == user['user_id']:
                    rc[lastState].remove(us)

        #IF LAST STATE WAS IN LIST AND WAIT LIST FEATURE ITS ACTIVE, THEN MOVE THE FIRST USER ON WAITLIST TO INLIST
        if rc['inListLimit']!=None and user['last_state'] == 'inList':

            if len(rc['inList'])<int(rc['inListLimit']) and len(rc['waitList'])>0:

                result=rc['waitList'][0]
                rc['inList'].append(rc['waitList'][0])
                rc['waitList'].pop(0)
                rc['maybeList'].append(user)  
                logging.info(f"User {user['name']} has change his state to out")
                return result

        #ADD THE USER TO THE STATE
        user['last_state'] = 'maybeList'

        if not exists_on_allNames:
            rc['allNames'].append(user)
        else:
            rc['allNames'][pos] = user 

        rc['maybeList'].append(user)

        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rc_id}, {"$set":{"rollCalls.$":rc}})

        logging.info(f"User {user['name']} has change his state to maybe")

    #RETURN A LIST WITH A TEXT OF EACH ROLLCALL WITH ALL HIS INFO
    def allRollCallsInfo(self, cid):
        txt = []

        for roll_call in self.chat_roll_calls:

            createdDate = 'Yet to decide'

            if roll_call['createdDate'] != None:
                createdDate = roll_call['createdDate'].strftime('%d-%m-%Y %H:%M')

            txt.append("Title: "+roll_call["title"]+f'\nID: {roll_call["rcId"]}'+f"\nEvent time: {createdDate} {' '+self.chat_config['timezone'] if createdDate !='Yet to decide' else ''}\nLocation: {roll_call['location'] if roll_call['location']!=None else 'Yet to decide'}\n\n"+(self.inListText(cid, roll_call['rcId']) if self.inListText(cid, roll_call['rcId'])!='In:\nNobody\n\n' else '')+(self.outListText(cid, roll_call['rcId']) if self.outListText(cid, roll_call['rcId'])!='Out:\nNobody\n\n' else '')+(self.maybeListText(cid, roll_call['rcId']) if self.maybeListText(cid, roll_call['rcId'])!='Maybe:\nNobody\n\n' else '')+(self.waitListText(cid, roll_call['rcId']) if self.waitListText(cid, roll_call['rcId'])!='Waiting:\nNobody' else '')+'Max limit: '+('♾' if roll_call['inListLimit']==None else str(roll_call['inListLimit'])))

        return txt

    #RETURN INFO OF A SINGLE ROLLCALL
    def rollCallInfo(self, rc_id):
        for roll_call in self.chat_roll_calls:
            if rc_id == roll_call['rcId']:
                return roll_call

    #FINISH A ROLLCALL
    def finishRollCall(self, cid, rc_id):
        self.rc_collection.update_one({"_id":cid}, {"$pull":{"rollCalls":{"rcId":rc_id}}})

    #DELETE A USER FROM A STATE
    def delete_user(self, name, cid, rc_id):

        rc = self.rollCallInfo(rc_id)
        user_removed = False

        #FOUND USER
        for us in rc['allNames']:
            if us['name'] == name:
                user_removed = True
                rc[us['last_state']].remove(us)

        if not user_removed:
            False

        self.rc_collection.update_one({"_id":cid, "rollCalls.rcId":rc_id}, {"$set":{"rollCalls.$":rc}})
        return True

#CLASS TO MANAGE USER OBJECTS      
class User:

    #USER OBJECT
    def __init__(self, first_name, username, user_id):
        self.name = first_name
        self.first_name = first_name
        self.username = username
        self.user_id = user_id
        self.comment = ''
        self.last_state = None

    def __str__(self):
        backslash="\n"
        return f"{self.name + (' ('+self.comment+')' if self.comment!='' else '')}"
