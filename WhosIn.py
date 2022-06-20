from re import IGNORECASE
from turtle import back
import telebot
from telebot.types import ReplyKeyboardMarkup
from telebot.types import ReplyKeyboardRemove
from config import API_KEY

bot = telebot.TeleBot(API_KEY)

chat={}

def roll_call_already_started(message):
    try:
        if chat[message.chat.id]['title']!='':
            bot.send_message(message.chat.id, f"Roll call with title {chat[message.chat.id]['title']} is still in progress")
            contin=False
            return contin
    except:
        contin=True
        return contin

def roll_call_not_started(message):
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
            return contin
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
        return contin

def create_txt_list(cid):
    chat[cid]['txtIn']=''
    i=0
    for us in chat[cid]['usersAttendance']:
        i+=1
        if us in chat[cid]['usersComments']:
            comment=chat[cid]['usersComments'][us]
            chat[cid]['txtIn']+=f'{i}. {us} ({comment})\n'
        else:
            chat[cid]['txtIn']+=f'{i}. {us}\n'

    chat[cid]['txtOut']=''
    o=0
    for us in chat[cid]['usersNotAttendance']:
        o+=1
        if us in chat[cid]['usersComments']:
            comment=chat[cid]['usersComments'][us]
            chat[cid]['txtOut']+=f'{o}. {us} ({comment})\n'
        else:
            chat[cid]['txtOut']+=f'{o}. {us}\n'
    
    chat[cid]['txtMaybe']=''
    m=0
    for us in chat[cid]['usersMaybeAttendance']:
        m+=1
        if us in chat[cid]['usersComments']:
            comment=chat[cid]['usersComments'][us]
            chat[cid]['txtMaybe']+=f'{m}. {us} ({comment})\n'
        else:
            chat[cid]['txtMaybe']+=f'{m}. {us}\n'


@bot.message_handler(func=lambda message:message.text.lower()=="/start")  # START COMMAND
def welcome_and_explanation(message):
    markup = ReplyKeyboardMarkup(row_width=3)         
    markup.add('/start_roll_call', '/in', '/out', '/maybe', '/whos_in', '/end_roll_call')        
    bot.send_message(message.chat.id, '''
Hi! im Whos_in_bot!
Type /help to see all the commands
    ''', reply_markup=markup)


@bot.message_handler(func=lambda message:message.text.lower()=="/help")  # HELP COMMAND
def welcome_and_explanation(message):
    bot.send_message(message.chat.id, '''
The commands are:
-/start  || To start the bot
-/help   || To see the commands
-/start_roll_call || To start a new roll call (optional title)
-/in || To let everybody know you will be attending (optional comment)
-/out || To let everybody know you wont be attending (optional comment)
-/maybe  || To let everybody know you dont know (optional comment)
-/whos_in || list attendees
-/whos_out || list attendees
-/whos_maybe || list attendees
-/set_title "title" || To set a title for the current roll call
-/set_in_for "name" || Allows you to respond for another user
-/set_out_for "name" || Allows you to respond for another user
-/set_maybe_for "name" || Allows you to respond for another user
-/shh || to apply minimum output for each command
-/louder || to disable minimum output for each command
-/end_roll_call   || to end a roll call
    ''')
@bot.message_handler(func=lambda message:message.text.lower()=="/start_roll_call")  # START ROLL CALL COMMAND
def start_roll_call(message):

    contin=roll_call_already_started(message)

    if contin==True:
        cid = message.chat.id  # chat id
        msg = message.text
        arr = msg.split(" ")
        title=''

        if len(arr) > 1:                      
            arr.pop(0)  # Define a title for the roll
            title = ' '.join(arr)

        if title == '':
            title = '<Empty>'  # if title is empty default title is Roll name

        chat[cid] = {}
        chat[cid]['txtIn']='Nobody'
        chat[cid]['txtOut']='Nobody'
        chat[cid]['txtMaybe']='Nobody'
        chat[cid]['title'] = title
        chat[cid]['usersNotAttendance']=[]
        chat[cid]['usersMaybeAttendance']=[]
        chat[cid]['usersAttendance']=[]
        chat[cid]['usersComments']={}
        chat[cid]['shh']=False

        bot.send_message(message.chat.id, "Roll call with title: "+title+" started!")
        print("A roll call with title "+title+"has started")

@bot.message_handler(func=lambda message:message.text.lower()=="/shh")
def shh(message):
    chat[message.chat.id]['shh']=True
    bot.send_message(message.chat.id, "Ok, i will keep quiet!")

@bot.message_handler(func=lambda message:message.text.lower()=="/louder")
def shh(message):
    chat[message.chat.id]['shh']=False
    bot.send_message(message.chat.id, "Ok, i can hear you!")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/in")  # IN COMMAND
def in_user(message):
    contin=roll_call_not_started(message)

    if contin==True:
        condition=True
        msg = message.text
        cid = message.chat.id  # chat id
        user = message.from_user.first_name  # name of user who attendance
        comment = ''
        arr = msg.split(" ")
        if len(arr) > 1:                                    
            arr.pop(0)
            comment = ' '.join(arr)

        #CHECK USER IN OTHER LIST AND ADD IT
        if 'usersNotAttendance' in chat[cid]:
            if user in chat[cid]['usersNotAttendance']:
                chat[cid]['usersNotAttendance'].remove(user)
                print("Usuario removido")
        if 'usersMaybeAttendance' in chat[cid]:
            if user in chat[cid]['usersMaybeAttendance']:
                chat[cid]['usersMaybeAttendance'].remove(user)
                print("Usuario removido")
        if 'usersAttendance' in chat[cid]:             
            if user in chat[cid]['usersAttendance']:
                if comment=="" and user not in chat[cid]['usersComments']:
                    bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                    condition=False
                else:
                    chat[cid]['usersComments'].pop(user, None)
            else:
                # If the variable doesn't exists creates one
                chat[cid]['usersAttendance'] += [user]
                print(user+" it's IN")
        else:                                          
            chat[cid]['usersAttendance'] = [user]
            print(user+" it's IN")
        #CHECK USER IN OTHER LIST AND ADD IT

        if comment!='':
            chat[cid]['usersComments'][user]=comment
        else:
            chat[cid]['usersComments'].pop(user, None)
        
        create_txt_list(cid)
        
        if condition==True and chat[cid]['shh']==False:
            backslash='\n'
            bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/out")  # OUT COMMAND
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            pass
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
    else:
        condition=True
        comment = ''
        msg = message.text
        cid = message.chat.id
        user = message.from_user.first_name  # name of user who not attendance

        if 'usersAttendance' in chat[cid]:
            for us in chat[cid]['usersAttendance']:
                if us==user:
                    chat[cid]['usersAttendance'].remove(us)
        if 'usersMaybeAttendance' in chat[cid]:
            for us in chat[cid]['usersMaybeAttendance']:
                if us==user:
                    chat[cid]['usersMaybeAttendance'].remove(us)


        if 'usersNotAttendance' in chat[cid]:           
            if user in chat[cid]['usersNotAttendance']:
                if comment=="" and user not in chat[cid]['usersComments']:
                    bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                    condition=False
                else:
                    chat[cid]['usersComments'].pop(user, None)
            else:
                chat[cid]['usersNotAttendance'] += [user]     
                print(user+" it's OUT")
        else:  # If the variable doesn't exists creates one
            chat[cid]['usersNotAttendance'] = [user]  
            print(user+" it's OUT")    

        arr = msg.split(" ")

        if len(arr) > 1:             
            arr.pop(0)  # Define the comment
            comment = ' '.join(arr)

        # text with a list of users
        
        if comment!='':
            chat[cid]['usersComments'][user]=comment
        else:
            chat[cid]['usersComments'].pop(user, None)

        create_txt_list(cid)

        if condition==True and chat[cid]['shh']==False:
            backslash='\n'
            bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/maybe")  # MAYBE COMMAND
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=-False
    if contin==True:
        condition=True
        comment=''
        msg = message.text
        cid = message.chat.id
        user = message.from_user.first_name  # name of user who attendance


        arr = msg.split(" ")

        if len(arr) > 1:                                    
            arr.pop(0)  # Define the comment
            comment = ' '.join(arr)

        if 'usersAttendance' in chat[cid]:
            for us in chat[cid]['usersAttendance']:
                if us==user:
                    chat[cid]['usersAttendance'].remove(us)
        if 'usersNotAttendance' in chat[cid]:
            for us in chat[cid]['usersNotAttendance']:
                if us==user:
                    chat[cid]['usersNotAttendance'].remove(us)

        if 'usersMaybeAttendance' in chat[cid]:             
            if user in chat[cid]['usersMaybeAttendance']:
                if comment=="" and user not in chat[cid]['usersComments']:
                    bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                    condition=False
                else:
                    chat[cid]['usersComments'].pop(user, None)
            else:
                chat[cid]['usersMaybeAttendance'] += [user]      
                print(user+" it's MAYBE") 
        else:                                               # if the variable doesn't exist it creates one
            chat[cid]['usersMaybeAttendance'] = [user]     
            print(user+" it's MAYBE")   
        # text list of who maybe attendance
       
        if comment!='':
            chat[cid]['usersComments'][user]=comment
        else:
            chat[cid]['usersComments'].pop(user, None)

        create_txt_list(cid)

        if condition==True and chat[cid]['shh']==False:
            backslash='\n'
            bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")

@bot.message_handler(func=lambda message:message.text.lower()=="/whos_in")  # WHOS IN COMMAND
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            condition=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        condition=False
    if condition==True:
        cid = message.chat.id
        
        if len(chat[cid]['usersAttendance'])==0:
            chat[cid]['txtIn']='Nobody'
        else:
            chat[cid]['txtIn']=''
            i=0
            for us in chat[cid]['usersAttendance']:
                i+=1
                print(i)
                if us in chat[cid]['usersComments']:
                    comment=chat[cid]['usersComments'][us]
                    chat[cid]['txtIn']+=f'{i}.{us} ({comment})\n'
                else:
                    chat[cid]['txtIn']+=f'{i}.{us}\n'
        bot.send_message(message.chat.id, f"""
In:
{chat[cid]['txtIn']}""")  # list of who will attendance


@bot.message_handler(func=lambda message:message.text.lower()=="/whos_out")  # WHOS IN COMMAND
def in_user(message):
    cid = message.chat.id
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
    if contin==True:
        if len(chat[cid]['usersNotAttendance'])==0:
            chat[cid]['txtOut']='Nobody'
        else:
            chat[cid]['txtOut']=''
            o=0
            for us in chat[cid]['usersNotAttendance']:
                o+=1
                if us in chat[cid]['usersComments']:
                    comment=chat[cid]['usersComments'][us]
                    chat[cid]['txtOut']+=f'{o}.{us} ({comment})\n'
                else:
                    chat[cid]['txtOut']+=f'{o}.{us}\n'
        
        bot.send_message(message.chat.id, f"""
Out:
{chat[cid]['txtOut']}""")  # list of who will not attendance


@bot.message_handler(func=lambda message:message.text.lower()=="/whos_maybe")  # WHOS IN COMMAND
def in_user(message):
    cid = message.chat.id
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
    if contin==True:
        if len(chat[cid]['usersMaybeAttendance'])==0:
            chat[cid]['txtMaybe']='Nobody'
        else:
            chat[cid]['txtMaybe']=''
            m=0
            for us in chat[cid]['usersMaybeAttendance']:
                m+=1
                if us in chat[cid]['usersComments']:
                    comment=chat[cid]['usersComments'][us]
                    chat[cid]['txtMaybe']+=f'{m}.{us} ({comment})\n'
                else:
                    chat[cid]['txtMaybe']+=f'{m}.{us}\n'
        bot.send_message(message.chat.id, f"""
Maybe:
{chat[cid]['txtMaybe']}""")  # list of who will maybe attendance


@bot.message_handler(func=lambda message:message.text.lower()=="/set_title")  # SET TITLE COMMAND
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
    if contin==True:
        cid = message.chat.id
        msg = message.text
        arr = msg.split(" ")

        if len(arr) > 1:                                  # 
            arr.pop(0)  # Define a title for the roll
            title = " ".join(arr)                         #
            chat[cid]['title'] = title
            bot.send_message(cid, 'The roll call title is set to:'+ title)
            print(user+"The title has change to "+title)
        
        else:
            title='<Empty>'
            chat[cid]['title'] = title
            bot.send_message(cid, 'The roll call title is set to:'+ title)
            print(user+"The title has change to "+title)


@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/set_in_for")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/sif")
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
    if contin==True:
        print(message.text)
        condition=True
        cid = message.chat.id
        msg = message.text
        arr=msg.split(" ")
        if len(arr)>1:
            comment=''
            userFor = arr[1]                        

            if 'usersNotAttendance' in chat[cid]:
                for us in chat[cid]['usersNotAttendance']:
                    if us==userFor:
                        chat[cid]['usersNotAttendance'].remove(us)
                        print("Usuario removido")
            if 'usersMaybeAttendance' in chat[cid]:
                for us in chat[cid]['usersMaybeAttendance']:
                    if us==userFor:
                        chat[cid]['usersMaybeAttendance'].remove(us)

            if len(arr) > 2:                                  
                arr.pop(0)  
                arr.pop(0)
                comment=" ".join(arr)
            
            if 'usersAttendance' in chat[cid]:             
                if userFor in chat[cid]['usersAttendance']:
                    if comment=="" and userFor not in chat[cid]['usersComments']:
                        bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                        condition=False
                    else:
                        chat[cid]['usersComments'].pop(userFor, None)
                else:
                    # If the variable doesn't exists creates one
                    chat[cid]['usersAttendance'] += [userFor]
                    print(userFor+" it's IN")
            else:                                          
                chat[cid]['usersAttendance'] = [userFor] 
                print(userFor+" it's IN")

            if comment!='':
                chat[cid]['usersComments'][userFor]=comment
            else:
                chat[cid]['usersComments'].pop(userFor, None)
            chat[cid]['txtIn']=''
            
            create_txt_list(cid)

            if condition==True and chat[cid]['shh']==False:
                backslash='\n'
                bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
        else:
            bot.send_message(message.chat.id, "Input username is missing")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/set_out_for")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/sof")
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
    if contin==True:
        condition=True
        cid = message.chat.id
        msg = message.text
        arr=msg.split(" ")
        if len(arr)>1:
            comment=''
            userFor = arr[1]                        

            if 'usersAttendance' in chat[cid]:
                for us in chat[cid]['usersAttendance']:
                    if us==userFor:
                        chat[cid]['usersAttendance'].remove(us)
                        print("Usuario removido")
            if 'usersMaybeAttendance' in chat[cid]:
                for us in chat[cid]['usersMaybeAttendance']:
                    if us==userFor:
                        chat[cid]['usersMaybeAttendance'].remove(us)

            if len(arr) > 1:                                  
                arr.pop(0)  
                arr.pop(0)
                comment=" ".join(arr)
            
            if 'usersNotAttendance' in chat[cid]:             
                if userFor in chat[cid]['usersNotAttendance']:
                    if comment=="" and userFor not in chat[cid]['usersComments']:
                        bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                        condition=False
                    else:
                        chat[cid]['usersComments'].pop(userFor, None)
                else:
                    # If the variable doesn't exists creates one
                    chat[cid]['usersNotAttendance'] += [userFor]
                    print(userFor+" it's OUT")
            else:                                          
                chat[cid]['usersNotAttendance'] = [userFor] 
                print(userFor+" it's OUT")

            if comment!='':
                chat[cid]['usersComments'][userFor]=comment
            else:
                chat[cid]['usersComments'].pop(userFor, None)
            
            create_txt_list(cid)

            if condition==True and chat[cid]['shh']==False:
                backslash='\n'
                bot.send_message(cid, 
f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
        else:
            bot.send_message(message.chat.id, "Input username is missing")

@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/set_maybe_for")
@bot.message_handler(func=lambda message:(message.text.split(" "))[0].lower() == "/smf")
def in_user(message):
    try:
        if chat[message.chat.id]['title']!='':
            contin=True
    except:
        bot.send_message(message.chat.id, "Roll call is not active")
        contin=False
    if contin==True:
        condition=True
        cid = message.chat.id
        msg = message.text
        arr=msg.split(" ")
        if len(arr)>1:
            comment=''
            userFor = arr[1]                        

            if 'usersNotAttendance' in chat[cid]:
                for us in chat[cid]['usersNotAttendance']:
                    if us==userFor:
                        chat[cid]['usersNotAttendance'].remove(us)
                        print("Usuario removido")
            if 'usersAttendance' in chat[cid]:
                for us in chat[cid]['usersAttendance']:
                    if us==userFor:
                        chat[cid]['usersAttendance'].remove(us)

            if len(arr) > 1:                                  
                arr.pop(0)  
                arr.pop(0)
                comment=" ".join(arr)
            
            if 'usersMaybeAttendance' in chat[cid]:             
                if userFor in chat[cid]['usersMaybeAttendance']:
                    if comment=="" and userFor not in chat[cid]['usersComments']:
                        bot.send_message(cid,"No duplicate proxy please :-), Thanks!")
                        condition=False
                    else:
                        chat[cid]['usersComments'].pop(userFor, None)
                else:
                    # If the variable doesn't exists creates one
                    chat[cid]['usersMaybeAttendance'] += [userFor]
                    print(userFor+" it's MAYBE")
            else:                                          
                chat[cid]['usersMaybeAttendance'] = [userFor] 
                print(userFor+" it's MAYBE")

            if comment!='':
                chat[cid]['usersComments'][userFor]=comment
            else:
                chat[cid]['usersComments'].pop(userFor, None)
        
            create_txt_list(cid)

            if condition==True and chat[cid]['shh']==False:
                backslash='\n'
                bot.send_message(cid, 
 f"""Title - {chat[cid]['title']}:\n{'In:'+backslash+chat[cid]['txtIn']+backslash if chat[cid]['txtIn']!='' else ''}{'Out:'+backslash+chat[cid]['txtOut']+backslash if chat[cid]['txtOut']!='' else ''}{'Maybe:'+backslash+chat[cid]['txtMaybe'] if chat[cid]['txtMaybe']!='' else ''}""")
        else:
            bot.send_message(message.chat.id, "Input username is missing")

@bot.message_handler(func=lambda message:message.text.lower()=="/end_roll_call")  #START ROLL CALL COMMAND
def start_roll_call(message):
    contin=roll_call_not_started(message)
    if contin==True:
        
        cid=message.chat.id
        chat['count']=0

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
