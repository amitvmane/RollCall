"""
/stats command and all stats-building helpers.

This module reports REAL attendance (final-IN status in ended rollcalls),
not vote counts. The Telegram bot's vote handlers increment
user_stats.total_in / total_out / total_maybe per vote — so a user who
flips IN→OUT→IN within a single session has total_in=2 even though they
attended at most once. Those counters are still displayed under "vote
breakdown" because they're useful as an engagement-flux indicator, but
the headline metrics (Voting%, Attendance%, leaderboard rank) are all
derived from the per-rollcall final-status table (`users` for real users,
`proxy_users` for proxies added via /sif /sof /smf).
"""
import logging

from bot_state import bot
from config import ADMINS
from db import (
    get_ghost_leaderboard, get_connection, db_type, release_connection,
    get_user_attendance_count, get_leaderboard_by_attendance,
    get_chat_ended_rollcall_count,
    get_proxy_attendance_count, get_proxy_stats,
    get_group_attendance_totals, get_bot_attendance_totals,
    find_proxy_in_chat, get_proxy_streaks,
)
from rollcall_manager import manager


def _esc(text: str) -> str:
    """Escape Markdown v1 special characters in user-supplied strings."""
    if not text:
        return text or ""
    for c in ('_', '*', '`', '['):
        text = text.replace(c, f'\\{c}')
    return text


def _pct(num: int, denom: int) -> str:
    return f"{round(num / denom * 100)}%" if denom > 0 else "—"


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] in ["/stats", "/s"])
async def stats_command(message):
    cid = message.chat.id
    text = message.text.strip()
    parts = text.split()

    target_user_id = message.from_user.id
    display_name = message.from_user.first_name or "User"
    target_proxy_name = None
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
                await bot.send_message(cid, f"Could not find user '{arg}' in ended rollcalls for this chat.")
                return
            kind = resolved[0]
            if kind == "ambiguous":
                count = resolved[1]
                await bot.send_message(
                    cid,
                    f"Found {count} users with the name '{arg}' in this chat. "
                    f"Please use the exact Telegram @username to disambiguate.",
                )
                return
            elif kind == "proxy":
                target_proxy_name = resolved[1]
                display_name = resolved[2]
                scope = "proxy"
            else:  # 'real'
                target_user_id = resolved[1]
                display_name = resolved[2]
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
        elif scope == "proxy":
            text = await build_proxy_stats_text(cid, target_proxy_name, display_name)
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
        lines.append(f"{i}. {_esc(name)} — {count} session(s) ghosted{warning}")

    lines.append("")
    lines.append(f"Current ghost limit: {limit} session(s)")
    if not tracking_on:
        lines.append("_(Ghost tracking is currently disabled for this group)_")

    return "\n".join(lines)


async def build_bot_stats_text() -> str:
    """Bot-wide stats. Shows real attendance (sum of final-IN slots across
    ALL groups) alongside the legacy vote-count totals — the latter are
    still useful as a global engagement-flux indicator."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

        def _fetch_one(pg_sql, sqlite_sql, params=()):
            if db_type == "postgresql":
                cursor.execute(pg_sql, params)
            else:
                cursor.execute(sqlite_sql, params)
            row = cursor.fetchone()
            if row is None:
                return 0
            return int((row[0] if not isinstance(row, dict) else next(iter(row.values()))) or 0)

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
        total_real_users = _fetch_one("SELECT COUNT(DISTINCT user_id) FROM users", "SELECT COUNT(DISTINCT user_id) FROM users")
        total_proxy_users = _fetch_one("SELECT COUNT(DISTINCT name) FROM proxy_users", "SELECT COUNT(DISTINCT name) FROM proxy_users")
        total_templates = _fetch_one("SELECT COUNT(*) FROM templates", "SELECT COUNT(*) FROM templates")

        # Legacy vote-count totals — kept for engagement signal, clearly
        # labelled as votes (not attendance).
        cursor.execute("SELECT SUM(total_in), SUM(total_out), SUM(total_maybe) FROM user_stats")
        row = cursor.fetchone()
        if row is None:
            sum_in = sum_out = sum_maybe = 0
        elif isinstance(row, dict):
            vals = list(row.values())
            sum_in    = int(vals[0] or 0)
            sum_out   = int(vals[1] or 0)
            sum_maybe = int(vals[2] or 0)
        else:
            sum_in    = int(row[0] or 0)
            sum_out   = int(row[1] or 0)
            sum_maybe = int(row[2] or 0)
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)

    totals = get_bot_attendance_totals()
    ended_rcs           = totals['ended_rollcalls']
    real_att_slots      = totals['real_attendance_slots']
    proxy_att_slots     = totals['proxy_attendance_slots']
    real_attended_users = totals['real_participants']
    proxy_attended      = totals['proxy_participants']
    total_att_slots     = real_att_slots + proxy_att_slots
    avg_attendance      = (total_att_slots / ended_rcs) if ended_rcs > 0 else 0.0

    return "\n".join([
        "*🤖 Bot-Wide Statistics*", "",
        "*Groups:*",
        f"🏘️ Total: {total_groups}",
        f"✅ Active (7d): {active_groups_7d}",
        f"✅ Active (30d): {active_groups_30d}", "",
        "*Rollcalls:*",
        f"📋 Total: {total_rollcalls}",
        f"✓ Ended: {ended_rcs}",
        f"📈 Last 30d: {rollcalls_30d}", "",
        "*Attendance (final-IN at /erc):*",
        f"✅ Total IN slots: {total_att_slots}  ({real_att_slots} member + {proxy_att_slots} proxy)",
        f"📊 Avg per ended rollcall: {avg_attendance:.1f}", "",
        "*Members:*",
        f"👥 Distinct real users: {total_real_users}",
        f"👤 Distinct proxy names: {total_proxy_users}", "",
        "*Vote breakdown (real-user votes cast, may exceed attendance if users flipped):*",
        f"🗳 IN: {sum_in}  OUT: {sum_out}  MAYBE: {sum_maybe}", "",
        f"📝 Templates: {total_templates}",
    ])


async def build_leaderboard_text(chat_id: int, limit: int = 10) -> str:
    """Top-N by REAL attendance (final-IN in ended rollcalls), including
    proxies. Tiebreak by total_rollcalls ASC so a participant who attended
    the same number of sessions but participated in fewer (higher %) ranks
    first. Proxies are marked with (via /sif)."""
    rows = get_leaderboard_by_attendance(chat_id, limit)
    if not rows:
        return "*Leaderboard:*\n\nNo data yet. Participate in some rollcalls first!"

    total_rcs = get_chat_ended_rollcall_count(chat_id)

    lines = [f"*Leaderboard (top {len(rows)} by attendance):*", ""]
    for rank, row in enumerate(rows, 1):
        attended = row['attended']
        voted = row['total_rollcalls']
        if row['kind'] == 'proxy':
            name_text = f"{_esc(row['display_name'])} (via /sif)"
        elif row.get('username'):
            name_text = f"@{_esc(row['username'])}"
        else:
            name_text = _esc(row['display_name'] or 'User')
        if total_rcs > 0:
            att_pct = _pct(attended, total_rcs)
            vote_pct = _pct(voted, total_rcs)
            lines.append(
                f"{rank}. {name_text} — ✅ {attended}/{total_rcs} ({att_pct})  ·  🗳 {voted}/{total_rcs} ({vote_pct})"
            )
        else:
            lines.append(f"{rank}. {name_text} — ✅ {attended}  ·  🗳 {voted}")

    return "\n".join(lines)


async def resolve_user_for_stats(chat_id: int, arg: str):
    """Look up a stats target by @username or by display name. Returns:
        ('real',      user_id,    display_name)  — real Telegram user
        ('proxy',     proxy_name, proxy_name)    — proxy added via /sif
        ('ambiguous', count,      arg)           — multiple matches, ask @username
        None                                     — no match

    All queries are restricted to ENDED rollcalls (`r.is_active = FALSE`)
    so in-progress sessions don't shadow real history.
    """
    raw = arg.strip()
    username = raw[1:] if raw.startswith("@") else None
    name = None if username else raw

    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        active_false = 'FALSE' if db_type == 'postgresql' else '0'

        if username:
            # Telegram @usernames are unique — no ambiguity possible.
            cursor.execute(f"""
                SELECT DISTINCT u.user_id, u.first_name FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND u.username = {ph} AND r.is_active = {active_false}
                ORDER BY u.user_id
                LIMIT 1
            """, (chat_id, username))
            row = cursor.fetchone()
            if row is not None:
                if isinstance(row, dict):
                    return ('real', row['user_id'], row.get('first_name') or arg)
                return ('real', row[0], row[1] or arg)
            return None

        # First-name path: detect ambiguity before picking.
        cursor.execute(f"""
            SELECT u.user_id, MAX(u.first_name) AS first_name, MAX(u.updated_at) AS latest_seen
            FROM users u
            JOIN rollcalls r ON u.rollcall_id = r.id
            WHERE r.chat_id = {ph} AND u.first_name = {ph} AND r.is_active = {active_false}
            GROUP BY u.user_id
            ORDER BY latest_seen DESC
        """, (chat_id, name))
        rows = cursor.fetchall()
        if rows:
            if len(rows) > 1:
                return ('ambiguous', len(rows), name)
            r = rows[0]
            if isinstance(r, dict):
                return ('real', r['user_id'], r.get('first_name') or arg)
            return ('real', r[0], r[1] or arg)

        # No real-user match — fall through to proxy lookup.
        if find_proxy_in_chat(chat_id, name):
            return ('proxy', name, name)

        return None
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


async def build_user_stats_text(chat_id: int, user_id: int, first_name: str) -> str:
    conn = get_connection()
    cursor = None
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)

    t_in    = int(data.get("total_in", 0) or 0)
    t_out   = int(data.get("total_out", 0) or 0)
    t_maybe = int(data.get("total_maybe", 0) or 0)
    t_wait  = int(data.get("total_waiting_to_in", 0) or 0)
    voted_in_rcs = int(data.get("total_rollcalls", 0) or 0)
    streak  = int(data.get("current_streak", 0) or 0)
    best    = int(data.get("best_streak", 0) or 0)

    total_rcs = get_chat_ended_rollcall_count(chat_id)
    attended = get_user_attendance_count(chat_id, user_id)

    return "\n".join([
        f"*Stats for {_esc(first_name)}:*", "",
        f"🗳 Voted in: {voted_in_rcs} of {total_rcs} rollcalls ({_pct(voted_in_rcs, total_rcs)})",
        f"✅ Attended: {attended} of {total_rcs} rollcalls ({_pct(attended, total_rcs)})",
        f"📊 Vote breakdown — IN: {t_in}  OUT: {t_out}  MAYBE: {t_maybe}",
        f"⏫ Promoted from waitlist: {t_wait}", "",
        f"🔥 Current streak: {streak} session(s)",
        f"🏆 Best streak: {best} session(s)",
    ])


async def build_proxy_stats_text(chat_id: int, proxy_name: str, display_name: str) -> str:
    """Per-proxy stats including streaks. Streaks live in the parallel
    proxy_stats table (keyed on chat_id + proxy_name) — added so proxies
    are first-class citizens for streak tracking alongside real users."""
    stats = get_proxy_stats(chat_id, proxy_name)
    voted = stats['total_rollcalls']
    attended = stats['attended']
    total_rcs = get_chat_ended_rollcall_count(chat_id)

    if voted == 0:
        return f"*Stats for {_esc(display_name)} (via /sif):*\n\nNo data yet for this proxy."

    streaks = get_proxy_streaks(chat_id, proxy_name)
    return "\n".join([
        f"*Stats for {_esc(display_name)} (via /sif):*", "",
        f"🗳 Voted in: {voted} of {total_rcs} rollcalls ({_pct(voted, total_rcs)})",
        f"✅ Attended: {attended} of {total_rcs} rollcalls ({_pct(attended, total_rcs)})",
        f"📊 Per-session breakdown — IN: {stats['total_in']}  OUT: {stats['total_out']}  MAYBE: {stats['total_maybe']}",
        "",
        f"🔥 Current streak: {streaks['current_streak']} session(s)",
        f"🏆 Best streak: {streaks['best_streak']} session(s)",
    ])


async def build_group_stats_text(chat_id: int) -> str:
    """Group stats. Sums REAL attendance (final-IN slots) across both real
    users and proxies. Vote counts are shown as a secondary detail with
    explicit labelling so they're not confused with attendance.
    """
    totals = get_group_attendance_totals(chat_id)
    if totals['total_rollcalls'] == 0:
        return "*Group stats:*\n\nNo ended rollcalls yet."

    total_rcs        = totals['total_rollcalls']
    real_slots       = totals['real_attendance_slots']
    proxy_slots      = totals['proxy_attendance_slots']
    total_att_slots  = real_slots + proxy_slots
    real_pax         = totals['real_participants']
    proxy_pax        = totals['proxy_participants']
    avg_per_rc       = total_att_slots / total_rcs if total_rcs > 0 else 0.0

    lines = [
        "*Group stats:*", "",
        f"📋 Ended rollcalls: {total_rcs}",
        f"✅ Total attendance (IN slots): {total_att_slots}",
        f"   • Members: {real_slots}    • Proxies: {proxy_slots}",
        f"📊 Average attendance per rollcall: {avg_per_rc:.1f}",
        f"👥 Distinct attendees: {real_pax + proxy_pax}",
        f"   • Members: {real_pax}    • Proxies: {proxy_pax}",
        "",
        "*Vote activity*",
        f"🗳 Real-user votes — IN: {totals['real_vote_in']}  OUT: {totals['real_vote_out']}  MAYBE: {totals['real_vote_maybe']}",
        f"👤 Proxy sessions  — IN: {totals['proxy_in']}  OUT: {totals['proxy_out']}  MAYBE: {totals['proxy_maybe']}",
        f"⏫ Waitlist → IN promotions: {totals['waitlist_promotions']}",
    ]
    if proxy_pax == 0:
        lines.append("")
        lines.append("_(No proxy members tracked in this chat yet.)_")
    return "\n".join(lines)
