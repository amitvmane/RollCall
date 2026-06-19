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
        ph = '%s' if db_type == 'postgresql' else '?'
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
        if db_type == 'postgresql':
            release_connection(conn)

    ghost_count = get_ghost_count(chat_id, user_id)
    return {
        "user_id": user_id,
        "total_rollcalls_in_chat": total_rollcalls,
        "sessions_attended": attended,
        "attendance_rate": _pct(attended, total_rollcalls),
        "total_in_votes": row.get("total_in", 0),
        "total_out_votes": row.get("total_out", 0),
        "total_maybe_votes": row.get("total_maybe", 0),
        "total_sessions_voted": row.get("total_rollcalls", 0),
        "voting_rate": _pct(row.get("total_rollcalls", 0), total_rollcalls),
        "total_waiting_to_in": row.get("total_waiting_to_in", 0),
        "best_streak": row.get("best_streak", 0),
        "current_streak": row.get("current_streak", 0),
        "ghost_count": ghost_count,
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
    return {
        "proxy_name": proxy_name,
        "total_rollcalls_in_chat": total_rollcalls,
        "sessions_attended": attended,
        "attendance_rate": _pct(attended, total_rollcalls),
        "total_in_votes": ps.get("total_in", 0),
        "total_out_votes": ps.get("total_out", 0),
        "total_maybe_votes": ps.get("total_maybe", 0),
        "best_streak": streaks.get("best_streak", 0),
        "current_streak": streaks.get("current_streak", 0),
        "ghost_count": ghost,
    }


def group_stats(chat_id: int) -> dict:
    """Return aggregate group attendance totals."""
    totals = get_group_attendance_totals(chat_id)
    total_rollcalls = get_chat_ended_rollcall_count(chat_id)
    leaderboard = get_leaderboard_by_attendance(chat_id, limit=5)
    ghost_board = get_ghost_leaderboard(chat_id)[:5]
    return {
        "total_rollcalls": total_rollcalls,
        "total_attendances": totals.get("total_in", 0),
        "unique_participants": totals.get("unique_users", 0),
        "top_attendees": [
            {
                "name": row.get("first_name") or row.get("proxy_name"),
                "sessions": row.get("attended", 0),
                "attendance_rate": _pct(row.get("attended", 0), total_rollcalls),
            }
            for row in leaderboard
        ],
        "ghost_leaderboard": [
            {
                "name": row.get("user_name") or row.get("proxy_name"),
                "ghost_count": row.get("ghost_count", 0),
            }
            for row in ghost_board
        ],
    }


def leaderboard(chat_id: int, limit: int = 10) -> list:
    """Return the full attendance leaderboard for a chat."""
    total_rollcalls = get_chat_ended_rollcall_count(chat_id)
    rows = get_leaderboard_by_attendance(chat_id, limit=limit)
    return [
        {
            "rank": i + 1,
            "name": row.get("first_name") or row.get("proxy_name"),
            "user_id": row.get("user_id"),
            "is_proxy": row.get("user_id") is None,
            "sessions": row.get("attended", 0),
            "attendance_rate": _pct(row.get("attended", 0), total_rollcalls),
        }
        for i, row in enumerate(rows)
    ]


def history(chat_id: int, limit: int = 10, offset: int = 0) -> list:
    """Return the last N ended rollcalls for the chat."""
    rows = get_rollcall_history(chat_id, limit=limit, offset=offset)
    return [
        {
            "id": row.get("id"),
            "title": row.get("title"),
            "ended_at": str(row.get("ended_at") or ""),
            "in_count": row.get("in_count", 0),
            "out_count": row.get("out_count", 0),
            "maybe_count": row.get("maybe_count", 0),
        }
        for row in rows
    ]
