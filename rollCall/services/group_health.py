"""
Group health card — the admin chores nobody remembers, as one checklist.

Every check reads state that already exists (rollcalls, templates, dues,
ghost review); nothing here writes. Each item is
{"level": "ok"|"warn"|"info", "text": str, "fix": str}, where `fix` names
the command (or web section) that resolves it. The Telegram command and the
web admin card both render this one list.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import db
from rollcall_manager import manager

# A member owing more than this is called out by name count, not hidden in
# a total. Deliberately one rupee figure, not a setting: the card nudges,
# the dues commands decide.
OWED_ALERT = 500
GHOST_REVIEW_GRACE_DAYS = 2


def _parse_ts(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check(chat_id: int) -> dict:
    """{"items": [...], "warnings": int}"""
    items = []
    chat = db.get_or_create_chat(chat_id)
    rollcalls = manager.get_rollcalls(chat_id)

    # 1. Is a game coming up at all?
    from services.templates import list_pending_once, list_templates
    scheduled = [t for t in list_templates(chat_id)
                 if t.get("schedule_enabled") and t.get("schedule_time")]
    once = list_pending_once(chat_id)
    if rollcalls:
        items.append({"level": "ok", "text": f"{len(rollcalls)} rollcall(s) open", "fix": ""})
    elif scheduled or once:
        items.append({"level": "ok", "text": "Next game is scheduled", "fix": ""})
    else:
        items.append({"level": "warn", "text": "No game open and nothing scheduled",
                      "fix": "/start_roll_call, or /schedule_template for a weekly game"})

    # 2. Open rollcalls missing the basics.
    for idx, rc in enumerate(rollcalls, start=1):
        if rc.finalizeDate is None:
            items.append({"level": "warn", "text": f"'{rc.title}' has no game time",
                          "fix": f"/set_rollcall_time ::{idx}"})
        if not rc.location:
            items.append({"level": "info", "text": f"'{rc.title}' has no location",
                          "fix": f"/location ::{idx}"})

    # 3. No-shows waiting for review.
    if chat.get("ghost_tracking_enabled"):
        cutoff = datetime.now(timezone.utc) - timedelta(days=GHOST_REVIEW_GRACE_DAYS)
        waiting = [r for r in db.get_unprocessed_rollcalls(chat_id)
                   if (ended := _parse_ts(r.get("ended_at"))) and ended <= cutoff]
        if waiting:
            items.append({"level": "warn",
                          "text": f"{len(waiting)} finished game(s) waiting for a no-show review",
                          "fix": "/mark_absent (or Admin → Review no-shows on the web)"})

    # 4. Money.
    if chat.get("dues_enabled"):
        if not chat.get("upi_vpa"):
            items.append({"level": "warn", "text": "No UPI ID set for collecting dues",
                          "fix": "/set_upi"})
        owing = [b for b in db.get_all_dues_balances(chat_id, nonzero_only=True) if b["balance"] > 0]
        big = [b for b in owing if b["balance"] > OWED_ALERT]
        if big:
            items.append({"level": "warn",
                          "text": f"{len(big)} member(s) owe more than ₹{OWED_ALERT} "
                                  f"(₹{sum(b['balance'] for b in owing)} outstanding in all)",
                          "fix": "/remind_dues"})
        elif owing:
            items.append({"level": "info",
                          "text": f"₹{sum(b['balance'] for b in owing)} outstanding across {len(owing)} member(s)",
                          "fix": "/dues"})
        else:
            items.append({"level": "ok", "text": "Everyone is settled up", "fix": ""})

    # 5. Weather alerts on, but a game they can't work for.
    if int(chat.get("weather_alerts") or 0):
        blind = [rc.title for rc in rollcalls if not rc.location or rc.finalizeDate is None]
        if blind:
            items.append({"level": "info",
                          "text": "Weather alerts need a location and game time: " + ", ".join(f"'{t}'" for t in blind),
                          "fix": "/location and /set_rollcall_time"})

    return {"items": items, "warnings": sum(1 for i in items if i["level"] == "warn")}


_ICON = {"ok": "✅", "warn": "⚠️", "info": "ℹ️"}


def render_text(result: dict) -> str:
    head = ("🩺 Group health — all good" if not result["warnings"]
            else f"🩺 Group health — {result['warnings']} thing(s) need attention")
    lines = [head, ""]
    for i in result["items"]:
        lines.append(f"{_ICON[i['level']]} {i['text']}" + (f"\n    → {i['fix']}" if i["fix"] else ""))
    return "\n".join(lines)
