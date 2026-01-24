import logging
import datetime
import re
import asyncio
import json

from telebot.async_telebot import AsyncTeleBot

import pytz

from config import TELEGRAM_TOKEN, ADMINS

from exceptions import *
from models import RollCall, User
from functions import *
from check_reminders import start
from rollcall_manager import manager
import traceback
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from db import add_or_update_proxy_user
from db import increment_user_stat, increment_rollcall_stat

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

logging.info("Bot already started")

def format_mention(user: User) -> str:
    """
    Build a Telegram mention string.
    Real users (int id): use @username or tg://user link.
    Proxy users (str id): just show their name.
    """
    if isinstance(user.user_id, int):
        if user.username:
            return f"@{user.username}"
        return f"[{user.name}](tg://user?id={user.user_id})"
    return user.name

def get_rollcall_db_id(rc: RollCall) -> int:
    """
    Helper to get DB rollcall id from RollCall object.
    Assumes rc has .db_id or similar; if not, extend RollCall to store it.
    """
    # If your RollCall already exposes db_id or rollcall_id, use that.
    # Fallback: manager can be extended later if needed.
    return getattr(rc, "db_id", None)


# START COMMAND, SIMPLE TEXT
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message):
    cid = message.chat.id

    # Initialize chat in manager
    manager.get_chat(cid)

    # CHECK FOR ADMIN RIGHTS
    if await admin_rights(message, manager) == False:
        await bot.send_message(message.chat.id, "Error - User does not have sufficient permissions for this operation")
        return

    # START MSG
    await bot.send_message(message.chat.id, 'Hi! im RollCall!\n\nUse /help to see all the commands')

# HELP COMMAND WITH ALL THE COMMANDS
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/help")
async def help_commands(message):
  #  await bot.send_message(message.chat.id, '''The commands are:\n-/start  - To start the bot\n-/help - To see the commands\n-/start_roll_call - To start a new roll call (optional title)\n-/in - To let everybody know you will be attending (optional comment)\n-/out - To let everybody know you won't be attending (optional comment)\n-/maybe - To let everybody know you don't know (optional comment)\n-/whos_in - List of those who will go\n-/whos_out - List of those who will not go\n-/whos_maybe - List of those who maybe will go\n-/set_title - To set a title for the current roll call\n-/set_in_for - Allows you to respond for another user\n-/set_out_for - Allows you to respond for another user\n-/set_maybe_for - Allows you to respond for another user\n-/shh - to apply minimum output for each command\n-/louder - to disable minimum output for each command\n-/set_limit - To set a limit to IN state\n-/end_roll_call - To end a roll call\n-/set_rollcall_time - To set a finalize time to the current rc. Accepts 2 parameters date (DD-MM-YYYY) and time (H:M). Write cancel to delete it\n-/set_rollcall_reminder - To set a reminder before the ends of the rc. Accepts 1 parameter, hours as integers. Write 'cancel' to delete the reminder\n-/timezone - To set your timezone, accepts 1 parameter (Continent/Country) or (Continent/State)\n-/when - To check the start time of a roll call\n-/location - To check the location of a roll call''')
    await bot.send_message(message.chat.id, '''**RollCall Bot Commands** 

**Basic:**
/start     - Start the bot  
/help      - Show this help
/start_roll_call (/src)  - Start new rollcall (optional title)

**Status Updates:**
/in        - Mark yourself attending (optional comment ::N)
/out       - Mark yourself not attending (optional comment ::N) 
/maybe     - Mark yourself maybe (optional comment ::N)

**Lists:**
/rollcalls (/r)    - List all active rollcalls with IDs
/whos_in (/wi)     - Show attending (::N for specific)
/whos_out (/wo)    - Show not attending (::N)
/whos_maybe (/wm)  - Show maybe (::N)
/whos_waiting (/ww)- Show waitlist (::N)
/panel ::N - Show control panel with buttons for rollcall #N

**Admin Commands:**
/end_roll_call (/erc) ::N      - End rollcall #N
/set_title (/st) ::N "title"   - Set title for #N
/set_limit (/sl) ::N limit     - Set max IN limit for #N
/delete_user ::N username      - Remove user from #N (admin only)
/set_admins                    - Enable admin mode (group admin only)
/unset_admins                  - Disable admin mode

**Proxy Voting (for others):**
/set_in_for (/sif) ::N username     - Mark other user IN
/set_out_for (/sof) ::N username    - Mark other user OUT
/set_maybe_for (/smf) ::N username  - Mark other user MAYBE

**Event Management:**
/set_rollcall_time (/srt) ::N "DD-MM-YYYY H:M"  - Set event time ('cancel' to clear)
/set_rollcall_reminder (/srr) ::N hours         - Reminder hours before ('cancel' to clear)  
/event_fee (/ef) ::N amount                    - Set total event fee
/individual_fee (/if) ::N                      - Calculate per-person fee
/when (/w) ::N                                 - Show event time
/location (/loc) ::N "place"                   - Set location

**Chat Settings:**
/shh                 - Silent mode (no lists after responses)
/louder              - Resume full output  
/timezone (/tz) "Asia/Kolkata" - Set timezone

**Super Admin:**
/broadcast "message"  - Send to all bot chats (super admin only)

**Info:**
/stats (/s) @username or group or firstname or top - Bot usage statistics for real telegram users , not for proxy users
/version (/v) - Show current version

**Usage:** Use `::N` to target rollcall #N (see /rollcalls)
''', parse_mode='none')


# SET ADMIN RIGHTS TO TRUE
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):
    cid = message.chat.id
    
    # Test if user has permissions to use this command
    member = await bot.get_chat_member(cid, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await bot.send_message(message.chat.id, "You don't have permissions to use this command :(")
        return
    
    manager.set_admin_rights(cid, True)
    await bot.send_message(cid, 'Admin permissions activated')

# SET ADMIN RIGHTS TO FALSE
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):
    cid = message.chat.id
    
    # Test if user has permissions to use this command
    member = await bot.get_chat_member(cid, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await bot.send_message(message.chat.id, "You don't have permissions to use this command :(")
        return
    
    manager.set_admin_rights(cid, False)
    await bot.send_message(cid, 'Admin permissions disabled')

# SEND ANNOUNCEMENTS TO ALL GROUPS
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):
    if len(message.text.split(" ")) < 2:
        await bot.send_message(message.chat.id, "Message is missing")
        return

    msg = message.text.split(" ")[1:]

    try:
        with open('./database.json', 'r') as read_file:
            data = json.load(read_file)
    except Exception as e:
        print(traceback.format_exc())
        print(e)
        return

    for k in data:
        try:
            await bot.send_message(int(k["chat_id"]), " ".join(msg))
        except:
            pass

# ADJUST TIMEZONE
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg = message.text
        cid = message.chat.id

        if len(msg.split(" ")) < 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")
        if len(msg.split(" ")[1].split("/")) != 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")

        # Initialize chat
        manager.get_chat(cid)

        # Formatting timezone
        response = auto_complete_timezone(" ".join(msg.split(" ")[1:]))

        if response != None:
            await bot.send_message(message.chat.id, f"Your timezone has been set to {response}")
            manager.set_timezone(cid, response)
        else:
            await bot.send_message(message.chat.id, f"Given timezone is invalid , check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode='HTML')

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(cid, e)

# Version command
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/version")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/v")
async def version_command(message):
    file = open('./version.json')
    data = json.load(file)
    for i in range(0, len(data)):
        version = data[-1-i]
        if version["DeployedOnProd"] == 'Y':
            txt = ''
            txt += f'Version: {version["Version"]}\nDescription: {version["Description"]}\nDeployed: {version["DeployedOnProd"]}\nDeployed datetime: {version["DeployedDatetime"]}'
            await bot.send_message(message.chat.id, txt)
            break

# GET ALL ROLLCALLS OF THE CURRENT CHAT
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/rollcalls")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/r")
async def show_reminders(message):
    cid = message.chat.id
    rollcalls = manager.get_rollcalls(cid)

    if len(rollcalls) == 0:
        await bot.send_message(cid, "Rollcall list is empty")
        return

    for rollcall in rollcalls:
        id = rollcalls.index(rollcall) + 1
        await bot.send_message(cid, f"Rollcall number {id}\n\n" + rollcall.allList().replace("__RCID__", str(id)))

# START A ROLL CALL
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):
    cid = message.chat.id
    msg = message.text
    title = ''

    # Save chat to database.json for broadcast
    with open('./database.json', 'r') as read_file:
        database = json.load(read_file)
        read_file.close()
    
    cond = True
    for i in database:
        if int(i['chat_id']) == cid:
            cond = False

    if cond == True:
        database.append({'chat_id': cid})
        with open('./database.json', 'w') as write_file:
            json.dump(database, write_file)

    try:
        rollcalls = manager.get_rollcalls(cid)
        
        if len(rollcalls) >= 3:
            raise amountOfRollCallsReached("Allowed Maximum number of active roll calls per group is 3.")

        # CHECK IF ADMIN_RIGHTS ARE ON
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        # SET THE RC TITLE
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            title = ' '.join(arr)
        else:
            title = '<Empty>'

        rc_index = len(rollcalls)  # Index before adding
        # Create new rollcall using manager
        rc = manager.add_rollcall(cid, title)
        markup = await get_status_keyboard(rc_index+1)
        await bot.send_message(message.chat.id, f"Roll call '{title}' started! ID: {rc_index+1}\nUse buttons below:", reply_markup=markup)
        #await bot.send_message(message.chat.id, f"Roll call with title: {title} started!\nRollcall id is set to {rc_index + 1}\nTo vote for this RollCall, please use ::RollCallID eg. /in ::{rc_index + 1}")

    except Exception as e:
        await bot.send_message(cid, e)

# SET A ROLLCALL START TIME
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if len(message.text.split(" ")) == 1:
            raise parameterMissing("invalid datetime format, refer help section for details")
        cid = message.chat.id
        msg = message.text
        rc_number = 0
        pmts = msg.split(" ")[1:]
        # IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")
        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        rc = manager.get_rollcall(cid, rc_number)
        # CANCEL THE CURRENT REMINDER TIME
        if (pmts[0]).lower() == 'cancel':
            rc.finalizeDate = None
            rc.reminder = None
            rc.save()
            await bot.send_message(message.chat.id, "Reminder time is canceled.")
            return
        # PARSING INPUT DATETIME
        input_datetime = " ".join(pmts).strip()
        tz = pytz.timezone(rc.timezone)
        date = datetime.datetime.strptime(input_datetime, "%d-%m-%Y %H:%M")
        date = tz.localize(date)
        now_date_string = datetime.datetime.now(pytz.timezone(rc.timezone)).strftime("%d-%m-%Y %H:%M")
        now_date = datetime.datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
        now_date = tz.localize(now_date)
        # ERROR FOR INVALID DATETIME
        if now_date > date:
            raise timeError("Please provide valid future datetime.")
        if rc.finalizeDate == None:
            rc.finalizeDate = date
            changed = False
            if rc.reminder != None:
                rc.reminder = None
                changed = True
            
            rc.save()
            backslash = '\n'
            await bot.send_message(cid, f"Event notification time is set to {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')} {rc.timezone} {backslash*2+'Reminder has been reset!' if changed else ''}")
            
            rollcalls = manager.get_rollcalls(cid)
            asyncio.create_task(start(rollcalls, rc.timezone, cid))
        else:
            rc.finalizeDate = date
            changed = False
            if rc.reminder != None:
                rc.reminder = None
                changed = True
            
            rc.save()
            backslash = '\n'
            await bot.send_message(cid, f"Event notification time is set to {date.strftime('%d-%m-%Y %H:%M')} {rc.timezone} {backslash*2+'Reminder has been reset!' if changed else ''}")
        
        # NEW: Time update notification
        await bot.send_message(
            cid,
            f"Time updated for '{rc.title}' (ID: {rc_number + 1}) → {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')} {rc.timezone}."
        )
        
    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

# SET A ROLLCALL REMINDER
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_reminder")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srr")
async def reminder(message):
    cid = message.chat.id
    msg = message.text
    rc_number = 0
    pmts = msg.split(" ")[1:]
    
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        # IF NUMBER HAS 00:00 FORMAT
        if len(pmts) > 0 and pmts[0] != 'cancel' and len(pmts[0]) == 2:
            if pmts[0][0] == "0":
                pmts[0] = pmts[0][1]

        # IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)

        # IF NOT EXISTS A FINALIZE DATE, RAISE ERROR
        if rc.finalizeDate == None:
            raise parameterMissing('First you need to set a finalize time for the current rollcall')

        # CANCEL REMINDER
        if len(pmts) > 0 and pmts[0].lower() == 'cancel':
            rc.reminder = None
            rc.save()
            await bot.send_message(message.chat.id, "Reminder Notification is canceled.")
            return

        # IF THERE ARE NOT PARAMETERS RAISE ERROR
        if len(pmts) == 0 or not pmts[0].isdigit(): 
            raise parameterMissing("The format is /set_rollcall_reminder hours")

        # IF HOUR IS NOT POSITIVE
        if int(pmts[0]) < 1:
            raise incorrectParameter("Hours must be higher than 1")

        hour = pmts[0]
        
        if rc.finalizeDate - datetime.timedelta(hours=int(hour)) < datetime.datetime.now(pytz.timezone(rc.timezone)):
            raise incorrectParameter("Reminder notification time is less than current time, please set it correctly.")

        rc.reminder = int(hour) if hour != 0 else None
        rc.save()
        await bot.send_message(cid, f'I will remind {hour}hour/s before the event! Thank you!')
        
    except ValueError as e:
        print(traceback.format_exc())
        await bot.send_message(cid, 'The correct format is /set_rollcall_reminder HH')
    except Exception as e:
        await bot.send_message(cid, e)

# SET AN EVENT_FEE
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/event_fee")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/ef")
async def event_fee(message):
    cid = message.chat.id
    pmts = message.text.split(" ")[1:]
    rc_number = 0
    
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        # IF RC_NUMBER IS SPECIFIED, STORE IT
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        event_price = " ".join(pmts)
        event_price_number = re.findall('[0-9]+', event_price)
        
        if len(event_price_number) == 0 or int(event_price_number[0]) <= 0:
            raise incorrectParameter("The correct format is '/event_fee Integer' Where 'Integer' it's up to 0 number")

        rc.event_fee = event_price
        rc.save()

        await bot.send_message(cid, f"Event Fee set to {event_price}\n\nAdditional unknown/penalty fees are not included and needs to be handled separately.")

    except Exception as e:
        await bot.send_message(cid, e)

# CHECK HOW MUCH IS INDIVIDUAL FEE
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/individual_fee")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/if")
async def individual_fee(message):
    cid = message.chat.id
    pmts = message.text.split(" ")[1:]
    rc_number = 0
    
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        # IF RC_NUMBER IS SPECIFIED, STORE IT
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        in_list = len(rc.inList)
        event_price = int(re.sub(r'[^0-9]', "", str(rc.event_fee)))

        if in_list > 0:
            individual_fee = round(event_price / in_list, 2)
        else:
            individual_fee = 0

        await bot.send_message(cid, f'Individual fee is {individual_fee}')
          
    except Exception as e:
        await bot.send_message(cid, e)

# CHECK WHEN A ROLLCALL WILL START
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/when")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/w")
async def when(message):
    cid = message.chat.id
    pmts = message.text.split(" ")
    rc_number = 0

    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        # IF RC_NUMBER IS SPECIFIED, STORE IT
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        
        if rc.finalizeDate == None:
            raise incorrectParameter("There is no start time for the event")

        await bot.send_message(cid, f"The event with title {rc.title} will start at {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')}!")

    except Exception as e:
        await bot.send_message(cid, e)

# SET A LOCATION FOR A ROLLCALL
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if len(message.text.split(" ")) < 2:
            raise incorrectParameter("The correct format is /location <place>")
        
        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")[1:]
        rc_number = 0
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")
        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        place = " ".join(pmts)
        rc = manager.get_rollcall(cid, rc_number)
        rc.location = place
        rc.save()
        
        # NEW: Location update notification
        await bot.send_message(
            cid,
            f"Location updated for '{rc.title}' (ID: {rc_number + 1}) → {place}."
        )
    except Exception as e:
        await bot.send_message(cid, e)

# SET A LIMIT FOR IN LIST
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_limit")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sl")
async def wait_limit(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if len(message.text.split(" ")) <= 1 or int(message.text.split(" ")[1]) < 0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        msg = message.text
        cid = message.chat.id
        pmts = msg.split(" ")[1:]
        rc_number = 0

        limit = int(pmts[0])

        try:
            if "::" in pmts[-1]:
                try:
                    rc_number = int(pmts[-1].replace("::", "")) - 1
                    del pmts[-1]
                except:
                    raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        except:
            pass

        rc = manager.get_rollcall(cid, rc_number)

        # Capture current state for notifications
        old_limit = rc.inListLimit
        was_full = old_limit is not None and len(rc.inList) >= int(old_limit)

        rc.inListLimit = limit
        rc.save()
        logging.info(f"Max limit of attendees is set to {limit}")
        await bot.send_message(cid, f"Max limit of attendees is set to {limit}")

        # Now rebalance IN and WAITING
        moved_from_in_to_wait = []
        moved_from_wait_to_in = []

        # MOVING USERS IF IN LIST HAS ALREADY REACHED THE LIMIT
        if len(rc.inList) > limit:
            moved_from_in_to_wait = rc.inList[limit:]
            rc.waitList.extend(rc.inList[limit:])
            rc.inList = rc.inList[:limit]
            rc.save()
        elif len(rc.inList) < limit:
            available_slots = limit - len(rc.inList)
            moved_from_wait_to_in = rc.waitList[:available_slots]
            rc.inList.extend(rc.waitList[:available_slots])
            rc.waitList = rc.waitList[available_slots:]
            rc.save()

        # NEW: Notifications for limit changes

        # IN -> WAITING
        if moved_from_in_to_wait:
            names = ", ".join(u.name for u in moved_from_in_to_wait)
            await bot.send_message(
                cid,
                f"{names} moved from IN to WAITING because limit was set to {limit} for '{rc.title}' (ID: {rc_number + 1})."
            )

        # WAITING -> IN (short + proxy owner tagging)
        if moved_from_wait_to_in:
            for u in moved_from_wait_to_in:
                # Short IN message
                if isinstance(u.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention(u)} → IN (from WAITING) for '{rc.title}' (#{rc_number + 1})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        cid,
                        f"{u.name} → IN (from WAITING) for '{rc.title}' (#{rc_number + 1})",
                    )

                # Notify proxy creator if this is a proxy
                await notify_proxy_owner_wait_to_in(rc, u, cid, rc.title, rc_number + 1)

                # --- Stats: WAITING -> IN ---
                rc_db_id = getattr(rc, "id", None)
                if rc_db_id is not None and isinstance(u.user_id, int):
                    increment_user_stat(cid, u.user_id, "total_waiting_to_in")
                    increment_user_stat(cid, u.user_id, "total_in")
                    increment_rollcall_stat(rc_db_id, "total_in")

        # Notify when limit is first reached
        if len(rc.inList) == limit and not was_full:
            await bot.send_message(
                cid,
                f"Rollcall '{rc.title}' (ID: {rc_number + 1}) has reached its max limit ({limit}). New IN will go to WAITING."
            )

    except parameterMissing as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)
    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)

# DELETE AN USER OF A ROLLCALL
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/delete_user")
async def delete_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        msg = message.text
        cid = message.chat.id
        arr = msg.split(" ")
        rc_number = 0
 
        if len(arr) > 1 and "::" in arr[-1]:
            try:
                rc_number = int(arr[-1].replace("::", "")) - 1
                del arr[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        name = " ".join(arr[1:])
        rc = manager.get_rollcall(cid, rc_number)
        
        if rc.delete_user(name) == True:
            rc.save()
            await bot.send_message(cid, "The user was deleted!")
        else:
            await bot.send_message(cid, "That user wasn't found")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

# RESUME NOTIFICATIONS
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/shh")
async def shh(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:
            manager.set_shh_mode(message.chat.id, True)
            await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

# NON RESUME NOTIFICATIONS
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/louder")
async def louder(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        else:
            manager.set_shh_mode(message.chat.id, False)
            await bot.send_message(message.chat.id, "Ok, i can hear you!")

    except rollCallNotStarted as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, "Roll call is not active")

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        user = User(message.from_user.first_name, message.from_user.username if message.from_user.username != "" else "None", message.from_user.id, rc.allNames)
        
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
            user.comment = comment

        result = rc.addIn(user)
        rc.save()
        # --- Stats: record IN ---
        rc_db_id = getattr(rc, "id", None)
        if rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_in")
            increment_rollcall_stat(rc_db_id, "total_in")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        if send_list(message, manager):
            await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        user = User(message.from_user.first_name, message.from_user.username, message.from_user.id, rc.allNames)
        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        # Capture state BEFORE addOut
        was_in = any(u.user_id == user.user_id for u in rc.inList)

        result = rc.addOut(user)
        rc.save()
        # --- Stats: record OUT ---
        rc_db_id = getattr(rc, "id", None)
        if rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_out")
            increment_rollcall_stat(rc_db_id, "total_out")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            # Someone moved from WAITING to IN
            if isinstance(result.user_id, int):
                await bot.send_message(
                    cid,
                    f"{format_mention(result)} → IN",
                    parse_mode="Markdown",
                )
            else:
                await bot.send_message(cid, f"{result.name} → IN")

            # Notify proxy creator if this is a proxy
            await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number + 1)

        # IN → OUT notification (short + tagged)
        if was_in and any(u.user_id == user.user_id for u in rc.outList):
            await bot.send_message(
                cid,
                f"{format_mention(user)} → OUT for '{rc.title}' (#{rc_number + 1})",
                parse_mode="Markdown",
            )

        if send_list(message, manager):
            await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))
    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
    
        rc = manager.get_rollcall(cid, rc_number)
        user = User(message.from_user.first_name, message.from_user.username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
            user.comment = comment

        result = rc.addMaybe(user)
        rc.save()
        # --- Stats: record MAYBE ---
        rc_db_id = getattr(rc, "id", None)
        if rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_maybe")
            increment_rollcall_stat(rc_db_id, "total_maybe")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            if type(result.user_id) == int:
                await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        if send_list(message, manager):
            await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        # Optional ::N
        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        arr = msg.split(" ")
        if len(arr) > 1:
            # Proxy user: name as user_id (string)
            proxy_name = arr[1]
            user = User(proxy_name, None, proxy_name, rc.allNames)
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            user.comment = comment

            # Persist proxy user INCLUDING proxy_owner_id in DB
            proxy_owner_id = message.from_user.id
            add_or_update_proxy_user(
                rc.id,
                user.user_id,          # proxy key (string)
                "in",                  # status
                comment,
                proxy_owner_id=proxy_owner_id,
            )

            # Remember in-memory owner mapping on RollCall
            rc.set_proxy_owner(user.user_id, proxy_owner_id)

            # Add to in/waiting lists using normal logic
            result = rc.addIn(user)
            rc.save()

            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif isinstance(result, User):
                # Just a simple confirmation; list is printed by send_list
                if isinstance(result.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention(result)} now you are in!",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            if send_list(message, manager):
                await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))
            
            # Always show updated panel for this rollcall
            await show_panel_for_rollcall(cid, rc_number + 1)
    
    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        arr = msg.split(" ")
        if len(arr) > 1:
            user = User(arr[1], None, arr[1], rc.allNames)
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            user.comment = comment

            # Capture state BEFORE addOut (proxies may match by name OR user_id)
            was_in = any((u.user_id == user.user_id or u.name == user.name) for u in rc.inList)

            result = rc.addOut(user)
            rc.save()

            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif isinstance(result, User):
                # Someone moved from WAITING to IN
                if isinstance(result.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention(result)} → IN",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{result.name} → IN")

                # Notify proxy creator if this is a proxy
                await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number + 1)

            # IN → OUT notification (short, proxies by name)
            if was_in and any((u.user_id == user.user_id or u.name == user.name) for u in rc.outList):
                await bot.send_message(
                    cid,
                    f"{user.name} → OUT for '{rc.title}' (#{rc_number + 1})",
                )

            if send_list(message, manager):
                await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))

            # Always show updated panel for this rollcall
            await show_panel_for_rollcall(cid, rc_number + 1)

    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        elif len(message.text.split(" ")) <= 1:
            raise parameterMissing("Input username is missing")

        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
                msg = " ".join(pmts)
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        arr = msg.split(" ")

        if len(arr) > 1:
            user = User(arr[1], None, arr[1], rc.allNames)
            comment = " ".join(arr[2:]) if len(arr) > 2 else ""
            user.comment = comment

            result = rc.addMaybe(user)
            rc.save()

            if result == 'AB':
                raise duplicateProxy("No duplicate proxy please :-), Thanks!")
            elif result == 'AC':
                await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")
            elif result == 'AA':
                raise repeatlyName("That name already exists!")
            elif isinstance(result, User):
                if type(result.user_id) == int:
                    await bot.send_message(cid, f"{'@'+result.username if result.username!=None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!", parse_mode="Markdown")
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            if send_list(message, manager):
                await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))            
                # Show updated panel for this rollcall
                await show_panel_for_rollcall(cid, rc_number + 1)
                return

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_in")
async def whos_in(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.inListText()}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_out")
async def whos_out(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.outListText()}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_maybe")
async def whos_maybe(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
       
        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.maybeListText()}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_waiting")
async def whos_waiting(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        
        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.waitListText()}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_title")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")[1:]
        rc_number = 0

        if len(pmts) > 1 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        
        title = " ".join(pmts)
        user = message.from_user.first_name

        if title == "":
            title = "<Empty>"

        rc = manager.get_rollcall(cid, rc_number)
        rc.title = title
        rc.save()
        
        await bot.send_message(cid, 'The roll call title is set to: ' + title)
        logging.info(user + "-" + "The title has change to " + title)

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        if admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        cid = message.chat.id
        pmts = message.text.split(" ")[1:]
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        # End current rollcall
        await bot.send_message(message.chat.id, "Roll ended!")
        await bot.send_message(cid, rc.finishList().replace("__RCID__", str(rc_number + 1)))
        logging.info("The roll call " + rc.title + " has ended")
        manager.remove_rollcall(cid, rc_number)

    # NEW: warning + optional re-broadcast
        updated_rollcalls = manager.get_rollcalls(cid)
        if len(updated_rollcalls) > 0:
            await bot.send_message(
                cid,
                "⚠️ Active rollcall IDs have been updated because one rollcall was ended.\n"
                "Use /rollcalls to see the current list and IDs."
            )

            # If you want automatic re-broadcast (can be removed if too noisy)
            for rollcall in updated_rollcalls:
                new_id = updated_rollcalls.index(rollcall) + 1
                text = f"Rollcall number {new_id}\n\n" + rollcall.allList().replace("__RCID__", str(new_id))
                await bot.send_message(cid, text)

    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/stats", "/s"])
async def stats_command(message):
    """
    /stats or /s              -> my stats in this chat
    /stats group              -> group totals
    /stats @username / name   -> that user's stats in this chat
    /stats top                -> top users by IN count in this chat
    """
    cid = message.chat.id
    text = message.text.strip()
    parts = text.split()

    # Default scope: me
    target_user_id = message.from_user.id
    display_name = message.from_user.first_name or "User"
    scope = "me"

    # Parse additional argument if present
    if len(parts) > 1:
        arg = " ".join(parts[1:]).strip()
        lower_arg = arg.lower()

        if lower_arg == "group":
            scope = "group"
        elif lower_arg == "top":
            scope = "top"
        else:
            # Try to resolve another user in this chat
            resolved = await resolve_user_for_stats(cid, arg)
            if resolved is None:
                await bot.send_message(
                    cid,
                    f"Could not find user '{arg}' in recent rollcalls for this chat."
                )
                return
            target_user_id, display_name = resolved
            scope = "other"

    try:
        if scope == "group":
            text = await build_group_stats_text(cid)
        elif scope == "top":
            text = await build_leaderboard_text(cid)
        else:
            text = await build_user_stats_text(cid, target_user_id, display_name)

        await bot.send_message(cid, text, parse_mode="Markdown")
    except Exception as e:
        logging.exception("Error in /stats")
        await bot.send_message(cid, "Error while fetching stats, please try again later.")


async def build_leaderboard_text(chat_id: int, limit: int = 10) -> str:
    """
    Build a simple leaderboard of top users by total_in in this chat.
    Uses user_stats + latest first_name/username from users table.
    """
    from db import get_connection, db_type

    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Get top users by total_in for this chat
        if db_type == "postgresql":
            cursor.execute(
                """
                SELECT user_id, total_in, total_out, total_maybe
                FROM user_stats
                WHERE chat_id = %s
                ORDER BY total_in DESC, total_out ASC
                LIMIT %s
                """,
                (chat_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT user_id, total_in, total_out, total_maybe
                FROM user_stats
                WHERE chat_id = ?
                ORDER BY total_in DESC, total_out ASC
                LIMIT ?
                """,
                (chat_id, limit),
            )   
        rows = cursor.fetchall()  
        if not rows:
            return "*Leaderboard:*\n\nNo data yet. Participate in some rollcalls first!"
        
        # Build a map from user_id -> latest (first_name, username)
        user_ids = [row[0] if not isinstance(row, dict) else row["user_id"] for row in rows]
        if db_type == "postgresql":
            cursor.execute(
                """
                SELECT DISTINCT ON (u.user_id)
                    u.user_id,
                    u.first_name,
                    u.username
                FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = %s AND u.user_id = ANY(%s)
                ORDER BY u.user_id, u.updated_at DESC
                """,
                (chat_id, user_ids),
            )
        else:
            # For SQLite, emulate DISTINCT ON by ordering and taking last per user in Python
            cursor.execute(
                """
                SELECT
                    u.user_id,
                    u.first_name,
                    u.username,
                    u.updated_at
                FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = ? AND u.user_id IN ({placeholders})
                ORDER BY u.user_id, u.updated_at ASC
                """.format(placeholders=",".join("?" * len(user_ids))),
                [chat_id] + user_ids,
            )
        user_rows = cursor.fetchall()

        name_map = {}
        if db_type == "postgresql":
            for ur in user_rows:
                if isinstance(ur, dict):
                    uid = ur["user_id"]
                    first_name = ur["first_name"]
                    username = ur["username"]
                else:
                    uid = ur[0]
                    first_name = ur[1]
                    username = ur[2]
                name_map[uid] = (first_name, username)
        else:
            # SQLite: last row per user_id due to ORDER BY ASC
            for ur in user_rows:
                if isinstance(ur, dict):
                    uid = ur["user_id"]
                    first_name = ur["first_name"]
                    username = ur["username"]
                else:
                    uid = ur[0]
                    first_name = ur[1]
                    username = ur[2]
                name_map[uid] = (first_name, username)

        # Build text
        lines = ["*Leaderboard (top {} by IN):*".format(len(rows)), ""]
        rank = 1
        for row in rows:
            if isinstance(row, dict):
                uid = row["user_id"]
                total_in = row["total_in"]
                total_out = row["total_out"]
                total_maybe = row["total_maybe"]
            else:
                uid = row[0]
                total_in = row[1]
                total_out = row[2]
                total_maybe = row[3]

            first_name, username = name_map.get(uid, ("User", None))
            if username:
                name_text = f"@{username}"
            else:
                name_text = first_name or "User"

            lines.append(
                f"{rank}. {name_text} – ✅ {total_in}  ❌ {total_out}  🤔 {total_maybe}"
            )
            rank += 1

        return "\n".join(lines)
    finally:
        if db_type == "postgresql":
            cursor.close()
            from db import release_connection
            release_connection(conn)


async def resolve_user_for_stats(chat_id: int, arg: str):
    """
    Resolve a stats target from @username or name to (user_id, display_name).

    Strategy:
    - If arg starts with @, match by username from users table.
    - Otherwise, match by firstname from users table (last seen).
    """
    from db import get_connection, db_type

    raw = arg.strip()
    username = None
    name = None

    if raw.startswith("@"):
        username = raw[1:]
    else:
        name = raw

    conn = get_connection()
    try:
        cursor = conn.cursor()

        if username:
            # Look up by username in users table for this chat
            if db_type == "postgresql":
                cursor.execute(
                    """
                    SELECT DISTINCT u.user_id, u.first_name
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = %s AND u.username = %s
                    ORDER BY u.updated_at DESC
                    LIMIT 1
                    """,
                    (chat_id, username),
                )
            else:
                cursor.execute(
                    """
                    SELECT DISTINCT u.user_id, u.first_name
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = ? AND u.username = ?
                    ORDER BY u.updated_at DESC
                    LIMIT 1
                    """,
                    (chat_id, username),
                )
        else:
            # Look up by first name in users table for this chat
            if db_type == "postgresql":
                cursor.execute(
                    """
                    SELECT DISTINCT u.user_id, u.first_name
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = %s AND u.first_name = %s
                    ORDER BY u.updated_at DESC
                    LIMIT 1
                    """,
                    (chat_id, name),
                )
            else:
                cursor.execute(
                    """
                    SELECT DISTINCT u.user_id, u.first_name
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = ? AND u.first_name = ?
                    ORDER BY u.updated_at DESC
                    LIMIT 1
                    """,
                    (chat_id, name),
                )

        row = cursor.fetchone()
        if not row:
            return None

        if isinstance(row, dict):
            user_id = row["user_id"]
            first_name = row["first_name"]
        else:
            user_id = row[0]
            first_name = row[1]

        return user_id, first_name or arg
    finally:
        if db_type == "postgresql":
            cursor.close()
            from db import release_connection
            release_connection(conn)

async def build_user_stats_text(chat_id: int, user_id: int, first_name: str) -> str:
    """
    Read user_stats for this user in this chat and format a compact summary.
    """
    from db import get_connection, db_type

    conn = get_connection()
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute(
                """
                SELECT total_in, total_out, total_maybe,
                       total_waiting_to_in
                FROM user_stats
                WHERE chat_id = %s AND user_id = %s
                """,
                (chat_id, user_id),
            )
        else:
            cursor.execute(
                """
                SELECT total_in, total_out, total_maybe,
                       total_waiting_to_in
                FROM user_stats
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            )

        row = cursor.fetchone()
        if not row:
            return f"*Stats for {first_name}:*\n\nNo data yet. Participate in a few rollcalls first!"

        if isinstance(row, dict):
            data = row
        else:
            cols = [d[0] for d in cursor.description]
            data = {cols[i]: row[i] for i in range(len(cols))}

        total_in = data.get("total_in", 0)
        total_out = data.get("total_out", 0)
        total_maybe = data.get("total_maybe", 0)
        total_wait = data.get("total_waiting_to_in", 0)

        lines = [
            f"*Stats for {first_name}:*",
            "",
            f"✅ IN: {total_in}",
            f"❌ OUT: {total_out}",
            f"🤔 MAYBE: {total_maybe}",
            f"⏫ From WAITING → IN: {total_wait}",
        ]
        return "\n".join(lines)
    finally:
        if db_type == "postgresql":
            cursor.close()
            from db import release_connection
            release_connection(conn)


async def build_group_stats_text(chat_id: int) -> str:
    """
    Basic group-level stats: total IN/OUT/MAYBE across all users.
    """
    from db import get_connection, db_type

    conn = get_connection()
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute(
                """
                SELECT
                    SUM(total_in) AS sum_in,
                    SUM(total_out) AS sum_out,
                    SUM(total_maybe) AS sum_maybe,
                    SUM(total_waiting_to_in) AS sum_wait
                FROM user_stats
                WHERE chat_id = %s
                """,
                (chat_id,),
            )
        else:
            cursor.execute(
                """
                SELECT
                    SUM(total_in) AS sum_in,
                    SUM(total_out) AS sum_out,
                    SUM(total_maybe) AS sum_maybe,
                    SUM(total_waiting_to_in) AS sum_wait
                FROM user_stats
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

        row = cursor.fetchone()
        if not row:
            return "*Group stats:*\n\nNo data yet."

        if isinstance(row, dict):
            data = row
        else:
            cols = [d[0] for d in cursor.description]
            data = {cols[i]: row[i] for i in range(len(cols))}

        sum_in = data.get("sum_in") or 0
        sum_out = data.get("sum_out") or 0
        sum_maybe = data.get("sum_maybe") or 0
        sum_wait = data.get("sum_wait") or 0

        lines = [
            "*Group stats:*",
            "",
            f"✅ Total IN: {sum_in}",
            f"❌ Total OUT: {sum_out}",
            f"🤔 Total MAYBE: {sum_maybe}",
            f"⏫ Total WAITING → IN: {sum_wait}",
        ]
        return "\n".join(lines)
    finally:
        if db_type == "postgresql":
            cursor.close()
            from db import release_connection
            release_connection(conn)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/panel")
async def show_panel(message):
    """
    Re-post the inline control panel for a rollcall.
    Usage:
      /panel           -> panel for rollcall #1
      /panel ::N       -> panel for rollcall #N
    """
    try:
        cid = message.chat.id
        pmts = message.text.split(" ")[1:]
        rc_number = 0

        # Optional ::N
        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        text = rc.allList().replace("__RCID__", str(rc_number + 1))
        markup = await get_status_keyboard(rc_number + 1)

        await bot.send_message(
            cid,
            text,
            reply_markup=markup,
        )

    except Exception as e:
        await bot.send_message(message.chat.id, e)



# ===== Inline keyboards for rollcall UI =====
async def get_status_keyboard(rc_number: int = 0) -> InlineKeyboardMarkup:
    """Keyboard with IN / OUT / MAYBE + lists + refresh + end."""
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("✅ IN", callback_data=f"btn_in_{rc_number}"),
        InlineKeyboardButton("❌ OUT", callback_data=f"btn_out_{rc_number}"),
        InlineKeyboardButton("❓ MAYBE", callback_data=f"btn_maybe_{rc_number}"),
    )
    markup.add(
        InlineKeyboardButton("📋 Lists", callback_data=f"btn_lists_{rc_number}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"btn_refresh_{rc_number}"),
    )
    markup.add(InlineKeyboardButton(f"🛑 End RollCall #{rc_number}", callback_data=f"btn_end_{rc_number}"))
    return markup


async def get_lists_keyboard(rc_number: int = 0) -> InlineKeyboardMarkup:
    """Keyboard to choose which list to view."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Who's IN", callback_data=f"btn_wi_{rc_number}"),
        InlineKeyboardButton("❌ Who's OUT", callback_data=f"btn_wo_{rc_number}"),
    )
    markup.add(
        InlineKeyboardButton("❓ Who's Maybe", callback_data=f"btn_wm_{rc_number}"),
        InlineKeyboardButton("⏳ Waiting", callback_data=f"btn_ww_{rc_number}"),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"btn_status_{rc_number}"))
    return markup


@bot.callback_query_handler(func=lambda call: True)
async def callback_handler(call):
    """
    Handle button clicks from inline keyboards.
    Supported actions:
        btn_in_N, btn_out_N, btn_maybe_N - change user status
        btn_lists_N - show lists submenu
        btn_wi_N / btn_wo_N / btn_wm_N - show IN / OUT / MAYBE list
        btn_ww_N - show waiting list
        btn_status_N - go back to main status keyboard
        btn_refresh_N - refresh main panel
        btn_end_N - end rollcall (admin rights)
    """
    try:
        data = call.data.split("_")
        # Expected callback_data pattern: "btn_<action>_<rc_number>"
        if len(data) != 3 or data[0] != "btn":
            await bot.answer_callback_query(call.id, "Invalid action")
            return
        action = data[1]
        rc_number = int(data[2])  # 1-based index from buttons
        cid = call.message.chat.id
        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 1 or rc_number > len(rollcalls):
            await bot.answer_callback_query(call.id, "Invalid rollcall!")
            return
        rc = rollcalls[rc_number - 1]
        
        # --- Status change actions (IN / OUT / MAYBE) ---
        if action in ("in", "out", "maybe"):
            user = User(
                call.from_user.first_name,
                call.from_user.username,
                call.from_user.id,
                rc.allNames,
            )
            
            # Capture state before action (for OUT notifications)
            was_in = any(u.user_id == user.user_id for u in rc.inList) if action == "out" else False
            
            if action == "in":
                result = rc.addIn(user)
            elif action == "out":
                result = rc.addOut(user)
            else:
                result = rc.addMaybe(user)
            rc.save()

            # IN → OUT notification (short + tagged) for button flow
            if action == "out" and was_in and any(u.user_id == user.user_id for u in rc.outList):
                await bot.send_message(
                    cid,
                    f"{format_mention(user)} → OUT for '{rc.title}' (#{rc_number})",
                    parse_mode="Markdown",
                )

            # --- Stats: record button-based status change ---
            rc_db_id = getattr(rc, "id", None)
            if rc_db_id is not None and isinstance(user.user_id, int):
                if action == "in":
                    increment_user_stat(cid, user.user_id, "total_in")
                    increment_rollcall_stat(rc_db_id, "total_in")
                elif action == "out":
                    increment_user_stat(cid, user.user_id, "total_out")
                    increment_rollcall_stat(rc_db_id, "total_out")
                else:
                    increment_user_stat(cid, user.user_id, "total_maybe")
                    increment_rollcall_stat(rc_db_id, "total_maybe")

            if result == "AB":
                await bot.answer_callback_query(call.id, "No duplicate proxy please 🙂")
                return
            elif result == "AC":
                await bot.answer_callback_query(call.id, "Event max limit reached, added to waitlist")
            elif result == "AA":
                await bot.answer_callback_query(call.id, "That name already exists!")
            else:
                await bot.answer_callback_query(call.id, "Status updated")
            
            # NEW: Notifications for OUT button
            if action == "out":
                # Capture state BEFORE addOut
                was_in = any(u.user_id == user.user_id for u in rc.inList)

                result = rc.addOut(user)
                rc.save()

                if result == 'AB':
                    await bot.answer_callback_query(call.id, "No duplicate proxy please :-), Thanks!")
                elif isinstance(result, User):
                    # Someone moved from WAITING to IN
                    if isinstance(result.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention(result)} → IN",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(cid, f"{result.name} → IN")

                    # Notify proxy creator if this is a proxy
                    await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number)

                # IN → OUT notification (short + tagged)
                if was_in and any(u.user_id == user.user_id for u in rc.outList):
                    await bot.send_message(
                        cid,
                        f"{format_mention(user)} → OUT for '{rc.title}' (#{rc_number})",
                        parse_mode="Markdown",
                    )

                await bot.answer_callback_query(call.id, "Updated!")
            
            # Refresh main status keyboard with full list
            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)
            await bot.edit_message_text(
                text,
                cid,
                call.message.message_id,
                reply_markup=markup,
            )
            return
        
        # --- Show lists submenu ---
        if action == "lists":
            markup = await get_lists_keyboard(rc_number)
            await bot.edit_message_text(
                "Select list:",
                cid,
                call.message.message_id,
                reply_markup=markup,
            )
            await bot.answer_callback_query(call.id)
            return
        
        # --- Individual lists (IN / OUT / MAYBE / Waiting) ---
        if action in ("wi", "wo", "wm", "ww"):
            if action == "wi":
                text = rc.inListText()
            elif action == "wo":
                text = rc.outListText()
            elif action == "wm":
                text = rc.maybeListText()
            else:
                text = rc.waitListText()
            await bot.edit_message_text(
                text if text.strip() else "List is empty.",
                cid,
                call.message.message_id,
                reply_markup=await get_lists_keyboard(rc_number),
            )
            await bot.answer_callback_query(call.id)
            return
        
        # --- Back to status keyboard ---
        if action == "status":
            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)
            await bot.edit_message_text(
                text,
                cid,
                call.message.message_id,
                reply_markup=markup,
            )
            await bot.answer_callback_query(call.id)
            return
        
        # --- Refresh main panel ---
        if action == "refresh":
            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)
            await bot.edit_message_text(
                text,
                cid,
                call.message.message_id,
                reply_markup=markup,
            )
            await bot.answer_callback_query(call.id, "Refreshed")
            return
        
        # --- End rollcall via button ---
        # Inside callback_handler, in the "end" action block:
        if action == "end":
            if await admin_rights(call.message, manager) is False:
                await bot.answer_callback_query(call.id, "Insufficient permissions")
                return
            
            ended_by = call.from_user.first_name or call.from_user.username or "someone"
            
            try:
                final_text = rc.finishList().replace("__RCID__", str(rc_number))
                final_text += f"\n\nEnded by {ended_by}"
                await bot.send_message(cid, final_text)
            except Exception:
                pass
            
            await bot.edit_message_text(
                f"Rollcall ended by {ended_by}!",
                cid,
                call.message.message_id,
            )
            
            # Remove the rollcall FIRST
            manager.remove_rollcall(cid, rc_number - 1)
            
            # NEW: Send updated panels for remaining rollcalls
            updated_rollcalls = manager.get_rollcalls(cid)
            if len(updated_rollcalls) > 0:
                await bot.send_message(
                    cid,
                    "⚠️ Rollcall IDs have been updated. Here are the current rollcalls with fresh panels:"
                )
                
                # Send fresh panel for each remaining rollcall
                for idx, rollcall in enumerate(updated_rollcalls):
                    new_id = idx + 1
                    text = rollcall.allList().replace("__RCID__", str(new_id))
                    markup = await get_status_keyboard(new_id)
                    await bot.send_message(
                        cid,
                        f"**Rollcall #{new_id}**\n\n{text}",
                        reply_markup=markup
                    )
            
            await bot.answer_callback_query(call.id, "Ended - Fresh panels sent")
            return
        
        # --- Fallback ---
        await bot.answer_callback_query(call.id, "Unknown action")
    except Exception as e:
        # Show error in callback but avoid crashing polling
        await bot.answer_callback_query(call.id, str(e))


async def show_panel_for_rollcall(chat_id: int, rc_number: int):
    """
    Send the main status panel for rollcall #rc_number (1-based).
    Used after commands like /sif, /sof, /smf so user sees updated state.
    """
    rollcalls = manager.get_rollcalls(chat_id)
    index = rc_number - 1
    if index < 0 or index >= len(rollcalls):
        return

    rc = rollcalls[index]
    text = rc.allList().replace("__RCID__", str(rc_number))
    markup = await get_status_keyboard(rc_number)
    await bot.send_message(chat_id, text, reply_markup=markup)


# ===== Proxy owner notification helper =====
async def notify_proxy_owner_wait_to_in(rc, moved_user: User, cid, title: str, rc_number: int):
    """
    Notify the user who created this proxy (if any) that their proxy moved
    from WAITING to IN. Message is short and tagged.
    """
    try:
        proxy_owners = getattr(rc, "proxy_owners", None)
        if not proxy_owners:
            return

        user_key = moved_user.user_id
        owner_id = proxy_owners.get(user_key)
        if not owner_id:
            return

        # Build owner mention
        try:
            member = await bot.get_chat_member(cid, owner_id)
            if member.user.username:
                owner_mention = f"@{member.user.username}"
            else:
                owner_mention = f"[{member.user.first_name}](tg://user?id={owner_id})"
        except Exception:
            owner_mention = "Proxy owner"

        txt = f"{owner_mention}, your proxy {format_mention(moved_user)} → IN for '{title}' (#{rc_number})"
        await bot.send_message(cid, txt, parse_mode="Markdown")
    except Exception:
        logging.exception("Failed to notify proxy owner for WAITING→IN")
