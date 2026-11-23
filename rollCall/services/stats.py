"""
Stats services — personal, group, leaderboard, ghost, history.

Returns raw dicts (not formatted strings) so adapters can choose their
own presentation (Markdown for the bot, JSON for the REST API, HTML
for the Mini App).
"""

import logging
from typing import Optional

from db import (
    get_bot_attendance_totals,
    get_chat_ended_rollcall_count,
    get_ghost_count,
    get_ghost_count_by_proxy_name,
    get_ghost_leaderboard,
    get_late_drop_leaderboard,
    get_group_attendance_totals,
    get_leaderboard_by_attendance,
    get_proxy_attendance_count,
    get_proxy_stats,
    get_proxy_streaks,
    get_response_times,
    get_rollcall_history,
    get_user_attendance_count,
    get_user_session_history,
    get_user_streaks,
    find_proxy_in_chat,
    find_user_by_username_for_stats,
    find_users_by_name_for_stats,
    get_user_stats_row,
)
from rollcall_manager import manager
from services.voting import LATE_DROP_HOURS
from utils.text import csv_cell


def resolve_user(chat_id: int, arg: str) -> Optional[tuple]:
    """
    Look up a stats target by @username or display name in ended rollcalls.

    Returns one of:
      ('real',      user_id,    display_name)  — matched a real Telegram user
      ('proxy',     proxy_name, proxy_name)    — matched a proxy (/sif) name
      ('ambiguous', count,      name)          — multiple real users share this name
      None                                     — no match

    All queries restrict to ENDED rollcalls so in-progress sessions don't
    shadow real history.
    """
    raw = arg.strip()
    username = raw[1:] if raw.startswith("@") else None
    name = None if username else raw

    if username:
        row = find_user_by_username_for_stats(chat_id, username)
        if row is not None:
            return ("real", row["user_id"], row.get("first_name") or arg)
        return None

    rows = find_users_by_name_for_stats(chat_id, name)
    if rows:
        if len(rows) > 1:
            return ("ambiguous", len(rows), name)
        r = rows[0]
        return ("real", r["user_id"], r.get("first_name") or arg)

    if find_proxy_in_chat(chat_id, name):
        return ("proxy", name, name)
    return None


def _pct(num: int, denom: int) -> Optional[float]:
    return round(num / denom * 100, 1) if denom > 0 else None


_PERSON_KEYS = (
    "total_rollcalls_in_chat", "sessions_attended", "attendance_rate",
    "total_in_votes", "total_out_votes", "total_maybe_votes",
    "total_sessions_voted", "voting_rate", "total_waiting_to_in",
    "best_streak", "current_streak", "ghost_count", "absent_limit",
    "late_drops",
)


def personal_stats(chat_id: int, user_id: int) -> dict:
    """Return attendance + vote stats for one real user in a chat.

    The whole person — guest names merged into them included — via
    services.identity.identity_stats, the same numbers the leaderboard,
    portal and milestones use. This used to read the user_id rows alone, so
    /stats showed a member fewer games than the leaderboard right beside it.
    """
    from services.identity import identity_stats
    data = identity_stats(chat_id, user_id=user_id)
    return {"user_id": user_id, **{k: data[k] for k in _PERSON_KEYS}}


_FORM_ICON = {"in": "✅", "out": "❌", "maybe": "🤔", "waitlist": "⏳",
              "ghost": "👻", "cancelled": "🚫", "miss": "▫️"}


def recap(chat_id: int, user_id: int, name: str = "") -> dict:
    """One member's recap: the same numbers as /stats and the leaderboard
    (personal_stats), plus recent form, top teammates and dues balance."""
    from db import get_top_teammates
    stats = personal_stats(chat_id, user_id)
    history = get_user_session_history(chat_id, user_id, limit=10)
    out = {
        "name": name,
        "games_played": stats["sessions_attended"],
        "games_total": stats["total_rollcalls_in_chat"],
        "attendance_rate": stats["attendance_rate"],
        "current_streak": stats["current_streak"],
        "best_streak": stats["best_streak"],
        "late_drops": stats["late_drops"],
        # Oldest first, so it reads left to right like a scoreline.
        "form": [row.get("status") or "miss" for row in reversed(history)],
        "teammates": get_top_teammates(chat_id, user_id, limit=3),
        "dues_balance": None,
    }
    from db import get_or_create_chat
    if get_or_create_chat(chat_id).get("dues_enabled"):
        from services.dues import my_dues
        out["dues_balance"] = my_dues(chat_id, user_id)["balance"]
    return out


def recap_text(r: dict, group_name: str = "") -> str:
    head = f"🏅 {r['name'] or 'Your'} recap" + (f" — {group_name}" if group_name else "")
    lines = [head, ""]
    rate = f" ({r['attendance_rate']:g}%)" if r["attendance_rate"] is not None else ""
    lines.append(f"🎯 Games played: {r['games_played']} of {r['games_total']}{rate}")
    lines.append(f"🔥 Streak: {r['current_streak']} now · best {r['best_streak']}")
    if r["form"]:
        lines.append("📈 Last games: " + "".join(_FORM_ICON.get(s, "▫️") for s in r["form"]))
    if r["teammates"]:
        lines.append("🤝 Played most with: " + ", ".join(f"{t['name']} ({t['games']})" for t in r["teammates"]))
    if r["late_drops"]:
        lines.append(f"⏰ Late drop-outs: {r['late_drops']}")
    if r["dues_balance"] is not None:
        b = r["dues_balance"]
        lines.append("💰 Dues: all settled" if b == 0 else
                     (f"💰 Dues: ₹{b} to pay" if b > 0 else f"💰 Dues: ₹{-b} in credit"))
    return "\n".join(lines)


def proxy_stats(chat_id: int, proxy_name: str) -> dict:
    """Return attendance stats for a named proxy user (folded with whoever
    it is merged into, if anyone — it is the same person)."""
    from exceptions import incorrectParameter
    from services.identity import identity_stats
    if not find_proxy_in_chat(chat_id, proxy_name):
        raise incorrectParameter(f"Proxy '{proxy_name}' not found in ended rollcalls.")
    data = identity_stats(chat_id, proxy_name=proxy_name)
    return {"proxy_name": proxy_name, **{k: data[k] for k in _PERSON_KEYS}}


def group_stats(chat_id: int) -> dict:
    """Return aggregate group attendance totals (full detail for all adapters)."""
    totals = get_group_attendance_totals(chat_id)
    total_rollcalls = get_chat_ended_rollcall_count(chat_id)
    lb = get_leaderboard_by_attendance(chat_id, limit=5)
    ghost_board = get_ghost_leaderboard(chat_id)[:5]
    real_slots = int(totals.get("real_attendance_slots") or 0)
    proxy_slots = int(totals.get("proxy_attendance_slots") or 0)
    real_pax = int(totals.get("real_participants") or 0)
    proxy_pax = int(totals.get("proxy_participants") or 0)
    total_att_slots = real_slots + proxy_slots
    return {
        "total_rollcalls": total_rollcalls,
        "real_attendance_slots": real_slots,
        "proxy_attendance_slots": proxy_slots,
        "total_attendance_slots": total_att_slots,
        "real_participants": real_pax,
        "proxy_participants": proxy_pax,
        "avg_attendance": round(total_att_slots / total_rollcalls, 1) if total_rollcalls > 0 else 0.0,
        "real_vote_in": int(totals.get("real_vote_in") or 0),
        "real_vote_out": int(totals.get("real_vote_out") or 0),
        "real_vote_maybe": int(totals.get("real_vote_maybe") or 0),
        "proxy_in": int(totals.get("proxy_in") or 0),
        "proxy_out": int(totals.get("proxy_out") or 0),
        "proxy_maybe": int(totals.get("proxy_maybe") or 0),
        "waitlist_promotions": int(totals.get("waitlist_promotions") or 0),
        "vote_peak": voting_peak_times(chat_id),
        "top_attendees": [
            {
                # Leaderboard rows carry display_name; there is no first_name
                # key, so real members used to come out nameless here.
                "name": row.get("display_name") or row.get("proxy_name"),
                "sessions": int(row.get("attended") or 0),
                "attendance_rate": _pct(int(row.get("attended") or 0), total_rollcalls),
            }
            for row in lb
        ],
        "ghost_leaderboard": [
            {
                "name": row.get("user_name") or row.get("proxy_name"),
                "ghost_count": int(row.get("ghost_count") or 0),
                # Who the row is, so the web can open their stats.
                "user_id": row.get("user_id"),
                "proxy_name": None if row.get("user_id") else row.get("proxy_name"),
            }
            for row in ghost_board
        ],
        "late_drop_leaderboard": [
            {"name": row.get("user_name"), "late_drops": row["late_drops"], "user_id": row.get("user_id")}
            for row in get_late_drop_leaderboard(chat_id, limit=5)
        ],
        "late_drop_hours": LATE_DROP_HOURS,
    }


def leaderboard(chat_id: int, limit: int = 10) -> dict:
    """Return the full attendance leaderboard for a chat.

    Returns dict with total_rollcalls_in_chat and entries list so adapters
    can compute percentages without a second query.
    """
    total_rollcalls = get_chat_ended_rollcall_count(chat_id)
    rows = get_leaderboard_by_attendance(chat_id, limit=limit)
    entries = [
        {
            "rank": i + 1,
            "display_name": row.get("display_name") or row.get("first_name") or row.get("proxy_name"),
            "username": row.get("username"),
            "user_id": row.get("user_id"),
            "kind": row.get("kind", "proxy" if row.get("user_id") is None else "real"),
            "sessions_attended": int(row.get("attended") or 0),
            "total_sessions_voted": int(row.get("total_rollcalls") or 0),
            "attendance_rate": _pct(int(row.get("attended") or 0), total_rollcalls),
            "voting_rate": _pct(int(row.get("total_rollcalls") or 0), total_rollcalls),
        }
        for i, row in enumerate(rows)
    ]
    return {
        "total_rollcalls_in_chat": total_rollcalls,
        "entries": entries,
    }


_EXPORT_COLUMNS = [
    "rank", "name", "username", "user_id", "kind",
    "sessions_attended", "total_sessions_voted", "attendance_rate", "voting_rate",
    "ghost_count", "current_streak", "best_streak",
]


def export_stats_csv(chat_id: int) -> str:
    """Leaderboard-style CSV export: one row per member (real or proxy) with
    attendance, ghost, and streak stats. Backs /export_stats.

    Same contract as dues_export_csv (services/dues.py) — returns the raw
    CSV string, caller builds the filename/wraps it for send_document.

    Base rows come from leaderboard() (one query); ghost_count and streaks
    are backfilled per row (get_ghost_count/get_proxy_streaks etc. — the
    same per-entity lookups personal_stats/proxy_stats already do
    individually). This is N+1, deliberately not optimized: unlike the
    bot-wide aggregators fixed in the 2026-08-08 DB audit (hit on every
    render, across every group), this runs once, admin-triggered, bounded
    by one chat's member count.
    """
    import csv
    import io

    lb = leaderboard(chat_id, limit=10_000)
    rows = []
    for entry in lb["entries"]:
        kind = entry["kind"]
        if kind == "real":
            uid = entry["user_id"]
            ghost = get_ghost_count(chat_id, uid)
            streaks = get_user_streaks(chat_id, uid) or {}
        else:
            name = entry["display_name"]
            ghost = get_ghost_count_by_proxy_name(chat_id, name)
            streaks = _folded_proxy_streaks(chat_id, name)
        rows.append({
            "rank": entry["rank"],
            "name": entry["display_name"],
            "username": entry.get("username") or "",
            "user_id": entry["user_id"] if entry["user_id"] is not None else "",
            "kind": kind,
            "sessions_attended": entry["sessions_attended"],
            "total_sessions_voted": entry["total_sessions_voted"],
            "attendance_rate": entry["attendance_rate"] if entry["attendance_rate"] is not None else "",
            "voting_rate": entry["voting_rate"] if entry["voting_rate"] is not None else "",
            "ghost_count": ghost,
            "current_streak": int(streaks.get("current_streak") or 0),
            "best_streak": int(streaks.get("best_streak") or 0),
        })

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows({k: csv_cell(v) for k, v in row.items()} for row in rows)
    return buf.getvalue()


def session_display_date(row: dict) -> str:
    """The date shown to users for a session — prefer finalize_date (when the
    game actually happened) over ended_at (when the bot got around to closing
    it, which can lag by days if it was offline — e.g. a batch of rollcalls
    all closing at once on the next boot, stamping all of them with that
    boot's timestamp instead of their real dates). Falls back to created_at,
    then ended_at, so there's always something to show."""
    return str(row.get("finalize_date") or row.get("created_at") or row.get("ended_at") or "")


def history(chat_id: int, limit: int = 10, offset: int = 0) -> list:
    """Return the last N ended rollcalls for the chat."""
    rows = get_rollcall_history(chat_id, limit=limit, offset=offset)
    return [
        {
            "id": row.get("id"),
            "title": row.get("title"),
            "ended_at": session_display_date(row),
            "in_count": int(row.get("in_count") or 0),
            "out_count": int(row.get("out_count") or 0),
            "maybe_count": int(row.get("maybe_count") or 0),
            # Cancelled games stay in history (people voted on them) but are
            # not turnout — callers render them apart instead of as a game.
            "is_cancelled": bool(row.get("is_cancelled")),
        }
        for row in rows
    ]


# A member needs this many first votes before they can rank: one lucky 3s
# vote shouldn't outrank someone who reliably answers in 30s. Lowered to
# whatever the busiest voter has, so a brand-new group still sees a board.
RESPONSE_TIME_MIN_VOTES = 3


def _median(values: list) -> int:
    v = sorted(values)
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) // 2


def response_time_board(chat_id: int, limit: int = 10) -> dict:
    """Who votes fastest after a rollcall opens, plus the quickest vote ever.

    Ranked by each member's MEDIAN first-vote delay. The average it used
    to sort by is ruled by outliers — one rollcall opened at midnight and
    answered at 8am adds hours — so a member who usually votes in seconds
    could sit outside the top 10 behind someone who voted once, in 9s.
    Grouped by user_id (same name ≠ same person), showing their latest name.

    `record` is the single fastest vote, reported separately: it is the
    number people remember ("I voted in 1 second!") and it is a different
    question from who is reliably quick.
    """
    by_user: dict = {}
    record = None
    for v in get_response_times(chat_id):
        e = by_user.setdefault(v["user_id"], {"secs": []})
        e["secs"].append(v["seconds"])
        e["display_name"], e["username"] = v["first_name"], v["username"]
        if record is None or v["seconds"] < record["seconds"]:
            record = {"user_id": v["user_id"], "display_name": v["first_name"],
                      "username": v["username"], "seconds": v["seconds"]}
    if record:
        # The name as it is now, not as it was on the day.
        latest = by_user[record["user_id"]]
        record["display_name"], record["username"] = latest["display_name"], latest["username"]

    floor = min(RESPONSE_TIME_MIN_VOTES, max((len(e["secs"]) for e in by_user.values()), default=0))
    entries = [
        {
            "user_id": uid,
            "display_name": e["display_name"],
            "username": e["username"],
            "median_response_seconds": _median(e["secs"]),
            "avg_response_seconds": sum(e["secs"]) // len(e["secs"]),
            "best_response_seconds": min(e["secs"]),
            "rollcall_count": len(e["secs"]),
        }
        for uid, e in by_user.items() if len(e["secs"]) >= floor
    ]
    entries.sort(key=lambda x: (x["median_response_seconds"], x["best_response_seconds"], -x["rollcall_count"]))
    return {"entries": entries[:limit], "record": record, "min_votes": floor}


def response_time_stats(chat_id: int, limit: int = 10) -> list:
    """Response-time leaderboard entries, fastest typical voter first."""
    return response_time_board(chat_id, limit)["entries"]


# A (day, limit) row needs this many games before the tip may call it
# best/worst. At 2, a slot tried 3 times (67%) out-ranked one played 44 times
# (57%) — a percentage from a handful of games is noise, not a pattern.
FILL_MIN_GAMES = 5

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def hour_label(h: int) -> str:
    """9 → '9 AM', 0 → '12 AM', 13 → '1 PM'."""
    return f"{(h % 12) or 12} {'AM' if h < 12 else 'PM'}"


def voting_peak_times(chat_id: int, days: int = 90) -> dict:
    """When this group votes: busiest weekday + hour, in the game's timezone.

    Built from each member's first vote per rollcall (users.created_at), so
    it needs no new table. Answers "when do people actually respond?" — when
    to open a rollcall, when a reminder lands, when the bot gets its burst.
    """
    from datetime import datetime, timedelta, timezone as _tz
    import pytz
    import db as _db

    since = (datetime.now(_tz.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    by_hour = [0] * 24
    by_weekday = [0] * 7
    slots: dict = {}
    zones: dict = {}
    total = 0
    for v in _db.get_vote_times(chat_id, since):
        at = v["at"]
        if isinstance(at, str):
            try:
                at = datetime.fromisoformat(at.replace("Z", "").replace("T", " ")[:19])
            except ValueError:
                continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=_tz.utc)      # DB CURRENT_TIMESTAMP is UTC
        name = v.get("tz") or "Asia/Kolkata"
        if name not in zones:
            try:
                zones[name] = pytz.timezone(name)
            except Exception:
                zones[name] = pytz.timezone("Asia/Kolkata")
        local = at.astimezone(zones[name])
        wd, hr = local.weekday(), local.hour
        by_hour[hr] += 1
        by_weekday[wd] += 1
        slots[(wd, hr)] = slots.get((wd, hr), 0) + 1
        total += 1

    def _slot(key, n):
        wd, hr = key
        return {"weekday": _WEEKDAYS[wd], "hour": hr,
                "label": f"{_WEEKDAYS[wd]} {hour_label(hr)}–{hour_label((hr + 1) % 24)}",
                "votes": n, "share": round(100 * n / total) if total else 0}

    top = sorted(slots.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    busiest_hour = max(range(24), key=lambda h: (by_hour[h], -h)) if total else None
    busiest_day = max(range(7), key=lambda d: (by_weekday[d], -d)) if total else None
    return {
        "days": days,
        "total_votes": total,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "top_slots": [_slot(k, n) for k, n in top],
        "busiest_hour": busiest_hour,
        "busiest_hour_label": hour_label(busiest_hour) if busiest_hour is not None else None,
        "busiest_day": _WEEKDAYS[busiest_day] if busiest_day is not None else None,
    }


def period_summary(chat_id: int, days: int = 7) -> dict:
    """Recap of the last N days: sessions run, avg attendance, top attendees.

    Powers /summary and the monthly season wrap-up card.
    """
    from datetime import datetime, timedelta, timezone as _tz
    import db as _db

    end = datetime.now(_tz.utc)
    start = end - timedelta(days=days)
    start_s = start.strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.strftime("%Y-%m-%d %H:%M:%S")

    sessions = _db.get_rollcalls_between(chat_id, start_s, end_s)
    attendance = _db.get_attendance_between(chat_id, start_s, end_s)

    total_slots = sum(int(s.get("in_count") or 0) for s in sessions)
    n = len(sessions)
    return {
        "days": days,
        "sessions": [
            # When the game was played, not when the bot closed it (which
            # lags by days after an outage) — same rule as /history.
            {"title": s.get("title"), "in_count": int(s.get("in_count") or 0),
             "ended_at": session_display_date(s)}
            for s in sessions
        ],
        "session_count": n,
        "avg_attendance": round(total_slots / n, 1) if n else 0.0,
        "top_attendees": [
            {"name": a["name"], "attended": int(a["attended"]),
             "kind": a.get("kind", "real")}
            for a in attendance[:5]
        ],
    }


_BADGE_GAMES_TIERS = (200, 100, 50, 25, 10)


def _folded_proxy_streaks(chat_id: int, proxy_name: str) -> dict:
    """Streaks for a canonical guest name plus any names merged into it —
    leaderboard rows are already folded, so their streak must be too."""
    from services.identity import get_alias_group
    from db import get_streaks_for
    group = get_alias_group(chat_id, proxy_name=proxy_name)
    if group["kind"] == "user":
        return get_user_streaks(chat_id, group["user_id"])
    return get_streaks_for(chat_id, [], [group["proxy_name"], *group["aliases"]])


def _entry_badges(chat_id: int, entry: dict) -> list:
    """Badge chips for a leaderboard entry — streak fire + highest games tier."""
    badges = []
    try:
        if entry.get("kind") == "proxy" or entry.get("user_id") is None:
            streak = _folded_proxy_streaks(chat_id, entry.get("display_name") or "").get("current_streak", 0)
        else:
            streak = get_user_streaks(chat_id, entry["user_id"]).get("current_streak", 0)
        if streak >= 5:
            badges.append(f"🔥{streak}")
        attended = int(entry.get("sessions_attended") or 0)
        for tier in _BADGE_GAMES_TIERS:
            if attended >= tier:
                badges.append(f"🏅{tier}")
                break
    except Exception:
        pass
    return badges


def _ts(value):
    """A DB timestamp (datetime on Postgres, text on SQLite) as a naive datetime."""
    from datetime import datetime
    if value is None or isinstance(value, datetime):
        return value.replace(tzinfo=None) if value is not None else None
    try:
        return datetime.fromisoformat(str(value).strip()).replace(tzinfo=None)
    except ValueError:
        return None


# "Sunday Football", "Sat 7am" — a weekday named in a rollcall title, as a
# whole word. Full names and the usual short forms; Monday=0.
_TITLE_DAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
               "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
               "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}


def title_weekday(title) -> Optional[int]:
    """The weekday a title names ('Sunday Football' → 6), or None when it
    names none — or two different ones ('Sat/Sun'), which can't be settled."""
    import re
    found = {_TITLE_DAYS[w] for w in re.findall(r"[a-z]+", str(title or "").lower()) if w in _TITLE_DAYS}
    return found.pop() if len(found) == 1 else None


def game_weekday(g: dict):
    """The weekday a finished game was PLAYED (Monday=0), and how it was known.

    1. 'time'  — the game time (finalize_date, stored as the game's local
                 wall time) when one was set.
    2. 'title' — a weekday named in the title ('Sunday Football').
    Otherwise (None, 'unknown'). Never guessed from when voting opened (a
    weekend game whose vote opens on Monday is not a Monday game) nor from
    when it was ended (an admin closing Sunday's game on Monday morning)."""
    from datetime import datetime

    fd = g.get("finalize_date")
    if fd:
        try:
            return datetime.strptime(str(fd)[:10], "%Y-%m-%d").weekday(), "time"
        except ValueError:
            pass
    wd = title_weekday(g.get("title"))
    return (wd, "title") if wd is not None else (None, "unknown")


def _fill_row(b: dict, limit: int) -> dict:
    """One (day, limit) bucket's numbers — shared by the weekday rows and the
    day-unknown rows so both are counted by the same rule."""
    return {
        "limit": limit or None,
        "games": b["games"],
        "limited_games": b["games"] if limit else 0,
        "filled": b["filled"],
        "fill_pct": round(100 * b["filled"] / b["games"]) if limit else None,
        "avg_in": round(b["spots"] / b["games"], 1),
        "avg_limit": limit or None,
        "peak_signups": b["peak"],
        "median_fill_seconds": _median(b["fill_secs"]) if b["fill_secs"] else None,
    }


def fill_by_weekday(chat_id: int, days: Optional[int] = None) -> dict:
    """Which game days fill up to their limit — all games, or the last N days.

    One row per (weekday the game was PLAYED, limit): a group that runs an
    8-a-side on Sundays and a 12-a-side on Wednesdays gets two rows, never one
    blurred "avg 2.2/7.5". No-limit games get their own row per day.

    Games whose day can't be known (no game time, no weekday in the title)
    are kept OUT of the weekday rows and the best/worst tip — a guessed day
    would put them on the wrong weekday — and reported apart, per limit, in
    unknown_day.

    A game counts as filled when its IN list reached the limit: signups at
    close (IN + waitlist, members and guests) plus late drop-outs who held a
    spot. avg_in is spots taken (capped at the limit); peak_signups is the
    most who wanted in (waitlist included) — demand beyond the limit says
    whether it could go up. Time to fill is when the limit-th signup first
    voted, from when the rollcall opened (median). Cancelled games are left
    out. Day = game_weekday()."""
    from datetime import datetime, timedelta, timezone as _tz
    import db as _db

    since = None
    if days:
        since = (datetime.now(_tz.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    buckets: dict = {}
    title_dated = 0
    for g in _db.get_fill_history(chat_id, since):
        wd, source = game_weekday(g)          # wd None → the day-unknown bucket
        title_dated += source == "title"
        limit = int(g.get("in_list_limit") or 0)
        b = buckets.setdefault((wd, limit), {"games": 0, "filled": 0, "spots": 0,
                                             "peak": 0, "fill_secs": []})
        b["games"] += 1
        b["spots"] += min(g["signups"], limit) if limit else g["signups"]
        b["peak"] = max(b["peak"], g["signups"])
        if limit and g["signups"] + g["late_drops"] >= limit:
            b["filled"] += 1
            opened = _ts(g.get("created_at"))
            joined = _ts(g["join_times"][limit - 1]) if len(g["join_times"]) >= limit else None
            if opened and joined:
                b["fill_secs"].append(max(0, int((joined - opened).total_seconds())))

    # Week order; within a day the biggest limit first, no-limit last.
    order = sorted(buckets, key=lambda k: (7 if k[0] is None else k[0], -k[1] if k[1] else 1))
    rows = [dict(_fill_row(buckets[k], k[1]), weekday=_WEEKDAYS[k[0]]) for k in order if k[0] is not None]
    unknown = [_fill_row(buckets[k], k[1]) for k in order if k[0] is None]
    # Per day, all limits together: "filled" is always against each game's own
    # limit, so adding limits up is fair. Computed before one-offs are folded
    # (they are still that day's games); same 5-game floor as the tip.
    per_day: dict = {}
    for wd, limit in order:
        if wd is not None and limit:
            d = per_day.setdefault(wd, [0, 0])
            d[0] += buckets[(wd, limit)]["filled"]
            d[1] += buckets[(wd, limit)]["games"]
    by_day = sorted(({"weekday": _WEEKDAYS[wd], "filled": f, "games": n, "fill_pct": round(100 * f / n)}
                     for wd, (f, n) in per_day.items() if n >= FILL_MIN_GAMES),
                    key=lambda r: (-r["fill_pct"], -r["games"]))
    # A (day, limit) played only once is a one-off, not a slot the group runs:
    # folded into one line so the table shows the regular slots. Never folds
    # everything away — a new group whose rows are all one-offs sees them all.
    one_offs = [r for r in rows if r["games"] == 1]
    if len(one_offs) < len(rows):
        rows = [r for r in rows if r["games"] > 1]
    else:
        one_offs = []
    # Best / worst only among rows with enough limited games to mean something:
    # fill rate first, then how close the average comes to the limit.
    ranked = sorted((r for r in rows if r["limit"] and r["games"] >= FILL_MIN_GAMES),
                    key=lambda r: (-r["fill_pct"], -(r["avg_in"] / r["limit"]), -r["games"]))
    best = ranked[0] if ranked else None
    worst = ranked[-1] if len(ranked) > 1 and ranked[-1]["fill_pct"] < ranked[0]["fill_pct"] else None
    out = {"days": days, "weekdays": rows, "unknown_day": unknown,
           "best": best["weekday"] if best else None,
           "best_limit": best["limit"] if best else None,
           "worst": worst["weekday"] if worst else None,
           "worst_limit": worst["limit"] if worst else None,
           # One limit across every row → the tip needn't name it.
           "single_limit": len({r["limit"] for r in rows if r["limit"]}) <= 1,
           "title_dated_games": title_dated,
           "undated_games": sum(r["games"] for r in unknown),
           "by_day": by_day,
           "one_off_games": len(one_offs),
           "one_off_days": [d for d in _WEEKDAYS if any(r["weekday"] == d for r in one_offs)],
           "total_games": sum(r["games"] for r in rows) + len(one_offs),
           "limited_games": sum(r["limited_games"] for r in rows)}
    out["dating_note"] = fill_dating_note(out)
    out["unknown_day_hint"] = UNKNOWN_DAY_HINT if unknown else ""
    out["one_off_note"] = fill_one_off_note(out)
    out["by_day_note"] = fill_by_day_note(out)
    return out


def fill_dating_note(data: dict) -> str:
    """One shared wording, /stats days and the web card: how many weekday-row
    games were placed by the day in their title. '' when none."""
    t = data.get("title_dated_games") or 0
    if not t:
        return ""
    return f"{t} game{'' if t == 1 else 's'} had no game time set — dated by the day in the title."


def fill_by_day_note(data: dict) -> str:
    """'By day: Sun fills 57% (27/47) · Sat 51% (22/43)' — every limit of a day
    together, most reliable first. Shared by /stats days and the web; ''
    when no day has enough games."""
    days = data.get("by_day") or []
    if not days:
        return ""
    return "By day: " + " · ".join(f"{d['weekday'][:3]} fills {d['fill_pct']}% ({d['filled']}/{d['games']})"
                                   for d in days)


def fill_one_off_note(data: dict) -> str:
    """'6 one-off games not shown — a day and limit played only once (Tue, Thu, Fri).'
    One shared wording for /stats days and the web card; '' when none."""
    k = data.get("one_off_games") or 0
    if not k:
        return ""
    days = ", ".join(d[:3] for d in data.get("one_off_days") or [])
    return (f"{k} one-off game{'' if k == 1 else 's'} not shown — "
            f"a day and limit played only once ({days}).")


UNKNOWN_DAY_HINT = "No game time and no day in the title. Set one with /when, or name the day in the title."


def fill_duration(secs: Optional[int]) -> str:
    """"~25m", "~6h", "~2d" — a median fill time, said roughly."""
    if secs is None:
        return ""
    if secs < 3600:
        return f"~{max(1, round(secs / 60))}m"
    if secs < 2 * 86400:
        return f"~{round(secs / 3600)}h"
    return f"~{round(secs / 86400)}d"


def member_stats(chat_id: int, *, user_id: Optional[int] = None, proxy_name: Optional[str] = None,
                 total_participants: Optional[int] = None, require_record: bool = True) -> Optional[dict]:
    """One member's (user_id) or guest's (proxy_name) stats as the web shows
    them: the /stats numbers plus rank and a recent-games strip. None when
    they have no record in this group (require_record=False keeps the
    all-zero card — the viewer's own, before their first game). Stats are
    open to every member (user decision 2026-11-22); callers decide who may
    ask, not this."""
    try:
        if user_id:
            p = personal_stats(chat_id, user_id)
            if require_record and not (p.get("total_sessions_voted") or p.get("sessions_attended")):
                return None
        elif proxy_name:
            p = proxy_stats(chat_id, proxy_name)
            if not p or (require_record and not (p.get("total_sessions_voted") or p.get("sessions_attended"))):
                return None
        else:
            return None
    except Exception:
        logging.exception("member_stats failed (chat %s)", chat_id)
        return None
    if total_participants is None:
        gs = group_stats(chat_id)
        total_participants = gs["real_participants"] + gs["proxy_participants"]
    rank = None
    for entry in leaderboard(chat_id, limit=100)["entries"]:
        if (user_id and entry.get("user_id") == user_id) or (
                proxy_name and entry.get("kind") == "proxy" and entry.get("display_name") == proxy_name):
            rank = entry["rank"]
            break
    p["rank"] = rank
    p["total_participants"] = total_participants
    p["recent_sessions"] = [
        {"status": r.get("status", "miss"), "ended_at": session_display_date(r)}
        for r in get_user_session_history(chat_id, user_id, limit=15)
    ] if user_id else []
    return p


def web_group_stats(
    group_token: str,
    *,
    lookup_name: Optional[str] = None,
    lookup_user_id: Optional[int] = None,
) -> dict:
    """
    Aggregate stats for the public web stats endpoint.

    Calls the same service functions used by the Telegram /stats handler so
    numbers are always consistent across all platforms.

    personal is populated only when lookup_name or lookup_user_id is given
    and a match is found in ended rollcalls.
    """
    import db as _db
    from exceptions import incorrectParameter as _ie

    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise _ie("This group link is invalid.")
    chat_id = chat["chat_id"]

    gs = group_stats(chat_id)               # same data as /stats group in Telegram
    lb_data = leaderboard(chat_id, limit=10)  # same as /stats top (top 10)
    hist = history(chat_id, limit=10)  # 10 to match the admin console's own default
    rt_board = response_time_board(chat_id, limit=10)

    # Badge chips per leaderboard entry (streak fire + games tier)
    for entry in lb_data["entries"]:
        entry["badges"] = _entry_badges(chat_id, entry)

    total_pax = gs["real_participants"] + gs["proxy_participants"]

    personal: dict | None = None
    if lookup_user_id:
        personal = member_stats(chat_id, user_id=lookup_user_id, total_participants=total_pax,
                                require_record=False)
    elif lookup_name:
        # An unverified name only ever reaches proxy (guest) stats. Another
        # member's record is for signed-in members of this group, through
        # member_stats() behind its own route — ?name=@alice used to hand
        # anyone holding the group link Alice's record.
        resolved = resolve_user(chat_id, lookup_name.strip())
        if resolved and resolved[0] == "proxy":
            personal = member_stats(chat_id, proxy_name=resolved[1], total_participants=total_pax,
                                    require_record=False)

    return {
        "total_rollcalls": gs["total_rollcalls"],
        "avg_attendance": gs["avg_attendance"],
        "total_participants": total_pax,
        "real_participants": gs["real_participants"],
        "proxy_participants": gs["proxy_participants"],
        "real_attendance_slots": gs["real_attendance_slots"],
        "proxy_attendance_slots": gs["proxy_attendance_slots"],
        "waitlist_promotions": gs["waitlist_promotions"],
        "leaderboard": lb_data["entries"],
        "ghost_leaderboard": gs["ghost_leaderboard"],
        "late_drop_leaderboard": gs.get("late_drop_leaderboard", []),
        "late_drop_hours": gs.get("late_drop_hours", LATE_DROP_HOURS),
        "recent_history": hist,
        "response_time_leaderboard": rt_board["entries"],
        "response_time_record": rt_board["record"],
        "response_time_min_votes": rt_board["min_votes"],
        "personal": personal,
        "fill_days": fill_by_weekday(chat_id),
        "vote_peak": gs.get("vote_peak"),
    }


def bot_stats() -> dict:
    """
    Bot-wide aggregate statistics (admin-only view).

    Returns raw counts; adapters are responsible for formatting.
    """
    from db import get_connection, db_type, release_connection
    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor()

        def _one(pg_sql, sqlite_sql, params=()):
            if db_type == "postgresql":
                cur.execute(pg_sql, params)
            else:
                cur.execute(sqlite_sql, params)
            row = cur.fetchone()
            if row is None:
                return 0
            val = row[0] if not isinstance(row, dict) else next(iter(row.values()))
            return int(val or 0)

        total_groups       = _one("SELECT COUNT(DISTINCT chat_id) FROM chats",
                                  "SELECT COUNT(DISTINCT chat_id) FROM chats")
        active_groups_7d   = _one(
            "SELECT COUNT(DISTINCT chat_id) FROM rollcalls WHERE created_at >= NOW() - INTERVAL '7 days'",
            "SELECT COUNT(DISTINCT chat_id) FROM rollcalls WHERE created_at >= datetime('now','-7 days')")
        active_groups_30d  = _one(
            "SELECT COUNT(DISTINCT chat_id) FROM rollcalls WHERE created_at >= NOW() - INTERVAL '30 days'",
            "SELECT COUNT(DISTINCT chat_id) FROM rollcalls WHERE created_at >= datetime('now','-30 days')")
        total_rollcalls    = _one("SELECT COUNT(*) FROM rollcalls", "SELECT COUNT(*) FROM rollcalls")
        rollcalls_30d      = _one(
            "SELECT COUNT(*) FROM rollcalls WHERE created_at >= NOW() - INTERVAL '30 days'",
            "SELECT COUNT(*) FROM rollcalls WHERE created_at >= datetime('now','-30 days')")
        total_real_users   = _one("SELECT COUNT(DISTINCT user_id) FROM users",
                                  "SELECT COUNT(DISTINCT user_id) FROM users")
        total_proxy_users  = _one("SELECT COUNT(DISTINCT name) FROM proxy_users",
                                  "SELECT COUNT(DISTINCT name) FROM proxy_users")
        total_templates    = _one("SELECT COUNT(*) FROM templates", "SELECT COUNT(*) FROM templates")

        cur.execute("SELECT SUM(total_in), SUM(total_out), SUM(total_maybe) FROM user_stats")
        row = cur.fetchone()
        if row is None:
            sum_in = sum_out = sum_maybe = 0
        elif isinstance(row, dict):
            vals = list(row.values())
            sum_in, sum_out, sum_maybe = int(vals[0] or 0), int(vals[1] or 0), int(vals[2] or 0)
        else:
            sum_in, sum_out, sum_maybe = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    finally:
        if cur:
            cur.close()
        if db_type == "postgresql":
            release_connection(conn)

    totals = get_bot_attendance_totals()
    ended_rcs      = totals["ended_rollcalls"]
    real_att       = totals["real_attendance_slots"]
    proxy_att      = totals["proxy_attendance_slots"]
    total_att      = real_att + proxy_att
    avg_attendance = round(total_att / ended_rcs, 1) if ended_rcs > 0 else 0.0

    return {
        "total_groups": total_groups,
        "active_groups_7d": active_groups_7d,
        "active_groups_30d": active_groups_30d,
        "total_rollcalls": total_rollcalls,
        "ended_rollcalls": ended_rcs,
        "rollcalls_30d": rollcalls_30d,
        "total_real_users": total_real_users,
        "total_proxy_users": total_proxy_users,
        "total_templates": total_templates,
        "total_attendance_slots": total_att,
        "real_attendance_slots": real_att,
        "proxy_attendance_slots": proxy_att,
        "avg_attendance_per_rollcall": avg_attendance,
        "real_participants": totals["real_participants"],
        "proxy_participants": totals["proxy_participants"],
        "sum_in_votes": sum_in,
        "sum_out_votes": sum_out,
        "sum_maybe_votes": sum_maybe,
    }
