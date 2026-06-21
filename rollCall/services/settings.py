"""
Chat-level settings services — timezone, shh/louder, location,
event_fee, individual_fee, when (finalize_date), set_limit (per-rollcall).

All return a dict describing what changed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytz

from exceptions import incorrectParameter, parameterMissing, timeError
from rollcall_manager import manager
from db import (
    get_or_create_chat,
    increment_rollcall_stat,
    increment_user_stat,
    log_admin_action,
    update_chat_settings,
)
from .common import resolve_rollcall_or_raise, serialize_rollcall, serialize_user


def get_chat_settings(chat_id: int) -> dict:
    """Return current chat-level settings."""
    chat = manager.get_chat(chat_id)
    row = get_or_create_chat(chat_id)
    return {
        "chat_id": chat_id,
        "group_name": row.get("group_name"),
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


def set_rollcall_time(
    chat_id: int,
    rc_number: int,
    datetime_str: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Set or cancel the finalize datetime for a rollcall.

    Pass datetime_str="cancel" to clear both finalizeDate and reminder.
    Otherwise pass "DD-MM-YYYY HH:MM" in the rollcall's configured timezone.

    Returns:
      {"rollcall": ..., "cancelled": bool, "reminder_reset": bool}
    Raises:
      rollCallNotStarted — no rollcall at rc_number
      incorrectParameter — bad format
      timeError          — date is in the past
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)

    if datetime_str.strip().lower() == "cancel":
        rc.finalizeDate = None
        rc.reminder = None
        rc.save()
        log_admin_action(chat_id, admin_user_id, admin_name,
                         "set_rollcall_time", details="cancelled",
                         rollcall_id=getattr(rc, "id", None))
        return {"rollcall": serialize_rollcall(rc, rc_number), "cancelled": True, "reminder_reset": False}

    tz = pytz.timezone(rc.timezone)
    try:
        date = datetime.strptime(datetime_str.strip(), "%d-%m-%Y %H:%M")
    except ValueError:
        raise incorrectParameter(
            f"'{datetime_str}' is not a valid datetime. Use DD-MM-YYYY HH:MM (e.g. 25-12-2026 18:30)."
        )
    date = tz.localize(date)
    now = datetime.now(tz)
    if now > date:
        raise timeError("Please provide valid future datetime.")

    reminder_reset = rc.reminder is not None
    rc.finalizeDate = date
    rc.reminder = None
    rc.save()
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_rollcall_time", details=date.strftime("%d-%m-%Y %H:%M"),
                     rollcall_id=getattr(rc, "id", None))
    return {
        "rollcall": serialize_rollcall(rc, rc_number),
        "cancelled": False,
        "reminder_reset": reminder_reset,
        "finalize_str": date.strftime("%d-%m-%Y %H:%M"),
        "timezone": rc.timezone,
    }


def set_reminder(
    chat_id: int,
    rc_number: int,
    hours: int | None,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Set or cancel the reminder for a rollcall.

    Pass hours=None to cancel. Otherwise hours must be >= 1 and the
    reminder time (finalizeDate - hours) must be in the future.

    Returns:
      {"rollcall": ..., "cancelled": bool, "hours": int|None}
    Raises:
      rollCallNotStarted — no rollcall at rc_number
      parameterMissing   — finalizeDate not set
      incorrectParameter — hours < 1 or reminder already in the past
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)

    if hours is None:
        rc.reminder = None
        rc.save()
        log_admin_action(chat_id, admin_user_id, admin_name,
                         "set_reminder", details="cancelled",
                         rollcall_id=getattr(rc, "id", None))
        return {"rollcall": serialize_rollcall(rc, rc_number), "cancelled": True, "hours": None}

    if rc.finalizeDate is None:
        raise parameterMissing("First you need to set a finalize time for the current rollcall.")
    if hours < 1:
        raise incorrectParameter("Hours must be higher than 1.")

    tz = pytz.timezone(rc.timezone)
    finalize = rc.finalizeDate
    if finalize.tzinfo is None:
        finalize = tz.localize(finalize)
    if finalize - timedelta(hours=hours) < datetime.now(tz):
        raise incorrectParameter("Reminder notification time is less than current time, please set it correctly.")

    rc.reminder = hours
    rc.save()
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_reminder", details=f"{hours}h",
                     rollcall_id=getattr(rc, "id", None))
    return {"rollcall": serialize_rollcall(rc, rc_number), "cancelled": False, "hours": hours}


def get_individual_fee(chat_id: int, rc_number: int = 0) -> dict:
    """
    Compute the per-person fee for a rollcall based on event_fee / IN-list size.

    Returns:
      {"event_fee": str, "in_count": int, "individual_fee": float}
    Raises:
      rollCallNotStarted — no rollcall at rc_number
      parameterMissing   — no event fee set
    """
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    if rc.event_fee is None:
        raise parameterMissing("No event fee set. Use /event_fee to set one first.")
    in_count = len(rc.inList)
    numeric = int(re.sub(r"[^0-9]", "", str(rc.event_fee)) or "0")
    individual = round(numeric / in_count, 2) if in_count > 0 else 0.0
    return {
        "event_fee": str(rc.event_fee),
        "in_count": in_count,
        "individual_fee": individual,
    }


def set_admin_rights(
    chat_id: int,
    enabled: bool,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Enable or disable admin-only mode for a chat.

    Returns:
      {"admin_rights": bool}
    """
    manager.set_admin_rights(chat_id, enabled)
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_admins" if enabled else "unset_admins")
    return {"admin_rights": enabled}
