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
    duplicateProxy, alreadyInList, repeatlyName, timeError, amountOfRollCallsReached, rollCallAlreadyStarted,
    duesGameAlreadyClosed, duesNothingToClose, databaseError,
)
from models import RollCall, User
from rollcall_manager import manager
# Re-exported for backward compat — all existing `from bot_state import
# _esc_md` call sites keep working. The actual implementation lives in
# utils/text.py (dependency-free) so the services layer can use it without
# importing bot_state (which constructs AsyncTeleBot on import).
from utils.text import esc_md as _esc_md

# Exceptions whose str(e) is a curated user-facing message — safe to expose
# directly. Anything outside this set is treated as an internal error.
_USER_FACING_EXCEPTIONS = (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    duplicateProxy, alreadyInList, repeatlyName, timeError, amountOfRollCallsReached, rollCallAlreadyStarted,
    duesGameAlreadyClosed, duesNothingToClose,
    # databaseError's message is deliberately generic ("couldn't save that —
    # try again"); the driver error is logged at the raise site, never here.
    databaseError,
)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# ── Bot instance & paths ──────────────────────────────────────────────────────

bot = AsyncTeleBot(token=TELEGRAM_TOKEN)
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
                from db import upsert_chat_member, update_chat_group_name
                first_name = (getattr(user, 'first_name', None) or '').strip() or str(uid)
                upsert_chat_member(chat.id, uid, first_name, user.username or None)
                title = getattr(chat, 'title', None)
                if title:
                    update_chat_group_name(chat.id, title)
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

# (chat_id, rollcall_db_id) keys whose ghost keyboard has the late-drop-out
# section expanded. Pure display state — selections themselves live in
# _ghost_selections and are persisted, this is not.
_ghost_show_out: set = set()

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

# Pending custom subsidy amount for /settle_dues's confirm card — consumed
# by the next free-text reply (a bare integer) rather than a callback tap.
# (chat_id, admin_user_id) -> {'rollcall_id': int, 'title': str, '_ts': float}
_pending_subsidy_input: dict = {}

# Pending partial-payment amount for the /mark_paid panel — same free-text
# reply mechanism as _pending_subsidy_input, kept separate since the two
# features are unrelated beyond sharing that mechanism.
# (chat_id, admin_user_id) -> {'member_name': str, '_ts': float}
_pending_payment_input: dict = {}

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

# Cooldown for recurring background-loop warnings (e.g. the scheduler
# repeatedly hitting "max 3 active rollcalls" on a template that fires
# daily): (chat_id, condition_key) -> last-announced timestamp. Keeps a
# genuinely-persistent condition from flooding the group with the same
# message every time a periodic check re-evaluates it — see
# _should_notify_group.
_group_warning_cooldowns: dict = {}
_GROUP_WARNING_COOLDOWN_SECONDS = 24 * 60 * 60

# Panel message tracking: (chat_id, rc_1based) -> message_id of the active panel message
_panel_msg_ids: dict = {}

# Audit log display settings
_AUDIT_PER_PAGE = 15


# ── Timestamp helper ──────────────────────────────────────────────────────────

def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── Graceful edit helpers ─────────────────────────────────────────────────────

async def safe_edit_text(cid: int, msg_id: int, text: str, reply_markup=None, parse_mode: str = None) -> bool:
    """Edit a message text; swallow failures gracefully (log WARNING, return False).

    Returns True if the edit succeeded, False otherwise. Callers that need a
    fallback send_message can branch on the return value.
    """
    try:
        await bot.edit_message_text(text, cid, msg_id, reply_markup=reply_markup, parse_mode=parse_mode)
        return True
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return True
        logging.warning("edit_message_text failed (chat=%s msg=%s): %s", cid, msg_id, e)
        return False


async def safe_edit_markup(cid: int, msg_id: int, reply_markup=None) -> bool:
    """Edit reply markup only; swallow failures gracefully."""
    try:
        await bot.edit_message_reply_markup(cid, msg_id, reply_markup=reply_markup)
        return True
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return True
        logging.warning("edit_message_reply_markup failed (chat=%s msg=%s): %s", cid, msg_id, e)
        return False


async def send_md_fallback(cid: int, text: str, **kwargs):
    """Send with Markdown, retrying as plain text if Telegram rejects parsing.

    User-controlled strings (names, titles) interpolated into Markdown can
    contain unmatched */_/` entities, which makes Telegram reject the whole
    message with a 400. For ledger announcements that message IS the
    durability layer — losing it is data loss. Degrading to plain text keeps
    the record in the chat history.

    Also retries once on transient network/timeout errors (getUpdates and
    send_message share the bot's aiohttp session, so a connectivity blip can
    make an otherwise-successful DB write's confirmation vanish silently —
    seen in production as a "no ack" report even though the write logged
    fine). A final failure is always logged loudly rather than swallowed.
    """
    try:
        return await bot.send_message(cid, text, parse_mode="Markdown", **kwargs)
    except Exception as e:
        if "can't parse entities" in str(e).lower():
            logging.warning("Markdown parse failed for chat %s; resending plain", cid)
            try:
                return await bot.send_message(cid, text, **kwargs)
            except Exception:
                logging.exception("send_md_fallback: plain-text resend failed for chat %s", cid)
                raise
        logging.warning("send_md_fallback: send failed for chat %s (%s), retrying once", cid, e)
        try:
            await asyncio.sleep(0.5)
            return await bot.send_message(cid, text, parse_mode="Markdown", **kwargs)
        except Exception:
            logging.exception("send_md_fallback: retry failed for chat %s — message lost: %r", cid, text[:200])
            raise


# ── Task helpers ──────────────────────────────────────────────────────────────

# Last unhandled-error signal for /health diagnostics
_last_error_state = {'at': None, 'msg': None}

# Telegram connectivity status — set in runner.py after startup get_me() check.
# ok=None means the check has not run yet (REST API started before bot init).
_telegram_status: dict = {"ok": None, "checked_at": None, "bot_username": None}


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


def _should_notify_group(chat_id: int, condition_key: str) -> bool:
    """Check whether a background-loop warning is due to be (re-)posted to
    the group: True on first occurrence of this exact (chat, condition)
    pair, or once the cooldown window has elapsed since it was last
    announced — False otherwise.

    This only *checks* the cooldown — it does not stamp it. Callers must
    call `_mark_group_notified(chat_id, condition_key)` themselves, and only
    after the send has actually succeeded. (Stamping unconditionally here,
    before the caller even attempts the send, meant a failed send — a
    network blip, a momentary Telegram outage — silently "used up" the
    cooldown window with nothing having been delivered.)

    For a condition that resolves itself instantly (a one-off blip) this
    never matters. It exists for conditions a periodic scheduler re-checks
    on every tick and that only a human can actually resolve (e.g. "3
    rollcalls already open" — clears when an admin runs /erc) — without
    this gate, a daily-recurring template stuck on that condition posts the
    identical warning to the group every single day forever.

    Callers should always `logging.warning(...)` unconditionally regardless
    of this return value, so every occurrence is still visible in the logs
    even while the group-facing message is suppressed."""
    key = (chat_id, condition_key)
    now = datetime.now().timestamp()
    last = _group_warning_cooldowns.get(key, 0)
    return now - last >= _GROUP_WARNING_COOLDOWN_SECONDS


def _mark_group_notified(chat_id: int, condition_key: str) -> None:
    """Record that the group was just successfully notified for (chat,
    condition) — call this only after the send succeeds, right after a
    `_should_notify_group` check returned True."""
    _group_warning_cooldowns[(chat_id, condition_key)] = datetime.now().timestamp()


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


async def is_chat_admin(cid: int, uid: int) -> bool:
    """True if the chat's admin-mode setting is off, or uid is a Telegram
    chat administrator/creator. Shared gate for financial-write inline
    callbacks (settle_dues, penalty panel, payment panel, pick_collector) —
    each call site keeps its own alert message/return value on failure.

    Distinct from functions.py's admin_rights(message, manager), which
    takes a message object (used at command entry points, not callbacks)."""
    if manager.get_admin_rights(cid):
        member = await bot.get_chat_member(cid, uid)
        return member.status in ("administrator", "creator")
    return True


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

def _ghost_row(rc_db_id: int, u: dict, selected_ids: set, suffix: str = "") -> InlineKeyboardButton:
    """One selectable candidate button for the ghost keyboard."""
    proxy_name = u.get('proxy_name')
    if proxy_name is not None:
        tick = "👻 " if proxy_name in selected_ids else ""
        return InlineKeyboardButton(
            f"{tick}{proxy_name}{suffix}",
            callback_data=f"ghost_togp_{rc_db_id}_{proxy_name}"
        )
    uid = u['user_id']
    name = u.get('first_name') or u.get('username') or str(uid)
    tick = "👻 " if uid in selected_ids else ""
    return InlineKeyboardButton(
        f"{tick}{name}{suffix}",
        callback_data=f"ghost_tog_{rc_db_id}_{uid}"
    )


def _build_ghost_select_keyboard(rc_db_id: int, in_users: list, selected_ids: set,
                                 out_users: list = None, show_out: bool = False) -> InlineKeyboardMarkup:
    """Build the ghost selection keyboard. Handles both real users and proxy users.

    `out_users` are members who ended the session in the OUT list. They are not
    shown by default — the normal question is "who said IN and didn't turn up"
    — but a late drop-out that left the side short is a no-show too, and there
    was previously no way to record one. The "＋ Someone who dropped out late"
    button reveals them; once revealed they toggle through exactly the same
    callbacks, so a selected OUT member survives a keyboard rebuild.
    """
    markup = InlineKeyboardMarkup(row_width=2)
    for u in in_users:
        markup.add(_ghost_row(rc_db_id, u, selected_ids))

    out_users = out_users or []
    if out_users:
        if show_out:
            markup.add(InlineKeyboardButton(
                "— dropped out late —", callback_data=f"ghost_lessout_{rc_db_id}"))
            for u in out_users:
                markup.add(_ghost_row(rc_db_id, u, selected_ids, suffix=" (was OUT)"))
        else:
            n_sel = sum(
                1 for u in out_users
                if (u.get('proxy_name') or u.get('user_id')) in selected_ids
            )
            label = "＋ Someone who dropped out late"
            if n_sel:
                label += f" ({n_sel} 👻)"
            markup.add(InlineKeyboardButton(label, callback_data=f"ghost_moreout_{rc_db_id}"))

    markup.add(InlineKeyboardButton("✅ Done", callback_data=f"ghost_done_{rc_db_id}"))
    return markup
