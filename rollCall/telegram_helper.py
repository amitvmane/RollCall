import logging
import datetime
import re
import asyncio
import json

from telebot.async_telebot import AsyncTeleBot

import pytz

from functions import get_database_chats, get_database
from config import TELEGRAM_TOKEN, ADMINS, CONN_DB
from exceptions import *
from models import RollCall, User
from functions import *
from check_reminders import start
import traceback

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)
logging.info("Bot already started")

db=get_database_chats(CONN_DB)

# START COMMAND, SIMPLE TEXT
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message):
    try:
        cid = message.chat.id
        resp=db.find_one({"chatId":cid})
         
        if resp==None:
            chat={

                "chatId":cid,

                "rollCalls":[],

                "config":{
                    "adminRights":False,
                    "shh":False,
                    "timezone":"Asia/Calcutta",
                    "adminList":[]}
            }

            db.insert_one(chat)
            resp=db.find_one({"chatId":cid})

        # # CHECK FOR ADMIN RIGHTS
        if resp['config']['adminRights']==True:

            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        # START MSG
        await bot.send_message(message.chat.id, 'Hi! im RollCall!\n\nType /help to see all the commands')

    except Exception as e:
        print(e)

# HELP COMMAND WITH ALL THE COMMANDS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/help")
async def help_commands(message):
    #HELP MSG
    await bot.send_message(message.chat.id, '''The commands are:\n-/start  - To start the bot\n-/help - To see the commands\n-/start_roll_call - To start a new roll call (optional title)\n-/in - To let everybody know you will be attending (optional comment)\n-/out - To let everybody know you wont be attending (optional comment)\n-/maybe - To let everybody know you dont know (optional comment)\n-/whos_in - List of those who will go\n-/whos_out - List of those who will not go\n-/whos_maybe - List of those who maybe will go\n-/set_title - To set a title for the current roll call\n-/set_in_for - Allows you to respond for another user\n-/set_out_for - Allows you to respond for another user\n-/set_maybe_for - Allows you to respond for another user\n-/shh - to apply minimum output for each command\n-/louder - to disable minimum output for each command\n-/set_limit - To set a limit to IN state\n-/end_roll_call - To end a roll call\n-/set_rollcall_time - To set a finalize time to the current rc. Accepts 2 parameters date (DD-MM-YYYY) and time (H:M). Write cancel to delete it\n-/set_rollcall_reminder - To set a reminder before the ends of the rc. Accepts 1 parameter, hours as integers. Write 'cancel' to delete the reminder\n-/timezone - To set your timezone, accepts 1 parameter (Continent/Country) or (Continent/State)\n-/when - To check the start time of a roll call\n-/location - To check the location of a roll call''')

# SET ADMIN RIGHTS TO TRUE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):
   
    #DEFINING VARIABLES
    cid=message.chat.id
    
    #Test if user has permissions to use this command
    permissions = await bot.get_chat_member(cid, message.from_user.id)

    if permissions.status not in ['admin', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return
        
    # DEFINING NEW STATE OF ADMIN RIGTS
    db.update_one({"chatId":cid}, {"$set":{"config.adminRights":True}})

    await bot.send_message(cid, 'Admin permissions activated')
    
# SET ADMIN RIGHTS TO FALSE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):

    #DEFINING VARIABLES
    cid=message.chat.id

    #Test if user has permissions to use this command
    permissions = await bot.get_chat_member(cid, message.from_user.id)

    if permissions.status not in ['admin', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return

    # DEFINING NEW STATE OF ADMIN RIGTS
    db.update_one({"chatId":cid}, {"$set":{"config.adminRights":False}})

    await bot.send_message(cid, 'Admin permissions disabled')

# SEND ANNOUNCEMENTS TO ALL GROUPS
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):

    if len(message.text.split(" ")) < 1:
        await bot.send_message(message.chat.id, "Message is missing")

    msg = message.text.split(" ")[1:]

    ids = db.distinct("chatId")

    for k in ids:
        try:
            await bot.send_message(int(k), " ".join(msg))
        except:
            pass

# ADJUST TIMEZONE
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg=message.text
        cid=message.chat.id

        if len(msg.split(" ")) < 2:
            raise parameterMissing(
                "The correct format is: /timezone continent/country or continent/state")
        if len(msg.split(" ")[1].split("/")) != 2:
            raise parameterMissing(
                "The correct format is: /timezone continent/country or continent/state")

        #Formating timezone
        timezone=auto_complete_timezone(" ".join(msg.split(" ")[1:]))

        if timezone != None:

            db.update_one({"chatId":cid}, {"$set":{"config.timezone":timezone}})
            await bot.send_message(message.chat.id, f"Your timezone has been set to {timezone}")

        else:
            await bot.send_message(message.chat.id, f"Your timezone doesn't exists, if you can't found your timezone, check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode = 'HTML')

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

# Version command
@ bot.message_handler(func = lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/version")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/v")
async def version_command(message):
    
    file=open('./version.json')
    data=json.load(file)
    for i in range(0,len(data)):

        version=data[-1-i]
 
        if version["DeployedOnProd"]=='Y':
            txt=''
            txt+=f'Version: {version["Version"]}\nDescription: {version["Description"]}\nDeployed: {version["DeployedOnProd"]}\nDeployed datetime: {version["DeployedDatetime"]}'
            await bot.send_message(message.chat.id, txt)
            break
        
#GET ALL ROLLCALLS OF THE CURRENT CHAT
@ bot.message_handler(func = lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/rollcalls")
@ bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/r")
async def rollCalls(message):
    try:

        cid = message.chat.id
        rollCalls = db.distinct("rollCalls", {"chatId":cid})

        if len(rollCalls)==0:
            await bot.send_message(cid, "There are not rollcalls yet")

        for rollCall in rollCalls:
            rollCall = RollCall(**rollCall)
         
            id=str(rollCall._id)
            await bot.send_message(cid, f"Rollcall number {id}\n\n"+rollCall.allList().replace("__RCID__", id))
    except Exception as e:
        print(traceback.format_exc())
        print(e)

# START A ROLL CALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):

    # DEFINING VARIABLES
    cid=message.chat.id
    msg=message.text
    resp = db.find_one({"chatId":cid})
    ids_to_use=[1,2,3]
    title=''

    try:

        #MAXIMUM ROLLCALLS ERROR
        if len(resp['rollCalls'])>=3:
            raise amountOfRollCallsReached("Allowed Maximum number of active roll calls per group is 3.")

        # CHECK FOR ADMIN RIGHTS
        if resp['config']['adminRights']==True:
            if not admin_rights(message):
                await bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
                return

        if len(resp['rollCalls'])>0:
            ids_used=db.distinct("rollCalls._id", {"chatId":cid})
            ids_to_use=list(set(ids_to_use)-set(ids_used))
            print(ids_to_use)

        # SET THE RC TITLE
        arr=msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            title=' '.join(arr)
        else:
            title='<Empty>'

        # ADD RC TO LIST
        db.update_one({"chatId":cid}, {"$push":{"rollCalls": RollCall(ids_to_use[0], title).__dict__}})
        await bot.send_message(message.chat.id, f"Roll call with title: {title} started!")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

#SET A ROLLCALL START TIME
@ bot.message_handler(func = lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:

        cid=message.chat.id
        msg=message.text
        rc_number=1 #DEFAULT RC NUMBER
        pmts=msg.split(" ")[1:]
        chat_db=db.find_one({"chatId":cid})

        if len(chat_db['rollCalls'])==0:
            raise rollCallNotStarted("Roll call is not active")
        
        if len(message.text.split(" ")) <= 2 and pmts[0]!='cancel':
            raise parameterMissing(
                "invalid datetime format, refer help section for details")

        #IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if rc_number not in db.distinct("rollCalls._id", {"chatId":cid}):
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

        title=db.aggregate([{"chatId":cid, "rollCalls._id":rc_number}])
 
        print(title)
        return

        #CANCEL THE CURRENT REMINDER TIME
        if (pmts[0]).lower() == 'cancel':
            db.update_one({'chatId':cid, 'rollCalls._id':rc_number}, {"$set":{"rollCalls.$.finalizeDate":None, "rollCalls.$.reminder":None}})
            await bot.send_message(message.chat.id, f"Reminder time of rollcall with title {rc['title']} has been canceled.")
            return

        #PARSING INPUT DATETIME
        input_datetime=" ".join(pmts).strip()

        tz=pytz.timezone(chat_db['config']['timezone'])
        date=datetime.datetime.strptime(input_datetime, "%d-%m-%Y %H:%M")
        date=tz.localize(date)

        now_date_string=datetime.datetime.now(tz).strftime("%d-%m-%Y %H:%M")
        now_date=datetime.datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
        now_date=tz.localize(now_date)

        ###

        #ERROR FOR INVALID DATETIME
        if now_date > date:
            raise timeError("Please provide valid future datetime.")
            
        db.update_one({'chatId':cid, 'rollCalls._id':rc_number}, {"$set":{"rollCalls.$.finalizeDate":date.strftime('%d-%m-%Y %H:%M'), "rollCalls.$.reminder":None}})

        await bot.send_message(cid, f"Title: {rc.title}\nID: {rc._id}\n\nEvent notification time is set to {date.strftime('%d-%m-%Y %H:%M')}. Reminder has been reset!")
        
        if all(_date == None for _date in chat_db['rollCalls'].distinct("finalizeDate")):
            asyncio.create_task(start(cid))
        
        # else:
        #     chat[cid]['rollCalls'][rc_number].finalizeDate=date
            
        #     changed=False
        #     if chat[cid]['rollCalls'][rc_number].reminder!=None:
        #         chat[cid]['rollCalls'][rc_number].reminder=None
        #         changed=True

        #     backslash='\n'
        #     await bot.send_message(cid, f"Event notification time is set to {date.strftime('%d-%m-%Y %H:%M')} {chat[cid]['rollCalls'][rc_number].timezone} {backslash*2+'Reminder has been reset!' if changed else ''}")


    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

#SET A ROLLCALL REMINDER
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_reminder")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srr")
async def reminder(message):
    
    #DEFINING VARIABLES
    cid=message.chat.id
    msg=message.text
    rc_number=0 #RC NUMBER DEFAULT
    pmts=msg.split(" ")[1:]
    chat_db=db.find_one({"chatId":cid})
    
    try:

        if len(chat_db['rollCalls'])==0:
            raise rollCallNotStarted("Roll call is not active")

        #IF NUMBER HAS 00:00 FORMAT
        if pmts[0]!='cancel' and len(pmts[0])==2:
            if pmts[0][0]=="0":
                pmts[0]=pmts[0][1]

        #IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        #IF NOT EXISTS A FINALIZE DATE, RAISE ERROR
        if chat[message.chat.id]['rollCalls'][rc_number].finalizeDate == None:
            raise parameterMissing(
                'First you need to set a finalize time for the current rollcall')

        #CANCEL REMINDER
        if pmts[0].lower() == 'cancel':
            chat[message.chat.id]['rollCalls'][rc_number].reminder=None
            await bot.send_message(message.chat.id, "Reminder Notification is canceled.")

        #IF THERE ARE NOT PARAMETERS RAISE ERROR
        if len(pmts) == 0 or not pmts[0].isdigit(): 
            raise parameterMissing(
                "The format is /set_rollcall_reminder hours")

        #IF HOUR IS NOT POSITIVE
        if int(pmts[0]) < 0 or int(pmts[0]) < 1:
            raise incorrectParameter("Hours must be higher than 1")

        hour=pmts[0]
        
        if chat[cid]['rollCalls'][rc_number].finalizeDate - datetime.timedelta(hours=int(hour)) < datetime.datetime.now(pytz.timezone(chat[message.chat.id]['rollCalls'][rc_number].timezone)):
            raise incorrectParameter("Reminder notification time is less than current time, please set it correctly.")

        chat[cid]['rollCalls'][rc_number].reminder=int(hour) if hour != 0 else None
        await bot.send_message(cid, f'I will remind {hour}hour/s before the event! Thank you!')
        
    except ValueError as e:
        print(traceback.format_exc())
        await bot.send_message(cid, 'The correct format is /set_rollcall_reminder HH')
    except Exception as e:
        await bot.send_message(cid, e)

#SET AN EVENT_FEE
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/event_fee")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/ef")
async def event_fee(message):
    
    #DEFINING VARIABLES
    cid=message.chat.id
    pmts=message.text.split(" ")[1:]
    rc_number=0
    
    try:
        if roll_call_not_started(message, chat)==False:
            raise rollCallNotStarted("Roll call is not active")
        
        #IF RC_NUMBER IS SPECIFIED, STORE IT
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        event_price=" ".join(pmts)
        event_price_number=re.findall('[0-9]+', event_price)
        
        if len(event_price_number)==0 or int(event_price_number[0])<=0:
            raise incorrectParameter("The correct format is '/event_fee Integer' Where 'Integer' it's up to 0 number")

        chat[message.chat.id]['rollCalls'][rc_number].event_fee = event_price

        await bot.send_message(cid, f"Event Fee set to {event_price}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

    except Exception as e:
        await bot.send_message(cid, e)

#CHECK HOW MUCH IS INDIVIDUAL FEE
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/individual_fee")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/if")
async def individual_fee(message):
    
    #DEFINING VARIABLES
    cid=message.chat.id
    pmts=message.text.split(" ")[1:]
    rc_number=0
    
    try:
        if roll_call_not_started(message, chat)==False:
            raise rollCallNotStarted("Roll call is not active")
        
        #IF RC_NUMBER IS SPECIFIED, STORE IT
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        in_list=len(chat[cid]['rollCalls'][rc_number].inList)
        event_price=int(re.sub(r'[^0-9]', "", str(chat[cid]['rollCalls'][rc_number].event_fee)))

        if in_list>0:
            individual_fee=round(event_price/in_list, 2)
        else:
            individual_fee=0

        await bot.send_message(cid, f'Individual fee is {individual_fee}')
          
    except Exception as e:
        await bot.send_message(cid, e)

#CHECK WHEN A ROLLCALL WILL BE START
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/when")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/w")
async def when(message):
    
    cid=message.chat.id
    pmts=message.text.split(" ")
    rc_number=0

    try:
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

         #IF RC_NUMBER IS SPECIFIED, STORE IT
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

        if chat[cid]['rollCalls'][rc_number].finalizeDate == None:
            raise incorrectParameter("There is no start time for the event")

        await bot.send_message(cid, f"The event with title {chat[message.chat.id]['rollCalls'][rc_number].title} will start at {chat[message.chat.id]['rollCalls'][rc_number].finalizeDate.strftime('%d-%m-%Y %H:%M')}!")

    except Exception as e:
        await bot.send_message(cid, e)

#SET A LOCATION FOR A ROLLCALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):
    try:
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
          
        if len(message.text.split(" ")) < 2:
            raise incorrectParameter("The correct format is /location <place>")
       
        cid=message.chat.id
        msg=message.text
        pmts=msg.split(" ")[1:]
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

        place=" ".join(pmts)

        chat[cid]['rollCalls'][rc_number].location=place

        await bot.send_message(cid, f"The rollcall with title - {chat[cid]['rollCalls'][0].title} has a new location!")

    except Exception as e:
        await bot.send_message(cid, e)

# SET A LIMIT FOR IN LIST
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_limit")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sl")
async def wait_limit(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1 or int(message.text.split(" ")[1])<0:
            raise parameterMissing(
                "Input limit is missing or it's not a positive number")

        # DEFINING VARIABLES
        msg= message.text
        cid= message.chat.id
        comment=""
        pmts=msg.split(" ")[1:]
        rc_number=0
        limit=int(pmts[0])

        try:
            if "::" in pmts[-1]:
                try:
                    rc_number=int(pmts[-1].replace("::",""))-1
                    del pmts[-1]
                except:
                    raise incorrectParameter("The rollcall number must be a positive integer")

                if len(chat[cid]['rollCalls'])<rc_number+1:
                    raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        except:
            pass

        # SETTING THE LIMIT TO INLIST
        chat[cid]["rollCalls"][rc_number].inListLimit=limit
        logging.info(f"Max limit of attendees is set to {limit}")
        await bot.send_message(cid, f'Max limit of attendees is set to {limit}')

        # MOVING USERS IF IN LIST HAS ALREADY REACH THE LIMIT
        if len(chat[cid]["rollCalls"][rc_number].inList) > limit:
            chat[cid]["rollCalls"][rc_number].waitList.extend(chat[cid]["rollCalls"][rc_number].inList[limit:])
            chat[cid]["rollCalls"][rc_number].inList=chat[cid]["rollCalls"][rc_number].inList[:limit]
        elif len(chat[cid]["rollCalls"][rc_number].inList) < limit:
            a=int(limit-len(chat[cid]["rollCalls"][rc_number].inList))
            chat[cid]["rollCalls"][rc_number].inList.extend(
                chat[cid]["rollCalls"][rc_number].waitList[: limit-len(chat[cid]["rollCalls"][rc_number].inList)])
            chat[cid]["rollCalls"][rc_number].waitList=chat[cid]["rollCalls"][rc_number].waitList[a:]

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

#DELETE AN USER OF A ROLLCALL
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
async def delete_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETER MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")
        # CHECK FOR ADMING RIGHTS
        if admin_rights(message, chat) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        # DEFINE VARIABLES
        msg=message.text
        cid=message.chat.id
        arr=msg.split(" ")
        rc_number=0
 
        if len(arr)>1 and "::" in arr[-1]:
            try:
                rc_number=int(arr[-1].replace("::",""))-1
                del arr[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        # DELETE THE USER
        name=" ".join(arr[1:])
        if chat[cid]["rollCalls"][rc_number].delete_user(name, chat[cid]["allNames"][rc_number]) == True:
            await bot.send_message(cid, "The user was deleted!")
        else:
            await bot.send_message(cid, "That user wasn't found")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

#RESUME NOTIFICATIONS
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0] =="/shh")
async def shh(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DESACTIVE THE MINIMUM OUTPUT FEATURE
            chat[message.chat.id]['shh']=True
            await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

#NON RESUME NOTIFICATIONS
@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0] =="/louder")
async def louder(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:

            # DESACTIVE THE MINIMUM OUTPUT FEATURE
            chat[message.chat.id]['shh']=False
            await bot.send_message(message.chat.id, "Ok, i can hear you!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
                msg=" ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")

        user =User(message.from_user.first_name, message.from_user.username if message.from_user.username != "" else "None", message.from_user.id, chat[cid]["allNames"][rc_number])
        
        # DEFINING THE USER COMMENT
        arr= msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment= ' '.join(arr)
            user.comment=comment

        # ADDING THE USER TO THE LIST
        result=chat[cid]["rollCalls"][rc_number].addIn(
            user, chat[cid]["allNames"][rc_number])
        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        # PRINTING THE LIST
        if send_list(message, chat):
            await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList().replace("__RCID__", str(rc_number+1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        

        # DEFINING VARIABLES
        msg= message.text
        pmts = msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=0

        
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
                msg=" ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        

        user= User(message.from_user.first_name, message.from_user.username, message.from_user.id, chat[cid]["allNames"][rc_number])

        # DEFINING THE USER COMMENT
        arr= msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment= ' '.join(arr)
            user.comment=comment

        # ADDING THE USER TO THE LIST
        result=chat[cid]["rollCalls"][rc_number].addOut(
            user, chat[cid]["allNames"][rc_number])
        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if type(result.user_id) == int:
                await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        # PRINTING THE LIST
        if send_list(message, chat):
            await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList().replace("__RCID__", str(rc_number+1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
                msg=" ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
    
        user= User(message.from_user.first_name, message.from_user.username, message.from_user.id, chat[cid]["allNames"][rc_number])

        # DEFINING THE USER COMMENT
        arr= msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment= ' '.join(arr)
            user.comment=comment

        # ADDING THE USER TO THE LIST
        result=chat[cid]["rollCalls"][rc_number].addMaybe(
            user, chat[cid]["allNames"][rc_number])
        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if type(result.user_id) == int:
                await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        # PRINTING THE LIST
        if send_list(message, chat):
            await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList().replace("__RCID__", str(rc_number+1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
                msg=" ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
    
        arr=msg.split(" ")

        # CREATING THE USER OBJECT
        if len(arr) > 1:
            user= User(arr[1], None, arr[1], chat[cid]["allNames"][rc_number])
            comment =" ".join(arr[2: ]) if len(arr) > 2 else ""
            user.comment=comment

            # ADDING THE USER TO THE LIST
            result=chat[cid]["rollCalls"][rc_number].addIn(
                user, chat[cid]["allNames"][rc_number])
            if result == 'AB':
                raise duplicateProxy(
                    "No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif isinstance(result, User):
                if type(result.user_id) == int:
                    await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            # PRINTING THE LIST
            if send_list(message, chat):
                await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList().replace("__RCID__", str(rc_number+1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
                msg=" ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        arr=msg.split(" ")

        # CREATING THE USER OBJECT
        if len(arr) > 1:
            user= User(arr[1], None, arr[1], chat[cid]["allNames"][rc_number])
            comment =" ".join(arr[2: ]) if len(arr) > 2 else ""
            user.comment=comment

            # ADDING THE USER TO THE LIST
            result=chat[cid]["rollCalls"][rc_number].addOut(
                user, chat[cid]["allNames"][rc_number])
            if result == 'AB':
                raise duplicateProxy(
                    "No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif isinstance(result, User):
                if type(result.user_id) == int:
                    await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            # PRINTING THE LIST
            if send_list(message, chat):
                await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList().replace("__RCID__", str(rc_number+1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

       

        # DEFINING VARIABLES
        msg= message.text
        pmts= msg.split(" ")
        cid= message.chat.id
        comment=""
        rc_number=0

        
        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
                msg=" ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        

        arr=msg.split(" ")

        # CREATING THE USER OBJECT
        if len(arr) > 1:
            user= User(arr[1], None, arr[1], chat[cid]["allNames"][rc_number])
            comment =" ".join(arr[2: ]) if len(arr) > 2 else ""
            user.comment=comment

            # ADDING THE USER TO THE LIST
            result=chat[cid]["rollCalls"][rc_number].addMaybe(
                user, chat[cid]["allNames"][rc_number])

            if result == 'AB':
                raise duplicateProxy(
                    "No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif isinstance(result, User):
                if type(result.user_id) == int:
                    await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            # PRINTING THE LIST
            if send_list(message, chat):
                await bot.send_message(cid, chat[cid]["rollCalls"][rc_number].allList().replace("__RCID__", str(rc_number+1)))
                return

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_in")  # WHOS IN COMMAND
async def whos_in(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        # DEFINING VARIABLES
        cid= message.chat.id
        rc_number=0

        pmts=message.text.split(" ")[1:]

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        # PRINTING LIST
        await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].inListText())

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_out")  # WHOS IN COMMAND
async def whos_out(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        # DEFINING VARIABLES
        cid= message.chat.id
        rc_number=0

        pmts=message.text.split(" ")[1:]

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        # PRINTING LIST
        await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].outListText())

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_maybe")  # WHOS IN COMMAND
async def whos_maybe(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
       
        # DEFINING VARIABLES
        cid= message.chat.id
        rc_number=0

        pmts=message.text.split(" ")[1:]

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        # PRINTING LIST
        await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].maybeListText())

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:message.text.lower().split("@")[0].split(" ")[0] =="/whos_waiting")  # WHOS IN COMMAND
async def whos_waiting(message):
    try:
        # CHECK FOR RC ALREADY RUNNING
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        # DEFINING VARIABLES
        cid= message.chat.id
        rc_number=0

        pmts=message.text.split(" ")[1:]

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        # PRINTING LIST
        await bot.send_message(cid, f"{chat[cid]['rollCalls'][rc_number].title if len(chat[cid]['rollCalls'])>1 else ''}"+" "+chat[cid]["rollCalls"][rc_number].waitListText())

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() =="/set_title")  # SET TITLE COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:
           # CHECK FOR RC ALREADY RUNNING
        if    roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")
        # CHECK FOR PARAMETERS MISSING
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        # DEFINING VARIABLES
        cid= message.chat.id
        msg= message.text
        pmts=msg.split(" ")[1:]
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        title = " ".join(pmts)
        user= message.from_user.first_name

        # DEFINING TITLE FOR RC
        if title=="":
            title="<Empty>"

        chat[cid]["rollCalls"][rc_number].title=title
        await bot.send_message(cid, 'The roll call title is set to: ' + title)

        logging.info(user+"-"+"The title has change to "+title)

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")  #END ROLL CALL COMMAND
@ bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:
        # CHECK FOR A RUNNING RC
        if roll_call_not_started(message, chat) == False:
            raise rollCallNotStarted("Roll call is not active")

        # CHECK IF ADMIN_RIGHTS ARE ON
        if admin_rights(message, chat) == False:
            raise insufficientPermissions(
                "Error - user does not have sufficient permissions for this operation")

        # DEFINING VARIABLES
        cid=message.chat.id
        pmts=message.text.split(" ")[1:]
        rc_number=0

        if len(pmts)>1 and "::" in pmts[-1]:
            try:
                rc_number=int(pmts[-1].replace("::",""))-1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            if len(chat[cid]['rollCalls'])<rc_number+1:
                raise incorrectParameter("The rollcall number doesn't exist, check /command to see all rollcalls")
        
        # SENDING LIST
        await bot.send_message(message.chat.id, "Roll ended!")

        await bot.send_message(cid, chat[cid]['rollCalls'][rc_number].finishList().replace("__RCID__", str(rc_number+1)))

        logging.info("The roll call "+chat[cid]["rollCalls"][rc_number].title+" has ended")

        # DELETING RC
        chat[cid]["rollCalls"].pop(rc_number)

    except Exception as e:
        await bot.send_message(message.chat.id, e)





        
        

