"""
Shared bot instance, in-memory state, and lightweight helper functions.
All handler modules import from here — nothing else should create a second bot instance.
"""
import os
import logging
import asyncio
from datetime import datetime
from typing import Optional

from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import TELEGRAM_TOKEN
from models import RollCall, User

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# ── Bot instance & paths ──────────────────────────────────────────────────────

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def data_file_path(filename: str) -> str:
    return os.path.join(BASE_DIR, filename)


# ── In-memory state ───────────────────────────────────────────────────────────

# (chat_id, rollcall_db_id) -> set of user_ids selected as ghosts
_ghost_selections: dict = {}

# (chat_id, user_id) -> {'rc_number': int, 'comment': str} for pending reconfirmation
_pending_reconf: dict = {}

# /schedules multi-select state: chat_id -> set of template names currently checked
_sched_selection: dict = {}

# Rate limiting: (chat_id, user_id) -> last action timestamp
_rate_limits: dict = {}
_RATE_LIMIT_SECONDS = 2

# Pending delete confirmations: (chat_id, admin_user_id) -> {'name': str, 'rc_number': int}
_pending_deletes: dict = {}

# Pending status overrides: (chat_id, admin_user_id) -> {'user': User, 'new_status': str, 'rc_number': int}
_pending_overrides: dict = {}

# Per-chat /buzz rate limiting: chat_id -> last buzz timestamp
_buzz_cooldowns: dict = {}
_BUZZ_COOLDOWN_SECONDS = 30

# Panel message tracking: (chat_id, rc_1based) -> message_id of the active panel message
_panel_msg_ids: dict = {}

# Audit log display settings
_AUDIT_PER_PAGE = 15


# ── Timestamp helper ──────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── Task helpers ──────────────────────────────────────────────────────────────

def _log_task_exc(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — logs any unhandled exception."""
    if not task.cancelled() and task.exception():
        logging.error(f"Background task '{task.get_name()}' raised: {task.exception()}")


# ── Rate-limit helpers ────────────────────────────────────────────────────────

def _is_rate_limited(chat_id: int, user_id: int) -> bool:
    """Return True if this user has acted within the rate limit window."""
    key = (chat_id, user_id)
    now = datetime.now().timestamp()
    last = _rate_limits.get(key, 0)
    if now - last < _RATE_LIMIT_SECONDS:
        return True
    _rate_limits[key] = now
    return False


def _is_buzz_rate_limited(chat_id: int) -> bool:
    """Return True if /buzz was used in this chat within the cooldown window."""
    now = datetime.now().timestamp()
    last = _buzz_cooldowns.get(chat_id, 0)
    if now - last < _BUZZ_COOLDOWN_SECONDS:
        return True
    _buzz_cooldowns[chat_id] = now
    return False


# ── User / mention helpers ────────────────────────────────────────────────────

def _get_display_name(tg_user) -> str:
    """Return a safe, non-None display name for a Telegram user object."""
    return tg_user.first_name or tg_user.last_name or str(tg_user.id)


def format_mention(user: User) -> str:
    """Real users: @username or tg:// link. Proxy users: plain name."""
    if isinstance(user.user_id, int):
        if user.username:
            return f"@{user.username}"
        return f"[{user.name}](tg://user?id={user.user_id})"
    return user.name


def format_mention_with_name(user: User) -> str:
    """@username (FirstName) or [FirstName](tg://...) for real users; plain name for proxies."""
    if isinstance(user.user_id, int):
        if user.username:
            return f"@{user.username} ({user.name})"
        return f"[{user.name}](tg://user?id={user.user_id})"
    return user.name


async def warn_no_username(cid: int, first_name: str) -> None:
    """Warn in group that this user has no Telegram username set."""
    try:
        await bot.send_message(
            cid,
            f"⚠️ {first_name}, you don't have a Telegram username set.\n"
            "Please set one: Settings → Edit Profile → Username\n"
            "The bot uses it for logging and identification.",
        )
    except Exception as e:
        logging.warning(f"[warn_no_username] Could not send warning to chat {cid} for {first_name}: {e}")


async def _dm_promoted_real_user(user_id: int, rc_title: str, rc_number: int) -> None:
    """DM a real user that they've been promoted from waitlist to IN."""
    try:
        await bot.send_message(
            user_id,
            f"🎉 Good news! A spot opened up and you're now *IN* for *{rc_title}* (#{rc_number}). See you there!",
            parse_mode="Markdown",
        )
    except Exception as e:
        logging.warning(
            f"[_dm_promoted_real_user] Could not DM user {user_id} for '{rc_title}' (#{rc_number}): {e} "
            f"— user may not have started the bot"
        )


# ── RollCall DB-id helper ─────────────────────────────────────────────────────

def get_rc_db_id(rc) -> Optional[int]:
    """Safely retrieve the DB primary key from a RollCall object (checks rc.id and rc.db_id)."""
    val = getattr(rc, "id", None) or getattr(rc, "db_id", None)
    if val is None:
        logging.warning(
            f"RollCall '{getattr(rc, 'title', '?')}' has no DB id — "
            "stats and proxy DB calls will be skipped for this rollcall."
        )
    return val


# ── Timestamp formatter (used by history/stats) ───────────────────────────────

def _fmt_ended_at(ended_at) -> str:
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


# ── Ghost keyboard builder ────────────────────────────────────────────────────

def _build_ghost_select_keyboard(rc_db_id: int, in_users: list, selected_ids: set) -> InlineKeyboardMarkup:
    """Build the ghost selection keyboard. Handles both real users and proxy users."""
    markup = InlineKeyboardMarkup(row_width=2)
    for u in in_users:
        proxy_name = u.get('proxy_name')
        if proxy_name is not None:
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
