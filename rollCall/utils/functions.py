import Levenshtein
import pytz
from telebot import TeleBot

from config import TELEGRAM_TOKEN

bot=TeleBot(TELEGRAM_TOKEN)

#FUNCTION TO RAISE NO ADMIN RIGHTS ERROR
def admin_rights(message):
    if bot.get_chat_member(message.chat.id,message.from_user.id).status not in ['admin', 'creator'] or not message.chat.type == 'private':
        return False
    return True
    
def auto_complete_timezone(inputTimezone):
    min_distance = float('inf')
    closest_tz = None

    for tz in pytz.all_timezones:
        distance = Levenshtein.distance(inputTimezone, tz)
        if distance < min_distance:
            min_distance = distance
            closest_tz = tz
    return closest_tz