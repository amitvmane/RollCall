"""
Ghost tracking services — toggle, set limit, clear, leaderboard, and the
post-session review itself.

Wraps manager + db ghost ops, returns plain dicts, no Telegram formatting.

The review logic (who can be marked, what marking one costs, who gets
forgiven) lived in handlers/ghost.py, which imports bot_state and therefore
telebot — unreachable from the REST API without dragging the bot in. It is
platform-agnostic logic, so it belongs here; handlers/ghost.py now delegates,
and the web review UI calls the same functions rather than reimplementing the
rules and drifting from them.
"""

import logging
from typing import Optional

from exceptions import incorrectParameter
from rollcall_manager import manager
from db import (
    add_ghost_event,
    decrement_ghost_count,
    get_ghost_count,
    get_ghost_count_by_proxy_name,
    get_ghost_leaderboard,
    get_rollcall_in_users,
    get_rollcall_out_users,
    get_user_ghost_count_by_name,
    increment_ghost_count,
    log_admin_action,
    mark_rollcall_absent_done,
    reset_ghost_count,
    reset_user_streak,
    update_chat_settings,
)


def get_ghost_settings(chat_id: int) -> dict:
    """Return current ghost tracking settings for a chat."""
    return {
        "ghost_tracking_enabled": manager.get_ghost_tracking_enabled(chat_id),
        "absent_limit": manager.get_absent_limit(chat_id),
    }


def toggle_ghost_tracking(
    chat_id: int,
    enabled: bool,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Enable or disable ghost tracking for a chat."""
    manager.set_ghost_tracking_enabled(chat_id, enabled)
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "toggle_ghost_tracking",
                     details="on" if enabled else "off")
    return get_ghost_settings(chat_id)


def set_absent_limit(
    chat_id: int,
    limit: int,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Set the ghost absence threshold for a chat (must be >= 1)."""
    if limit < 1:
        raise incorrectParameter("Absent limit must be at least 1.")
    manager.set_absent_limit(chat_id, limit)
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_absent_limit", details=str(limit))
    return get_ghost_settings(chat_id)


def clear_absent(
    chat_id: int,
    admin_user_id: int,
    admin_name: str,
    target_user_id: Optional[int] = None,
    proxy_name: Optional[str] = None,
) -> dict:
    """
    Clear ghost count for one user/proxy or ALL users in the chat.

    - Pass target_user_id (int) to clear one real user.
    - Pass proxy_name (str) to clear one proxy.
    - Pass neither to clear everyone (full reset).

    Returns {"cleared": True}.
    """
    if target_user_id is not None:
        reset_ghost_count(chat_id, target_user_id)
    elif proxy_name is not None:
        reset_ghost_count(chat_id, -1, proxy_name=proxy_name)
    else:
        # Clear all — iterate over the leaderboard rows
        leaderboard = get_ghost_leaderboard(chat_id)
        for row in leaderboard:
            uid = row.get("user_id")
            pname = row.get("proxy_name")
            if uid:
                reset_ghost_count(chat_id, uid)
            elif pname:
                reset_ghost_count(chat_id, -1, proxy_name=pname)

    log_admin_action(chat_id, admin_user_id, admin_name,
                     "clear_absent",
                     details=(
                         f"user:{target_user_id}" if target_user_id else
                         f"proxy:{proxy_name}" if proxy_name else "all"
                     ))
    return {"cleared": True}


def find_ghost_record(chat_id: int, name: str) -> Optional[dict]:
    """
    Find a ghost record by name (exact first, then Levenshtein fuzzy fallback ≤3).

    Returns the matching record dict or None if no close match exists.
    """
    record = get_user_ghost_count_by_name(chat_id, name)
    if record:
        return record
    leaderboard = get_ghost_leaderboard(chat_id)
    best = None
    best_score = None
    try:
        from Levenshtein import distance as lev_distance
        for entry in leaderboard:
            entry_name = entry.get("user_name") or entry.get("proxy_name") or ""
            score = lev_distance(name.lower(), entry_name.lower())
            if best_score is None or score < best_score:
                best_score = score
                best = entry
    except ImportError:
        pass
    if best and best_score is not None and best_score <= 3:
        return best
    return None


def ghost_leaderboard(chat_id: int) -> list:
    """Return the ghost (no-show) leaderboard for a chat."""
    return [
        {
            "name": row.get("user_name") or row.get("proxy_name"),
            "user_id": row.get("user_id"),
            "is_proxy": row.get("user_id") is None,
            "ghost_count": row.get("ghost_count", 0),
        }
        for row in get_ghost_leaderboard(chat_id)
    ]


# ── Post-session review ───────────────────────────────────────────────────────

def candidates(rollcall_id: int) -> list:
    """Everyone who can be marked a no-show for this session.

    IN members first (the usual case), then anyone who ended up OUT. A
    drop-out so late that no replacement could be arranged leaves the side
    short exactly like a no-show does, and every ghost path used to read the
    IN list only, so it could not be recorded at all.

    Each row carries `was_out` so a caller can present the two groups
    differently — marking someone who pulled out is a judgement call, not the
    default.
    """
    rows = []
    seen = set()
    for u in get_rollcall_in_users(rollcall_id):
        ident = u.get("proxy_name") or u.get("user_id")
        seen.add(ident)
        rows.append({**u, "was_out": False})
    for u in get_rollcall_out_users(rollcall_id):
        ident = u.get("proxy_name") or u.get("user_id")
        if ident in seen:
            continue          # can't be both; IN wins
        seen.add(ident)
        rows.append({**u, "was_out": True})
    return rows


def apply_marking(chat_id: int, rollcall_id: int, selected: set, candidate_rows: list) -> list:
    """Write ghost records for `selected` and return human-readable lines.

    `selected` holds user_id (int) for real users, proxy_name (str) for
    proxies. Rows not present in `candidate_rows` are skipped and logged —
    a selection that doesn't correspond to anyone in the session is a bug or
    a stale client, never something to write blindly.

    Does NOT finalise the session — see review_session.
    """
    user_map = {u["user_id"]: u for u in candidate_rows if u.get("user_id") is not None}
    proxy_map = {u["proxy_name"]: u for u in candidate_rows if u.get("proxy_name") is not None}
    lines = []
    for item in selected:
        if isinstance(item, int):
            u = user_map.get(item)
            if not u:
                logging.warning("Ghost: real user %s not found in candidates for rc=%s", item, rollcall_id)
                continue
            name = u.get("first_name") or u.get("username") or str(item)
            increment_ghost_count(chat_id, item, name)
            add_ghost_event(rollcall_id, chat_id, item, name)
            reset_user_streak(chat_id, item)
            lines.append(f"👻 {name} — ghosted {get_ghost_count(chat_id, item)} session(s) total")
        else:
            proxy_name = str(item)
            if proxy_name not in proxy_map:
                logging.warning("Ghost: proxy %s not found in candidates for rc=%s", proxy_name, rollcall_id)
                continue
            increment_ghost_count(chat_id, -1, proxy_name, proxy_name=proxy_name)
            add_ghost_event(rollcall_id, chat_id, None, user_name=proxy_name, proxy_name=proxy_name)
            new_count = get_ghost_count_by_proxy_name(chat_id, proxy_name)
            lines.append(f"👻 {proxy_name} (via /sif) — ghosted {new_count} session(s) total")
    return lines


def forgive_attendees(chat_id: int, in_users: list, selected: set) -> int:
    """Forgive one past absence for every IN member NOT marked a no-show.

    This is the half of the review that people actually feel: answering it is
    what clears old absences for everyone who did turn up. Only IN members are
    ever forgiven — someone who dropped out was never on the hook for this
    session, so marking (or not marking) them changes nothing here.

    decrement_ghost_count floors at 0, so selected=set() means "everyone
    attended". Returns how many were forgiven.
    """
    n = 0
    for u in in_users:
        proxy_name = u.get("proxy_name")
        real_uid = u.get("user_id")
        if proxy_name:
            if proxy_name in selected:
                continue
            decrement_ghost_count(chat_id, -1, proxy_name=proxy_name)
            n += 1
        elif real_uid is not None:
            if real_uid in selected:
                continue
            decrement_ghost_count(chat_id, real_uid)
            n += 1
    return n


def review_session(chat_id: int, rollcall_id: int, selected: set) -> dict:
    """Record a whole review in one call: mark, forgive, close.

    Order matters and is the reason this exists as one function rather than
    three calls at each site: marking reads the pre-review counts, forgiveness
    must see the final selection, and the session is only closed once both
    have happened. Idempotent-ish by virtue of absent_marked — a session
    already reviewed won't appear in pending_reviews again.
    """
    rows = candidates(rollcall_id)
    lines = apply_marking(chat_id, rollcall_id, selected, rows)
    forgiven = forgive_attendees(chat_id, get_rollcall_in_users(rollcall_id), selected)
    mark_rollcall_absent_done(rollcall_id)
    return {"ghosts": len(lines), "forgiven": forgiven, "lines": lines}
