from re import IGNORECASE
import telebot
from telebot import types
from telebot.types import ReplyKeyboardMarkup
from telebot.types import ReplyKeyboardRemove
import numpy
from dotenv import load_dotenv
import os
load_dotenv()
bot = telebot.TeleBot(os.getenv("API_KEY"))


chat={}
commands=["/start","/help","/start_roll_call","/shh","/louder","/end_roll_call","/in","/out","/maybe","/set_title","/whos_in","/whos_out","/whos_maybe","/set_in_for","/sif","/set_out_for","/sof","/set_maybe_for","/smf",]

def roll_call_already_started(message):
    try:
        if chat[message.chat.id]['title']!='':
            bot.send_message(message.chat.id, f"Roll call with title {chat[message.chat.id]['title']} is still in progress")
            return False
    except:
        return True

def roll_call_not_started(message):
    try:
        if chat[message.chat.id]['title']!='':
            return True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        return False

def create_txt_list(cid):
    chat[cid]['txtIn']=''
    i=0
    for us in chat[cid]['usersAttendance']:
        i+=1
        
        if next((x for x in chat[cid]['usersComments'] if x["id"] == us["id"]), None)!=None:
            for userComment in chat[cid]['usersComments']:
                if userComment["id"] == us["id"]:
                    comment=userComment["comment"]
                    chat[cid]['txtIn']+=f'{i}. {us["name"]} ({comment})\n'
                    print(f"{us['name']} ({comment})")
        else:
            chat[cid]['txtIn']+=f'{i}. {us["name"]}\n'
            print(f"{us['name']}")

    chat[cid]['txtOut']=''
    o=0
    for us in chat[cid]['usersNotAttendance']:
        print(chat[cid]['usersNotAttendance'])
        o+=1
        if len(chat[cid]["usersComments"])>0:
            for user in chat[cid]['usersComments']:
                if user["id"]==us["id"]:
                    comment=user["comment"]
                    chat[cid]['txtOut']+=f'{o}. {us["name"]} ({comment})\n'
                else:
                    chat[cid]['txtOut']+=f'{o}. {us["name"]}\n'
        else:
            chat[cid]['txtOut']+=f'{o}. {us["name"]}\n'
    
    chat[cid]['txtMaybe']=''
    m=0
    for us in chat[cid]['usersMaybeAttendance']:
        m+=1
        if len(chat[cid]["usersComments"])>0:
            for user in chat[cid]['usersComments']:
                if user["id"]==us["id"]:
                    comment=user["comment"]
                    chat[cid]['txtMaybe']+=f'{m}. {us["name"]} ({comment})\n'
                else:
                    chat[cid]['txtMaybe']+=f'{m}. {us["name"]}\n'
        else:
            chat[cid]['txtMaybe']+=f'{m}. {us["name"]}\n'

def replace_repeated_name(cid, user):
    allUsers=numpy.concatenate((chat[cid]["usersAttendance"],chat[cid]["usersNotAttendance"],chat[cid]["usersMaybeAttendance"]))
    r=0
    for us in allUsers:
        if user["name"]==us["name"]:
            if user["id"]!=us["id"]:
                user["name"]+=f' ({user["username"]})'

def admin_rights(message):
    try:
        if len(chat[message.chat.id]['admins'])>0:
            return True
    except:
        return False

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/set_admins" and bot.get_chat_member(message.chat.id, message.from_user.id).status=='admin')  # START COMMAND
def set_admins(message):
    try:
        if chat[message.chat.id]:
            pass
    except:
        chat[message.chat.id]={}
   
    admins=bot.get_chat_administrators(message.chat.id)
    arrAdmins = []
    for r in admins:
        arrAdmins.append(r.user.id)
    chat[message.chat.id]['admins']=arrAdmins

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/unset_admins" and bot.get_chat_member(message.chat.id, message.from_user.id).status=='admin')  # START COMMAND
def unset_admins(message):
    try:
        if chat[message.chat.id]:
            pass
    except:
        chat[message.chat.id]={}
    chat[message.chat.id]['admins']=[]

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/start")  # START COMMAND
def welcome_and_explanation(message):
    try:
        if chat[message.chat.id]:
            pass
    except:
        chat[message.chat.id]={}
    if admin_rights(message):
        if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
            bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
            return            
    bot.send_message(message.chat.id, '''
Hi! im RollCall!
Type /help to see all the commands
    ''')

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/help")  # HELP COMMAND
def welcome_and_explanation(message):
    bot.send_message(message.chat.id, '''
The commands are:
-/start  - To start the bot
-/help - To see the commands
-/start_roll_call - To start a new roll call (optional title)
-/in - To let everybody know you will be attending (optional comment)
-/out - To let everybody know you wont be attending (optional comment)
-/maybe - To let everybody know you dont know (optional comment)
-/whos_in - List of those who will go
-/whos_out - List of those who will not go
-/whos_maybe - List of those who maybe will go
-/set_title - To set a title for the current roll call
-/set_in_for - Allows you to respond for another user
-/set_out_for - Allows you to respond for another user
-/set_maybe_for - Allows you to respond for another user
-/shh - to apply minimum output for each command
-/louder - to disable minimum output for each command
-/end_roll_call - To end a roll call
    ''')
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")  # START ROLL CALL COMMAND
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/src")
def start_roll_call(message):

    try:
        if chat[message.chat.id]:
            pass
    except:
        chat[message.chat.id]={}

    if admin_rights(message):
        if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
            bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
            return

    if roll_call_already_started(message):
        cid = message.chat.id  # chat id
        msg = message.text
        arr = msg.split(" ")
        title=''

        if len(arr) > 1:                      
            arr.pop(0)  # Define a title for the roll
            title = ' '.join(arr)

        if title == '':
            title = '<Empty>'  # if title is empty default title is Roll name

        chat[cid]['txtIn']='Nobody'
        chat[cid]['txtOut']='Nobody'
        chat[cid]['txtMaybe']='Nobody'
        chat[cid]['title'] = title
        chat[cid]['usersNotAttendance']=[]
        chat[cid]['usersMaybeAttendance']=[]
        chat[cid]['usersAttendance']=[]
        chat[cid]['usersComments']=[]
        chat[cid]['shh']=False
        chat[cid]["id"]=0

        

        bot.send_message(message.chat.id, "Roll call with title: "+title+" started!")
        print("A roll call with title "+title+" has started")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
def delete_user(message):
    cid=message.chat.id
    if admin_rights(message):
        if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
            bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
            return
    
    arr=message.text.split(" ")
    if len(arr)>1:
        arr.pop(0)
        user=' '.join(arr)

        userToRemove=next((x for x in chat[cid]['usersAttendance'] if x["name"] == user), None)
        if userToRemove!=None:
            chat[cid]['usersAttendance'].remove(userToRemove)
            bot.send_message(message.chat.id, "The user has been removed from all lists")
            return
        userToRemove=next((x for x in chat[cid]['usersNotAttendance'] if x["name"] == user), None)
        if userToRemove!=None:
            chat[cid]['usersNotAttendance'].remove(userToRemove)
            bot.send_message(message.chat.id, "The user has been removed from all lists")
            return
        userToRemove=next((x for x in chat[cid]['usersMaybeAttendance'] if x["name"] == user), None)
        if userToRemove!=None:
            chat[cid]['usersMaybeAttendance'].remove(userToRemove)
            bot.send_message(message.chat.id, "The user has been removed from all lists")
            return

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/shh")
def shh(message):
    if roll_call_not_started(message):
        chat[message.chat.id]['shh']=True
        bot.send_message(message.chat.id, "Ok, i will keep quiet!")

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/louder")
def louder(message):
    if roll_call_not_started(message):
        chat[message.chat.id]['shh']=False
        bot.send_message(message.chat.id, "Ok, i can hear you!")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/in")  # IN COMMAND
def in_user(message):
    if roll_call_not_started(message):
        print("IN COMMAND\n------------------------------------------------------------------")
        condition=True
        msg = message.text
        cid = message.chat.id  # chat id
        user = {"name":message.from_user.first_name, "username":message.from_user.username if message.from_user.username else "Null" , "id":message.from_user.id}
        comment = ''

        replace_repeated_name(cid, user)

        next((chat[cid]["usersComments"].remove(x) for x in chat[cid]['usersComments'] if x["id"] == user["id"]), None)

        arr = msg.split(" ")
        if len(arr) > 1:                                    
            arr.pop(0)
            comment = ' '.join(arr)
        
        #CHECK USER IN OTHER LIST AND ADD IT
        if len(chat[cid]["usersNotAttendance"])>0:
            next((chat[cid]['usersNotAttendance'].remove(x) for x in chat[cid]['usersNotAttendance'] if x["id"] == user["id"]), None)
        if len(chat[cid]["usersMaybeAttendance"])>0:
            next((chat[cid]['usersMaybeAttendance'].remove(x) for x in chat[cid]['usersMaybeAttendance'] if x["id"] == user["id"]), None)
        if 'usersAttendance' in chat[cid]:             
            if user in chat[cid]['usersAttendance']:
                if comment=="" and next((x for x in chat[cid]['usersComments'] if x["id"] == user["id"]), None)==None:
                    bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                    condition=False
                else:
                    for us in chat[cid]['usersComments']:
                        if us["id"]==user["id"]:
                            chat[cid]['usersComments'].remove(us)
            else:
                chat[cid]['usersAttendance'] += [user]
                print(user["name"]+" it's IN")
        else:                                          
            chat[cid]['usersAttendance'] = [user]
            print(user["name"] +" it's IN")

        #CHECK USER IN OTHER LIST AND ADD IT

        if comment!='':
            chat[cid]['usersComments']+=[{"id":user["id"], "comment":comment, "name":user["name"]}]
        else:
            for us in chat[cid]['usersComments']:
                if us["id"]==user["id"]:
                    chat[cid]['usersComments'].remove(us)
                    
        print("Users Attendance:", chat[cid]["usersAttendance"])
        print("Users Not Attendance:", chat[cid]["usersNotAttendance"])
        print("Users Maybe Attendance", chat[cid]["usersMaybeAttendance"])
        print("Comments list:", chat[cid]["usersComments"])

        create_txt_list(cid)
        
        if condition==True and chat[cid]['shh']==False:
            markup=ReplyKeyboardRemove()
            backslash='\n'
            bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""", reply_markup=markup)

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/out")  # OUT COMMAND
def out_user(message):
    if roll_call_not_started(message):
        print("OUT COMMAND\n------------------------------------------------------------------")
        condition=True
        comment = ''
        msg = message.text
        cid = message.chat.id
        user = {"name":message.from_user.first_name, "username":message.from_user.username if message.from_user.username else "Null" , "id":message.from_user.id} # name of user who not attendance
        arr = msg.split(" ")

        replace_repeated_name(cid, user)

        next((chat[cid]["usersComments"].remove(x) for x in chat[cid]['usersComments'] if x["id"] == user["id"]), None)

        if len(arr) > 1:             
            arr.pop(0)  # Define the comment
            comment = ' '.join(arr)

        if len(chat[cid]["usersAttendance"])>0:
            next((chat[cid]['usersAttendance'].remove(x) for x in chat[cid]['usersAttendance'] if x["id"] == user["id"]), None)
        if len(chat[cid]["usersMaybeAttendance"])>0:
            next((chat[cid]['usersMaybeAttendance'].remove(x) for x in chat[cid]['usersMaybeAttendance'] if x["id"] == user["id"]), None)

        if 'usersNotAttendance' in chat[cid]:           
            if user in chat[cid]['usersNotAttendance']:
                if comment=="" and next((x for x in chat[cid]['usersComments'] if x["id"] == user["id"]), None)==None:
                    bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                    condition=False
                else:
                    for us in chat[cid]['usersComments']:
                        if us["id"]==user["id"]:
                            chat[cid]['usersComments'].remove(us)
            else:
                chat[cid]['usersNotAttendance'] += [user]     
                print(user["name"]+" it's OUT")
        else:  # If the variable doesn't exists creates one
            chat[cid]['usersNotAttendance'] = [user]  
            print(user["name"]+" it's OUT")    

        # text with a list of users
        if comment!='':
            chat[cid]['usersComments']+=[{"id":user["id"], "comment":comment, "name":user["name"]}]
        else:
            for us in chat[cid]['usersComments']:
                if us["id"]==user["id"]:
                    chat[cid]['usersComments'].remove(us)

        print("Users Attendance:", chat[cid]["usersAttendance"])
        print("Users Not Attendance:", chat[cid]["usersNotAttendance"])
        print("Users Maybe Attendance", chat[cid]["usersMaybeAttendance"])
        print("Comments list:", chat[cid]["usersComments"])

        create_txt_list(cid)

        if condition==True and chat[cid]['shh']==False:
            backslash='\n'
            bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/maybe")  # MAYBE COMMAND
def maybe_user(message):
    if roll_call_not_started(message):
        print("MAYBE COMMAND\n------------------------------------------------------------------")
        condition=True
        comment=''
        msg = message.text
        cid = message.chat.id
        user = {"name":message.from_user.first_name, "username":message.from_user.username if message.from_user.username else "Null" , "id":message.from_user.id}  # name of user who attendance

        arr = msg.split(" ")

        replace_repeated_name(cid, user)

        next((chat[cid]["usersComments"].remove(x) for x in chat[cid]['usersComments'] if x["id"] == user["id"]), None)

        if len(arr) > 1:                                    
            arr.pop(0)  # Define the comment
            comment = ' '.join(arr)

        if len(chat[cid]["usersAttendance"])>0:
            next((chat[cid]['usersAttendance'].remove(x) for x in chat[cid]['usersAttendance'] if x["id"] == user["id"]), None)
        if len(chat[cid]["usersNotAttendance"])>0:
            next((chat[cid]['usersNotAttendance'].remove(x) for x in chat[cid]['usersNotAttendance'] if x["id"] == user["id"]), None)

        if 'usersMaybeAttendance' in chat[cid]:             
            if user in chat[cid]['usersMaybeAttendance']:
                if comment=="" and next((x for x in chat[cid]['usersComments'] if x["id"] == user["id"]), None)==None:
                    bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                    condition=False
                else:
                    for us in chat[cid]['usersComments']:
                        if us["id"]==user["id"]:
                            chat[cid]['usersComments'].remove(us)
            else:
                chat[cid]['usersMaybeAttendance'] += [user]      
                print(user["name"]+" it's MAYBE") 
        else:                                               # if the variable doesn't exist it creates one
            chat[cid]['usersMaybeAttendance'] = [user]     
            print(user["name"]+" it's MAYBE")   
        # text list of who maybe attendance
       
        if comment!='':
            chat[cid]['usersComments']+=[{"id":user["id"], "comment":comment, "name":user["name"]}]
        else:
            for us in chat[cid]['usersComments']:
                if us["id"]==user["id"]:
                    chat[cid]['usersComments'].remove(us)

        print("Users Attendance:", chat[cid]["usersAttendance"])
        print("Users Not Attendance:", chat[cid]["usersNotAttendance"])
        print("Users Maybe Attendance", chat[cid]["usersMaybeAttendance"])
        print("Comments list:", chat[cid]["usersComments"])

        create_txt_list(cid)

        if condition==True and chat[cid]['shh']==False:
            backslash='\n'
            bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/whos_in")  # WHOS IN COMMAND
def whos_in(message):
    if roll_call_not_started(message):
        cid = message.chat.id
        
        chat[cid]['txtIn']=''
        i=0
        for us in chat[cid]['usersAttendance']:
            i+=1
            if len(chat[cid]["usersComments"])>0:
                for user in chat[cid]['usersComments']:
                    if user["id"]==us["id"]:
                        comment=user["comment"]
                        chat[cid]['txtIn']+=f'{i}. {us["name"]} ({comment})\n'
                    else:
                        chat[cid]['txtIn']+=f'{i}. {us["name"]}\n'
            else:
                chat[cid]['txtIn']+=f'{i}. {us["name"]}\n'
        bot.send_message(message.chat.id, f"""
In:
{chat[cid]['txtIn']}""")  # list of who will attendance

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/whos_out")  # WHOS IN COMMAND
def whos_out(message):
    cid = message.chat.id
    if roll_call_not_started(message):
        chat[cid]['txtOut']=''
        o=0
        for us in chat[cid]['usersNotAttendance']:
            o+=1
            if len(chat[cid]["usersComments"])>0:
                for user in chat[cid]['usersComments']:
                    if user["id"]==us["id"]:
                        comment=user["comment"]
                        chat[cid]['txtOut']+=f'{o}. {us["name"]} ({comment})\n'
                    else:
                        chat[cid]['txtOut']+=f'{o}. {us["name"]}\n'
            else:
                chat[cid]['txtOut']+=f'{o}. {us["name"]}\n'
        
        bot.send_message(message.chat.id, f"""
Out:
{chat[cid]['txtOut']}""")  # list of who will not attendance

@bot.message_handler(func=lambda message:message.text.lower().split("@")[0]=="/whos_maybe")  # WHOS IN COMMAND
def whos_maybe(message):
    cid = message.chat.id
    if roll_call_not_started(message):
        chat[cid]['txtMaybe']=''
        m=0
        for us in chat[cid]['usersMaybeAttendance']:
            m+=1
            if len(chat[cid]["usersComments"])>0:
                for user in chat[cid]['usersComments']:
                    if user["id"]==us["id"]:
                        comment=user["comment"]
                        chat[cid]['txtMaybe']+=f'{m}. {us["name"]} ({comment})\n'
                    else:
                        chat[cid]['txtMaybe']+=f'{m}. {us["name"]}\n'
            else:
                chat[cid]['txtMaybe']+=f'{m}. {us["name"]}\n'

        bot.send_message(message.chat.id, f"""
Maybe:
{chat[cid]['txtMaybe']}""")  # list of who will maybe attendance

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/sif")
def set_in_for(message):
    if roll_call_not_started(message):
        condition=True
        cid = message.chat.id
        msg = message.text
        arr=msg.split(" ")

        if len(arr)>1:
            comment=''
            chat[cid]["id"]+=1
            userFor = {"name":arr[1], "id":arr[1]}                        

            next((chat[cid]["usersComments"].remove(x) for x in chat[cid]['usersComments'] if x["name"] == userFor["name"]), None)

            if len(chat[cid]["usersNotAttendance"])>0:
                for us in chat[cid]["usersNotAttendance"]:
                    if us["name"]==userFor["name"] and us["id"]==userFor["id"]:
                        chat[cid]['usersNotAttendance'].remove(userFor)
                    elif us["name"]==userFor["name"] and us["id"]!=userFor["id"]:
                        bot.send_message(cid, "No duplicate proxy please :-), Thanks!")
                        return

            if len(chat[cid]["usersMaybeAttendance"])>0:
                for us in chat[cid]["usersMaybeAttendance"]:
                    if us["name"]==userFor["name"] and us["id"]==userFor["id"]:
                        chat[cid]['usersMaybeAttendance'].remove(userFor)
                    elif us["name"]==userFor["name"] and us["id"]!=userFor["id"]:
                        bot.send_message(cid, "No duplicate proxy please :-), Thanks!")
                        return

            if len(chat[cid]["usersAttendance"])>0:
                if next((bot.send_message(cid, "No duplicate proxy please :-), Thanks!") for x in chat[cid]['usersAttendance'] if x["name"] == userFor["name"] and x["id"] != userFor["id"]), None)!=None:
                    return
            
            if len(arr) > 2:                                  
                arr.pop(0)  
                arr.pop(0)
                comment=" ".join(arr)
            
            if 'usersAttendance' in chat[cid]:             
                if next((x for x in chat[cid]['usersAttendance'] if x["name"] == userFor["name"] and x["id"] == userFor["id"]), None)!=None:
                    if comment=="" and next((x for x in chat[cid]['usersComments'] if x["name"] == userFor["name"]), None)==None:
                        bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                        condition=False
                    else:
                        for us in chat[cid]['usersComments']:
                            if us["id"]==userFor["id"]:
                                chat[cid]['usersComments'].remove(us)
                else:
                    # If the variable doesn't exists creates one
                    chat[cid]['usersAttendance'] += [userFor]
                    print(userFor["name"]+" it's IN")
            else:                                          
                chat[cid]['usersAttendance'] = [userFor] 
                print(userFor["name"]+" it's IN")

            if comment!='':
                chat[cid]['usersComments']+=[{"id":userFor["id"], "comment":comment, "name":userFor["name"]}]
            else:
                for us in chat[cid]['usersComments']:
                    if us["id"]==userFor["id"]:
                        chat[cid]['usersComments'].remove(us)
            
            create_txt_list(cid)

            if condition==True and chat[cid]['shh']==False:
                backslash='\n'
                bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
        else:
            bot.send_message(message.chat.id, "Input username is missing")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/sof")
def set_out_for(message):
    if roll_call_not_started(message):
        condition=True
        cid = message.chat.id
        msg = message.text
        arr=msg.split(" ")

        if len(arr)>1:
            comment=''
            chat[cid]["id"]+=1
            userFor = {"name":arr[1], "id":arr[1]}                        

            next((chat[cid]["usersComments"].remove(x) for x in chat[cid]['usersComments'] if x["name"] == userFor["name"]), None)

            if len(chat[cid]["usersAttendance"])>0:
                for us in chat[cid]["usersAttendance"]:
                    if us["name"]==userFor["name"] and us["id"]==userFor["id"]:
                        chat[cid]['usersAttendance'].remove(userFor)
                    elif us["name"]==userFor["name"] and us["id"]!=userFor["id"]:
                        bot.send_message(cid, "No duplicate proxy please :-), Thanks!")
                        return

            if len(chat[cid]["usersMaybeAttendance"])>0:
                for us in chat[cid]["usersMaybeAttendance"]:
                    if us["name"]==userFor["name"] and us["id"]==userFor["id"]:
                        chat[cid]['usersMaybeAttendance'].remove(userFor)
                    elif us["name"]==userFor["name"] and us["id"]!=userFor["id"]:
                        bot.send_message(cid, "No duplicate proxy please :-), Thanks!")
                        return

            if len(chat[cid]["usersNotAttendance"])>0:
                if next((bot.send_message(cid, "No duplicate proxy please :-), Thanks!") for x in chat[cid]['usersNotAttendance'] if x["name"] == userFor["name"] and x["id"] != userFor["id"]), None)!=None:
                    return

            if len(arr)>2:                                  
                arr.pop(0)  
                arr.pop(0)
                comment=" ".join(arr)
            
            if 'usersNotAttendance' in chat[cid]:             
                if next((x for x in chat[cid]['usersNotAttendance'] if x["name"] == userFor["name"] and x["id"] == userFor["id"]), None)!=None:
                    if comment=="" and next((x for x in chat[cid]['usersComments'] if x["name"] == userFor["name"]), None)==None:
                        bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                        condition=False
                    else:
                        for us in chat[cid]['usersComments']:
                            if us["id"]==userFor["id"]:
                                chat[cid]['usersComments'].remove(us)
                else:
                    # If the variable doesn't exists creates one
                    chat[cid]['usersNotAttendance'] += [userFor]
                    print(userFor["name"]+" it's OUT")
            else:                                          
                chat[cid]['usersNotAttendance'] = [userFor] 
                print(userFor["name"]+" it's OUT")

            if comment!='':
                chat[cid]['usersComments']+=[{"id":userFor["id"], "comment":comment, "name":userFor["name"]}]
            else:
                for us in chat[cid]['usersComments']:
                    if us["id"]==userFor["id"]:
                        chat[cid]['usersComments'].remove(us)
            
            create_txt_list(cid)

            if condition==True and chat[cid]['shh']==False:
                backslash='\n'
                bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
        else:
            bot.send_message(message.chat.id, "Input username is missing")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/smf")
def set_maybe_for(message):
    if roll_call_not_started(message):
        condition=True
        cid = message.chat.id
        msg = message.text
        arr=msg.split(" ")
        
        if len(arr)>1:
            comment=''
            chat[cid]["id"]+=1
            userFor = {"name":arr[1], "id":arr[1]}                           

            next((chat[cid]["usersComments"].remove(x) for x in chat[cid]['usersComments'] if x["name"] == userFor["name"]), None)

            if len(chat[cid]["usersAttendance"])>0:
                for us in chat[cid]["usersAttendance"]:
                    if us["name"]==userFor["name"] and us["id"]==userFor["id"]:
                        chat[cid]['usersAttendance'].remove(userFor)
                    elif us["name"]==userFor["name"] and us["id"]!=userFor["id"]:
                        bot.send_message(cid, "No duplicate proxy please :-), Thanks!")
                        return

            if len(chat[cid]["usersNotAttendance"])>0:
                for us in chat[cid]["usersNotAttendance"]:
                    if us["name"]==userFor["name"] and us["id"]==userFor["id"]:
                        chat[cid]['usersNotAttendance'].remove(userFor)
                    elif us["name"]==userFor["name"] and us["id"]!=userFor["id"]:
                        bot.send_message(cid, "No duplicate proxy please :-), Thanks!")
                        return

            if len(chat[cid]["usersMaybeAttendance"])>0:
                if next((bot.send_message(cid, "No duplicate proxy please :-), Thanks!") for x in chat[cid]['usersMaybeAttendance'] if x["name"] == userFor["name"] and x["id"] != userFor["id"]), None)!=None:
                    return

            if len(arr)>2:                                  
                arr.pop(0)  
                arr.pop(0)
                comment=" ".join(arr)
            
            if 'usersMaybeAttendance' in chat[cid]:             
                if next((x for x in chat[cid]['usersMaybeAttendance'] if x["name"] == userFor["name"] and x["id"] == userFor["id"]), None)!=None:
                    if comment=="" and next((x for x in chat[cid]['usersComments'] if x["name"] == userFor["name"]), None)==None:
                        bot.send_message(cid,"That name already exists!")
                        condition=False
                    else:
                        for us in chat[cid]['usersComments']:
                            if us["id"]==userFor["id"]:
                                chat[cid]['usersComments'].remove(us)
                else:
                    # If the variable doesn't exists creates one
                    chat[cid]['usersMaybeAttendance'] += [userFor]
                    print(userFor["name"]+" it's MAYBE")
            else:                                          
                chat[cid]['usersMaybeAttendance'] = [userFor] 
                print(userFor["name"]+" it's MAYBE")

            if comment!='':
                chat[cid]['usersComments']+=[{"id":userFor["id"], "comment":comment, "name":userFor["name"]}]
            else:
                for us in chat[cid]['usersComments']:
                    if us["id"]==userFor["id"]:
                        chat[cid]['usersComments'].remove(us)
        
            create_txt_list(cid)

            if condition==True and chat[cid]['shh']==False:
                backslash='\n'
                bot.send_message(cid, 
 f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
        else:
            bot.send_message(message.chat.id, "Input username is missing")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower()=="/set_title")  # SET TITLE COMMAND
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/st")
def set_title(message):
    if roll_call_not_started(message):
        cid = message.chat.id
        msg = message.text
        arr = msg.split(" ")
        user = message.from_user.first_name

        if len(arr) > 1:                                  # 
            arr.pop(0)  # Define a title for the roll
            title = " ".join(arr)                         #
            chat[cid]['title'] = title
            bot.send_message(cid, 'The roll call title is set to: '+ title)
            print(user+"-"+"The title has change to "+title)
        
        else:
            title='<Empty>'
            chat[cid]['title'] = title
            bot.send_message(cid, 'The roll call title is set to: '+ title)
            print(user+"The title has change to "+title)

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")  #START ROLL CALL COMMAND
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].split("@")[0].lower() == "/erc")
def end_roll_call(message):

    if admin_rights(message):
        if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator']:
            bot.send_message(message.chat.id, "Error - user does not have sufficient permissions for this operation")
            print(admin_rights(message))
            return

    if roll_call_not_started(message):
        
        cid=message.chat.id
        chat['count']=0

        if chat[cid]['txtIn']=='Nobody':
            chat[cid]['txtIn']=""
        if chat[cid]['txtOut']=='Nobody':
            chat[cid]['txtOut']=""
        if chat[cid]['txtMaybe']=='Nobody':
            chat[cid]['txtMaybe']=""

        bot.send_message(message.chat.id, "Roll ended!")
        backslash='\n'
        bot.send_message(cid, 
f"""{chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else f'{"In:"+backslash+"Nobody"+backslash+backslash}'}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else f'{"Out:"+backslash+"Nobody"+backslash+backslash}'}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else f'{"Maybe:"+backslash+"Nobody"}'}""")

        print("The roll call "+chat[cid]['title']+" has ended")
        
        chat[cid].pop('txtIn')
        chat[cid].pop('txtOut')
        chat[cid].pop('txtMaybe')
        chat[cid].pop('title')
        chat[cid].pop('usersNotAttendance')
        chat[cid].pop('usersMaybeAttendance')
        chat[cid].pop('usersAttendance')
        chat[cid].pop('usersComments')
if __name__ == '__main__':
    bot.infinity_polling()