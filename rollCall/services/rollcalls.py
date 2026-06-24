"""
Rollcall lifecycle services — start, end, list, get.

These are framework-agnostic: take primitive args, call into
`rollcall_manager.manager` and `db`, return dicts. No telebot, no markdown,
no send_message.

Adapters (Telegram handlers, future REST API) are responsible for parsing
their input, calling these services, and formatting the returned dicts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from exceptions import (
    amountOfRollCallsReached,
    incorrectParameter,
    insufficientPermissions,
    rollCallNotStarted,
)
from rollcall_manager import manager
from db import (
    increment_user_stat,
    log_admin_action,
    reset_proxy_streak,
    reset_user_streak,
    update_proxy_streak_on_checkin,
    update_streak_on_checkin,
)

from .common import (
    MAX_ROLLCALLS_PER_CHAT,
    resolve_rollcall_or_raise,
    serialize_rollcall,
)


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def start_rollcall(
    chat_id: int,
    title: str | None,
    started_by_user_id: int,
    started_by_name: str,
    started_by_username: str | None = None,
) -> dict:
    """
    Create a new active rollcall for the chat.

    Args:
      chat_id           — Telegram chat id (or platform-equivalent later)
      title             — title for the rollcall. `None` or empty falls back to '<Empty>'.
      started_by_*      — identity of the user starting it (for audit log)

    Returns:
      Dict with the created rollcall's serialized state, plus a `rc_index`
      (0-based position) so callers know which `::N` to address it as.

    Raises:
      amountOfRollCallsReached — if chat already has MAX_ROLLCALLS_PER_CHAT
                                  active rollcalls.

    NOTE: This service does NOT enforce admin permissions. Adapters must
    check chat-admin status with platform-specific APIs before calling
    (Telegram bot does this via `admin_rights`). Centralising the
    permission check here would require platform-specific code in the
    service, defeating the abstraction.
    """
    rollcalls = manager.get_rollcalls(chat_id)
    if len(rollcalls) >= MAX_ROLLCALLS_PER_CHAT:
        raise amountOfRollCallsReached(
            f"Allowed Maximum number of active roll calls per group is {MAX_ROLLCALLS_PER_CHAT}."
        )

    clean_title = (title or "").strip() or "<Empty>"
    rc_index = len(rollcalls)
    rc = manager.add_rollcall(chat_id, clean_title)

    logging.info(
        f"[{_ts()}] [CHAT {chat_id}] Rollcall started: '{clean_title}' "
        f"(RC #{rc_index + 1}) by {started_by_name} "
        f"(@{started_by_username or 'none'})"
    )
    log_admin_action(
        chat_id, started_by_user_id, started_by_name,
        "new_rollcall", target_name=clean_title,
    )

    # Fire web-push notifications to subscribers — non-blocking, best-effort
    from bot_state import _log_task_exc
    asyncio.create_task(_push_rollcall_started(chat_id, clean_title)).add_done_callback(_log_task_exc)

    return serialize_rollcall(rc, rc_index)


async def _push_rollcall_started(chat_id: int, title: str) -> None:
    """Background task: send web push to group subscribers when a rollcall opens."""
    try:
        import os as _os
        from services import push as push_svc
        chat = manager.get_chat(chat_id)
        group_token = chat.get("group_web_token") if chat else None
        if not group_token:
            return
        web_base = _os.environ.get("WEB_BASE_URL", "").rstrip("/")
        url = f"{web_base}/web/group/{group_token}" if web_base else f"/web/group/{group_token}"
        await push_svc.notify_rollcall_started(group_token, title, url)
    except Exception:
        logging.exception("[push] _push_rollcall_started failed chat=%s", chat_id)


async def _push_rollcall_ended(chat_id: int, title: str) -> None:
    """Background task: send web push when a rollcall closes."""
    try:
        import os as _os
        from services import push as push_svc
        chat = manager.get_chat(chat_id)
        group_token = chat.get("group_web_token") if chat else None
        if not group_token:
            return
        web_base = _os.environ.get("WEB_BASE_URL", "").rstrip("/")
        url = f"{web_base}/web/group/{group_token}" if web_base else f"/web/group/{group_token}"
        await push_svc.notify_rollcall_ended(group_token, title, url)
    except Exception:
        logging.exception("[push] _push_rollcall_ended failed chat=%s", chat_id)


async def end_rollcall(
    chat_id: int,
    rc_number: int,
    ended_by_user_id: int,
    ended_by_name: str,
    ended_by_username: str | None = None,
) -> dict:
    """
    End the rollcall at the given 0-based position. Performs all DB-level
    finalization (streak updates, per-user stat increments, removal from
    the manager, panel-id renumber).

    Args:
      chat_id           — chat owning the rollcall
      rc_number         — 0-based index. 0 = first active rollcall.
      ended_by_*        — identity of user ending it

    Returns:
      {
        "ended": {...serialized snapshot of the ended rollcall...},
        "rc_number_ended_1based": int,
        "ghost_eligible": bool,           # True if ghost-mark prompt should be shown
        "ghost_rc_db_id": int | None,     # DB id of the ended rollcall for ghost callbacks
        "ended_by": {"id": ..., "name": ..., "username": ...},
        "remaining": [...serialized rollcalls left active after this one ended...],
        "renumbered": [{"old": 2, "new": 1, "title": "..."}, ...],
      }

    Raises:
      rollCallNotStarted   — no active rollcall in this chat
      incorrectParameter   — rc_number is out of range

    Caller should hold `manager.get_erc_lock(chat_id)` if it needs the
    end-vs-vote race exclusion that the bot's `/erc` handler uses today.
    This service intentionally does NOT acquire the erc lock itself —
    adapters that want serialization must hold it; adapters that don't
    care (e.g. tests) can call directly.
    """
    rollcalls = manager.get_rollcalls(chat_id)
    if len(rollcalls) == 0:
        raise rollCallNotStarted("Roll call is not active")
    if rc_number < 0 or rc_number >= len(rollcalls):
        raise incorrectParameter(
            "The rollcall number doesn't exist, check /rollcalls to see all rollcalls"
        )
    rc = manager.get_rollcall(chat_id, rc_number)
    if rc is None:
        raise rollCallNotStarted("Roll call is not active")

    rc_db_id = getattr(rc, "id", None)
    ghost_tracking_on = manager.get_ghost_tracking_enabled(chat_id)
    in_users = rc.inList
    has_any_users = len(in_users) > 0

    # Streak + stat bookkeeping (same logic as the existing /erc handler)
    participants = {
        u.user_id for u in (rc.inList + rc.outList + rc.maybeList + rc.waitList)
        if isinstance(u.user_id, int)
    }
    in_user_ids = {u.user_id for u in in_users if isinstance(u.user_id, int)}
    for uid in in_user_ids:
        update_streak_on_checkin(chat_id, uid)
    # Streak resets for participants who voted but didn't end up IN
    # (OUT or MAYBE). No-shows are handled separately by the ghost flow.
    for uid in participants - in_user_ids:
        reset_user_streak(chat_id, uid)
    for uid in participants:
        increment_user_stat(chat_id, uid, "total_rollcalls")

    # Same semantics for proxy participants, keyed on name.
    proxy_participants = {
        u.name for u in (rc.inList + rc.outList + rc.maybeList + rc.waitList)
        if not isinstance(u.user_id, int)
    }
    proxy_in_names = {u.name for u in in_users if not isinstance(u.user_id, int)}
    for name in proxy_in_names:
        update_proxy_streak_on_checkin(chat_id, name)
    for name in proxy_participants - proxy_in_names:
        reset_proxy_streak(chat_id, name)

    # Snapshot before removal so caller can render finish lists / ghost UI.
    ended_snapshot = serialize_rollcall(rc, rc_number)
    ended_number_1based = rc_number + 1
    title = rc.title

    manager.remove_rollcall(chat_id, rc_number)

    logging.info(
        f"[{_ts()}] [CHAT {chat_id}] Rollcall ended: '{title}' "
        f"by {ended_by_name} (@{ended_by_username or 'none'})"
    )
    log_admin_action(
        chat_id, ended_by_user_id, ended_by_name,
        "end_rollcall", target_name=title,
    )

    # Fire web-push to subscribers — non-blocking, best-effort
    from bot_state import _log_task_exc
    asyncio.create_task(_push_rollcall_ended(chat_id, title)).add_done_callback(_log_task_exc)

    # Renumber map: after removing rollcall #N, every later rollcall shifts
    # down by 1. Adapters use this to update panel msg ids and announce
    # the new IDs to the chat.
    remaining = manager.get_rollcalls(chat_id)
    renumbered = []
    for idx, rollcall in enumerate(remaining):
        new_id = idx + 1
        old_id = new_id if new_id < ended_number_1based else new_id + 1
        if old_id != new_id:
            renumbered.append({"old": old_id, "new": new_id, "title": rollcall.title})

    # The ghost prompt should fire only if ghost tracking is on, the
    # rollcall had real participation, and it hasn't already been marked
    # absent (e.g. via a previous prompt that was answered).
    ghost_eligible = bool(
        ghost_tracking_on
        and has_any_users
        and rc_db_id
        and not getattr(rc, "absent_marked", False)
    )

    return {
        "ended": ended_snapshot,
        "rc_number_ended_1based": ended_number_1based,
        "ghost_eligible": ghost_eligible,
        "ghost_rc_db_id": rc_db_id if ghost_eligible else None,
        "ended_by": {
            "id": ended_by_user_id,
            "name": ended_by_name,
            "username": ended_by_username,
        },
        "remaining": [serialize_rollcall(r, i) for i, r in enumerate(remaining)],
        "renumbered": renumbered,
    }


def list_rollcalls(chat_id: int) -> list[dict]:
    """
    List all active rollcalls for a chat.

    Returns a list of serialized rollcall dicts in display order
    (rc_index 0..N-1, displayed as #1..#N).
    """
    rollcalls = manager.get_rollcalls(chat_id)
    return [serialize_rollcall(rc, i) for i, rc in enumerate(rollcalls)]


def get_rollcall(chat_id: int, rc_number: int = 0) -> dict:
    """
    Get a single rollcall by 0-based index. Default is the first.

    Raises:
      rollCallNotStarted — no rollcalls active
      incorrectParameter — rc_number out of range
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    return serialize_rollcall(rc, rc_number)


def set_title(
    chat_id: int,
    rc_number: int,
    title: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Rename a rollcall. Empty title falls back to '<Empty>'.

    Returns:
      Serialized rollcall dict with the new title.
    Raises:
      rollCallNotStarted — no rollcalls active
      incorrectParameter — rc_number out of range
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    clean_title = title.strip() or "<Empty>"
    rc.title = clean_title
    rc.save()
    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "set_title", target_name=clean_title,
        rollcall_id=getattr(rc, "id", None), details=str(rc_number + 1),
    )
    logging.info(f"[{_ts()}] [CHAT {chat_id}] Title set to '{clean_title}' by {admin_name}")
    return serialize_rollcall(rc, rc_number)
