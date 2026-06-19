"""
Template services — list, upsert, start (spawn rollcall), delete,
get_schedule, set_schedule, disable_schedule, enable_schedule.

Framework-agnostic: primitives in, dicts out, curated exceptions only.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import pytz

from exceptions import (
    amountOfRollCallsReached,
    incorrectParameter,
    parameterMissing,
)
from functions import WEEKDAY_MAP, get_next_weekday_datetime
from rollcall_manager import manager
from db import (
    create_or_update_template,
    delete_template,
    disable_template_schedule,
    enable_template_schedule,
    get_template,
    get_templates,
    log_admin_action,
    set_template_schedule,
)

from .common import MAX_ROLLCALLS_PER_CHAT, serialize_rollcall


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ─── Serialization ────────────────────────────────────────────────────────────

def _serialize_template(t: dict) -> dict:
    """Normalize a DB template row into a consistent dict."""
    return {
        "name": t.get("name"),
        "title": t.get("title"),
        "limit": t.get("inlistlimit"),
        "location": t.get("location"),
        "fee": t.get("eventfee"),
        "offset_days": t.get("offsetdays"),
        "offset_hours": t.get("offsethours"),
        "offset_minutes": t.get("offsetminutes"),
        "event_day": t.get("event_day"),
        "event_time": t.get("event_time"),
        "schedule_day": t.get("schedule_day"),
        "schedule_time": t.get("schedule_time"),
        # schedule_enabled is stored as 1/"1"/True or 0/"0"/False/None
        # depending on DB type and migration path; normalize explicitly.
        "schedule_enabled": str(t.get("schedule_enabled", "0")) not in ("0", "False", "None", ""),
        "recurrence_type": t.get("recurrence_type") or "weekly",
        "last_scheduled_date": t.get("last_scheduled_date"),
    }


# ─── List / get ───────────────────────────────────────────────────────────────

def list_templates(chat_id: int) -> list[dict]:
    """Return all templates for a chat."""
    return [_serialize_template(t) for t in get_templates(chat_id)]


def get_one_template(chat_id: int, name: str) -> dict:
    """
    Return a single template by name.
    Raises:
      parameterMissing — name is empty
      incorrectParameter — template not found
    """
    name = _validate_name(name)
    t = get_template(chat_id, name)
    if not t:
        raise incorrectParameter(f"Template '{name}' not found. Use /templates to list.")
    return _serialize_template(t)


# ─── Upsert ───────────────────────────────────────────────────────────────────

def _validate_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        raise parameterMissing("Template name is required")
    return n


def upsert_template(
    chat_id: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
    title: Optional[str] = None,
    limit: Optional[int] = None,
    location: Optional[str] = None,
    fee: Optional[str] = None,
    offset_days: Optional[int] = None,
    offset_hours: Optional[int] = None,
    offset_minutes: Optional[int] = None,
    event_day: Optional[str] = None,
    event_time: Optional[str] = None,
) -> dict:
    """
    Create or update a template. Partial updates merge with the existing
    row — fields passed as None are preserved from the existing template
    (matching the bot handler's behaviour).

    Returns the full serialized template after save.
    Raises:
      parameterMissing — name empty
      incorrectParameter — event_day is not a valid weekday name
    """
    name = _validate_name(name)
    if event_day is not None and event_day.lower() not in WEEKDAY_MAP:
        raise incorrectParameter(
            f"'{event_day}' is not a valid weekday. "
            "Use: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
        )

    # Merge with existing row so callers can do partial updates
    existing = get_template(chat_id, name) or {}
    merged = {
        "title":          title if title is not None else existing.get("title"),
        "inlistlimit":    limit if limit is not None else existing.get("inlistlimit"),
        "location":       location if location is not None else existing.get("location"),
        "eventfee":       fee if fee is not None else existing.get("eventfee"),
        "offsetdays":     offset_days if offset_days is not None else existing.get("offsetdays"),
        "offsethours":    offset_hours if offset_hours is not None else existing.get("offsethours"),
        "offsetminutes":  offset_minutes if offset_minutes is not None else existing.get("offsetminutes"),
        "event_day":      event_day if event_day is not None else existing.get("event_day"),
        "event_time":     event_time if event_time is not None else existing.get("event_time"),
    }

    ok = create_or_update_template(chat_id, name, **merged)
    if not ok:
        raise incorrectParameter("Failed to save template. Please try again.")

    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "set_template", target_name=name,
    )
    return _serialize_template({"name": name, **merged,
                                "schedule_day": existing.get("schedule_day"),
                                "schedule_time": existing.get("schedule_time"),
                                "schedule_enabled": existing.get("schedule_enabled"),
                                "recurrence_type": existing.get("recurrence_type"),
                                "last_scheduled_date": existing.get("last_scheduled_date")})


# ─── Start (spawn rollcall from template) ─────────────────────────────────────

async def start_template(
    chat_id: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
    extra_title: Optional[str] = None,
) -> dict:
    """
    Create a new active rollcall from the template's settings.

    Returns the serialized rollcall that was created.
    Raises:
      parameterMissing — name empty
      incorrectParameter — template not found
      amountOfRollCallsReached — already at 3 active rollcalls
    """
    name = _validate_name(name)
    tmpl = get_template(chat_id, name)
    if not tmpl:
        raise incorrectParameter(f"Template '{name}' not found.")

    rollcalls = manager.get_rollcalls(chat_id)
    if len(rollcalls) >= MAX_ROLLCALLS_PER_CHAT:
        raise amountOfRollCallsReached(
            f"Allowed Maximum number of active roll calls per group is {MAX_ROLLCALLS_PER_CHAT}."
        )

    base_title = tmpl.get("title") or ""
    if extra_title:
        title = (base_title + " – " + extra_title).strip(" –")
    else:
        title = base_title or name

    rc = manager.add_rollcall(chat_id, title)

    if tmpl.get("inlistlimit") is not None:
        rc.inListLimit = tmpl["inlistlimit"]
    if tmpl.get("location"):
        rc.location = tmpl["location"]
    if tmpl.get("eventfee"):
        rc.event_fee = tmpl["eventfee"]

    chat = manager.get_chat(chat_id)
    tzname = chat.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
        tzname = "Asia/Kolkata"
    rc.timezone = tzname
    rc.finalizeDate = None

    event_day = tmpl.get("event_day")
    event_time = tmpl.get("event_time")
    if event_day and event_time:
        dt = get_next_weekday_datetime(tz, event_day, event_time)
        if dt:
            rc.finalizeDate = dt

    if rc.finalizeDate is None:
        days = tmpl.get("offsetdays")
        hours = tmpl.get("offsethours")
        minutes = tmpl.get("offsetminutes")
        if any(v is not None for v in (days, hours, minutes)):
            now = datetime.now(tz)
            rc.finalizeDate = now + timedelta(
                days=days or 0, hours=hours or 0, minutes=minutes or 0
            )

    rc.save()

    rc_index = len(manager.get_rollcalls(chat_id)) - 1
    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "start_template", target_name=name, details=title,
    )
    return serialize_rollcall(rc, max(rc_index, 0))


# ─── Delete ───────────────────────────────────────────────────────────────────

def delete_one_template(
    chat_id: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Delete a template. Returns {"name": ..., "deleted": True}.
    Raises:
      parameterMissing — name empty
      incorrectParameter — template not found / delete failed
    """
    name = _validate_name(name)
    if not get_template(chat_id, name):
        raise incorrectParameter(f"Template '{name}' not found.")
    ok = delete_template(chat_id, name)
    if not ok:
        raise incorrectParameter(f"Failed to delete template '{name}'.")
    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "delete_template", target_name=name,
    )
    return {"name": name, "deleted": True}


# ─── Schedule ─────────────────────────────────────────────────────────────────

_VALID_RECURRENCE = {"weekly", "biweekly", "monthly"}
_VALID_WEEKDAYS = set(WEEKDAY_MAP.keys())


def get_schedule(chat_id: int, name: str) -> dict:
    """Return the schedule info for a template."""
    t = get_one_template(chat_id, name)
    return {
        "name": name,
        "schedule_day": t.get("schedule_day"),
        "schedule_time": t.get("schedule_time"),
        "schedule_enabled": t.get("schedule_enabled", False),
        "recurrence_type": t.get("recurrence_type", "weekly"),
        "last_scheduled_date": t.get("last_scheduled_date"),
    }


def set_schedule(
    chat_id: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
    recurrence_type: str = "weekly",
    schedule_day: Optional[str] = None,
    schedule_time: Optional[str] = None,
    monthly_day: Optional[int] = None,
) -> dict:
    """
    Set or update a template's auto-start schedule.

    For weekly / biweekly: schedule_day (full weekday name) + schedule_time HH:MM.
    For monthly: monthly_day (1-31) + schedule_time HH:MM.
    Returns the updated schedule dict.

    Raises:
      parameterMissing — name empty or required fields missing
      incorrectParameter — invalid recurrence type / weekday / time / template not found
    """
    name = _validate_name(name)
    if not get_template(chat_id, name):
        raise incorrectParameter(f"Template '{name}' not found.")

    recurrence_type = recurrence_type.lower()
    if recurrence_type not in _VALID_RECURRENCE:
        raise incorrectParameter(
            f"Invalid recurrence type '{recurrence_type}'. "
            "Use: weekly, biweekly, monthly"
        )

    if recurrence_type == "monthly":
        if monthly_day is None:
            raise parameterMissing("monthly_day (1-31) is required for monthly schedules")
        if not 1 <= monthly_day <= 31:
            raise incorrectParameter("monthly_day must be 1-31")
        if not schedule_time:
            raise parameterMissing("schedule_time (HH:MM) is required")
        try:
            sh, sm = map(int, schedule_time.split(":"))
            if not (0 <= sh < 24 and 0 <= sm < 60):
                raise ValueError
        except ValueError:
            raise incorrectParameter(f"'{schedule_time}' is not a valid time. Use HH:MM")
        sched_day_str = str(monthly_day)
    else:
        if not schedule_day:
            raise parameterMissing("schedule_day (weekday name) is required")
        sched_day_lower = schedule_day.lower()
        if sched_day_lower not in _VALID_WEEKDAYS:
            raise incorrectParameter(
                f"'{schedule_day}' is not a valid weekday. "
                "Use: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
            )
        if not schedule_time:
            raise parameterMissing("schedule_time (HH:MM) is required")
        try:
            sh, sm = map(int, schedule_time.split(":"))
            if not (0 <= sh < 24 and 0 <= sm < 60):
                raise ValueError
        except ValueError:
            raise incorrectParameter(f"'{schedule_time}' is not a valid time. Use HH:MM")
        sched_day_str = sched_day_lower

    ok = set_template_schedule(chat_id, name, sched_day_str, schedule_time, recurrence_type)
    if not ok:
        raise incorrectParameter("Failed to save schedule. Please try again.")

    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "schedule_template", target_name=name,
        details=f"{recurrence_type} {sched_day_str} {schedule_time}",
    )
    return get_schedule(chat_id, name)


def disable_schedule(
    chat_id: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Disable auto-start for a template. Returns updated schedule dict."""
    name = _validate_name(name)
    if not get_template(chat_id, name):
        raise incorrectParameter(f"Template '{name}' not found.")
    ok = disable_template_schedule(chat_id, name)
    if not ok:
        raise incorrectParameter("Failed to disable schedule.")
    log_admin_action(chat_id, admin_user_id, admin_name, "schedule_template_off", target_name=name)
    return get_schedule(chat_id, name)


def enable_schedule(
    chat_id: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Re-enable a previously disabled schedule. Returns updated schedule dict."""
    name = _validate_name(name)
    if not get_template(chat_id, name):
        raise incorrectParameter(f"Template '{name}' not found.")
    ok = enable_template_schedule(chat_id, name)
    if not ok:
        raise incorrectParameter("Failed to enable schedule.")
    log_admin_action(chat_id, admin_user_id, admin_name, "schedule_template_on", target_name=name)
    return get_schedule(chat_id, name)
