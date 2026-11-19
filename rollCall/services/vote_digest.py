"""
Bundled vote notices — the middle setting between "a message per vote" and
silence (/shh).

A group in bundled mode is silent exactly like /shh, and every N hours gets
ONE summary of what moved on its open rollcalls since the last summary — and
nothing at all when nothing moved. Three modes, one column:

  each     shh_mode off                         (today's default)
  bundled  shh_mode on,  vote_digest_hours = N  (N = 1..48)
  silent   shh_mode on,  vote_digest_hours = 0  (today's /shh)

What changed is found by comparing the lists against a snapshot stored when
the last summary went out (system_config), not by timestamps: a snapshot
also sees people removed from a list, and says where each person moved FROM.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import db
from exceptions import incorrectParameter
from rollcall_manager import manager

DEFAULT_HOURS = 6
MAX_HOURS = 48

_STATUS_LABEL = {"in": "IN", "out": "OUT", "maybe": "MAYBE", "waitlist": "WAITLIST"}


def _snap_key(chat_id: int) -> str:
    return f"vote_digest_snap:{chat_id}"


def _sent_key(chat_id: int) -> str:
    return f"vote_digest_sent:{chat_id}"


def mode(chat_id: int) -> dict:
    """{"mode": "each"|"bundled"|"silent", "hours": int}"""
    hours = int(db.get_or_create_chat(chat_id).get("vote_digest_hours") or 0)
    if not manager.get_shh_mode(chat_id):
        return {"mode": "each", "hours": 0}
    return {"mode": "bundled", "hours": hours} if hours > 0 else {"mode": "silent", "hours": 0}


def _snapshot(chat_id: int) -> dict:
    """{rc_db_id: {"title": str, "people": {key: [name, status]}}} for open rollcalls."""
    snap = {}
    for rc in manager.get_rollcalls(chat_id):
        people = {}
        for status, users in (("in", rc.inList), ("out", rc.outList),
                              ("maybe", rc.maybeList), ("waitlist", rc.waitList)):
            for u in users:
                people[str(u.user_id)] = [u.name, status]
        snap[str(rc.id)] = {"title": rc.title, "people": people}
    return snap


def _save_snapshot(chat_id: int, snap: dict) -> None:
    db.set_system_config(_snap_key(chat_id), json.dumps(snap))
    db.set_system_config(_sent_key(chat_id), datetime.now(timezone.utc).isoformat(timespec="seconds"))


def set_mode(chat_id: int, mode_name: str, hours: int | None,
             admin_user_id: int, admin_name: str) -> dict:
    """Switch between each / bundled / silent. Returns mode()."""
    mode_name = (mode_name or "").strip().lower()
    if mode_name == "bundled":
        hours = DEFAULT_HOURS if hours is None else int(hours)
        if not 1 <= hours <= MAX_HOURS:
            raise incorrectParameter(f"Hours must be between 1 and {MAX_HOURS}.")
        manager.set_shh_mode(chat_id, True)
        db.update_chat_settings(chat_id, vote_digest_hours=hours)
        # Start counting from now: the first summary covers changes made
        # after bundling was switched on, not the whole history.
        _save_snapshot(chat_id, _snapshot(chat_id))
        details = f"bundled every {hours}h"
    elif mode_name in ("each", "silent"):
        manager.set_shh_mode(chat_id, mode_name == "silent")
        db.update_chat_settings(chat_id, vote_digest_hours=0)
        details = mode_name
    else:
        raise incorrectParameter("Mode must be each, bundled or silent.")
    db.log_admin_action(chat_id, admin_user_id, admin_name, "vote_notices", details=details)
    return mode(chat_id)


def clear_bundling(chat_id: int) -> None:
    """/shh and /louder pick an explicit mode; bundling stops with either."""
    if int(db.get_or_create_chat(chat_id).get("vote_digest_hours") or 0):
        db.update_chat_settings(chat_id, vote_digest_hours=0)


def build_digest(chat_id: int, previous: dict, current: dict) -> str | None:
    """Plain-text summary of what moved between two snapshots, or None."""
    sections = []
    for rc_id, rc in current.items():
        before = (previous.get(rc_id) or {}).get("people", {})
        now = rc["people"]
        lines = []
        for key, (name, status) in now.items():
            old = before.get(key)
            if old is None:
                lines.append(f"• {name} → {_STATUS_LABEL.get(status, status)}")
            elif old[1] != status:
                lines.append(f"• {name}: {_STATUS_LABEL.get(old[1], old[1])} → {_STATUS_LABEL.get(status, status)}")
        for key, (name, status) in before.items():
            if key not in now:
                lines.append(f"• {name} removed (was {_STATUS_LABEL.get(status, status)})")
        if lines:
            in_count = sum(1 for _, s in now.values() if s == "in")
            sections.append(f"{rc['title']} — {in_count} IN\n" + "\n".join(lines))
    if not sections:
        return None
    return "🗳 Vote update\n\n" + "\n\n".join(sections)


def due_digest(chat_id: int, now: datetime | None = None) -> str | None:
    """If this chat's bundled summary is due, return its text and advance the
    snapshot; otherwise None. Stamps even when nothing changed, so a quiet
    group is checked again only after another full interval."""
    m = mode(chat_id)
    if m["mode"] != "bundled":
        return None
    now = now or datetime.now(timezone.utc)
    sent = db.get_system_config(_sent_key(chat_id))
    if sent:
        try:
            last = datetime.fromisoformat(sent)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if (now - last).total_seconds() < m["hours"] * 3600:
                return None
        except ValueError:
            pass
    try:
        previous = json.loads(db.get_system_config(_snap_key(chat_id)) or "{}")
    except ValueError:
        previous = {}
    current = _snapshot(chat_id)
    text = build_digest(chat_id, previous, current)
    _save_snapshot(chat_id, current)
    if text:
        logging.info("[vote_digest] chat=%s summary due (%d rollcall(s))", chat_id, len(current))
    return text
