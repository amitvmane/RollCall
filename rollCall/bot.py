import logging

from telebot.async_telebot import AsyncTeleBot

from config.config import TELEGRAM_TOKEN, ADMINS
from exceptions.exceptions import *
from middleware.middleware import MyMiddleware

from commands import common
from commands import chat_config
from commands import chat_analytics
from commands import dev
from commands import rollcall_interact
from commands import rollcall_start_end
from commands import rollcall_config
from commands import rollcall_in_out_maybe

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)

#CONFIG
bot.setup_middleware(MyMiddleware())

logging.info("Bot already started")

bot.register_message_handler(lambda message: common.welcome_and_explanation(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0] == "/start")

bot.register_message_handler(lambda message: common.help_commands(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0] == "/help")

bot.register_message_handler(lambda message: chat_config.set_admins(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0] == "/set_admins")

bot.register_message_handler(lambda message: chat_config.unset_admins(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")

bot.register_message_handler(lambda message: chat_config.config_timezone(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ['/timezone', '/tz'])

bot.register_message_handler(lambda message: dev.broadcast(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.version_command(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/version", '/v'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.registered_chats(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/registered_chats", '/rc'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.registered_users(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/registered_users", '/ru'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.downtime_uptime(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/down_and_up", '/dau'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.active_rollcalls_count(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/active_rollcalls", '/ar'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.chat_zones(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/chat_zones", '/cz'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: dev.rollcalls_count(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/rollcalls_count", '/rollc'] and message.from_user.id in ADMINS)

bot.register_message_handler(lambda message: rollcall_interact.rollCalls(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/rollcalls", '/r'])

bot.register_message_handler(lambda message: rollcall_start_end.start_roll_call(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/start_roll_call", '/src'])

bot.register_message_handler(lambda message: rollcall_config.set_rollcall_time(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/set_rollcall_time", '/srt'])

bot.register_message_handler(lambda message: rollcall_config.set_rollcall_reminder(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/set_rollcall_reminder", '/srr'])

bot.register_message_handler(lambda message: rollcall_config.event_fee(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/event_fee", '/ef'])

bot.register_message_handler(lambda message: rollcall_interact.individual_fee(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/individual_fee", '/if'])

bot.register_message_handler(lambda message: rollcall_interact.when(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/when", '/w'])

bot.register_message_handler(lambda message: rollcall_config.set_location(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/location", '/loc'])

bot.register_message_handler(lambda message: rollcall_config.wait_limit(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/set_limit", '/sl'])

bot.register_message_handler(lambda message: rollcall_interact.delete_user(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/delete_user"])

bot.register_message_handler(lambda message: chat_config.shh(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0] == "/shh")

bot.register_message_handler(lambda message: chat_config.louder(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0] == "/louder")

bot.register_message_handler(lambda message: rollcall_in_out_maybe.in_user(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == '/in')

bot.register_message_handler(lambda message: rollcall_in_out_maybe.out_user(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == '/out')

bot.register_message_handler(lambda message: rollcall_in_out_maybe.maybe_user(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == '/maybe')

bot.register_message_handler(lambda message: rollcall_in_out_maybe.set_in_for(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ['/set_in_for', '/sif'])

bot.register_message_handler(lambda message: rollcall_in_out_maybe.set_out_for(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ['/set_out_for', '/sof'])

bot.register_message_handler(lambda message: rollcall_in_out_maybe.set_maybe_for(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ['/set_maybe_for', '/smf'])

bot.register_message_handler(lambda message: rollcall_interact.whos_in(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_in")

bot.register_message_handler(lambda message: rollcall_interact.whos_out(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_out")

bot.register_message_handler(lambda message: rollcall_interact.whos_maybe(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_maybe")

bot.register_message_handler(lambda message: rollcall_interact.whos_waiting(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_waiting")

bot.register_message_handler(lambda message: rollcall_config.set_title(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/set_title", '/st'])

bot.register_message_handler(lambda message: rollcall_start_end.end_roll_call(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/end_roll_call", '/erc'])

bot.register_message_handler(lambda message: chat_analytics.voting_users(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/voting_users", '/vu'])

bot.register_message_handler(lambda message: chat_analytics.voting_users_in(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/voting_users_in", '/vui'])

bot.register_message_handler(lambda message: chat_analytics.undecided_users(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/undecided_users", '/uu'])

bot.register_message_handler(lambda message: chat_analytics.consistent_users(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/consistent_users", '/cu'])

bot.register_message_handler(lambda message: chat_analytics.rollcalls_per_month(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/rollcalls_per_month", '/rpm'])

bot.register_message_handler(lambda message: rollcall_interact.freeze(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/f", '/freeze'])

bot.register_message_handler(lambda message: rollcall_interact.unfreeze(bot, message), 
                            func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/uf", '/unfreeze'])