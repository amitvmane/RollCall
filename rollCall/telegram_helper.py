import os
import logging
import re
import asyncio
import json
from datetime import datetime, timedelta
from typing import Optional

from telebot.async_telebot import AsyncTeleBot

import pytz

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

def _ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
from db import create_or_update_template, get_templates, get_template, delete_template
from db import set_template_schedule, disable_template_schedule
from db import get_all_chat_ids
from db import (
    get_ghost_count, increment_ghost_count, reset_ghost_count,
    get_ghost_leaderboard, get_user_ghost_count_by_name, get_ghost_count_by_proxy_name,
    mark_rollcall_absent_done, get_unprocessed_rollcalls,
    add_ghost_event, get_rollcall_in_users, save_ghost_selections,
    update_streak_on_checkin, reset_streak_on_ghost, get_rollcall_history,
    upsert_chat_member, mark_member_inactive, get_active_members
)

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Ghost tracking in-memory state
# (chat_id, rollcall_db_id) -> set of user_ids selected as ghosts
_ghost_selections: dict = {}
# (chat_id, user_id) -> {'rc_number': int, 'comment': str} for pending reconfirmation
_pending_reconf: dict = {}

# Rate limiting: (chat_id, user_id) -> last action timestamp
_rate_limits: dict = {}
_RATE_LIMIT_SECONDS = 2

# Pending delete confirmations: (chat_id, admin_user_id) -> {'name': str, 'rc_number': int}
_pending_deletes: dict = {}

# Panel message tracking: (chat_id, rc_1based) -> message_id of the active panel message.
# Used by _update_panel() to edit the panel in-place instead of posting a new message.
_panel_msg_ids: dict = {}


def _fmt_ended_at(ended_at) -> str:
    """Format a rollcall ended_at timestamp to a human-readable date string."""
    if not ended_at:
        return "Unknown date"
    if isinstance(ended_at, str):
        try:
            ended_at = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
        except Exception:
            return str(ended_at)
    try:
        return ended_at.strftime("%d %b %Y")
    except Exception:
        return str(ended_at)


def _build_ghost_select_keyboard(rc_db_id: int, in_users: list, selected_ids: set) -> InlineKeyboardMarkup:
    """Build the ghost selection keyboard from a list of IN users.

    ``in_users`` may contain real users (integer user_id) and proxy users
    (user_id=None, proxy_name=str).  Proxy users use a separate callback prefix
    so their string name isn't confused with an integer Telegram ID.
    """
    markup = InlineKeyboardMarkup(row_width=2)
    for u in in_users:
        proxy_name = u.get('proxy_name')
        if proxy_name is not None:
            # /sif proxy user — keyed by name string
            tick = "👻 " if proxy_name in selected_ids else ""
            markup.add(InlineKeyboardButton(
                f"{tick}{proxy_name}",
                callback_data=f"ghost_togp_{rc_db_id}_{proxy_name}"
            ))
        else:
            uid = u['user_id']
            name = u.get('first_name') or u.get('username') or str(uid)
            tick = "👻 " if uid in selected_ids else ""
            markup.add(InlineKeyboardButton(
                f"{tick}{name}",
                callback_data=f"ghost_tog_{rc_db_id}_{uid}"
            ))
    markup.add(InlineKeyboardButton("✅ Done", callback_data=f"ghost_done_{rc_db_id}"))
    return markup


def data_file_path(filename: str) -> str:
    return os.path.join(BASE_DIR, filename)


def _get_display_name(tg_user) -> str:
    """Return a safe, non-None display name for a Telegram user object."""
    return tg_user.first_name or tg_user.last_name or str(tg_user.id)


def _is_rate_limited(chat_id: int, user_id: int) -> bool:
    """Return True if this user has acted within the rate limit window."""
    key = (chat_id, user_id)
    now = datetime.now().timestamp()
    last = _rate_limits.get(key, 0)
    if now - last < _RATE_LIMIT_SECONDS:
        return True
    _rate_limits[key] = now
    return False

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

def format_mention_with_name(user: User) -> str:
    """
    No username set  → [FirstName](tg://user?id=...)
    Username set     → @username (FirstName)
    """
    if isinstance(user.user_id, int):
        if user.username:
            return f"@{user.username} ({user.name})"
        else:
            return f"[{user.name}](tg://user?id={user.user_id})"
    return user.name


async def warn_no_username(cid: int, first_name: str):
    """Warn in group that this user has no Telegram username set."""
    try:
        await bot.send_message(
            cid,
            f"⚠️ {first_name}, you don't have a Telegram username set.\n"
            "Please set one: Settings → Edit Profile → Username\n"
            "The bot uses it for logging and identification.",
        )
    except Exception:
        pass


def get_rollcall_db_id(rc: RollCall) -> int:
    """
    Helper to get DB rollcall id from RollCall object.
    Assumes rc has .db_id or similar; if not, extend RollCall to store it.
    """
    # If your RollCall already exposes db_id or rollcall_id, use that.
    # Fallback: manager can be extended later if needed.
    return getattr(rc, "db_id", None)

# ✅ FIX 1: Replace old get_rollcall_db_id with this safe unified helper
def get_rc_db_id(rc) -> Optional[int]:
    """
    Safely retrieve the DB primary key from a RollCall object.
    Checks both rc.id and rc.db_id for compatibility.
    Returns None if neither is set — callers must guard before using stats/proxy DB calls.
    """
    val = getattr(rc, "id", None) or getattr(rc, "db_id", None)
    if val is None:
        logging.warning(
            f"RollCall '{getattr(rc, 'title', '?')}' has no DB id — "
            "stats and proxy DB calls will be skipped for this rollcall."
        )
    return val

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
/schedule_template name <weekday> <HH:MM> — Auto-start on schedule
/schedule_template name off — Disable schedule

Ghost tracking (admin only):
/toggle_ghost_tracking — Enable/disable no-show tracking
/set_absent_limit N — Reconfirmation threshold
/absent_stats — Ghost leaderboard
/mark_absent — Mark no-shows from a past session
/clear_absent name — Clear ghost count

Admin tools:
/delete_user name [::N] — Remove user (with confirmation)
/buzz [message] [::N] — Ping members who haven't voted yet
/set_admins / /unset_admins — Toggle admin-only mode

Chat settings:
/shh — Silent mode (no list after each vote)
/louder — Show full list after each vote
/timezone (/tz) Region/City — e.g. Asia/Kolkata

Info:
/stats (/s) [name|@user|group|top|bot] — Attendance stats & streaks
/history [N] — Last N ended rollcalls (default 10)
/version (/v) — Bot version

Super admin:
/broadcast "message" — Send to all bot chats

Tip: append ::N to most commands to target rollcall #N
''')



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

@bot.message_handler(func=lambda message: message.text.split(" ")[0].split("@")[0].lower() == "/templates")
async def list_templates(message):
    """List templates defined for this chat."""
    cid = message.chat.id
    templates = get_templates(cid)

    if not templates:
        await bot.send_message(cid, "No templates defined for this chat.")
        return

    lines = []
    for t in templates:
        t_title = t.get("title") or "(no title)"
        sched_enabled = t.get("schedule_enabled")
        sched_day = t.get("schedule_day")
        sched_time = t.get("schedule_time")
        event_day = t.get("event_day")
        event_time = t.get("event_time")
        last_run = t.get("last_scheduled_date")

        if sched_enabled and sched_day and sched_time:
            sched_info = f"  🗓 Opens {sched_day.capitalize()} {sched_time}"
            if event_day and event_time:
                sched_info += f" → closes {event_day.capitalize()} {event_time}"
            if last_run:
                sched_info += f"  (last: {last_run})"
        elif event_day and event_time:
            sched_info = f"  📅 Event: {event_day.capitalize()} {event_time}"
        else:
            sched_info = ""

        lines.append(f"- {t['name']}: {t_title}{sched_info}")

    await bot.send_message(cid, "Templates:\n" + "\n".join(lines))


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/schedule_template")
async def schedule_template_cmd(message):
    """Admin only: enable or disable scheduled auto-start for a template.

    Usage:
      /schedule_template <name> <weekday> <HH:MM>   — set schedule and enable
      /schedule_template <name> off                  — disable schedule
      /schedule_template <name>                      — show current schedule
    """
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        cid = message.chat.id
        parts = message.text.strip().split()

        if len(parts) < 2:
            await bot.send_message(
                cid,
                "Usage:\n"
                "/schedule_template <name> <weekday> <HH:MM>  — enable auto-start\n"
                "/schedule_template <name> off                 — disable\n"
                "/schedule_template <name>                     — show current schedule\n\n"
                "Example: /schedule_template sunday_game friday 09:00"
            )
            return

        name = parts[1]
        tmpl = get_template(cid, name)
        if not tmpl:
            await bot.send_message(cid, f"Template '{name}' not found. Use /templates to list available templates.")
            return

        # Show current schedule if no further args
        if len(parts) == 2:
            sched_enabled = tmpl.get("schedule_enabled")
            sched_day = tmpl.get("schedule_day")
            sched_time = tmpl.get("schedule_time")
            event_day = tmpl.get("event_day")
            event_time = tmpl.get("event_time")
            last_run = tmpl.get("last_scheduled_date")
            if sched_enabled and sched_day and sched_time:
                status = (
                    f"🗓 *{name}* schedule: 🟢 enabled\n"
                    f"Opens: {sched_day.capitalize()} {sched_time}\n"
                )
                if event_day and event_time:
                    status += f"Closes: {event_day.capitalize()} {event_time}\n"
                if last_run:
                    status += f"Last auto-started: {last_run}"
            else:
                status = f"🗓 *{name}* schedule: 🔴 disabled"
            await bot.send_message(cid, status, parse_mode="Markdown")
            return

        # Disable
        if parts[2].lower() == "off":
            ok = disable_template_schedule(cid, name)
            if ok:
                await bot.send_message(cid, f"🔴 Schedule disabled for template '{name}'.")
            else:
                await bot.send_message(cid, f"Failed to update template '{name}'.")
            return

        # Enable: expect <weekday> <HH:MM>
        if len(parts) < 4:
            await bot.send_message(
                cid,
                "To enable scheduling provide both a weekday and a time.\n"
                "Example: /schedule_template sunday_game friday 09:00"
            )
            return

        sched_day = parts[2].lower()
        sched_time = parts[3]

        if sched_day not in WEEKDAY_MAP:
            await bot.send_message(
                cid,
                f"'{sched_day}' is not a valid weekday.\n"
                "Use: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
            )
            return

        # Validate HH:MM format
        try:
            sh, sm = map(int, sched_time.split(":"))
            if not (0 <= sh < 24 and 0 <= sm < 60):
                raise ValueError
        except ValueError:
            await bot.send_message(cid, f"'{sched_time}' is not a valid time. Use HH:MM (e.g. 09:00).")
            return

        # Require event_day + event_time so the rollcall has a close time
        event_day = tmpl.get("event_day")
        event_time = tmpl.get("event_time")
        if not event_day or not event_time:
            await bot.send_message(
                cid,
                f"Template '{name}' has no event_day/event_time set.\n"
                "Set them first so the auto-started rollcall knows when to close:\n"
                f"/set_template {name} event_day=sunday event_time=17:00"
            )
            return

        # Validate schedule is strictly before event in the weekly cycle
        sched_mins = weekly_minutes(sched_day, sched_time)
        event_mins = weekly_minutes(event_day, event_time)
        if sched_mins is None or event_mins is None:
            await bot.send_message(cid, "Could not validate schedule vs event time. Check day/time formats.")
            return
        if sched_mins >= event_mins:
            await bot.send_message(
                cid,
                f"Schedule time ({sched_day.capitalize()} {sched_time}) must be "
                f"before event time ({event_day.capitalize()} {event_time}) in the weekly cycle."
            )
            return

        ok = set_template_schedule(cid, name, sched_day, sched_time)
        if ok:
            await bot.send_message(
                cid,
                f"🟢 Schedule set for template *{name}*:\n"
                f"Opens: {sched_day.capitalize()} at {sched_time}\n"
                f"Closes: {event_day.capitalize()} at {event_time}",
                parse_mode="Markdown"
            )
        else:
            await bot.send_message(cid, f"Failed to save schedule for '{name}'.")

    except Exception as e:
        await bot.send_message(message.chat.id, e)


# START A ROLL CALL
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):
    cid = message.chat.id
    msg = message.text
    title = ''

    # Save chat to database.json for broadcast
    with open(data_file_path('database.json'), 'r') as read_file:
        database = json.load(read_file)
        read_file.close()
    
    cond = True
    for i in database:
        if int(i['chat_id']) == cid:
            cond = False

    if cond == True:
        database.append({'chat_id': cid})
        with open(data_file_path('database.json'), 'w') as write_file:
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
        logging.info(f"[{_ts()}] [CHAT {cid}] Rollcall started: '{title}' (RC #{rc_index+1}) by {message.from_user.first_name} (@{message.from_user.username})")
        markup = await get_status_keyboard(rc_index+1)
        sent = await bot.send_message(message.chat.id, f"Roll call '{title}' started! ID: {rc_index+1}\nUse buttons below:", reply_markup=markup)
        _panel_msg_ids[(cid, rc_index + 1)] = sent.message_id
        #await bot.send_message(message.chat.id, f"Roll call with title: {title} started!\nRollcall id is set to {rc_index + 1}\nTo vote for this RollCall, please use ::RollCallID eg. /in ::{rc_index + 1}")

    except Exception as e:
        await bot.send_message(cid, e)

@bot.message_handler(func=lambda message: message.text.split(" ")[0].split("@")[0].lower() == "/start_template")
async def start_template(message):
    """
    Start a new rollcall from a saved template.

    Usage:
      /start_template template_name
      /start_template template_name "Extra title"
    """
    cid = message.chat.id
    parts = message.text.split(" ", 2)

    if len(parts) < 2:
        await bot.send_message(
            cid,
            "Usage:\n"
            "/start_template template_name [optional extra title]\n"
            'Example: /start_template sunday "With guests"'
        )
        return

    template_name = parts[1]
    extra = parts[2].strip() if len(parts) > 2 else ""

    tmpl = get_template(cid, template_name)
    if not tmpl:
        await bot.send_message(cid, f"Template '{template_name}' not found.")
        return

    # Build title from template + extra
    base_title = tmpl.get("title") or ""
    if extra:
        title = (base_title + " – " + extra).strip(" –")
    else:
        title = base_title or template_name

    # Create new rollcall via manager
    rc = manager.add_rollcall(cid, title)

    # Apply defaults from template (names match templates table)
    if tmpl.get("inlistlimit") is not None:
        rc.inListLimit = tmpl["inlistlimit"]
    if tmpl.get("location"):
        rc.location = tmpl["location"]
    if tmpl.get("eventfee"):
        rc.event_fee = tmpl["eventfee"]

    # Time offsets and calendar fields from template
    days = tmpl.get("offsetdays")
    hours = tmpl.get("offsethours")
    minutes = tmpl.get("offsetminutes")
    event_day = tmpl.get("event_day")
    event_time = tmpl.get("event_time")

    # Always get chat timezone first
    chat = manager.get_chat(cid)
    tzname = chat.get("timezone", "Asia/Calcutta")
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Calcutta")
        tzname = "Asia/Calcutta"

    rc.timezone = tzname
    rc.finalizeDate = None

    # 1) Prefer explicit event_day + event_time
    if event_day and event_time:
        dt = get_next_weekday_datetime(tz, event_day, event_time)
        if dt:
            rc.finalizeDate = dt

    # 2) Fallback: existing offsets logic (relative to now)
    if rc.finalizeDate is None and any(v is not None for v in (days, hours, minutes)):
        now = datetime.now(tz)
        delta = timedelta(
            days=days or 0,
            hours=hours or 0,
            minutes=minutes or 0,
        )
        rc.finalizeDate = now + delta

    rc.save()

    # Show initial panel using your existing helper
    rollcalls = manager.get_rollcalls(cid)
    rc_number = len(rollcalls)  # 1-based
    await show_panel_for_rollcall(cid, rc_number)


# SET / UPDATE A TEMPLATE
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_template")
async def set_template(message):
    try:
        cid = message.chat.id

        # Only admins for this chat
        if await admin_rights(message, manager) is False:
            await bot.send_message(cid, "Error - User does not have sufficient permissions for this operation")
            return

        msg = message.text.strip()
        # Normalize “ ” ‘ ’ to standard quotes
        msg = msg.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")

        parts = msg.split(" ", 2)
        if len(parts) < 2:
            await bot.send_message(
                cid,
                "Format:\n"
                "/set_template name \"Title\" [limit=N] [location=Place] [fee=\"Amount\"] "
                "[offset_days=D] [offset_hours=H] [offset_minutes=M] [event_day=weekday] [event_time=HH:MM]"
            )
            return

        name = parts[1]
        title = None
        tail = ""

        if len(parts) == 2:
            tail = ""
        else:
            tail = parts[2].strip()

        # --- extract quoted multi-word title if present ---
        if tail.startswith('"'):
            end_quote = tail.find('"', 1)
            if end_quote != -1:
                # "Wed Game" -> title = Wed Game, tail = rest
                title = tail[1:end_quote]
                tail = tail[end_quote + 1 :].strip()
            else:
                # No closing quote; take everything after first quote as title
                title = tail[1:]
                tail = ""
        else:
            # Optional: allow a single-word title before options
            # /set_template MG WedGame limit=...
            first_space = tail.find(" ")
            if first_space == -1 and tail:
                title = tail
                tail = ""
            elif first_space > 0:
                title = tail[:first_space]
                tail = tail[first_space + 1 :].strip()

        # Default values
        inlistlimit = None
        location = None
        eventfee = None
        offsetdays = None
        offsethours = None
        offsetminutes = None
        event_day = None
        event_time = None

        # key=value parsing
        tokens = tail.split()
        for tok in tokens:
            if "=" not in tok:
                continue
            key, val = tok.split("=", 1)
            key = key.strip().lower()
            val = val.strip().strip('"').strip("'")

            if key == "limit":
                try:
                    inlistlimit = int(val)
                except ValueError:
                    pass
            elif key == "location":
                location = val
            elif key == "fee":
                eventfee = val
            elif key == "offset_days":
                try:
                    offsetdays = int(val)
                except ValueError:
                    pass
            elif key == "offset_hours":
                try:
                    offsethours = int(val)
                except ValueError:
                    pass
            elif key == "offset_minutes":
                try:
                    offsetminutes = int(val)
                except ValueError:
                    pass
            elif key == "event_day":
                event_day = val.lower()  # e.g. wednesday
            elif key == "event_time":
                event_time = val         # e.g. 07:00

        ok = create_or_update_template(
            chatid=cid,
            name=name,
            title=title,
            inlistlimit=inlistlimit,
            location=location,
            eventfee=eventfee,
            offsetdays=offsetdays,
            offsethours=offsethours,
            offsetminutes=offsetminutes,
            event_day=event_day,
            event_time=event_time,
        )

        if ok:
            await bot.send_message(
                cid,
                f"Template '{name}' saved for this chat.\n"
                f"Title: {title or 'none'}\n"
                f"Limit: {inlistlimit if inlistlimit is not None else 'none'}\n"
                f"Location: {location or 'none'}\n"
                f"Fee: {eventfee or 'none'}\n"
                f"Offsets: days={offsetdays}, hours={offsethours}, minutes={offsetminutes}\n"
                f"Event_Day={event_day}, Event_Time={event_time}"
            )
        else:
            await bot.send_message(cid, "Failed to save template. Please try again.")

    except Exception as e:
        print(traceback.format_exc())
        await bot.send_message(message.chat.id, e)


# SET A ROLLCALL START TIME
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_rollcall_time")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/srt")
async def set_rollcall_time(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        if len(message.text.split(" ")) == 1:
            raise parameterMissing("invalid datetime format, refer help section for details")
        cid = message.chat.id
        msg = message.text
        rc_number = 0
        pmts = msg.split(" ")[1:]
        # IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
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
        date = datetime.strptime(input_datetime, "%d-%m-%Y %H:%M")
        date = tz.localize(date)
        now_date_string = datetime.now(pytz.timezone(rc.timezone)).strftime("%d-%m-%Y %H:%M")
        now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
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
            await bot.send_message(cid, f"Event notification time is set to {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')} {rc.timezone} for '{rc.title}' (ID: {rc_number + 1}).{backslash*2+'Reminder has been reset!' if changed else ''}")
            
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
            await bot.send_message(cid, f"Event notification time is set to {date.strftime('%d-%m-%Y %H:%M')} {rc.timezone} for '{rc.title}' (ID: {rc_number + 1}).{backslash*2+'Reminder has been reset!' if changed else ''}")
        
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
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        # IF NUMBER HAS 00:00 FORMAT
        if len(pmts) > 0 and pmts[0] != 'cancel' and len(pmts[0]) == 2:
            if pmts[0][0] == "0":
                pmts[0] = pmts[0][1]

        # IF RC_NUMBER IS SPECIFIED IN PARAMETERS THEN STORE THE VALUE
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
        
        if rc.finalizeDate - timedelta(hours=int(hour)) < datetime.now(pytz.timezone(rc.timezone)):
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
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        # IF RC_NUMBER IS SPECIFIED, STORE IT
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
        if rc.event_fee is None:
            raise parameterMissing("No event fee set. Use /event_fee to set one first.")
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
        
        if rc.finalizeDate == None:
            raise incorrectParameter("There is no start time for the event")

        await bot.send_message(cid, f"The event with title {rc.title} will start at {rc.finalizeDate.strftime('%d-%m-%Y %H:%M')}!")

    except Exception as e:
        await bot.send_message(cid, e)

# SET A LOCATION FOR A ROLLCALL
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/location")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/loc")
async def set_location(message):
    cid = message.chat.id
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if len(message.text.split(" ")) < 2:
            raise incorrectParameter("The correct format is /location <place>")
        msg = message.text
        pmts = msg.split(" ")[1:]
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

        msg = message.text
        cid = message.chat.id
        pmts = msg.split(" ")[1:]
        rc_number = 0

        if len(pmts) == 0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        if "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The rollcall number must be a positive integer")

        if len(pmts) == 0 or not str(pmts[0]).isdigit() or int(pmts[0]) <= 0:
            raise parameterMissing("Input limit is missing or it's not a positive number")

        limit = int(pmts[0])

        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)

        old_limit = rc.inListLimit
        was_full = old_limit is not None and len(rc.inList) >= int(old_limit)

        rc.inListLimit = limit
        rc.save()
        logging.info(f"[{_ts()}] Max limit of attendees is set to {limit}")
        await bot.send_message(cid, f"Max limit of attendees is set to {limit}")

        moved_from_in_to_wait = []
        moved_from_wait_to_in = []

        if len(rc.inList) > limit:
            moved_from_in_to_wait = rc.inList[limit:]
            rc.waitList.extend(rc.inList[limit:])
            rc.inList = rc.inList[:limit]
            for u in moved_from_in_to_wait:
                rc._save_user_to_db(u, 'waitlist')
            rc.save()

        elif len(rc.inList) < limit:
            available_slots = limit - len(rc.inList)
            moved_from_wait_to_in = rc.waitList[:available_slots]
            rc.inList.extend(rc.waitList[:available_slots])
            rc.waitList = rc.waitList[available_slots:]
            for u in moved_from_wait_to_in:
                rc._save_user_to_db(u, 'in')
            rc.save()

        if moved_from_in_to_wait:
            names = ", ".join(u.name for u in moved_from_in_to_wait)
            await bot.send_message(
                cid,
                f"{names} moved from IN to WAITING because limit was set to {limit} for '{rc.title}' (ID: {rc_number + 1})."
            )

        if moved_from_wait_to_in:
            for u in moved_from_wait_to_in:
                if isinstance(u.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name(u)} → IN (from WAITING) for '{rc.title}' (#{rc_number + 1})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        cid,
                        f"{u.name} → IN (from WAITING) for '{rc.title}' (#{rc_number + 1})",
                    )

                await notify_proxy_owner_wait_to_in(rc, u, cid, rc.title, rc_number + 1)

                rc_db_id = get_rc_db_id(rc)
                if rc_db_id is not None and isinstance(u.user_id, int):
                    increment_user_stat(cid, u.user_id, "total_waiting_to_in")
                    increment_user_stat(cid, u.user_id, "total_in")
                    increment_rollcall_stat(rc_db_id, "total_in")

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
        admin_id = message.from_user.id

        # Store pending delete and ask for confirmation
        _pending_deletes[(cid, admin_id)] = {'name': name, 'rc_number': rc_number}
        markup = InlineKeyboardMarkup(row_width=2)
        markup.add(
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"delconf_yes_{rc_number}_{admin_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"delconf_no_{rc_number}_{admin_id}"),
        )
        await bot.send_message(
            cid,
            f"⚠️ Remove *{name}* from rollcall #{rc_number + 1}?",
            parse_mode="Markdown",
            reply_markup=markup,
        )

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: message.text.split(" ")[0].split("@")[0].lower() == "/delete_template")
async def delete_template_command(message):
    """
    Delete a template from this chat.
    Usage: /delete_template name
    """
    cid = message.chat.id

    if await admin_rights(message, manager) is False:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return

    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await bot.send_message(
            cid,
            "Usage:\n/delete_template name\nExample: /delete_template sunday",
        )
        return

    name = parts[1].strip()
    ok = delete_template(cid, name)
    if ok:
        await bot.send_message(cid, f"Template '{name}' deleted.")
    else:
        await bot.send_message(cid, f"Template '{name}' not found or could not be deleted.")


# RESUME NOTIFICATIONS
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/shh")
async def shh(message):
    manager.set_shh_mode(message.chat.id, True)
    await bot.send_message(message.chat.id, "Ok, i will keep quiet!")

# NON RESUME NOTIFICATIONS
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/louder")
async def louder(message):
    manager.set_shh_mode(message.chat.id, False)
    await bot.send_message(message.chat.id, "Ok, i can hear you!")

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return
        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
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
        _username = message.from_user.username or None
        _display_name = _get_display_name(message.from_user)
        if not _username:
            asyncio.create_task(warn_no_username(cid, _display_name))
        user = User(_display_name, _username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        # Ghost reconfirmation check
        if isinstance(user.user_id, int) and manager.get_ghost_tracking_enabled(cid):
            ghost_count = get_ghost_count(cid, user.user_id)
            absent_limit = manager.get_absent_limit(cid)
            if ghost_count >= absent_limit:
                _pending_reconf[(cid, user.user_id)] = {'rc_number': rc_number, 'comment': comment}
                markup = InlineKeyboardMarkup(row_width=2)
                markup.add(
                    InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=f"reconf_in_{rc_number}_{user.user_id}"),
                    InlineKeyboardButton("❌ I'm out", callback_data=f"reconf_out_{rc_number}_{user.user_id}"),
                )
                await bot.send_message(
                    cid,
                    f"👻 *Warning:* You've ghosted *{ghost_count}* session(s) before.\n"
                    f"⚠️ Absent Limit: *{absent_limit}*\n\n"
                    f"Are you committing to be at *{rc.title}*?",
                    parse_mode="Markdown",
                    reply_markup=markup
                )
                return

        # Keep the members table current so /buzz has fresh names
        if isinstance(user.user_id, int):
            upsert_chat_member(cid, user.user_id, _display_name, _username)

        result = rc.addIn(user)
        rc.save()

        # --- Stats: record IN only if user actually joined inList (not duplicate or waitlisted) ---
        rc_db_id = get_rc_db_id(rc)
        if result not in ('AB', 'AC') and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_in")
            increment_rollcall_stat(rc_db_id, "total_in")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif result == 'AC':
            await bot.send_message(cid, f"Event max limit is reached, {user.name} was added in waitlist")

        if send_list(message, manager):
            await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return
        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
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
        _username = message.from_user.username or None
        _display_name = _get_display_name(message.from_user)
        if not _username:
            asyncio.create_task(warn_no_username(cid, _display_name))
        user = User(_display_name, _username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        # Keep the members table current so /buzz has fresh names
        if isinstance(user.user_id, int):
            upsert_chat_member(cid, user.user_id, _display_name, _username)

        # Capture state BEFORE addOut
        was_in = any(u.user_id == user.user_id for u in rc.inList)

        result = rc.addOut(user)
        rc.save()

        # --- Stats: record OUT only if state actually changed (not duplicate) ---
        rc_db_id = get_rc_db_id(rc)
        if result != 'AB' and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_out")
            increment_rollcall_stat(rc_db_id, "total_out")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            # Someone moved from WAITING to IN
            if isinstance(result.user_id, int):
                await bot.send_message(
                    cid,
                    f"{format_mention_with_name(result)} → IN",
                    parse_mode="Markdown",
                )
            else:
                await bot.send_message(cid, f"{result.name} → IN")

            # Notify proxy creator if this is a proxy
            await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number + 1)

        # IN → OUT notification
        if was_in and any(u.user_id == user.user_id for u in rc.outList):
            await bot.send_message(
                cid,
                f"{format_mention_with_name(user)} → OUT for '{rc.title}' (#{rc_number + 1})",
                parse_mode="Markdown",
            )

        if send_list(message, manager):
            await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return
        msg = message.text
        pmts = msg.split(" ")
        cid = message.chat.id
        comment = ""
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
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
        _username = message.from_user.username or None
        _display_name = _get_display_name(message.from_user)
        if not _username:
            asyncio.create_task(warn_no_username(cid, _display_name))
        user = User(_display_name, _username, message.from_user.id, rc.allNames)

        arr = msg.split(" ")
        if len(arr) > 1:
            arr.pop(0)
            comment = ' '.join(arr)
        user.comment = comment

        # Keep the members table current so /buzz has fresh names
        if isinstance(user.user_id, int):
            upsert_chat_member(cid, user.user_id, _display_name, _username)

        result = rc.addMaybe(user)
        rc.save()

        # --- Stats: record MAYBE only if state actually changed (not duplicate) ---
        rc_db_id = get_rc_db_id(rc)
        if result != 'AB' and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(cid, user.user_id, "total_maybe")
            increment_rollcall_stat(rc_db_id, "total_maybe")

        if result == 'AB':
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        elif isinstance(result, User):
            # Someone moved from WAITING to IN (when user moved from IN to MAYBE, freeing a slot)
            if type(result.user_id) == int:
                await bot.send_message(
                    cid,
                    f"{'@'+result.username if result.username != None else f'[{result.name}](tg://user?id={result.user_id})'} now you are in!",
                    parse_mode="Markdown"
                )
            else:
                await bot.send_message(cid, f"{result.name} now you are in!")

        if send_list(message, manager):
            await _update_panel(cid, rc_number + 1, rc)

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
        if len(pmts) > 0 and "::" in pmts[-1]:
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
            
            # Check ghost count and ask confirmation if at/above limit
            ghost_count = get_ghost_count_by_proxy_name(cid, proxy_name)
            if ghost_count > 0:
                limit = manager.get_absent_limit(cid)
                if ghost_count >= limit:
                    # Ask confirmation with buttons (consistent UI with /in)
                    markup = InlineKeyboardMarkup(row_width=2)
                    markup.add(
                        InlineKeyboardButton("✅ Yes, add anyway", callback_data=f"proxy_add_{rc_number}_{proxy_name}"),
                        InlineKeyboardButton("❌ Cancel", callback_data=f"proxy_cancel_{rc_number}_{proxy_name}"),
                    )
                    await bot.send_message(
                        cid,
                        f"👻 *Warning:* *{proxy_name}* has ghosted *{ghost_count}* session(s) before.\n"
                        f"⚠️ Absent Limit: *{limit}*\n\n"
                        f"Still add to *{rc.title}*?",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                    return
            
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
                        f"{format_mention_with_name(result)} now you are in!",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(cid, f"{result.name} now you are in!")

            #if send_list(message, manager):
            #    await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))
            
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

        if len(pmts) > 0 and "::" in pmts[-1]:
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
                        f"{format_mention_with_name(result)} → IN",
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

            #if send_list(message, manager):
            #    await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))

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

        if len(pmts) > 0 and "::" in pmts[-1]:
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

            # Always show updated panel for this rollcall
            await show_panel_for_rollcall(cid, rc_number + 1)

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
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")[1:]
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
                
        title = " ".join(pmts)
        user = message.from_user.first_name

        if title == "":
            title = "<Empty>"

        rc = manager.get_rollcall(cid, rc_number)
        rc.title = title
        rc.save()
        
        await bot.send_message(cid, 'The roll call title is set to: ' + title)
        logging.info(f"[{_ts()}] Title changed: {user} -> {title}")

    except Exception as e:
        await bot.send_message(message.chat.id, e)

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        pmts = message.text.split(" ")[1:]
        rc_number = 0
        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except:
                raise incorrectParameter("The RollCallnumber must be a positive integer")
        rollcalls = manager.get_rollcalls(cid)
        if len(rollcalls) < rc_number + 1:
            raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")
        rc = manager.get_rollcall(cid, rc_number)
        # Capture ghost tracking info before removing from manager
        rc_db_id = rc.id
        ghost_tracking_on = manager.get_ghost_tracking_enabled(cid)
        
        # Check BOTH real users and proxy users in IN list
        in_users = rc.inList
        has_any_users = len(in_users) > 0

        # Update attendance streaks for real users who were IN
        for u in in_users:
            if isinstance(u.user_id, int):
                update_streak_on_checkin(cid, u.user_id)

        # End current rollcall
        await bot.send_message(message.chat.id, "🎉 Roll ended!")
        await bot.send_message(cid, rc.finishList().replace("__RCID__", str(rc_number + 1)))
        logging.info(f"[{_ts()}] Rollcall ended: '{rc.title}' (RC #{rc_number+1})")
        _panel_msg_ids.pop((cid, rc_number + 1), None)
        manager.remove_rollcall(cid, rc_number)
        logging.info(f"[{_ts()}] [CHAT {cid}] Rollcall ended: '{rc.title}' by {message.from_user.first_name} (@{message.from_user.username})")
        # Ghost tracking prompt - ask if ANY users were in rollcall (real OR proxy)
        if ghost_tracking_on and has_any_users and rc_db_id and not rc.absent_marked:
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rc_db_id}"),
                InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{rc_db_id}")
            )
            await bot.send_message(cid, "👻 Did anyone ghost today's session?", reply_markup=markup)
        # warning + optional re-broadcast
        updated_rollcalls = manager.get_rollcalls(cid)
        if len(updated_rollcalls) > 0:
            await bot.send_message(
                cid,
                "⚠️ Active rollcall IDs have been updated because one rollcall was ended.\n"
                "Use /rollcalls to see the current list and IDs."
            )
            for rollcall in updated_rollcalls:
                new_id = updated_rollcalls.index(rollcall) + 1
                text = f"Rollcall number {new_id}\n\n" + rollcall.allList().replace("__RCID__", str(new_id))
                await bot.send_message(cid, text)
    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/stats", "/s"])
async def stats_command(message):
    """
    /stats or /s -> my stats in this chat
    /stats group -> group totals
    /stats @username / name -> that user's stats in this chat
    /stats top -> top users by IN count in this chat
    /stats ghost -> ghost leaderboard for this chat
    /stats bot -> bot-wide statistics (OWNER ONLY)
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
        elif lower_arg in ["ghost", "ghosts", "absent"]:
            scope = "ghost"
        elif lower_arg in ["bot", "global", "all"]:
            # ✅ BOT-WIDE STATS - OWNER ONLY CHECK
            if message.from_user.id not in ADMINS:
                await bot.send_message(
                    cid, 
                    "⛔ Bot-wide statistics are restricted to bot administrators only."
                )
                return
            scope = "bot"
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
        elif scope == "ghost":
            text = await build_ghost_stats_text(cid, manager)
        elif scope == "bot":
            text = await build_bot_stats_text()
        else:
            text = await build_user_stats_text(cid, target_user_id, display_name)

        await bot.send_message(cid, text, parse_mode="Markdown")
    except Exception as e:
        logging.exception("Error in /stats")
        await bot.send_message(cid, "Error while fetching stats, please try again later.")


async def build_ghost_stats_text(cid: int, mgr) -> str:
    """Build the ghost leaderboard text for /stats ghost."""
    leaderboard = get_ghost_leaderboard(cid)
    limit = mgr.get_absent_limit(cid)
    tracking_on = mgr.get_ghost_tracking_enabled(cid)

    if not leaderboard:
        return "🏆 No ghosts yet — everyone's been showing up!"

    lines = ["👻 *Ghost Leaderboard*", "─────────────────"]
    for i, entry in enumerate(leaderboard, 1):
        # Handle both real users and proxy users
        if entry.get('proxy_name'):
            name = f"{entry['proxy_name']} (via /sif)"
        else:
            name = entry.get('user_name') or f"User {entry['user_id']}"
        count = entry['ghost_count']
        warning = " ⚠️" if count >= limit else ""
        lines.append(f"{i}. {name} — {count} session(s) ghosted{warning}")

    lines.append("")
    lines.append(f"Current ghost limit: {limit} session(s)")
    if not tracking_on:
        lines.append("_(Ghost tracking is currently disabled for this group)_")

    return "\n".join(lines)


async def build_bot_stats_text() -> str:
    """
    Build bot-wide statistics (all chats combined).
    Only accessible to bot owners.
    """
    from db import get_connection, db_type
    
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Total unique groups
        if db_type == "postgresql":
            cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM chats")
        else:
            cursor.execute("SELECT COUNT(DISTINCT chat_id) FROM chats")
        total_groups = cursor.fetchone()[0]
        
        # Active groups (last 7 days)
        if db_type == "postgresql":
            cursor.execute("""
                SELECT COUNT(DISTINCT chat_id) 
                FROM rollcalls 
                WHERE created_at >= NOW() - INTERVAL '7 days'
            """)
        else:
            cursor.execute("""
                SELECT COUNT(DISTINCT r.chat_id) 
                FROM rollcalls r
                WHERE r.created_at >= datetime('now', '-7 days')
            """)
        active_groups_7d = cursor.fetchone()[0]
        
        # Active groups (last 30 days)
        if db_type == "postgresql":
            cursor.execute("""
                SELECT COUNT(DISTINCT chat_id) 
                FROM rollcalls 
                WHERE created_at >= NOW() - INTERVAL '30 days'
            """)
        else:
            cursor.execute("""
                SELECT COUNT(DISTINCT r.chat_id) 
                FROM rollcalls r
                WHERE r.created_at >= datetime('now', '-30 days')
            """)
        active_groups_30d = cursor.fetchone()[0]
        
        # Total rollcalls created
        if db_type == "postgresql":
            cursor.execute("SELECT COUNT(*) FROM rollcalls")
        else:
            cursor.execute("SELECT COUNT(*) FROM rollcalls")
        total_rollcalls = cursor.fetchone()[0]
        
        # Rollcalls last 30 days
        if db_type == "postgresql":
            cursor.execute("""
                SELECT COUNT(*) FROM rollcalls 
                WHERE created_at >= NOW() - INTERVAL '30 days'
            """)
        else:
            cursor.execute("""
                SELECT COUNT(*) FROM rollcalls 
                WHERE created_at >= datetime('now', '-30 days')
            """)
        rollcalls_30d = cursor.fetchone()[0]
        
        # Total unique users participated
        if db_type == "postgresql":
            cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")
        else:
            cursor.execute("SELECT COUNT(DISTINCT user_id) FROM users")
        total_users = cursor.fetchone()[0]
        
        # Total templates created
        if db_type == "postgresql":
            cursor.execute("SELECT COUNT(*) FROM templates")
        else:
            cursor.execute("SELECT COUNT(*) FROM templates")
        total_templates = cursor.fetchone()[0]
        
        # Total IN/OUT/MAYBE across all chats
        if db_type == "postgresql":
            cursor.execute("""
                SELECT 
                    SUM(total_in) as sum_in,
                    SUM(total_out) as sum_out,
                    SUM(total_maybe) as sum_maybe
                FROM user_stats
            """)
        else:
            cursor.execute("""
                SELECT 
                    SUM(total_in) as sum_in,
                    SUM(total_out) as sum_out,
                    SUM(total_maybe) as sum_maybe
                FROM user_stats
            """)
        
        row = cursor.fetchone()
        if isinstance(row, dict):
            data = row
        else:
            cols = [d[0] for d in cursor.description]
            data = {cols[i]: row[i] for i in range(len(cols))}
        
        sum_in = data.get("sum_in") or 0
        sum_out = data.get("sum_out") or 0
        sum_maybe = data.get("sum_maybe") or 0
        
        lines = [
            "*🤖 Bot-Wide Statistics*",
            "",
            "*Groups:*",
            f"🏘️ Total: {total_groups}",
            f"✅ Active (7d): {active_groups_7d}",
            f"✅ Active (30d): {active_groups_30d}",
            "",
            "*Rollcalls:*",
            f"📋 Total: {total_rollcalls}",
            f"📈 Last 30d: {rollcalls_30d}",
            "",
            "*Users:*",
            f"👥 Total: {total_users}",
            f"✅ Total IN: {sum_in}",
            f"❌ Total OUT: {sum_out}",
            f"🤔 Total MAYBE: {sum_maybe}",
            "",
            f"📝 Templates: {total_templates}",
        ]
        
        return "\n".join(lines)
        
    finally:
        if db_type == "postgresql":
            cursor.close()
            from db import release_connection
            release_connection(conn)


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
                SELECT total_in, total_out, total_maybe, total_waiting_to_in,
                       total_rollcalls, current_streak, best_streak
                FROM user_stats
                WHERE chat_id = %s AND user_id = %s
                """,
                (chat_id, user_id),
            )
        else:
            cursor.execute(
                """
                SELECT total_in, total_out, total_maybe, total_waiting_to_in,
                       total_rollcalls, current_streak, best_streak
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

        total_in       = data.get("total_in", 0) or 0
        total_out      = data.get("total_out", 0) or 0
        total_maybe    = data.get("total_maybe", 0) or 0
        total_wait     = data.get("total_waiting_to_in", 0) or 0
        total_rc       = data.get("total_rollcalls", 0) or 0
        cur_streak     = data.get("current_streak", 0) or 0
        best_streak    = data.get("best_streak", 0) or 0

        attendance_pct = f"{round(total_in / total_rc * 100)}%" if total_rc > 0 else "—"

        lines = [
            f"*Stats for {first_name}:*",
            "",
            f"✅ IN: {total_in}",
            f"❌ OUT: {total_out}",
            f"🤔 MAYBE: {total_maybe}",
            f"⏫ From WAITING → IN: {total_wait}",
            f"📋 Total rollcalls: {total_rc}",
            f"📊 Attendance rate: {attendance_pct}",
            "",
            f"🔥 Current streak: {cur_streak} session(s)",
            f"🏆 Best streak: {best_streak} session(s)",
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


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/history")
async def history_command(message):
    """
    /history [N] — Show the last N ended rollcalls for this chat (default 10, max 20).
    """
    cid = message.chat.id
    try:
        parts = message.text.strip().split()
        limit = 10
        if len(parts) > 1:
            try:
                limit = max(1, min(20, int(parts[1])))
            except ValueError:
                pass

        records = get_rollcall_history(cid, limit)
        if not records:
            await bot.send_message(cid, "No ended rollcalls found for this chat yet.")
            return

        lines = [f"*📋 Last {len(records)} rollcall(s):*", ""]
        for i, r in enumerate(records, 1):
            ended = _fmt_ended_at(r.get("ended_at"))
            title = r.get("title") or "Untitled"
            in_count = r.get("in_count", 0)
            ghost_count = r.get("ghost_count", 0)
            ghost_str = f"  👻 {ghost_count}" if ghost_count else ""
            lines.append(f"{i}. *{title}* — {ended}  ✅ {in_count}{ghost_str}")

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logging.exception("Error in /history")
        await bot.send_message(cid, "Error fetching history, please try again later.")


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/buzz")
async def buzz_command(message):
    """
    /buzz [message] [::N]
    Ping users who have not yet voted in the active rollcall.
    If no rollcall is running, pings all active known group members.
    Supports ::N to target a specific rollcall when multiple are open.
    Before pinging, verifies each member is still in the group and marks
    leavers inactive in the DB so they won't be pinged again.

    Admin-only.
    """
    cid = message.chat.id
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        msg = message.text.strip()
        parts = msg.split()

        # Parse optional ::N rollcall selector and optional custom message
        rc_number = 0
        custom_msg = None
        filtered = []
        for part in parts[1:]:
            if part.startswith("::"):
                try:
                    rc_number = int(part.replace("::", "")) - 1
                except ValueError:
                    pass
            else:
                filtered.append(part)
        if filtered:
            custom_msg = " ".join(filtered)

        no_rollcall = roll_call_not_started(message, manager) == False

        # Load candidates from DB
        candidates = get_active_members(cid)
        if not candidates:
            await bot.send_message(
                cid,
                "No known group members yet. Members are recorded the first time they vote in any rollcall."
            )
            return

        # If a rollcall is running, exclude members who have already voted
        if not no_rollcall:
            rollcalls = manager.get_rollcalls(cid)
            if len(rollcalls) <= rc_number:
                raise incorrectParameter(
                    f"Rollcall #{rc_number + 1} doesn't exist. Check /rollcalls."
                )
            rc = manager.get_rollcall(cid, rc_number)
            voted_ids = {
                u.user_id for u in rc.inList + rc.outList + rc.maybeList + rc.waitList
                if isinstance(u.user_id, int)
            }
            candidates = [u for u in candidates if u['user_id'] not in voted_ids]

            if not candidates:
                await bot.send_message(
                    cid,
                    f"✅ Everyone the bot knows has already voted on *{rc.title}*!",
                    parse_mode="Markdown"
                )
                return

        # Verify each candidate is still in the group (concurrent API calls)
        async def _check_member(u):
            uid = u['user_id']
            try:
                member = await bot.get_chat_member(cid, uid)
                if member.status in ("left", "kicked"):
                    mark_member_inactive(cid, uid)
                    return None
                return u
            except Exception:
                # User not found or API error — skip but don't mark inactive
                return None

        results = await asyncio.gather(*[_check_member(u) for u in candidates])
        to_ping = [u for u in results if u is not None]

        if not to_ping:
            if no_rollcall:
                await bot.send_message(cid, "All known members appear to have left the group.")
            else:
                await bot.send_message(
                    cid,
                    f"✅ Everyone the bot knows has already voted on *{rc.title}*!",
                    parse_mode="Markdown"
                )
            return

        mentions = _build_mention_list(to_ping)

        if no_rollcall:
            note = custom_msg or "Just a heads-up from the group! 👋"
            await bot.send_message(cid, f"📣 {note}\n\n{mentions}", parse_mode="Markdown")
        else:
            note = custom_msg or f"rollcall *{rc.title}* is open — have you voted?"
            await bot.send_message(cid, f"👋 Hey {mentions}\n\n{note}", parse_mode="Markdown")

    except (rollCallNotStarted, incorrectParameter, insufficientPermissions,
            parameterMissing) as e:
        await bot.send_message(cid, str(e))
    except Exception:
        logging.exception("Error in /buzz")
        await bot.send_message(cid, "Error running buzz, please try again later.")


def _build_mention_list(users: list) -> str:
    """Build a space-separated string of Telegram user mentions.

    Prefers @username (plain text, always renders) over inline tg:// links.
    Inline links show the stored first_name but only work in parse_mode=Markdown
    and require the user to have interacted with the bot at some point.
    Both forms produce a notification ping for the mentioned user.
    """
    parts = []
    for u in users:
        uid = u.get('user_id')
        username = u.get('username')
        name = (u.get('first_name') or username or str(uid)).strip()
        # Escape Markdown special chars in the display name so the link renders
        safe_name = name.replace("[", "\\[").replace("]", "\\]").replace("_", "\\_").replace("*", "\\*")
        if username:
            parts.append(f"@{username}")
        elif uid:
            parts.append(f"[{safe_name}](tg://user?id={uid})")
    return " ".join(parts)


async def _update_panel(cid: int, rc_number: int, rc) -> None:
    """Edit the existing panel message in-place; fall back to a new send if needed.

    ``rc_number`` is 1-based (the public rollcall number shown to users).
    Stores/updates ``_panel_msg_ids[(cid, rc_number)]`` so future calls always
    target the right message.
    """
    text = rc.allList().replace("__RCID__", str(rc_number))
    markup = await get_status_keyboard(rc_number)
    key = (cid, rc_number)

    existing_msg_id = _panel_msg_ids.get(key)
    if existing_msg_id:
        try:
            await bot.edit_message_text(
                text, cid, existing_msg_id, reply_markup=markup
            )
            return  # success — panel updated in-place
        except Exception as e:
            err = str(e).lower()
            if "message is not modified" in err:
                return  # content unchanged — fine, nothing to do
            # Message was deleted, too old, or otherwise unreachable — fall through
            logging.debug(f"Panel edit failed for ({cid}, {rc_number}): {e}")

    # No stored panel or edit failed — send a fresh panel and remember its id
    sent = await bot.send_message(cid, text, reply_markup=markup)
    _panel_msg_ids[key] = sent.message_id


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

        sent = await bot.send_message(cid, text, reply_markup=markup)
        # Register as the new panel to target for future in-place edits
        _panel_msg_ids[(cid, rc_number + 1)] = sent.message_id

    except Exception as e:
        await bot.send_message(message.chat.id, e)



# ===== Inline keyboards for rollcall UI =====
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
    markup.add(
        InlineKeyboardButton(f"🛑 End RollCall #{rc_number}", callback_data=f"btn_end_{rc_number}")
    )
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


async def get_end_confirm_keyboard(rc_number: int) -> InlineKeyboardMarkup:
    """Keyboard to confirm or cancel ending a rollcall."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Yes", callback_data=f"btn_endconfirm_{rc_number}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"btn_endcancel_{rc_number}"),
    )
    return markup


@bot.callback_query_handler(func=lambda call: call.data and (
    call.data.startswith("ghost_") or call.data.startswith("reconf_") or call.data.startswith("mabs_")
    or call.data.startswith("proxy_add_") or call.data.startswith("proxy_cancel_")
    or call.data.startswith("delconf_")
))
async def ghost_callback_handler(call):
    """
    Handle ghost tracking and reconfirmation inline keyboard callbacks.

    Patterns:
      ghost_yes_<rc_db_id>           - organiser says yes, someone ghosted — show IN list
      ghost_no_<rc_db_id>            - organiser says no — mark session processed
      ghost_tog_<rc_db_id>_<user_id> - toggle a user's ghost selection
      ghost_done_<rc_db_id>          - confirm ghost selections and save
      reconf_in_<rc_num>_<user_id>   - user confirms IN after ghost warning
      reconf_maybe_<rc_num>_<user_id>- user downgrades to MAYBE after warning
      reconf_out_<rc_num>_<user_id>  - user opts out after warning
      mabs_sel_<rc_db_id>            - admin selects a rollcall in /mark_absent
    """
    try:
        cid = call.message.chat.id
        data = call.data

        # ----------------------------------------------------------------
        # ghost_no — all showed up, mark session as processed
        # ----------------------------------------------------------------
        if data.startswith("ghost_no_"):
            rc_db_id = int(data.split("_", 2)[2])
            mark_rollcall_absent_done(rc_db_id)
            _ghost_selections.pop((cid, rc_db_id), None)
            await bot.answer_callback_query(call.id, "✅ Got it!")
            await bot.edit_message_text(
                "✅ No ghosts — everyone showed up! Great session! 🎉",
                cid, call.message.message_id
            )
            return

        # ----------------------------------------------------------------
        # ghost_yes — show IN list for selection
        # ----------------------------------------------------------------
        if data.startswith("ghost_yes_"):
            rc_db_id = int(data.split("_", 2)[2])
            in_users = get_rollcall_in_users(rc_db_id)
            if not in_users:
                await bot.answer_callback_query(call.id, "No IN users found for this session.")
                return
            # Load persisted selections from DB or start fresh
            from db import load_ghost_selections
            saved = load_ghost_selections(cid, rc_db_id)
            _ghost_selections[(cid, rc_db_id)] = saved if saved else set()
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, _ghost_selections[(cid, rc_db_id)])
            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                "👻 Who ghosted? Tap to select, then tap Done.",
                cid, call.message.message_id, reply_markup=markup
            )
            return

        # ----------------------------------------------------------------
        # ghost_togp — toggle a PROXY user's ghost selection (added via /sif)
        # Must be checked before ghost_tog_ to avoid prefix-match collision.
        # ----------------------------------------------------------------
        if data.startswith("ghost_togp_"):
            parts = data.split("_", 3)   # ["ghost", "togp", rc_db_id, name]
            rc_db_id = int(parts[2])
            proxy_name = parts[3]
            key = (cid, rc_db_id)
            if key not in _ghost_selections:
                from db import load_ghost_selections
                saved = load_ghost_selections(cid, rc_db_id)
                _ghost_selections[key] = saved if saved else set()
            if proxy_name in _ghost_selections[key]:
                _ghost_selections[key].discard(proxy_name)
            else:
                _ghost_selections[key].add(proxy_name)
            logging.info(f"[{_ts()}] ghost_togp: {proxy_name}, key={key}, selected now={_ghost_selections[key]}")
            save_ghost_selections(cid, rc_db_id, _ghost_selections[key])
            in_users = get_rollcall_in_users(rc_db_id)
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, _ghost_selections[key])
            await bot.answer_callback_query(call.id)
            try:
                await bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ----------------------------------------------------------------
        # ghost_tog — toggle a real user's ghost selection
        # ----------------------------------------------------------------
        if data.startswith("ghost_tog_"):
            parts = data.split("_")
            rc_db_id = int(parts[2])
            user_id = int(parts[3])
            key = (cid, rc_db_id)
            if key not in _ghost_selections:
                from db import load_ghost_selections
                saved = load_ghost_selections(cid, rc_db_id)
                _ghost_selections[key] = saved if saved else set()
            if user_id in _ghost_selections[key]:
                _ghost_selections[key].discard(user_id)
            else:
                _ghost_selections[key].add(user_id)
            save_ghost_selections(cid, rc_db_id, _ghost_selections[key])
            in_users = get_rollcall_in_users(rc_db_id)
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, _ghost_selections[key])
            await bot.answer_callback_query(call.id)
            try:
                await bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=markup)
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # ----------------------------------------------------------------
        # ghost_done — save ghost records and confirm
        # ----------------------------------------------------------------
        if data.startswith("ghost_done_"):
            rc_db_id = int(data.split("_", 2)[2])
            key = (cid, rc_db_id)
            if key not in _ghost_selections:
                # Bot may have restarted — restore selections from DB
                from db import load_ghost_selections
                saved = load_ghost_selections(cid, rc_db_id)
                if saved:
                    _ghost_selections[key] = saved
            selected = _ghost_selections.pop(key, set())
            mark_rollcall_absent_done(rc_db_id)
            
            logging.info(f"[{_ts()}] ghost_done: key={key}, selected from map={selected}")

            if not selected:
                await bot.answer_callback_query(call.id, "No ghosts selected — marking all as attended.")
                await bot.edit_message_text("✅ No ghosts selected — all marked as attended.", cid, call.message.message_id)
                return

            in_users = get_rollcall_in_users(rc_db_id)
            # Build separate maps for real users (int id) and proxy users (str name)
            user_map = {u['user_id']: u for u in in_users if u['user_id'] is not None}
            proxy_map = {u['proxy_name']: u for u in in_users if u.get('proxy_name') is not None}
            lines = []
            
            logging.info(f"[{_ts()}] Ghost callback: in_users={[u.get('first_name') or u.get('proxy_name') for u in in_users]}, selected={selected}")

            for item in selected:
                if isinstance(item, int):
                    # Real Telegram user — track ghost count in DB
                    u = user_map.get(item)
                    if not u:
                        logging.warning(f"[{_ts()}] Ghost: real user {item} not found in user_map")
                        continue
                    name = u.get('first_name') or u.get('username') or str(item)
                    logging.info(f"[{_ts()}] Ghosting real user: {name}")
                    increment_ghost_count(cid, item, name)
                    add_ghost_event(rc_db_id, cid, item, name)
                    reset_streak_on_ghost(cid, item)
                    new_count = get_ghost_count(cid, item)
                    lines.append(f"👻 {name} — ghosted {new_count} session(s) total")
                else:
                    # Proxy user added via /sif — track ghost count for proxy user
                    proxy_name = str(item)
                    if proxy_name not in proxy_map:
                        logging.warning(f"[{_ts()}] Ghost: proxy {proxy_name} not found in proxy_map: {list(proxy_map.keys())}")
                        continue
                    logging.info(f"[{_ts()}] Ghosting proxy user: {proxy_name}")
                    increment_ghost_count(cid, -1, proxy_name, proxy_name=proxy_name)
                    add_ghost_event(rc_db_id, cid, None, proxy_name=proxy_name)
                    new_count = get_ghost_count_by_proxy_name(cid, proxy_name)
                    lines.append(f"👻 {proxy_name} (via /sif) — ghosted {new_count} session(s) total")

            summary = "\n".join(lines)
            await bot.answer_callback_query(call.id, f"{len(selected)} ghost(s) recorded.")
            await bot.edit_message_text(
                f"👻 Ghost session recorded!\n\n{summary}",
                cid, call.message.message_id
            )
            return

        # ----------------------------------------------------------------
        # reconf_in — user confirms they will show up
        # ----------------------------------------------------------------
        if data.startswith("reconf_in_"):
            parts = data.split("_")
            rc_number = int(parts[2])
            uid = int(parts[3])
            if call.from_user.id != uid:
                await bot.answer_callback_query(call.id, "This confirmation is not for you.")
                return
            state = _pending_reconf.pop((cid, uid), {})
            rollcalls = manager.get_rollcalls(cid)
            if rc_number >= len(rollcalls):
                await bot.answer_callback_query(call.id, "Roll call no longer active.")
                return
            rc = rollcalls[rc_number]
            _username = call.from_user.username or None
            user = User(_get_display_name(call.from_user), _username, uid, rc.allNames)
            user.comment = state.get('comment', '')
            result = rc.addIn(user)
            rc.save()
            rc_db_id = get_rc_db_id(rc)
            if result not in ('AB', 'AC') and rc_db_id and isinstance(uid, int):
                increment_user_stat(cid, uid, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")
            await bot.answer_callback_query(call.id, "💪 You're IN!")
            await bot.edit_message_text(
                f"💪 {user.name} committed to IN!\n\n{rc.allList().replace('__RCID__', str(rc_number + 1))}",
                cid, call.message.message_id
            )
            return

        # ----------------------------------------------------------------
        # reconf_maybe — user downgrades to MAYBE
        # ----------------------------------------------------------------
        if data.startswith("reconf_maybe_"):
            parts = data.split("_")
            rc_number = int(parts[2])
            uid = int(parts[3])
            if call.from_user.id != uid:
                await bot.answer_callback_query(call.id, "This confirmation is not for you.")
                return
            _pending_reconf.pop((cid, uid), None)
            rollcalls = manager.get_rollcalls(cid)
            if rc_number >= len(rollcalls):
                await bot.answer_callback_query(call.id, "Roll call no longer active.")
                return
            rc = rollcalls[rc_number]
            _username = call.from_user.username or None
            user = User(_get_display_name(call.from_user), _username, uid, rc.allNames)
            rc.addMaybe(user)
            rc.save()
            rc_db_id = get_rc_db_id(rc)
            if rc_db_id and isinstance(uid, int):
                increment_user_stat(cid, uid, "total_maybe")
                increment_rollcall_stat(rc_db_id, "total_maybe")
            await bot.answer_callback_query(call.id, "🤔 Marked as Maybe")
            await bot.edit_message_text(
                f"🤔 {user.name} marked as Maybe.\n\n{rc.allList().replace('__RCID__', str(rc_number + 1))}",
                cid, call.message.message_id
            )
            return

        # ----------------------------------------------------------------
        # reconf_out — user opts out
        # ----------------------------------------------------------------
        if data.startswith("reconf_out_"):
            parts = data.split("_")
            rc_number = int(parts[2])
            uid = int(parts[3])
            if call.from_user.id != uid:
                await bot.answer_callback_query(call.id, "This confirmation is not for you.")
                return
            _pending_reconf.pop((cid, uid), None)
            rollcalls = manager.get_rollcalls(cid)
            if rc_number >= len(rollcalls):
                await bot.answer_callback_query(call.id, "Roll call no longer active.")
                return
            rc = rollcalls[rc_number]
            _username = call.from_user.username or None
            user = User(_get_display_name(call.from_user), _username, uid, rc.allNames)
            rc.addOut(user)
            rc.save()
            rc_db_id = get_rc_db_id(rc)
            if rc_db_id and isinstance(uid, int):
                increment_user_stat(cid, uid, "total_out")
                increment_rollcall_stat(rc_db_id, "total_out")
            await bot.answer_callback_query(call.id, "❌ Marked as Out")
            await bot.edit_message_text(
                f"❌ {user.name} is out.\n\n{rc.allList().replace('__RCID__', str(rc_number + 1))}",
                cid, call.message.message_id
            )
            return

        # ----------------------------------------------------------------
        # proxy_add_ — confirmed adding ghosted proxy user
        # ----------------------------------------------------------------
        if data.startswith("proxy_add_"):
            parts = data.split("_", 3)  # ["proxy", "add", rc_number, proxy_name]
            rc_number = int(parts[2])
            proxy_name = parts[3]
            
            rc = manager.get_rollcall(cid, rc_number)
            if not rc:
                await bot.answer_callback_query(call.id, "Rollcall not found")
                return
            
            user = User(proxy_name, None, proxy_name, rc.allNames)
            proxy_owner_id = call.from_user.id
            rc.set_proxy_owner(proxy_name, proxy_owner_id)
            
            add_or_update_proxy_user(
                rc.id,
                proxy_name,
                "in",
                "",
                proxy_owner_id=proxy_owner_id,
            )
            
            result = rc.addIn(user)
            rc.save()
            
            await bot.answer_callback_query(call.id, f"✅ Added {proxy_name}")
            await bot.edit_message_text(f"✅ {proxy_name} added to IN list", cid, call.message.message_id)
            
            # Send updated list
            await bot.send_message(cid, rc.allList().replace("__RCID__", str(rc_number + 1)))
            return
        
        # ----------------------------------------------------------------
        # proxy_cancel_ — cancelled adding ghosted proxy user
        # ----------------------------------------------------------------
        if data.startswith("proxy_cancel_"):
            await bot.answer_callback_query(call.id, "❌ Cancelled")
            await bot.edit_message_text("❌ Cancelled — user not added", cid, call.message.message_id)
            return

        # ----------------------------------------------------------------
        # delconf_yes_ / delconf_no_ — delete user confirmation
        # ----------------------------------------------------------------
        if data.startswith("delconf_yes_") or data.startswith("delconf_no_"):
            parts = data.split("_", 3)   # ["delconf", "yes"/"no", rc_number, admin_id]
            confirmed = parts[1] == "yes"
            admin_id = int(parts[3])

            if call.from_user.id != admin_id:
                await bot.answer_callback_query(call.id, "This action is not for you.", show_alert=True)
                return

            pending = _pending_deletes.pop((cid, admin_id), None)
            if not confirmed or pending is None:
                await bot.answer_callback_query(call.id, "❌ Cancelled")
                await bot.edit_message_text("❌ Delete cancelled.", cid, call.message.message_id)
                return

            name = pending['name']
            rc_number = pending['rc_number']
            rc = manager.get_rollcall(cid, rc_number)
            if rc and rc.delete_user(name):
                rc.save()
                await bot.answer_callback_query(call.id, f"✅ Deleted {name}")
                await bot.edit_message_text(f"✅ *{name}* removed from rollcall #{rc_number + 1}.", cid, call.message.message_id, parse_mode="Markdown")
            else:
                await bot.answer_callback_query(call.id, "User not found")
                await bot.edit_message_text(f"⚠️ User *{name}* not found.", cid, call.message.message_id, parse_mode="Markdown")
            return

        # ----------------------------------------------------------------
        # mabs_sel — admin selected a rollcall from /mark_absent list
        # ----------------------------------------------------------------
        if data.startswith("mabs_sel_"):
            rc_db_id = int(data.split("_", 2)[2])
            in_users = get_rollcall_in_users(rc_db_id)
            if not in_users:
                await bot.answer_callback_query(call.id, "No IN users found for this session.")
                await bot.edit_message_text("⚠️ No IN users were found for this session.", cid, call.message.message_id)
                return
            _ghost_selections[(cid, rc_db_id)] = set()
            markup = _build_ghost_select_keyboard(rc_db_id, in_users, set())
            await bot.answer_callback_query(call.id)
            await bot.edit_message_text(
                "👻 Who ghosted? Tap to select, then tap Done.",
                cid, call.message.message_id, reply_markup=markup
            )
            return

        await bot.answer_callback_query(call.id, "Unknown ghost action")

    except Exception as e:
        logging.exception("Error in ghost_callback_handler")
        try:
            await bot.answer_callback_query(call.id, str(e)[:200])
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: True)
async def callback_handler(call):
    """
    Handle button clicks from inline rollcall keyboards.

    Supported callback_data patterns:
      btn_in_<N>          - mark current user IN for rollcall N
      btn_out_<N>         - mark current user OUT for rollcall N
      btn_maybe_<N>       - mark current user MAYBE for rollcall N
      btn_lists_<N>       - open list submenu for rollcall N
      btn_wi_<N>          - show IN list
      btn_wo_<N>          - show OUT list
      btn_wm_<N>          - show MAYBE list
      btn_ww_<N>          - show WAITING list
      btn_status_<N>      - return to main status panel
      btn_refresh_<N>     - refresh current panel
      btn_end_<N>         - open end confirmation
      btn_endconfirm_<N>  - confirm end
      btn_endcancel_<N>   - cancel end
    """
    try:
        raw_data = call.data or ""
        cid = call.message.chat.id

        data = raw_data.split("_")
        if len(data) != 3 or data[0] != "btn":
            await bot.answer_callback_query(call.id, "Invalid action")
            return

        action = data[1]

        try:
            rc_number = int(data[2])
        except ValueError:
            await bot.answer_callback_query(call.id, "Invalid rollcall number")
            return

        rollcalls = manager.get_rollcalls(cid)
        if rc_number < 1 or rc_number > len(rollcalls):
            await bot.answer_callback_query(call.id, "Invalid rollcall!")
            return

        rc = rollcalls[rc_number - 1]

        # --------------------------------------------------------------
        # Status actions: IN / OUT / MAYBE
        # --------------------------------------------------------------
        if action in ("in", "out", "maybe"):
            _username = call.from_user.username or None
            if not _username:
                asyncio.create_task(warn_no_username(cid, call.from_user.first_name))
            _first_name = _get_display_name(call.from_user)
            user = User(
                _first_name,
                _username,
                call.from_user.id,
                rc.allNames,
            )

            # Keep the members table current so /buzz has fresh names
            upsert_chat_member(cid, call.from_user.id, _first_name, _username)

            # Ghost reconfirmation check for panel IN button
            if action == "in" and isinstance(user.user_id, int) and manager.get_ghost_tracking_enabled(cid):
                ghost_count = get_ghost_count(cid, user.user_id)
                absent_limit = manager.get_absent_limit(cid)
                if ghost_count >= absent_limit:
                    _pending_reconf[(cid, user.user_id)] = {'rc_number': rc_number - 1, 'comment': ''}
                    markup = InlineKeyboardMarkup(row_width=2)
                    markup.add(
                        InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=f"reconf_in_{rc_number - 1}_{user.user_id}"),
                        InlineKeyboardButton("❌ I'm out", callback_data=f"reconf_out_{rc_number - 1}_{user.user_id}"),
                    )
                    await bot.send_message(
                        cid,
                        f"👻 *Warning:* You've ghosted *{ghost_count}* session(s) before.\n"
                        f"Absent limit: *{absent_limit}*\n\n"
                        f"Are you committing to be at *{rc.title}*?",
                        parse_mode="Markdown",
                        reply_markup=markup
                    )
                    return
            
            if action == "in":
                result = rc.addIn(user)
            elif action == "out":
                result = rc.addOut(user)
            else:
                result = rc.addMaybe(user)

            rc.save()

            rc_db_id = get_rc_db_id(rc)

            # Record stats only for real state changes
            if rc_db_id is not None and isinstance(user.user_id, int):
                if action == "in":
                    if result not in ("AB", "AC", "AA"):
                        increment_user_stat(cid, user.user_id, "total_in")
                        increment_rollcall_stat(rc_db_id, "total_in")
                elif action == "out":
                    if result != "AB":
                        increment_user_stat(cid, user.user_id, "total_out")
                        increment_rollcall_stat(rc_db_id, "total_out")
                else:  # maybe
                    if result != "AB":
                        increment_user_stat(cid, user.user_id, "total_maybe")
                        increment_rollcall_stat(rc_db_id, "total_maybe")

            if result == "AB":
                await bot.answer_callback_query(call.id, "No duplicate proxy please 🙂")
                return

            elif result == "AA":
                await bot.answer_callback_query(call.id, "That name already exists!")
                return

            elif result == "AC":
                await bot.answer_callback_query(call.id, "Event max limit reached, added to waitlist")
                if isinstance(user.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name(user)} → WAITING for '{rc.title}' (#{rc_number})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        cid,
                        f"{user.name} → WAITING for '{rc.title}' (#{rc_number})",
                    )

            else:
                await bot.answer_callback_query(call.id, "Status updated")

                if action == "in":
                    if isinstance(user.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name(user)} → IN for '{rc.title}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(
                            cid,
                            f"{user.name} → IN for '{rc.title}' (#{rc_number})",
                        )

                elif action == "out":
                    if isinstance(user.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name(user)} → OUT for '{rc.title}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(
                            cid,
                            f"{user.name} → OUT for '{rc.title}' (#{rc_number})",
                        )

                else:
                    if isinstance(user.user_id, int):
                        await bot.send_message(
                            cid,
                            f"{format_mention_with_name(user)} → MAYBE for '{rc.title}' (#{rc_number})",
                            parse_mode="Markdown",
                        )
                    else:
                        await bot.send_message(
                            cid,
                            f"{user.name} → MAYBE for '{rc.title}' (#{rc_number})",
                        )

            # Promotion from WAITING -> IN can happen after OUT or MAYBE in latest models.py
            if action in ("out", "maybe") and isinstance(result, User):
                if isinstance(result.user_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name(result)} → IN (from WAITING) for '{rc.title}' (#{rc_number})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        cid,
                        f"{result.name} → IN (from WAITING) for '{rc.title}' (#{rc_number})",
                    )

                await notify_proxy_owner_wait_to_in(rc, result, cid, rc.title, rc_number)

                if rc_db_id is not None and isinstance(result.user_id, int):
                    increment_user_stat(cid, result.user_id, "total_waiting_to_in")
                    increment_user_stat(cid, result.user_id, "total_in")
                    increment_rollcall_stat(rc_db_id, "total_in")

            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)
            try:
                await bot.edit_message_text(
                    text,
                    cid,
                    call.message.message_id,
                    reply_markup=markup,
                )
                # Keep track of which message is the live panel so commands
                # (/in /out /maybe) can also edit it in-place.
                _panel_msg_ids[(cid, rc_number)] = call.message.message_id
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise
            return

        # --------------------------------------------------------------
        # Lists submenu
        # --------------------------------------------------------------
        if action == "lists":
            await bot.answer_callback_query(call.id)
            markup = await get_lists_keyboard(rc_number)
            try:
                await bot.edit_message_text(
                    "Select list:",
                    cid,
                    call.message.message_id,
                    reply_markup=markup,
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise    
            return

        # --------------------------------------------------------------
        # Individual lists
        # --------------------------------------------------------------
        if action in ("wi", "wo", "wm", "ww"):
            await bot.answer_callback_query(call.id)

            if action == "wi":
                text = rc.inListText()
            elif action == "wo":
                text = rc.outListText()
            elif action == "wm":
                text = rc.maybeListText()
            else:
                text = rc.waitListText()

            try:
                await bot.edit_message_text(
                    text if text.strip() else "List is empty.",
                    cid,
                    call.message.message_id,
                    reply_markup=await get_lists_keyboard(rc_number),
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise 
            return

        # --------------------------------------------------------------
        # Back to main panel
        # --------------------------------------------------------------
        if action == "status":
            await bot.answer_callback_query(call.id)
            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)
            try: 
                await bot.edit_message_text(
                    text,
                    cid,
                    call.message.message_id,
                    reply_markup=markup,
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise 
                
            return

        # --------------------------------------------------------------
        # Refresh panel
        # --------------------------------------------------------------
        if action == "refresh":
            await bot.answer_callback_query(call.id, "Refreshed")
            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)
            try:
                await bot.edit_message_text(
                    text,
                    cid,
                    call.message.message_id,
                    reply_markup=markup,
                )
                _panel_msg_ids[(cid, rc_number)] = call.message.message_id
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise

            return

        # --------------------------------------------------------------
        # Show end confirmation
        # --------------------------------------------------------------
        if action == "end":
            # ✅ Check permissions before even showing the confirmation
            member = await bot.get_chat_member(cid, call.from_user.id)
            admin_mode = manager.get_admin_rights(cid)
            
            if admin_mode and member.status not in ["administrator", "creator"]:
                await bot.answer_callback_query(call.id, "⛔ Only admins can end rollcalls", show_alert=True)
                return
            
            await bot.answer_callback_query(call.id)
            markup = await get_end_confirm_keyboard(rc_number)
            try: 
                await bot.edit_message_text(
                    f"Are you sure you want to end rollcall '{rc.title}' (#{rc_number})?",
                    cid,
                    call.message.message_id,
                    reply_markup=markup,
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise 
                
            return

        # --------------------------------------------------------------
        # Confirm end rollcall
        # --------------------------------------------------------------
        if action == "endconfirm":

            member = await bot.get_chat_member(cid, call.from_user.id)
            if member.status not in ["administrator", "creator"]:
                await bot.send_message(cid, "⛔ Insufficient permissions to end rollcall.")
                return

            # Capture ghost tracking info BEFORE removing from manager
            rc_db_id = rc.id
            ghost_tracking_on = manager.get_ghost_tracking_enabled(cid)
            has_any_users = len(rc.inList) > 0
            absent_already_marked = rc.absent_marked

            # Update attendance streaks for real users who were IN
            for u in rc.inList:
                if isinstance(u.user_id, int):
                    update_streak_on_checkin(cid, u.user_id)

            await bot.answer_callback_query(call.id, "Rollcall ended")
            ended_by = call.from_user.first_name or call.from_user.username or "someone"

            try:
                final_text = rc.finishList().replace("__RCID__", str(rc_number))
                final_text = f"{final_text}\n\nRollcall ended by {ended_by}"
                await bot.send_message(cid, final_text)
            except Exception:
                pass

            _panel_msg_ids.pop((cid, rc_number), None)
            manager.remove_rollcall(cid, rc_number - 1)

            # Ghost tracking prompt — mirrors /erc command behaviour
            if ghost_tracking_on and has_any_users and rc_db_id and not absent_already_marked:
                markup = InlineKeyboardMarkup(row_width=2)
                markup.add(
                    InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rc_db_id}"),
                    InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{rc_db_id}")
                )
                await bot.send_message(cid, "👻 Did anyone ghost today's session?", reply_markup=markup)

            updated_rollcalls = manager.get_rollcalls(cid)
            if len(updated_rollcalls) > 0:
                await bot.send_message(
                    cid,
                    "⚠️ Active rollcall IDs have been updated because one rollcall was ended. "
                    "Use /rollcalls to see the current list and IDs.",
                )
                for idx, rollcall in enumerate(updated_rollcalls):
                    new_id = idx + 1
                    text = rollcall.allList().replace("__RCID__", str(new_id))
                    markup = await get_status_keyboard(new_id)
                    await bot.send_message(cid, text, reply_markup=markup)
            return

        # --------------------------------------------------------------
        # Cancel end rollcall
        # --------------------------------------------------------------
        if action == "endcancel":
            await bot.answer_callback_query(call.id, "Cancelled")
            text = rc.allList().replace("__RCID__", str(rc_number))
            markup = await get_status_keyboard(rc_number)

            try:
                await bot.edit_message_text(
                    text,
                    cid,
                    call.message.message_id,
                    reply_markup=markup,
                )
            except Exception as e:
                if "message is not modified" not in str(e):
                    raise 
            
            return

        await bot.answer_callback_query(call.id, "Unknown action")

    except Exception as e:
        logging.exception("Error in callback_handler")
        try:
            await bot.answer_callback_query(call.id, str(e)[:200])
        except Exception:
            pass


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

        txt = f"{owner_mention}, your proxy {format_mention_with_name(moved_user)} → IN for '{title}' (#{rc_number})"
        await bot.send_message(cid, txt, parse_mode="Markdown")
    except Exception:
        logging.exception("Failed to notify proxy owner for WAITING→IN")


# ===========================================================================
# Ghost tracking commands
# ===========================================================================

@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/toggle_ghost_tracking")
async def toggle_ghost_tracking(message):
    """Admin only: enable or disable ghost tracking for this chat."""
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split()
        arg = parts[1].lower() if len(parts) > 1 else None
        if arg in ("true", "on", "1", "enable", "enabled"):
            new_state = True
        elif arg in ("false", "off", "0", "disable", "disabled"):
            new_state = False
        else:
            new_state = not manager.get_ghost_tracking_enabled(cid)
        manager.set_ghost_tracking_enabled(cid, new_state)
        if new_state:
            await bot.send_message(
                cid,
                "👻 Ghost tracking enabled for this group.\n"
                "Users will be asked after /erc if anyone ghosted."
            )
        else:
            await bot.send_message(
                cid,
                "🔕 Ghost tracking disabled for this group.\n"
                "/erc will end sessions without ghost prompts."
            )
    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/set_absent_limit")
async def set_absent_limit(message):
    """Admin only: set the ghost threshold for this chat."""
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split()
        if len(parts) < 2:
            await bot.send_message(cid, "Usage: /set_absent_limit <number>\nExample: /set_absent_limit 3")
            return
        try:
            limit = int(parts[1])
            if limit < 1:
                raise ValueError()
        except ValueError:
            await bot.send_message(cid, "⚠️ Please provide a positive integer. Example: /set_absent_limit 3")
            return
        manager.set_absent_limit(cid, limit)
        await bot.send_message(
            cid,
            f"✅ Ghost limit set to {limit}.\n"
            f"Users who ghost {limit}+ session(s) will be asked to reconfirm their IN vote. 👻"
        )
    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/clear_absent")
async def clear_absent(message):
    """Admin only: reset a user's ghost count by name."""
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        parts = message.text.strip().split(None, 1)
        if len(parts) < 2:
            await bot.send_message(cid, "Usage: /clear_absent <name>\nExample: /clear_absent John")
            return
        target_name = parts[1].strip()

        # Try exact match first, then fuzzy via leaderboard
        record = get_user_ghost_count_by_name(cid, target_name)
        if not record:
            # Fuzzy: find closest name in leaderboard
            leaderboard = get_ghost_leaderboard(cid)
            best = None
            best_score = None
            try:
                from Levenshtein import distance as lev_distance
                for entry in leaderboard:
                    name = entry.get('user_name') or entry.get('proxy_name') or ""
                    score = lev_distance(target_name.lower(), name.lower())
                    if best_score is None or score < best_score:
                        best_score = score
                        best = entry
            except ImportError:
                pass
            if best and best_score is not None and best_score <= 3:
                record = best
            else:
                await bot.send_message(cid, f"⚠️ No ghost record found for '{target_name}'.")
                return

        # Handle both real users and proxy users
        proxy_name = record.get('proxy_name')
        user_id = record.get('user_id', -1)
        reset_ghost_count(cid, user_id, proxy_name=proxy_name)
        name = record.get('user_name') or proxy_name or target_name
        await bot.send_message(cid, f"✅ {name}'s ghost record has been cleared. Fresh start! 👻➡️✅")
    except Exception as e:
        await bot.send_message(message.chat.id, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/mark_absent")
async def mark_absent(message):
    """Admin only: select ghosts for a previously ended roll call."""
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id

        if not manager.get_ghost_tracking_enabled(cid):
            await bot.send_message(
                cid,
                "⚠️ Ghost tracking is not enabled for this group.\n"
                "Admin can enable it with /toggle_ghost_tracking"
            )
            return

        sessions = get_unprocessed_rollcalls(cid, days=30)
        if not sessions:
            await bot.send_message(cid, "✅ No sessions need absent marking — you're all caught up!")
            return

        markup = InlineKeyboardMarkup(row_width=1)
        for s in sessions:
            date_str = _fmt_ended_at(s.get('ended_at'))
            title = s.get('title') or "Untitled"
            label = f"📋 {title} — {date_str}"
            markup.add(InlineKeyboardButton(label, callback_data=f"mabs_sel_{s['id']}"))

        await bot.send_message(cid, "Which session do you want to review?", reply_markup=markup)
    except Exception as e:
        await bot.send_message(message.chat.id, e)
