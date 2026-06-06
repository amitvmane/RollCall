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
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    duplicateProxy, repeatlyName, timeError, amountOfRollCallsReached, rollCallAlreadyStarted,
)
from models import RollCall, User

# Exceptions whose str(e) is a curated user-facing message — safe to expose
# directly. Anything outside this set is treated as an internal error.
_USER_FACING_EXCEPTIONS = (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    duplicateProxy, repeatlyName, timeError, amountOfRollCallsReached, rollCallAlreadyStarted,
)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# ── Bot instance & paths ──────────────────────────────────────────────────────

bot = AsyncTeleBot(token=TELEGRAM_TOKEN, use_class_middlewares=True)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# Auto-track real users on any group interaction so /buzz reaches lurkers
# who haven't voted yet. Wrapped in try/except because under tests the
# telebot module is a MagicMock — BaseMiddleware isn't a real class then,
# and the subclass declaration would fail at import time.
try:
    from telebot.asyncio_handler_backends import BaseMiddleware

    class _MemberTrackingMiddleware(BaseMiddleware):
        def __init__(self):
            super().__init__()
            self.update_types = ['message', 'callback_query']

        async def pre_process(self, message, data):
            try:
                # CallbackQuery: chat lives on .message.chat; plain Message: on .chat
                msg_obj = getattr(message, 'message', None)
                chat = msg_obj.chat if msg_obj is not None else getattr(message, 'chat', None)
                user = getattr(message, 'from_user', None)
                if chat is None or user is None or getattr(user, 'is_bot', False):
                    return
                # chat_members is a per-group roster; DMs (chat.id > 0) are excluded.
                if getattr(chat, 'id', 0) >= 0:
                    return
                uid = getattr(user, 'id', None)
                if not isinstance(uid, int):
                    return
                from db import upsert_chat_member
                first_name = (getattr(user, 'first_name', None) or '').strip() or str(uid)
                upsert_chat_member(chat.id, uid, first_name, user.username or None)
            except Exception:
                logging.exception("member tracking middleware: ignored failure")

        async def post_process(self, message, data, exception):
            pass

    bot.setup_middleware(_MemberTrackingMiddleware())
except Exception:
    logging.debug("Member-tracking middleware not installed (likely test environment)")


def data_file_path(filename: str) -> str:
    return os.path.join(BASE_DIR, filename)


# ── In-memory state ───────────────────────────────────────────────────────────

# (chat_id, rollcall_db_id) -> set of user_ids selected as ghosts
_ghost_selections: dict = {}

# (chat_id, user_id) -> {'rc_number': int, 'comment': str, '_ts': float} for pending reconfirmation.
# _ts is required so the memory_prune_loop drops abandoned entries via _prune_pending.
_pending_reconf: dict = {}

# /schedules multi-select state: chat_id -> set of template names currently checked
_sched_selection: dict = {}

# Rate limiting: (chat_id, user_id) -> last action timestamp
_rate_limits: dict = {}
_RATE_LIMIT_SECONDS = 2

# Pending delete confirmations: (chat_id, admin_user_id) -> {'name': str, 'rc_number': int, '_ts': float}
_pending_deletes: dict = {}

# Pending status overrides: (chat_id, admin_user_id) -> {'user': User, 'new_status': str, 'rc_number': int, '_ts': float}
_pending_overrides: dict = {}

# Pending /sif post-ghost-warning add: (chat_id, admin_user_id, proxy_name) -> {'comment': str, '_ts': float}
_pending_proxy_add: dict = {}

# How long pending confirmations stay valid before being garbage-collected (seconds).
_PENDING_TTL_SECONDS = 3600


def _prune_pending(d: dict) -> None:
    """Drop entries older than _PENDING_TTL_SECONDS from a pending-action dict."""
    now = datetime.now().timestamp()
    stale = [k for k, v in d.items() if (now - (v.get('_ts', now))) > _PENDING_TTL_SECONDS]
    for k in stale:
        d.pop(k, None)


# ── Centralized handler error wrapping ───────────────────────────────────────
# Exceptions whose `str(e)` is a curated user-facing message; the wrapper
# sends them verbatim. Anything else is logged with full traceback and the
# user gets a generic message (no internal details leaked).
from functools import wraps as _wraps  # noqa: E402

_GENERIC_ERROR_MSG = "⚠️ Something went wrong. The error has been logged."


def _handler_chat_id(arg):
    """Pull a chat_id out of a Message or CallbackQuery."""
    try:
        if getattr(arg, 'chat', None) is not None:
            return arg.chat.id
        if getattr(arg, 'message', None) is not None:
            return arg.message.chat.id
    except Exception:
        return None
    return None


async def reply_error(target, e):
    """Reply with an error message that's safe to expose to users.

    `target` may be a chat_id (int), a Message, or a CallbackQuery.

    User-facing exception classes (defined in exceptions.py) are sent verbatim
    because their message is curated. Anything else is logged with a full
    traceback and the user sees a generic "something went wrong" message — no
    Python internals (Markdown parse errors, KeyError text, DB error strings)
    leak into the chat."""
    cid = target if isinstance(target, int) else _handler_chat_id(target)
    if cid is None:
        return
    if isinstance(e, _USER_FACING_EXCEPTIONS):
        msg = str(e)
    else:
        logging.exception(f"Non-user-facing exception caught in handler: {type(e).__name__}: {e}")
        _record_error(e)
        msg = _GENERIC_ERROR_MSG
    try:
        await bot.send_message(cid, msg)
    except Exception:
        logging.exception("reply_error: failed to send error reply")

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

# Last unhandled-error signal for /health diagnostics
_last_error_state = {'at': None, 'msg': None}


def _record_error(exc: BaseException) -> None:
    """Record the last unhandled error so /health can surface it."""
    try:
        _last_error_state['at'] = datetime.now().isoformat(timespec='seconds')
        _last_error_state['msg'] = f"{type(exc).__name__}: {str(exc)[:80]}"
    except Exception:
        pass


def _log_task_exc(task: asyncio.Task) -> None:
    """Done-callback for fire-and-forget tasks — logs any unhandled exception."""
    if not task.cancelled() and task.exception():
        exc = task.exception()
        logging.error(f"Background task '{task.get_name()}' raised: {exc}")
        _record_error(exc)


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


def _esc_md(text: str) -> str:
    """Escape Markdown v1 special characters in user-supplied strings.
    Includes `]` so display names cannot break `[name](tg://user?id=X)` links."""
    if not text:
        return text or ""
    for c in ('_', '*', '`', '[', ']'):
        text = text.replace(c, f'\\{c}')
    return text


def format_mention_with_name_md(user: User) -> str:
    """Markdown-safe version: escapes special chars in @username/name; preserves tg:// links."""
    if isinstance(user.user_id, int):
        if user.username:
            return f"@{_esc_md(user.username)} ({_esc_md(user.name)})"
        return f"[{_esc_md(user.name)}](tg://user?id={user.user_id})"
    return _esc_md(user.name)


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
            f"🎉 Good news! A spot opened up and you're now *IN* for *{_esc_md(rc_title)}* (#{rc_number}). See you there!",
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
