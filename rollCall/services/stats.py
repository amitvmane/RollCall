"""
Stats services — personal, group, leaderboard, ghost, history.

Returns raw dicts (not formatted strings) so adapters can choose their
own presentation (Markdown for the bot, JSON for the REST API, HTML
for the Mini App).
"""

from typing import Optional

from db import (
    get_bot_attendance_totals,
    get_chat_ended_rollcall_count,
    get_ghost_count,
    get_ghost_count_by_proxy_name,
    get_ghost_leaderboard,
    get_group_attendance_totals,
    get_leaderboard_by_attendance,
    get_proxy_attendance_count,
    get_proxy_stats,
    get_proxy_streaks,
    get_rollcall_history,
    get_user_attendance_count,
    find_proxy_in_chat,
)
from rollcall_manager import manager


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
    from db import get_connection, db_type, release_connection
    raw = arg.strip()
    username = raw[1:] if raw.startswith("@") else None
    name = None if username else raw

    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        active_false = "FALSE" if db_type == "postgresql" else "0"

        if username:
            cur.execute(f"""
                SELECT DISTINCT u.user_id, u.first_name FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND u.username = {ph}
                  AND r.is_active = {active_false}
                ORDER BY u.user_id LIMIT 1
            """, (chat_id, username))
            row = cur.fetchone()
            if row is not None:
                if isinstance(row, dict):
                    return ("real", row["user_id"], row.get("first_name") or arg)
                return ("real", row[0], row[1] or arg)
            return None

        cur.execute(f"""
            SELECT u.user_id, MAX(u.first_name) AS first_name,
                   MAX(u.updated_at) AS latest_seen
            FROM users u
            JOIN rollcalls r ON u.rollcall_id = r.id
            WHERE r.chat_id = {ph} AND u.first_name = {ph}
              AND r.is_active = {active_false}
            GROUP BY u.user_id
            ORDER BY latest_seen DESC
        """, (chat_id, name))
        rows = cur.fetchall()
        if rows:
            if len(rows) > 1:
                return ("ambiguous", len(rows), name)
            r = rows[0]
            if isinstance(r, dict):
                return ("real", r["user_id"], r.get("first_name") or arg)
            return ("real", r[0], r[1] or arg)

        if find_proxy_in_chat(chat_id, name):
            return ("proxy", name, name)
        return None
    finally:
        if cur:
            cur.close()
        if db_type == "postgresql":
            release_connection(conn)


def _pct(num: int, denom: int) -> Optional[float]:
    return round(num / denom * 100, 1) if denom > 0 else None


def personal_stats(chat_id: int, user_id: int) -> dict:
    """Return attendance + vote stats for one real user in a chat."""
    from db import get_connection, db_type, release_connection
    total_rollcalls = get_chat_ended_rollcall_count(chat_id)
    attended = get_user_attendance_count(chat_id, user_id)

    conn = get_connection()
    cur = None
    row = {}
    try:
        cur = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        cur.execute(f"""
            SELECT total_in, total_out, total_maybe, total_rollcalls,
                   total_waiting_to_in, best_streak, current_streak
            FROM user_stats WHERE chat_id = {ph} AND user_id = {ph}
        """, (chat_id, user_id))
        r = cur.fetchone()
        if r:
            row = dict(r)
    finally:
        if cur:
            cur.close()
        if db_type == "postgresql":
            release_connection(conn)

    ghost_count = get_ghost_count(chat_id, user_id)
    absent_limit = manager.get_absent_limit(chat_id)
    return {
        "user_id": user_id,
        "total_rollcalls_in_chat": total_rollcalls,
        "sessions_attended": attended,
        "attendance_rate": _pct(attended, total_rollcalls),
        "total_in_votes": int(row.get("total_in") or 0),
        "total_out_votes": int(row.get("total_out") or 0),
        "total_maybe_votes": int(row.get("total_maybe") or 0),
        "total_sessions_voted": int(row.get("total_rollcalls") or 0),
        "voting_rate": _pct(int(row.get("total_rollcalls") or 0), total_rollcalls),
        "total_waiting_to_in": int(row.get("total_waiting_to_in") or 0),
        "best_streak": int(row.get("best_streak") or 0),
        "current_streak": int(row.get("current_streak") or 0),
        "ghost_count": ghost_count,
        "absent_limit": absent_limit,
    }


def proxy_stats(chat_id: int, proxy_name: str) -> dict:
    """Return attendance stats for a named proxy user."""
    from exceptions import incorrectParameter
    hit = find_proxy_in_chat(chat_id, proxy_name)
    if not hit:
        raise incorrectParameter(f"Proxy '{proxy_name}' not found in ended rollcalls.")
    attended = get_proxy_attendance_count(chat_id, proxy_name)
    total_rollcalls = get_chat_ended_rollcall_count(chat_id)
    ps = get_proxy_stats(chat_id, proxy_name) or {}
    streaks = get_proxy_streaks(chat_id, proxy_name) or {}
    ghost = get_ghost_count_by_proxy_name(chat_id, proxy_name)
    absent_limit = manager.get_absent_limit(chat_id)
    return {
        "proxy_name": proxy_name,
        "total_rollcalls_in_chat": total_rollcalls,
        "total_sessions_voted": int(ps.get("total_rollcalls") or 0),
        "sessions_attended": attended,
        "attendance_rate": _pct(attended, total_rollcalls),
        "voting_rate": _pct(int(ps.get("total_rollcalls") or 0), total_rollcalls),
        "total_in_votes": int(ps.get("total_in") or 0),
        "total_out_votes": int(ps.get("total_out") or 0),
        "total_maybe_votes": int(ps.get("total_maybe") or 0),
        "best_streak": int(streaks.get("best_streak") or 0),
        "current_streak": int(streaks.get("current_streak") or 0),
        "ghost_count": ghost,
        "absent_limit": absent_limit,
    }


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
        "top_attendees": [
            {
                "name": row.get("first_name") or row.get("proxy_name"),
                "sessions": int(row.get("attended") or 0),
                "attendance_rate": _pct(int(row.get("attended") or 0), total_rollcalls),
            }
            for row in lb
        ],
        "ghost_leaderboard": [
            {
                "name": row.get("user_name") or row.get("proxy_name"),
                "ghost_count": int(row.get("ghost_count") or 0),
            }
            for row in ghost_board
        ],
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


def history(chat_id: int, limit: int = 10, offset: int = 0) -> list:
    """Return the last N ended rollcalls for the chat."""
    rows = get_rollcall_history(chat_id, limit=limit, offset=offset)
    return [
        {
            "id": row.get("id"),
            "title": row.get("title"),
            "ended_at": str(row.get("ended_at") or ""),
            "in_count": int(row.get("in_count") or 0),
            "out_count": int(row.get("out_count") or 0),
            "maybe_count": int(row.get("maybe_count") or 0),
        }
        for row in rows
    ]


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
