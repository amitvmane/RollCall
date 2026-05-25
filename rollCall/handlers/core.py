"""
Core handlers: /start, /help, /rollcalls, /version, /set_admins, /unset_admins, /timezone, /broadcast
"""
import json
import logging
import traceback

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
    await bot.send_message(message.chat.id, '''RollCall Bot Commands

Basic:
/start_roll_call (/src) [title] — Start a new rollcall
/end_roll_call (/erc) [::N] — End rollcall
/rollcalls (/r) — List all active rollcalls
/panel [::N] — Show inline control panel

Voting (append ::N for a specific rollcall):
/in [comment] — Mark yourself IN
/out [comment] — Mark yourself OUT
/maybe [comment] — Mark yourself MAYBE

Lists:
/whos_in (/wi) [::N] — IN list
/whos_out (/wo) [::N] — OUT list
/whos_maybe (/wm) [::N] — MAYBE list
/whos_waiting (/ww) [::N] — Waitlist

Proxy (for non-Telegram members, admin only):
/set_in_for (/sif) name [::N]
/set_out_for (/sof) name [::N]
/set_maybe_for (/smf) name [::N]

Event settings (admin only):
/set_title (/st) title [::N]
/set_limit (/sl) N [::N] — Max attendees
/set_rollcall_time (/srt) DD-MM-YYYY H:M [::N]
/set_rollcall_reminder (/srr) hours [::N]
/event_fee (/ef) amount [::N]
/individual_fee (/if) [::N] — Per-person fee split
/location (/loc) place [::N]
/when (/w) [::N] — Show event time

Templates (admin only):
/set_template name "Title" [limit=N] [location=X] [fee=X]
/templates — List saved templates
/start_template name [extra title]
/delete_template name
/schedule_template name <weekday> <HH:MM> — Weekly auto-start
/schedule_template name <weekday> <HH:MM> biweekly — Every 2 weeks
/schedule_template name monthly <day> <HH:MM> — Monthly on day N
/schedule_template name off — Disable schedule
/schedules — View all scheduled templates + pause/resume toggles

Ghost tracking (admin only):
/toggle_ghost_tracking — Enable/disable no-show tracking
/set_absent_limit N — Reconfirmation threshold
/mark_absent — Mark no-shows from a past session
/clear_absent name — Clear ghost count

Admin tools:
/delete_user name [::N] — Remove user (with confirmation)
/set_status name <in|out|maybe> [::N] — Move user to a different status
/buzz [message] [::N] — Ping members who haven't voted (30s cooldown)
/audit_log [N] — Show last N admin actions (default 20)
/set_admins / /unset_admins — Toggle admin-only mode

Chat settings:
/shh — Silent mode (no list after each vote)
/louder — Show full list after each vote
/timezone (/tz) Region/City — e.g. Asia/Kolkata

Info:
/stats (/s) [name|@user|group|top|bot] — Attendance stats & streaks
/history [N] [page] — Paginated ended rollcalls (default 10 per page)
/version (/v) — Bot version

Super admin:
/broadcast "message" — Send to all bot chats

Tip: append ::N to most commands to target rollcall #N
''')


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
        if len(msg.split(" ")[1].split("/")) != 2:
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
        print(traceback.format_exc())
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
