"""
/stats command handler — formats stats data from services.stats and services.ghost.

All data queries go through services/stats.py and services/ghost.py.
This module is responsible only for Telegram Markdown formatting.
"""
import logging

from bot_state import bot
from config import ADMINS
from rollcall_manager import manager
from services import ghost as ghost_svc
from services import stats as stats_svc


def _esc(text: str) -> str:
    if not text:
        return text or ""
    for c in ("_", "*", "`", "["):
        text = text.replace(c, f"\\{c}")
    return text


def _pct_str(rate) -> str:
    if rate is None:
        return "—"
    return f"{int(rate)}%" if rate == int(rate) else f"{rate}%"


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
            resolved = stats_svc.resolve_user(cid, arg)
            if resolved is None:
                await bot.send_message(cid, f"Could not find user '{arg}' in ended rollcalls for this chat.")
                return
            kind = resolved[0]
            if kind == "ambiguous":
                count = resolved[1]
                await bot.send_message(
                    cid,
                    f"Found {count} users with the name '{arg}' in this chat. "
                    "Please use the exact Telegram @username to disambiguate.",
                )
                return
            elif kind == "proxy":
                target_proxy_name = resolved[1]
                display_name = resolved[2]
                scope = "proxy"
            else:
                target_user_id = resolved[1]
                display_name = resolved[2]
                scope = "other"

    try:
        if scope == "group":
            out = _fmt_group_stats(stats_svc.group_stats(cid))
        elif scope == "top":
            out = _fmt_leaderboard(stats_svc.leaderboard(cid))
        elif scope == "ghost":
            out = _fmt_ghost_stats(cid)
        elif scope == "bot":
            out = _fmt_bot_stats(stats_svc.bot_stats())
        elif scope == "proxy":
            out = _fmt_proxy_stats(stats_svc.proxy_stats(cid, target_proxy_name), display_name)
        else:
            out = _fmt_personal_stats(stats_svc.personal_stats(cid, target_user_id), display_name)

        await bot.send_message(cid, out, parse_mode="Markdown")
    except Exception:
        logging.exception("Error in /stats")
        await bot.send_message(cid, "Error while fetching stats, please try again later.")


# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt_personal_stats(data: dict, first_name: str) -> str:
    if data["total_sessions_voted"] == 0 and data["sessions_attended"] == 0:
        return f"*Stats for {_esc(first_name)}:*\n\nNo data yet. Participate in a few rollcalls first!"
    ghost_count = data["ghost_count"]
    absent_limit = data["absent_limit"]
    ghost_warn = " ⚠️ (next /in asks for reconfirmation)" if ghost_count >= absent_limit and ghost_count > 0 else ""
    total_rcs = data["total_rollcalls_in_chat"]
    voted = data["total_sessions_voted"]
    attended = data["sessions_attended"]
    return "\n".join([
        f"*Stats for {_esc(first_name)}:*", "",
        f"🗳 Voted in: {voted} of {total_rcs} rollcalls ({_pct_str(data['voting_rate'])})",
        f"✅ Attended: {attended} of {total_rcs} rollcalls ({_pct_str(data['attendance_rate'])})",
        f"👻 Ghosted: {ghost_count} session(s){ghost_warn}",
        f"📊 Vote breakdown — IN: {data['total_in_votes']}  OUT: {data['total_out_votes']}  MAYBE: {data['total_maybe_votes']}",
        f"⏫ Promoted from waitlist: {data['total_waiting_to_in']}", "",
        f"🔥 Current streak: {data['current_streak']} session(s)",
        f"🏆 Best streak: {data['best_streak']} session(s)",
    ])


def _fmt_proxy_stats(data: dict, display_name: str) -> str:
    voted = data["total_sessions_voted"]
    if voted == 0:
        return f"*Stats for {_esc(display_name)} (via /sif):*\n\nNo data yet for this proxy."
    ghost_count = data["ghost_count"]
    absent_limit = data["absent_limit"]
    ghost_warn = " ⚠️ (next /sif asks for reconfirmation)" if ghost_count >= absent_limit and ghost_count > 0 else ""
    total_rcs = data["total_rollcalls_in_chat"]
    attended = data["sessions_attended"]
    return "\n".join([
        f"*Stats for {_esc(display_name)} (via /sif):*", "",
        f"🗳 Voted in: {voted} of {total_rcs} rollcalls ({_pct_str(data['voting_rate'])})",
        f"✅ Attended: {attended} of {total_rcs} rollcalls ({_pct_str(data['attendance_rate'])})",
        f"👻 Ghosted: {ghost_count} session(s){ghost_warn}",
        f"📊 Per-session breakdown — IN: {data['total_in_votes']}  OUT: {data['total_out_votes']}  MAYBE: {data['total_maybe_votes']}",
        "",
        f"🔥 Current streak: {data['current_streak']} session(s)",
        f"🏆 Best streak: {data['best_streak']} session(s)",
    ])


def _fmt_group_stats(data: dict) -> str:
    if data["total_rollcalls"] == 0:
        return "*Group stats:*\n\nNo ended rollcalls yet."
    total_rcs = data["total_rollcalls"]
    real_slots = data["real_attendance_slots"]
    proxy_slots = data["proxy_attendance_slots"]
    total_slots = data["total_attendance_slots"]
    real_pax = data["real_participants"]
    proxy_pax = data["proxy_participants"]
    lines = [
        "*Group stats:*", "",
        f"📋 Ended rollcalls: {total_rcs}",
        f"✅ Total attendance (IN slots): {total_slots}",
        f"   • Members: {real_slots}    • Proxies: {proxy_slots}",
        f"📊 Average attendance per rollcall: {data['avg_attendance']}",
        f"👥 Distinct attendees: {real_pax + proxy_pax}",
        f"   • Members: {real_pax}    • Proxies: {proxy_pax}",
        "",
        "*Vote activity*",
        f"🗳 Real-user votes — IN: {data['real_vote_in']}  OUT: {data['real_vote_out']}  MAYBE: {data['real_vote_maybe']}",
        f"👤 Proxy sessions  — IN: {data['proxy_in']}  OUT: {data['proxy_out']}  MAYBE: {data['proxy_maybe']}",
        f"⏫ Waitlist → IN promotions: {data['waitlist_promotions']}",
    ]
    if proxy_pax == 0:
        lines += ["", "_(No proxy members tracked in this chat yet.)_"]
    return "\n".join(lines)


def _fmt_leaderboard(data: dict) -> str:
    entries = data["entries"]
    total_rcs = data["total_rollcalls_in_chat"]
    if not entries:
        return "*Leaderboard:*\n\nNo data yet. Participate in some rollcalls first!"
    lines = [f"*Leaderboard (top {len(entries)} by attendance):*", ""]
    for row in entries:
        attended = row["sessions_attended"]
        voted = row["total_sessions_voted"]
        if row["kind"] == "proxy":
            name_text = f"{_esc(row['display_name'])} (via /sif)"
        elif row.get("username"):
            name_text = f"@{_esc(row['username'])}"
        else:
            name_text = _esc(row["display_name"] or "User")
        if total_rcs > 0:
            lines.append(
                f"{row['rank']}. {name_text} — ✅ {attended}/{total_rcs} ({_pct_str(row['attendance_rate'])})  ·  🗳 {voted}/{total_rcs} ({_pct_str(row['voting_rate'])})"
            )
        else:
            lines.append(f"{row['rank']}. {name_text} — ✅ {attended}  ·  🗳 {voted}")
    return "\n".join(lines)


def _fmt_ghost_stats(cid: int) -> str:
    leaderboard = ghost_svc.ghost_leaderboard(cid)
    limit = manager.get_absent_limit(cid)
    tracking_on = manager.get_ghost_tracking_enabled(cid)
    if not leaderboard:
        return "🏆 No ghosts yet — everyone's been showing up!"
    lines = ["👻 *Ghost Leaderboard*", "─────────────────"]
    for i, entry in enumerate(leaderboard, 1):
        name = f"{entry['name']} (via /sif)" if entry["is_proxy"] else entry["name"]
        count = entry["ghost_count"]
        warning = " ⚠️" if count >= limit else ""
        lines.append(f"{i}. {_esc(name)} — {count} session(s) ghosted{warning}")
    lines += ["", f"Current ghost limit: {limit} session(s)"]
    if not tracking_on:
        lines.append("_(Ghost tracking is currently disabled for this group)_")
    return "\n".join(lines)


# ── Public shims (used by integration tests and external callers) ─────────────

async def build_user_stats_text(chat_id: int, user_id: int, first_name: str) -> str:
    return _fmt_personal_stats(stats_svc.personal_stats(chat_id, user_id), first_name)


async def build_proxy_stats_text(chat_id: int, proxy_name: str, display_name: str) -> str:
    return _fmt_proxy_stats(stats_svc.proxy_stats(chat_id, proxy_name), display_name)


async def build_group_stats_text(chat_id: int) -> str:
    return _fmt_group_stats(stats_svc.group_stats(chat_id))


async def build_leaderboard_text(chat_id: int) -> str:
    return _fmt_leaderboard(stats_svc.leaderboard(chat_id))


async def build_ghost_stats_text(chat_id: int, _mgr=None) -> str:
    return _fmt_ghost_stats(chat_id)


async def resolve_user_for_stats(chat_id: int, arg: str):
    return stats_svc.resolve_user(chat_id, arg)


def _fmt_bot_stats(data: dict) -> str:
    return "\n".join([
        "*🤖 Bot-Wide Statistics*", "",
        "*Groups:*",
        f"🏘️ Total: {data['total_groups']}",
        f"✅ Active (7d): {data['active_groups_7d']}",
        f"✅ Active (30d): {data['active_groups_30d']}", "",
        "*Rollcalls:*",
        f"📋 Total: {data['total_rollcalls']}",
        f"✓ Ended: {data['ended_rollcalls']}",
        f"📈 Last 30d: {data['rollcalls_30d']}", "",
        "*Attendance (final-IN at /erc):*",
        f"✅ Total IN slots: {data['total_attendance_slots']}  ({data['real_attendance_slots']} member + {data['proxy_attendance_slots']} proxy)",
        f"📊 Avg per ended rollcall: {data['avg_attendance_per_rollcall']}", "",
        "*Members:*",
        f"👥 Distinct real users: {data['total_real_users']}",
        f"👤 Distinct proxy names: {data['total_proxy_users']}", "",
        "*Vote breakdown (real-user votes cast, may exceed attendance if users flipped):*",
        f"🗳 IN: {data['sum_in_votes']}  OUT: {data['sum_out_votes']}  MAYBE: {data['sum_maybe_votes']}", "",
        f"📝 Templates: {data['total_templates']}",
    ])
