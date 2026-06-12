"""
/stats command and all stats-building helpers.
"""
import logging

from bot_state import bot
from config import ADMINS
from db import (
    get_ghost_leaderboard, get_connection, db_type, release_connection,
    get_user_attendance_count, get_leaderboard_by_attendance,
    get_chat_ended_rollcall_count,
)
from rollcall_manager import manager


def _esc(text: str) -> str:
    """Escape Markdown v1 special characters in user-supplied strings."""
    if not text:
        return text or ""
    for c in ('_', '*', '`', '['):
        text = text.replace(c, f'\\{c}')
    return text


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/stats", "/s"])
async def stats_command(message):
    cid = message.chat.id
    text = message.text.strip()
    parts = text.split()

    target_user_id = message.from_user.id
    display_name = message.from_user.first_name or "User"
    scope = "me"

    if len(parts) > 1:
        arg = " ".join(parts[1:]).strip()
        lower_arg = arg.lower()

        if lower_arg == "group":
            scope = "group"
        elif lower_arg == "top":
            scope = "top"
        elif lower_arg in ["ghost", "ghosts", "absent"]:
            scope = "ghost"
        elif lower_arg in ["bot", "global", "all"]:
            if message.from_user.id not in ADMINS:
                await bot.send_message(cid, "⛔ Bot-wide statistics are restricted to bot administrators only.")
                return
            scope = "bot"
        else:
            resolved = await resolve_user_for_stats(cid, arg)
            if resolved is None:
                await bot.send_message(cid, f"Could not find user '{arg}' in recent rollcalls for this chat.")
                return
            target_user_id, display_name = resolved
            scope = "other"

    try:
        if scope == "group":
            text = await build_group_stats_text(cid)
        elif scope == "top":
            text = await build_leaderboard_text(cid)
        elif scope == "ghost":
            text = await build_ghost_stats_text(cid, manager)
        elif scope == "bot":
            text = await build_bot_stats_text()
        else:
            text = await build_user_stats_text(cid, target_user_id, display_name)

        await bot.send_message(cid, text, parse_mode="Markdown")
    except Exception:
        logging.exception("Error in /stats")
        await bot.send_message(cid, "Error while fetching stats, please try again later.")


async def build_ghost_stats_text(cid: int, mgr) -> str:
    leaderboard = get_ghost_leaderboard(cid)
    limit = mgr.get_absent_limit(cid)
    tracking_on = mgr.get_ghost_tracking_enabled(cid)

    if not leaderboard:
        return "🏆 No ghosts yet — everyone's been showing up!"

    lines = ["👻 *Ghost Leaderboard*", "─────────────────"]
    for i, entry in enumerate(leaderboard, 1):
        if entry.get('proxy_name'):
            name = f"{entry['proxy_name']} (via /sif)"
        else:
            name = entry.get('user_name') or f"User {entry['user_id']}"
        count = entry['ghost_count']
        warning = " ⚠️" if count >= limit else ""
        lines.append(f"{i}. {name} — {count} session(s) ghosted{warning}")

    lines.append("")
    lines.append(f"Current ghost limit: {limit} session(s)")
    if not tracking_on:
        lines.append("_(Ghost tracking is currently disabled for this group)_")

    return "\n".join(lines)


async def build_bot_stats_text() -> str:
    conn = get_connection()
    try:
        cursor = conn.cursor()

        def _fetch_one(pg_sql, sqlite_sql, params=()):
            if db_type == "postgresql":
                cursor.execute(pg_sql, params)
            else:
                cursor.execute(sqlite_sql, params)
            return cursor.fetchone()[0]

        total_groups = _fetch_one(
            "SELECT COUNT(DISTINCT chat_id) FROM chats",
            "SELECT COUNT(DISTINCT chat_id) FROM chats",
        )
        active_groups_7d = _fetch_one(
            "SELECT COUNT(DISTINCT chat_id) FROM rollcalls WHERE created_at >= NOW() - INTERVAL '7 days'",
            "SELECT COUNT(DISTINCT r.chat_id) FROM rollcalls r WHERE r.created_at >= datetime('now', '-7 days')",
        )
        active_groups_30d = _fetch_one(
            "SELECT COUNT(DISTINCT chat_id) FROM rollcalls WHERE created_at >= NOW() - INTERVAL '30 days'",
            "SELECT COUNT(DISTINCT r.chat_id) FROM rollcalls r WHERE r.created_at >= datetime('now', '-30 days')",
        )
        total_rollcalls = _fetch_one("SELECT COUNT(*) FROM rollcalls", "SELECT COUNT(*) FROM rollcalls")
        rollcalls_30d = _fetch_one(
            "SELECT COUNT(*) FROM rollcalls WHERE created_at >= NOW() - INTERVAL '30 days'",
            "SELECT COUNT(*) FROM rollcalls WHERE created_at >= datetime('now', '-30 days')",
        )
        total_users = _fetch_one("SELECT COUNT(DISTINCT user_id) FROM users", "SELECT COUNT(DISTINCT user_id) FROM users")
        total_templates = _fetch_one("SELECT COUNT(*) FROM templates", "SELECT COUNT(*) FROM templates")

        if db_type == "postgresql":
            cursor.execute("SELECT SUM(total_in), SUM(total_out), SUM(total_maybe) FROM user_stats")
        else:
            cursor.execute("SELECT SUM(total_in), SUM(total_out), SUM(total_maybe) FROM user_stats")
        row = cursor.fetchone()
        if isinstance(row, dict):
            sum_in = row.get("sum_in") or 0
            sum_out = row.get("sum_out") or 0
            sum_maybe = row.get("sum_maybe") or 0
        else:
            sum_in = row[0] or 0
            sum_out = row[1] or 0
            sum_maybe = row[2] or 0

        return "\n".join([
            "*🤖 Bot-Wide Statistics*", "",
            "*Groups:*",
            f"🏘️ Total: {total_groups}",
            f"✅ Active (7d): {active_groups_7d}",
            f"✅ Active (30d): {active_groups_30d}", "",
            "*Rollcalls:*",
            f"📋 Total: {total_rollcalls}",
            f"📈 Last 30d: {rollcalls_30d}", "",
            "*Users:*",
            f"👥 Total: {total_users}",
            f"✅ Total IN: {sum_in}",
            f"❌ Total OUT: {sum_out}",
            f"🤔 Total MAYBE: {sum_maybe}", "",
            f"📝 Templates: {total_templates}",
        ])
    finally:
        cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


async def build_leaderboard_text(chat_id: int, limit: int = 10) -> str:
    """Top-N by REAL attendance (final-IN in ended rollcalls), tiebreak by
    total_rollcalls ASC so a user with the same attended count but fewer
    rollcalls participated (higher %) ranks first."""
    rows = get_leaderboard_by_attendance(chat_id, limit)
    if not rows:
        return "*Leaderboard:*\n\nNo data yet. Participate in some rollcalls first!"

    user_ids = [r['user_id'] for r in rows]

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute(
                "SELECT DISTINCT ON (u.user_id) u.user_id, u.first_name, u.username "
                "FROM users u JOIN rollcalls r ON u.rollcall_id = r.id "
                "WHERE r.chat_id = %s AND u.user_id = ANY(%s) "
                "ORDER BY u.user_id, u.updated_at DESC",
                (chat_id, user_ids),
            )
        else:
            cursor.execute(
                "SELECT u.user_id, u.first_name, u.username, u.updated_at "
                "FROM users u JOIN rollcalls r ON u.rollcall_id = r.id "
                "WHERE r.chat_id = ? AND u.user_id IN ({}) "
                "ORDER BY u.user_id, u.updated_at ASC".format(",".join("?" * len(user_ids))),
                [chat_id] + user_ids,
            )
        name_map = {}
        for ur in cursor.fetchall():
            uid, first_name, username = (ur["user_id"], ur["first_name"], ur["username"]) if isinstance(ur, dict) else (ur[0], ur[1], ur[2])
            name_map[uid] = (first_name, username)
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)

    # Denominator for both rates = total ended rollcalls in the chat
    # (so attendance% can be < 100% for someone who skipped sessions).
    total_rcs = get_chat_ended_rollcall_count(chat_id)

    lines = [f"*Leaderboard (top {len(rows)} by attendance):*", ""]
    for rank, row in enumerate(rows, 1):
        uid = row['user_id']
        attended = row['attended']
        voted = row['total_rollcalls']
        first_name, username = name_map.get(uid, ("User", None))
        name_text = f"@{_esc(username)}" if username else _esc(first_name or "User")
        if total_rcs > 0:
            att_pct = f"{round(attended / total_rcs * 100)}%"
            vote_pct = f"{round(voted / total_rcs * 100)}%"
            lines.append(
                f"{rank}. {name_text} — ✅ {attended}/{total_rcs} ({att_pct})  ·  🗳 {voted}/{total_rcs} ({vote_pct})"
            )
        else:
            lines.append(f"{rank}. {name_text} — ✅ {attended}  ·  🗳 {voted}")

    return "\n".join(lines)


async def resolve_user_for_stats(chat_id: int, arg: str):
    raw = arg.strip()
    username = raw[1:] if raw.startswith("@") else None
    name = None if username else raw

    conn = get_connection()
    try:
        cursor = conn.cursor()
        if username:
            if db_type == "postgresql":
                cursor.execute(
                    "SELECT DISTINCT u.user_id, u.first_name FROM users u "
                    "JOIN rollcalls r ON u.rollcall_id = r.id "
                    "WHERE r.chat_id = %s AND u.username = %s ORDER BY u.updated_at DESC LIMIT 1",
                    (chat_id, username),
                )
            else:
                cursor.execute(
                    "SELECT DISTINCT u.user_id, u.first_name FROM users u "
                    "JOIN rollcalls r ON u.rollcall_id = r.id "
                    "WHERE r.chat_id = ? AND u.username = ? ORDER BY u.updated_at DESC LIMIT 1",
                    (chat_id, username),
                )
        else:
            if db_type == "postgresql":
                cursor.execute(
                    "SELECT DISTINCT u.user_id, u.first_name FROM users u "
                    "JOIN rollcalls r ON u.rollcall_id = r.id "
                    "WHERE r.chat_id = %s AND u.first_name = %s ORDER BY u.updated_at DESC LIMIT 1",
                    (chat_id, name),
                )
            else:
                cursor.execute(
                    "SELECT DISTINCT u.user_id, u.first_name FROM users u "
                    "JOIN rollcalls r ON u.rollcall_id = r.id "
                    "WHERE r.chat_id = ? AND u.first_name = ? ORDER BY u.updated_at DESC LIMIT 1",
                    (chat_id, name),
                )
        row = cursor.fetchone()
        if not row:
            return None
        user_id, first_name = (row["user_id"], row["first_name"]) if isinstance(row, dict) else (row[0], row[1])
        return user_id, first_name or arg
    finally:
        cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


async def build_user_stats_text(chat_id: int, user_id: int, first_name: str) -> str:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute(
                "SELECT total_in, total_out, total_maybe, total_waiting_to_in, "
                "total_rollcalls, current_streak, best_streak "
                "FROM user_stats WHERE chat_id = %s AND user_id = %s",
                (chat_id, user_id),
            )
        else:
            cursor.execute(
                "SELECT total_in, total_out, total_maybe, total_waiting_to_in, "
                "total_rollcalls, current_streak, best_streak "
                "FROM user_stats WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
        row = cursor.fetchone()
        if not row:
            return f"*Stats for {_esc(first_name)}:*\n\nNo data yet. Participate in a few rollcalls first!"

        data = row if isinstance(row, dict) else {c[0]: row[i] for i, c in enumerate(cursor.description)}

        t_in    = data.get("total_in", 0) or 0
        t_out   = data.get("total_out", 0) or 0
        t_maybe = data.get("total_maybe", 0) or 0
        t_wait  = data.get("total_waiting_to_in", 0) or 0
        voted_in_rcs = data.get("total_rollcalls", 0) or 0  # rollcalls user participated in (any bucket)
        streak  = data.get("current_streak", 0) or 0
        best    = data.get("best_streak", 0) or 0

        # Two distinct percentages, both with the SAME denominator (total
        # ended rollcalls in the chat). Voting% = engagement (did you bother
        # to vote?); Attendance% = actual attendance (did you end up IN at
        # /erc?). Both required because voting alone trivially hits 100% for
        # anyone who participated once if denominator is per-user.
        total_rcs = get_chat_ended_rollcall_count(chat_id)
        attended = get_user_attendance_count(chat_id, user_id)
        if total_rcs > 0:
            vote_pct = f"{round(voted_in_rcs / total_rcs * 100)}%"
            att_pct = f"{round(attended / total_rcs * 100)}%"
        else:
            vote_pct = att_pct = "—"

        return "\n".join([
            f"*Stats for {_esc(first_name)}:*", "",
            f"🗳 Voted in: {voted_in_rcs} of {total_rcs} rollcalls ({vote_pct})",
            f"✅ Attended: {attended} of {total_rcs} rollcalls ({att_pct})",
            f"📊 Vote breakdown — IN: {t_in}  OUT: {t_out}  MAYBE: {t_maybe}",
            f"⏫ Promoted from waitlist: {t_wait}", "",
            f"🔥 Current streak: {streak} session(s)",
            f"🏆 Best streak: {best} session(s)",
        ])
    finally:
        cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


async def build_group_stats_text(chat_id: int) -> str:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute(
                "SELECT SUM(total_in) AS sum_in, SUM(total_out) AS sum_out, "
                "SUM(total_maybe) AS sum_maybe, SUM(total_waiting_to_in) AS sum_wait "
                "FROM user_stats WHERE chat_id = %s", (chat_id,),
            )
        else:
            cursor.execute(
                "SELECT SUM(total_in) AS sum_in, SUM(total_out) AS sum_out, "
                "SUM(total_maybe) AS sum_maybe, SUM(total_waiting_to_in) AS sum_wait "
                "FROM user_stats WHERE chat_id = ?", (chat_id,),
            )
        row = cursor.fetchone()
        if not row:
            return "*Group stats:*\n\nNo data yet."
        data = row if isinstance(row, dict) else {c[0]: row[i] for i, c in enumerate(cursor.description)}
        return "\n".join([
            "*Group stats:*", "",
            f"✅ Total IN: {data.get('sum_in') or 0}",
            f"❌ Total OUT: {data.get('sum_out') or 0}",
            f"🤔 Total MAYBE: {data.get('sum_maybe') or 0}",
            f"⏫ Total WAITING → IN: {data.get('sum_wait') or 0}",
        ])
    finally:
        cursor.close()
        if db_type == "postgresql":
            release_connection(conn)
