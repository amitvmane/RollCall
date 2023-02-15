from utils.analytics.functions import get_users_voting, get_users_voting_in, users_changing_from_in_to_out, users_consistently_responding, rollcalls_month


async def voting_users(bot, message):
    result = get_users_voting(await bot.get_chat_members_count(message.chat.id), message.chat.id)
    await bot.send_message(message.chat.id, f'The percentage of use of the bot in this chat is {str(result)}%')

async def voting_users_in(bot, message):
    result = get_users_voting_in(message.chat.id)
    await bot.send_photo(message.chat.id, result)

async def undecided_users(bot, message):
    result = users_changing_from_in_to_out(message.chat.id)
    await bot.send_photo(message.chat.id, result)

async def consistent_users(bot, message):
    result = users_consistently_responding(message.chat.id)
    await bot.send_photo(message.chat.id, result)

async def rollcalls_per_month(bot, message):
    result = rollcalls_month(message.chat.id)
    await bot.send_photo(message.chat.id, result)