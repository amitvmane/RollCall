"""
Chat-level settings services — timezone, shh/louder, location,
event_fee, individual_fee, when (finalize_date), set_limit (per-rollcall).

All return a dict describing what changed.
"""

import pytz

from exceptions import incorrectParameter
from rollcall_manager import manager
from db import log_admin_action, update_chat_settings
from .common import resolve_rollcall_or_raise, serialize_rollcall


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
