"""
Core handlers: /start, /help, /rollcalls, /version, /set_admins, /unset_admins, /timezone, /broadcast
"""
import json
import logging

from bot_state import bot, data_file_path
from config import ADMINS
from exceptions import parameterMissing
from functions import admin_rights, auto_complete_timezone
from rollcall_manager import manager
from db import get_all_chat_ids, log_admin_action


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message):
    cid = message.chat.id
    manager.get_chat(cid)
    if await admin_rights(message, manager) == False:
        await bot.send_message(cid, "Error - User does not have sufficient permissions for this operation")
        return
    await bot.send_message(cid, 'Hi! im RollCall!\n\nUse /help to see all the commands')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/help")
async def help_commands(message):
    await bot.send_message(message.chat.id, r'''🎯 *RollCall Bot — Commands*

🗳 *Voting*
/in [comment] — Mark yourself IN ✅
/out [comment] — Mark yourself OUT ❌
/maybe [comment] — Mark yourself MAYBE 🤔

📋 *View Lists*
/rollcalls (/r) — All active rollcalls
/whos\_in (/wi) — Who's IN
/whos\_out (/wo) — Who's OUT
/whos\_maybe (/wm) — Who's undecided
/whos\_waiting (/ww) — Waitlist

📊 *Stats & History*
/stats — Your attendance stats & streak
/stats group — Group summary
/stats top — Leaderboard (top 10 by IN)
/stats ghost — No-show leaderboard
/stats @user or name — Another user's stats
/history [N] [page] — Past ended rollcalls

━━━━━━━━━━━━━━━━━━
🔧 *Admin — Rollcall*
/start\_roll\_call (/src) [title] — Start a rollcall
/end\_roll\_call (/erc) [::N] — End rollcall
/panel [::N] — Resend vote panel with buttons

⚙️ *Admin — Settings*
/set\_title (/st) title — Event title
/set\_limit (/sl) N — Max attendees (0 = unlimited)
/set\_rollcall\_time (/srt) DD-MM-YYYY HH:MM — Auto-close time
/set\_rollcall\_reminder (/srr) hours — Reminder before close
/event\_fee (/ef) amount — Total event fee
/individual\_fee (/if) — Per-person fee split
/location (/loc) place — Event location
/when (/w) — Show scheduled event time
/shh — Silent mode (panel edits silently, no ack messages)
/louder — Loud mode (ack message shown after each vote)
/timezone (/tz) Region/City — e.g. Asia/Kolkata

👥 *Admin — Proxy* _(non-Telegram members)_
/set\_in\_for (/sif) name [::N]
/set\_out\_for (/sof) name [::N]
/set\_maybe\_for (/smf) name [::N]

📅 *Admin — Templates*
/templates — List saved templates
/set\_template name "Title" [limit=N] [location=X] [fee=X]
/start\_template name [title] — Start rollcall from template
/delete\_template name
/schedule\_template name <weekday> <HH:MM> — Weekly auto-start
/schedule\_template name <weekday> <HH:MM> biweekly
/schedule\_template name monthly <day> <HH:MM>
/schedule\_template name off — Disable schedule
/schedules — View & toggle schedules

🗂 *Admin — User Management*
/delete\_user name [::N] — Remove user (asks confirmation)
/set\_status name <in|out|maybe> [::N] — Override user status
/buzz [message] [::N] — Ping non-voters (30s cooldown)
/set\_admins / /unset\_admins — Toggle admin-only mode

👻 *Admin — Ghost Tracking* _(no-show monitoring)_
/toggle\_ghost\_tracking [on|off]
/set\_absent\_limit N — Missed sessions before reconfirmation
/mark\_absent — Review & mark no-shows from a past session
/clear\_absent name — Reset ghost count for a user

📝 *Admin — Audit*
/audit\_log [N] — Last N admin actions (default 20)

🔑 *Super Admin*
/broadcast "message" — Send message to all bot chats

💡 *Tips*
• Add `::2` or `::3` to target a specific rollcall when multiple are active
• Shortcuts: /src /erc /wi /wo /wm /ww /sif /sof /smf /st /sl /ef /s /r /v /tz
''', parse_mode='Markdown')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):
    cid = message.chat.id
    member = await bot.get_chat_member(cid, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return
    manager.set_admin_rights(cid, True)
    log_admin_action(cid, message.from_user.id, message.from_user.first_name, "set_admins")
    await bot.send_message(cid, 'Admin permissions activated')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):
    cid = message.chat.id
    member = await bot.get_chat_member(cid, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return
    manager.set_admin_rights(cid, False)
    log_admin_action(cid, message.from_user.id, message.from_user.first_name, "unset_admins")
    await bot.send_message(cid, 'Admin permissions disabled')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):
    if len(message.text.split(" ")) < 2:
        await bot.send_message(message.chat.id, "Message is missing")
        return

    broadcast_text = " ".join(message.text.split(" ")[1:])
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        await bot.send_message(message.chat.id, "No chats found to broadcast to.")
        return

    success, failed = 0, 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, broadcast_text)
            success += 1
        except Exception as e:
            logging.warning(f"[broadcast] Failed to send to chat {chat_id}: {e}")
            failed += 1

    await bot.send_message(message.chat.id, f"Broadcast complete. Sent: {success}, Failed: {failed}")


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg = message.text
        cid = message.chat.id

        if len(msg.split(" ")) < 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")
        if len(msg.split(" ")[1].split("/")) < 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")

        manager.get_chat(cid)
        response = auto_complete_timezone(" ".join(msg.split(" ")[1:]))

        if response is not None:
            await bot.send_message(cid, f"Your timezone has been set to {response}")
            manager.set_timezone(cid, response)
            log_admin_action(cid, message.from_user.id, message.from_user.first_name, "timezone", details=response)
        else:
            await bot.send_message(cid, f"Given timezone is invalid , check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode='HTML')

    except Exception as e:
        logging.exception("[config_timezone] Unexpected error")
        await bot.send_message(message.chat.id, str(e))


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/version")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/v")
async def version_command(message):
    try:
        with open(data_file_path('version.json'), 'r') as file:
            data = json.load(file)
    except FileNotFoundError:
        logging.error("[version_command] version.json not found")
        await bot.send_message(message.chat.id, "Version information is currently unavailable.")
        return
    except json.JSONDecodeError as e:
        logging.error(f"[version_command] Failed to parse version.json: {e}")
        await bot.send_message(message.chat.id, "Version information is currently unavailable.")
        return

    for i in range(len(data)):
        version = data[-1 - i]
        if version.get("DeployedOnProd") == 'Y':
            txt = (
                f"Version: {version['Version']}\n"
                f"Description: {version['Description']}\n"
                f"Deployed: {version['DeployedOnProd']}\n"
                f"Deployed datetime: {version['DeployedDatetime']}"
            )
            await bot.send_message(message.chat.id, txt)
            return

    logging.warning("[version_command] No deployed version found in version.json")
    await bot.send_message(message.chat.id, "No released version information found.")


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
