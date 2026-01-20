from exceptions import *
import logging
import Levenshtein
import datetime
import pytz
import asyncio
from telebot.async_telebot import AsyncTeleBot
from config import TELEGRAM_TOKEN

bot = AsyncTeleBot(TELEGRAM_TOKEN)

# FUNCTION TO RAISE RC ALREADY STARTED ERROR
# USELESS IN NEW FEATURE
def roll_call_already_started(message, manager):
    """Check if roll call already started - deprecated with multiple rollcalls"""
    try:
        rollcalls = manager.get_rollcalls(message.chat.id)
        if len(rollcalls) == 1:
            logging.error(f"Roll call with title {rollcalls[0].title} is still in progress")
            return False
        else:
            return True
    except:
        return True

# FUNCTION TO RAISE RC NOT STARTED ERROR
def roll_call_not_started(message, manager):
    """Check if any roll call is active"""
    try:
        rollcalls = manager.get_rollcalls(message.chat.id)
        if len(rollcalls) == 0:
            logging.error("Roll call is not active")
            return False
        else:
            return True
    except:
        return False

# FUNCTION TO RAISE NO ADMIN RIGHTS ERROR
async def admin_rights(message, manager):
    """Check if user has admin rights (if required)"""
    try:
        chat_id = message.chat.id
        
        # If admin rights are not required, allow all users
        if not manager.get_admin_rights(chat_id):
            return True
        
        # Check if user is admin or creator
        member = await bot.get_chat_member(chat_id, message.from_user.id)
        if member.status not in ['administrator', 'creator']:
            logging.error("Error - user does not have sufficient permissions for this operation")
            return False
        
        return True
    except Exception as e:
        logging.error(f"Error checking admin rights: {e}")
        return True  # Default to allowing if check fails

# FUNCTION TO CHECK IF SHH/LOUDER IS ACTIVE
def send_list(message, manager):
    """Check if bot should send detailed lists (not in shh mode)"""
    chat_id = message.chat.id
    return not manager.get_shh_mode(chat_id)

# AUTOCOMPLETE TIMEZONE
def auto_complete_timezone(timezone):
    """Auto-complete timezone string using fuzzy matching"""
    try:
        # Parse input
        parts = timezone.split("/")
        if len(parts) < 2:
            return None
            
        continent = parts[0].lower()
        place = parts[1].lower().replace(" ", "_")
        
        # Handle common aliases
        if place == 'india':
            place = 'calcutta'
        if place == 'argentina':
            place = 'buenos_aires'
        
        # Find best match
        best_match = None
        best_distance = float('inf')
        
        for tz in pytz.all_timezones:
            tz_parts = tz.split("/")
            
            # Check continent matches
            if tz_parts[0].lower() != continent:
                continue
            
            # Get the place part (could be 2nd or 3rd component)
            if len(tz_parts) == 2:
                tz_place = tz_parts[1].lower()
            elif len(tz_parts) == 3:
                tz_place = tz_parts[2].lower()
            else:
                continue
            
            # Calculate distance with threshold
            threshold = int(len(place) * 0.35)
            try:
                diff = Levenshtein.distance(place, tz_place, score_cutoff=threshold)
                
                if diff <= threshold and diff < best_distance:
                    best_distance = diff
                    best_match = tz
            except:
                # Levenshtein.distance might raise if distance exceeds cutoff
                continue
        
        return best_match
    except Exception as e:
        logging.error(f"Error in auto_complete_timezone: {e}")
        return None
