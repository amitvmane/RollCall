"""
Template services — list, upsert, start (spawn rollcall), delete,
get_schedule, set_schedule, disable_schedule, enable_schedule.

Framework-agnostic: primitives in, dicts out, curated exceptions only.
"""
import logging
import time
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
    create_scheduled_rollcall,
    delete_template,
    disable_template_schedule,
    enable_template_schedule,
    get_template,
    get_templates,
    get_upcoming_scheduled_rollcalls,
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
        "schedule_expires_at": t.get("schedule_expires_at"),
    }


# ─── List / get ───────────────────────────────────────────────────────────────

def list_templates(chat_id: int) -> list[dict]:
    """Return all REUSABLE templates for a chat.

    Excludes /schedule_once's internal one-off templates (reserved-name
    prefix — see the module note below) from every admin-facing listing
    this backs: /templates, the web page's template manager, the token-
    gated REST API. Those rows exist purely to carry fields through to a
    single firing and get deleted the moment that happens; before they
    fire, they are still real rows an admin could otherwise see and be
    confused by ("what is __once_-100.._1790..., did I create that?").

    get_template(chat_id, name) — singular, used by list_pending_once and
    the firing logic to resolve a name it already knows — is intentionally
    NOT filtered: that is exactly how a one-off's fields get found again at
    fire time.
    """
    return [_serialize_template(t) for t in get_templates(chat_id)
            if not is_reserved_once_template_name(t.get("name"))]


def list_pending_once(chat_id: int) -> list[dict]:
    """Return pending one-time scheduled rollcalls for a chat, with the
    referenced template's real fields resolved when one matches.

    The one-time "Schedule" flow (New Rollcall modal) always saves a
    template first, then repurposes a scheduled_rollcalls row's `title`
    column to hold that template's NAME rather than a raw display title —
    no new column needed to link them (see check_reminders.py's firing
    logic). This is the single shared place that resolves that link, used
    by every "what's scheduled" surface (the web page's own one-time list,
    /schedules, and Coming Up This Week) so they can't drift out of sync
    with each other.

    Rows whose title doesn't match any template (pre-feature rows, or a
    template that was deleted after being scheduled) come back with
    display_title=None — callers should fall back to showing the raw title.
    """
    rows = get_upcoming_scheduled_rollcalls(chat_id)
    out = []
    for r in rows:
        tmpl = get_template(chat_id, r["title"])
        out.append({
            "id": r["id"],
            "title": r["title"],
            "scheduled_at": r["scheduled_at"],
            "created_by_name": r["created_by_name"],
            "display_title": tmpl.get("title") if tmpl else None,
            "location": tmpl.get("location") if tmpl else None,
            "fee": tmpl.get("eventfee") if tmpl else None,
            "limit": tmpl.get("inlistlimit") if tmpl else None,
        })
    return out


# ─── One-off scheduling (no persistent template required) ─────────────────────
#
# A one-off ("this Friday only, because of the holiday") is created through
# the SAME mechanism as a recurring template's one-time web schedule — a
# template row referenced by name from a scheduled_rollcalls row — because
# that mechanism already exists, is already tested, and a prior version of
# this project explicitly rejected adding new columns to scheduled_rollcalls
# for exactly this kind of case (there's nothing a one-off needs that a
# template doesn't already carry: title/location/fee/limit/event_day/
# event_time). See CLAUDE.md's schema-reuse precedent.
#
# What WAS missing: every existing path (the web "Schedule -> Once" modal,
# and until now every Telegram command) forces the admin to name and keep a
# real, permanent template for what is conceptually a single event — /templates
# lists every template row forever, with no cleanup tied to a one-off firing.
# One /schedule_once a week is one more permanent row a week.
#
# The fix: template creation is OPTIONAL. Omit save_as_template and this uses
# an internally-generated, clearly-marked name that gets deleted the moment
# it fires — /templates never sees it. Pass save_as_template=<name> and you
# get a real, reusable template exactly like /set_template would produce,
# left in place afterward on purpose.

ONCE_TEMPLATE_PREFIX = "__once_"


def is_reserved_once_template_name(name: str) -> bool:
    """True for the internally-generated names schedule_once() hands to
    upsert_template() when the admin didn't ask to keep one. Used by the
    scheduler to know which fired templates are safe to delete, and by the
    public template-naming entry points (/set_template, the REST template
    route) to stop a human from accidentally typing a name that would make
    their own template look disposable and get deleted out from under them.
    """
    return (name or "").strip().lower().startswith(ONCE_TEMPLATE_PREFIX)


def _generate_once_template_name(chat_id: int) -> str:
    """chat_id + current second is enough uniqueness in practice — a
    collision needs two /schedule_once calls in the same chat in the same
    second, and even then upsert_template's merge-on-existing semantics
    make it harmless: the second call just overwrites the still-pending
    first one rather than corrupting anything."""
    return f"{ONCE_TEMPLATE_PREFIX}{chat_id}_{int(time.time())}"


def resolve_next_weekday_utc_iso(chat_id: int, weekday: str, time_str: str) -> str:
    """"tuesday" + "09:00" -> the next such instant, as a UTC ISO string in
    the exact format create_scheduled_rollcall/get_pending_scheduled_rollcalls
    compare against ("%Y-%m-%dT%H:%M:%SZ" — see db.py's scheduled_rollcalls
    queries, which do a plain lexicographic string comparison, so the format
    must match exactly or "is it due yet" silently compares wrong).

    Platform-agnostic (pure computation from the chat's own timezone), kept
    separate from schedule_once() itself so schedule_once can take an
    already-resolved instant — matching create_scheduled_rollcall's existing
    contract — and a future caller with its own picker (a web "quick
    schedule" flow, say) can supply one directly without this weekday parse.

    Raises incorrectParameter if weekday/time_str can't be read — this is a
    closing-time-shaped input, and the project's rule since the 10.5 audit is
    that an unreadable one is refused, never silently dropped.
    """
    if (weekday or "").strip().lower() not in WEEKDAY_MAP:
        raise incorrectParameter(
            f"'{weekday}' is not a valid weekday. "
            "Use: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
        )
    chat = manager.get_chat(chat_id)
    tzname = chat.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
    dt = get_next_weekday_datetime(tz, weekday, time_str)
    if dt is None:
        raise incorrectParameter(
            f"Couldn't read '{time_str}' as a time. Expected 24-hour HH:MM, e.g. 09:00."
        )
    return dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def schedule_once(
    chat_id: int,
    *,
    fire_at_iso: str,
    title: str,
    admin_user_id: int,
    admin_name: str,
    limit: Optional[int] = None,
    location: Optional[str] = None,
    fee: Optional[str] = None,
    event_day: Optional[str] = None,
    event_time: Optional[str] = None,
    save_as_template: Optional[str] = None,
) -> dict:
    """Schedule a rollcall to auto-open once at `fire_at_iso`, without
    requiring the admin to manage a permanent template — see the module
    note above for why a template still exists internally either way.

    save_as_template=None (the default): the underlying template is given
    a reserved, hidden name and deleted by the scheduler once it fires —
    nothing left in /templates.

    save_as_template="name": the underlying template is saved under that
    real name and kept afterward, exactly like /set_template would produce
    — for an admin who realizes this one is worth reusing.

    Returns {"scheduled_at", "template_name", "persistent"}.
    Raises: incorrectParameter (bad event_day/event_time, or
    save_as_template collides with the reserved prefix).
    """
    if save_as_template:
        if is_reserved_once_template_name(save_as_template):
            raise incorrectParameter(
                f"Template names can't start with '{ONCE_TEMPLATE_PREFIX}' "
                "— that prefix is reserved for one-off schedules."
            )
        name = save_as_template
        persistent = True
    else:
        name = _generate_once_template_name(chat_id)
        persistent = False

    upsert_template(
        chat_id, name, admin_user_id, admin_name,
        title=title, limit=limit, location=location, fee=fee,
        event_day=event_day, event_time=event_time,
    )
    create_scheduled_rollcall(
        chat_id=chat_id, title=name, scheduled_at=fire_at_iso,
        created_by_uid=admin_user_id, created_by_name=admin_name,
    )
    return {"scheduled_at": fire_at_iso, "template_name": name, "persistent": persistent}


def upcoming_events(chat_id: int, limit: int = 10) -> list[dict]:
    """Merge three sources into one chronological "what's coming up" list —
    backs /calendar:
      - active rollcalls with a future finalizeDate ("closes")
      - pending one-time scheduled rollcalls ("starts") — reuses
        list_pending_once above rather than re-deriving the title-resolution
      - enabled recurring templates' NEXT occurrence ("recurs") — via
        check_reminders.next_occurrence_datetime, so this can't drift from
        what the scheduler will actually fire

    Each entry: {"kind": "closes"|"starts"|"recurs", "when": tz-aware
    datetime, "label": str}, sorted ascending, capped to `limit`.
    """
    chat = manager.get_chat(chat_id)
    tzname = chat.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(tz)

    events = []

    for rc in manager.get_rollcalls(chat_id):
        fd = getattr(rc, "finalizeDate", None)
        if fd is None:
            continue
        if fd.tzinfo is None:
            fd = tz.localize(fd)
        if fd < now:
            continue
        events.append({"kind": "closes", "when": fd, "label": rc.title})

    for row in list_pending_once(chat_id):
        try:
            when = datetime.fromisoformat(str(row["scheduled_at"]).replace("Z", "+00:00"))
        except (ValueError, AttributeError, KeyError):
            continue
        if when.tzinfo is None:
            when = pytz.UTC.localize(when)
        if when < now:
            continue
        label = row.get("display_title") or row.get("title") or "Rollcall"
        events.append({"kind": "starts", "when": when, "label": label})

    # Local import: avoids a module-level import cycle with check_reminders
    # (which itself imports services.templates locally for the same reason
    # — see _fire_scheduled_rollcalls).
    from check_reminders import next_occurrence_datetime

    for t in get_templates(chat_id):
        if str(t.get("schedule_enabled", "0")) in ("0", "False", "None", ""):
            continue
        sched_time = t.get("schedule_time")
        if not sched_time:
            continue
        recurrence = t.get("recurrence_type") or "weekly"
        nxt = next_occurrence_datetime(
            tz, sched_time, t.get("schedule_day"), recurrence, t.get("last_scheduled_date"),
        )
        if nxt is None:
            continue
        label = f"{t.get('title') or t.get('name')} ({recurrence})"
        events.append({"kind": "recurs", "when": nxt, "label": label})

    events.sort(key=lambda e: e["when"])
    return events[:limit]


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

_UNSAFE_NAME_CHARS = set("<>\"'`\\")


def _valid_hhmm(value: str) -> bool:
    """True if `value` parses as a 24-hour HH:MM — by construction, not by
    hand: "25:99" splits into two fine-looking ints and only fails inside
    datetime(), which is exactly the check functions.py's
    get_next_weekday_datetime already applies for the same reason."""
    try:
        hour, minute = value.split(":")
        datetime(2000, 1, 1, int(hour), int(minute))
        return True
    except (ValueError, TypeError):
        return False


def _validate_name(name: str) -> str:
    """Used by every template operation (get/start/delete/schedule/upsert)
    — deliberately lenient (non-empty only). The unsafe-character check
    lives in _validate_new_name below, applied only at template CREATION,
    so an existing template whose name predates that rule (or was created
    via a path that doesn't enforce it) stays fully usable — starting,
    editing, or deleting it must never be blocked by its own name."""
    n = (name or "").strip()
    if not n:
        raise parameterMissing("Template name is required")
    return n


def _validate_new_name(name: str) -> str:
    """Stricter check for names not yet in the DB — rejects characters that
    render unescaped in some admin surfaces (defense in depth; the web
    page's own renderer also escapes safely regardless of this)."""
    n = _validate_name(name)
    if _UNSAFE_NAME_CHARS & set(n):
        raise incorrectParameter(
            "Template name can't contain < > \" ' ` or \\ "
            "(they're rendered unescaped in some admin surfaces)."
        )
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

    To explicitly CLEAR a string field (title/location/fee/event_day/
    event_time) rather than preserve it, pass "" — it's treated as "set to
    empty" and normalized to None in storage. Pass limit=0 to explicitly
    clear the cap (0 is never a valid real limit). None still always means
    "don't touch this field", which the token-gated REST API's true
    partial-update contract depends on.

    Returns the full serialized template after save.
    Raises:
      parameterMissing — name empty
      incorrectParameter — event_day is not a valid weekday name, or only
        one of event_day/event_time ends up set (both-or-neither)
    """
    name = _validate_name(name)
    if event_day and event_day.lower() not in WEEKDAY_MAP:
        raise incorrectParameter(
            f"'{event_day}' is not a valid weekday. "
            "Use: monday, tuesday, wednesday, thursday, friday, saturday, sunday"
        )
    if event_time and not _valid_hhmm(event_time):
        # A bad event_day was already rejected above, but event_time had no
        # equivalent check — it was accepted here, stored, and then silently
        # produced rc.finalizeDate = None every time the template fired
        # (build_rollcall_from_template's get_next_weekday_datetime call
        # returns None for anything it can't parse, per the fix in
        # services/rollcalls.py's _resolve_close_time). A template that
        # never closes is the same silent-failure shape the 10.4
        # template-offset bug had, just reachable from a different command.
        raise incorrectParameter(
            f"'{event_time}' isn't a 24-hour time. Use HH:MM, e.g. 18:30."
        )

    def _norm_str(v):
        return v or None

    # Merge with existing row so callers can do partial updates. The
    # stricter character check only applies when this is a genuine
    # creation (no existing row) — an existing template must never become
    # unusable because of its own already-established name.
    existing = get_template(chat_id, name)
    if existing is None:
        name = _validate_new_name(name)
    existing = existing or {}
    merged = {
        "title":          _norm_str(title) if title is not None else existing.get("title"),
        "inlistlimit":    (None if limit == 0 else limit) if limit is not None else existing.get("inlistlimit"),
        "location":       _norm_str(location) if location is not None else existing.get("location"),
        "eventfee":       _norm_str(fee) if fee is not None else existing.get("eventfee"),
        "offsetdays":     offset_days if offset_days is not None else existing.get("offsetdays"),
        "offsethours":    offset_hours if offset_hours is not None else existing.get("offsethours"),
        "offsetminutes":  offset_minutes if offset_minutes is not None else existing.get("offsetminutes"),
        "event_day":      _norm_str(event_day) if event_day is not None else existing.get("event_day"),
        "event_time":     _norm_str(event_time) if event_time is not None else existing.get("event_time"),
    }

    # Only enforce both-or-neither when this call actually touches one of
    # the two fields — an unrelated update (e.g. just changing limit) must
    # not be blocked by a pre-existing half-set legacy row it never asked
    # to change.
    if (event_day is not None or event_time is not None) and bool(merged["event_day"]) != bool(merged["event_time"]):
        raise incorrectParameter(
            "event_day and event_time must be set together (or both left unset)."
        )

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
                                "last_scheduled_date": existing.get("last_scheduled_date"),
                                "schedule_expires_at": existing.get("schedule_expires_at")})


# ─── Start (spawn rollcall from template) ─────────────────────────────────────

def build_rollcall_from_template(chat_id: int, tmpl: dict, title: str):
    """Create a rollcall and apply `tmpl`'s settings to it. Returns the rollcall.

    THE single answer to "which template fields become what on a rollcall".
    Everything that starts one from a template goes through here — the
    /start_template command, the one-time web schedule, and the recurring
    auto-start in check_reminders. The auto-start used to carry its own copy
    of this, and that copy had never been given the offset_* fallback below,
    so an offset-configured template auto-opened with no close time and then
    never auto-closed.

    Takes the already-fetched `tmpl` row rather than a name, so the scheduler
    doesn't re-read (and possibly re-resolve differently) a template it is
    holding.
    """
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

    # event_day/event_time — a fixed weekly slot.
    event_day = tmpl.get("event_day")
    event_time = tmpl.get("event_time")
    if event_day and event_time:
        dt = get_next_weekday_datetime(tz, event_day, event_time)
        if dt:
            rc.finalizeDate = dt

    # offset_* — "closes N after it opens", for a one-off with no fixed day.
    if rc.finalizeDate is None:
        days = tmpl.get("offsetdays")
        hours = tmpl.get("offsethours")
        minutes = tmpl.get("offsetminutes")
        if any(v is not None for v in (days, hours, minutes)):
            rc.finalizeDate = datetime.now(tz) + timedelta(
                days=days or 0, hours=hours or 0, minutes=minutes or 0
            )

    rc.save()
    return rc


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

    rc = build_rollcall_from_template(chat_id, tmpl, title)

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

_VALID_RECURRENCE = {"daily", "weekly", "biweekly", "monthly"}
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
        "schedule_expires_at": t.get("schedule_expires_at"),
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
    expires_at: Optional[str] = None,
) -> dict:
    """
    Set or update a template's auto-start schedule.

    For weekly / biweekly: schedule_day (full weekday name) + schedule_time HH:MM.
    For monthly: monthly_day (1-31) + schedule_time HH:MM.
    For daily: schedule_day is ignored (fires every day) — only schedule_time
    is required.

    expires_at — optional "YYYY-MM-DD"; the schedule auto-disables itself
    (template and its content are untouched, only schedule_enabled flips
    off — see check_template_schedules) once the chat's local date passes
    this. Defaults to one year from today if not given, so a schedule can
    never be silently left running for years after everyone's forgotten
    about it — callers that genuinely want a long-lived schedule should
    pass an explicit far-future date.

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
            "Use: daily, weekly, biweekly, monthly"
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
    elif recurrence_type == "daily":
        if not schedule_time:
            raise parameterMissing("schedule_time (HH:MM) is required")
        try:
            sh, sm = map(int, schedule_time.split(":"))
            if not (0 <= sh < 24 and 0 <= sm < 60):
                raise ValueError
        except ValueError:
            raise incorrectParameter(f"'{schedule_time}' is not a valid time. Use HH:MM")
        sched_day_str = None
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

    if expires_at:
        try:
            datetime.strptime(expires_at, "%Y-%m-%d")
        except ValueError:
            raise incorrectParameter(f"'{expires_at}' is not a valid date. Use YYYY-MM-DD")
    else:
        expires_at = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")

    ok = set_template_schedule(chat_id, name, sched_day_str, schedule_time, recurrence_type, expires_at)
    if not ok:
        raise incorrectParameter("Failed to save schedule. Please try again.")

    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "schedule_template", target_name=name,
        details=f"{recurrence_type} {sched_day_str or ''} {schedule_time} until {expires_at}",
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
