"""
Chat-level settings services — timezone, shh/louder, location,
event_fee, individual_fee, when (finalize_date), set_limit (per-rollcall).

All return a dict describing what changed.
"""

from __future__ import annotations

import pytz

from exceptions import incorrectParameter
from rollcall_manager import manager
from db import (
    increment_rollcall_stat,
    increment_user_stat,
    log_admin_action,
    update_chat_settings,
)
from .common import resolve_rollcall_or_raise, serialize_rollcall, serialize_user


def get_chat_settings(chat_id: int) -> dict:
    """Return current chat-level settings."""
    chat = manager.get_chat(chat_id)
    return {
        "timezone": chat.get("timezone", "Asia/Kolkata"),
        "shh_mode": manager.get_shh_mode(chat_id),
        "admin_rights": manager.get_admin_rights(chat_id),
        "ghost_tracking_enabled": manager.get_ghost_tracking_enabled(chat_id),
        "absent_limit": manager.get_absent_limit(chat_id),
    }


def set_timezone(
    chat_id: int,
    timezone: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Set the chat's timezone.
    Raises:
      incorrectParameter — unrecognised IANA timezone string.
    """
    try:
        pytz.timezone(timezone)
    except pytz.exceptions.UnknownTimeZoneError:
        raise incorrectParameter(
            f"'{timezone}' is not a valid timezone. "
            "Check https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )
    update_chat_settings(chat_id, timezone=timezone)
    chat = manager.get_chat(chat_id)
    chat["timezone"] = timezone
    for rc in manager.get_rollcalls(chat_id):
        rc.timezone = timezone
        rc.save()
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "timezone", details=timezone)
    return {"timezone": timezone}


def set_shh_mode(
    chat_id: int,
    enabled: bool,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Toggle silent (shh) or verbose (louder) mode."""
    manager.set_shh_mode(chat_id, enabled)
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "shh" if enabled else "louder")
    return {"shh_mode": enabled}


def set_rollcall_limit(
    chat_id: int,
    limit: int,
    admin_user_id: int,
    admin_name: str,
    rc_number: int = 0,
) -> dict:
    """
    Set the IN-list cap for one rollcall.
    Pass limit=0 to remove the cap.
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    if limit < 0:
        raise incorrectParameter("Limit must be 0 (no cap) or a positive integer.")
    rc.inListLimit = limit if limit > 0 else None
    rc.save()
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_limit", details=str(limit))
    return serialize_rollcall(rc, rc_number)


def _rc_db_id(rc) -> int | None:
    return getattr(rc, "id", None) or getattr(rc, "db_id", None)


def set_wait_limit(
    chat_id: int,
    limit: int,
    admin_user_id: int,
    admin_name: str,
    rc_number: int = 0,
) -> dict:
    """
    Set the IN-list cap and rebalance the lists immediately.

    Returns a dict with:
      - rollcall: serialized rollcall after change
      - old_limit: int | None
      - new_limit: int
      - was_full: bool
      - demoted: list[dict]  — users moved IN → WAIT (real users only, serialized)
      - promoted: list[dict] — users moved WAIT → IN (serialized)
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)

    if limit <= 0:
        raise incorrectParameter("Input limit is missing or it's not a positive number")

    old_limit = rc.inListLimit
    was_full = old_limit is not None and len(rc.inList) >= int(old_limit)

    rc.inListLimit = limit
    rc.save()

    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_limit", details=str(limit))

    demoted = []
    promoted = []

    if len(rc.inList) > limit:
        excess = rc.inList[limit:]
        rc.waitList.extend(excess)
        rc.inList = rc.inList[:limit]
        for u in excess:
            rc._save_user_to_db(u, "waitlist")
        rc.save()
        demoted = [serialize_user(u) for u in excess if isinstance(u.user_id, int)]

    elif len(rc.inList) < limit:
        slots = limit - len(rc.inList)
        moving = rc.waitList[:slots]
        rc.inList.extend(moving)
        rc.waitList = rc.waitList[slots:]
        rc_db_id = _rc_db_id(rc)
        for u in moving:
            rc._save_user_to_db(u, "in")
            if rc_db_id is not None and isinstance(u.user_id, int):
                increment_user_stat(chat_id, u.user_id, "total_waiting_to_in")
                increment_user_stat(chat_id, u.user_id, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")
        rc.save()
        promoted = [serialize_user(u) for u in moving]

    return {
        "rollcall": serialize_rollcall(rc, rc_number),
        "old_limit": old_limit,
        "new_limit": limit,
        "was_full": was_full,
        "demoted": demoted,
        "promoted": promoted,
    }


def set_location(
    chat_id: int,
    location: str,
    admin_user_id: int,
    admin_name: str,
    rc_number: int = 0,
) -> dict:
    """Set the location field on a rollcall."""
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    rc.location = location.strip() or None
    rc.save()
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "location", details=location)
    return serialize_rollcall(rc, rc_number)


def set_event_fee(
    chat_id: int,
    fee: str,
    admin_user_id: int,
    admin_name: str,
    rc_number: int = 0,
) -> dict:
    """Set the event fee on a rollcall."""
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    rc.event_fee = fee.strip() or None
    rc.save()
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "event_fee", details=fee)
    return serialize_rollcall(rc, rc_number)
