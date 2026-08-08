"""
Periodic background jobs — weekly dues nudges, monthly digest cards, and
idle-group re-engagement.

Driven by the minute tick in check_reminders.check_template_schedules().
Every job's run-once stamp lives in system_config (persistent), so a bot
restart can never re-fire a DM or group post that already went out.

Firing rules (all in each chat's own timezone):
  weekly dues nudge   — Sunday >= 18:00, once per ISO week, opt-in via
                        /dues_nudges on (chats.dues_weekly_nudge)
  monthly digest      — 1st of month >= 09:00, once per month:
                        season wrap-up card (respects shh) + dues statement
                        (money post — always sends, per the ledger rule)
  idle re-engagement  — daily sweep; groups with templates but no rollcall
                        in 14 days get ONE admin DM, re-armed after 30 days
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytz

from bot_state import bot, _log_task_exc

IDLE_DAYS = 14
IDLE_RENUDGE_DAYS = 30


def _tz_for(chat_id: int):
    try:
        from rollcall_manager import manager
        return pytz.timezone(manager.get_chat(chat_id).get("timezone", "Asia/Kolkata"))
    except Exception:
        return pytz.timezone("Asia/Kolkata")


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def run_periodic_jobs() -> None:
    """One tick — cheap when nothing is due. Called every minute."""
    try:
        await _weekly_dues_nudges()
    except Exception:
        logging.exception("[periodic] weekly dues nudge sweep failed")
    try:
        await _weekly_dues_report()
    except Exception:
        logging.exception("[periodic] weekly dues report sweep failed")
    try:
        await _monthly_digests()
    except Exception:
        logging.exception("[periodic] monthly digest sweep failed")
    try:
        await _idle_reengagement()
    except Exception:
        logging.exception("[periodic] idle re-engagement sweep failed")


# ── Weekly dues nudges ────────────────────────────────────────────────────────

async def _weekly_dues_nudges() -> None:
    import db as _db

    for chat_id in _db.get_all_chat_ids_with_dues():
        tz = _tz_for(chat_id)
        now = datetime.now(tz)
        if now.weekday() != 6 or now.hour < 18:  # Sunday evening
            continue
        week = now.strftime("%G-W%V")
        stamp_key = f"dues_nudge:{chat_id}"
        if _db.get_system_config(stamp_key) == week:
            continue
        _db.set_system_config(stamp_key, week)

        try:
            from services import dues as dues_svc
            result = dues_svc.remind_dues(chat_id)
            if not result.get("dm_targets") and not result.get("no_dm"):
                continue  # everyone settled — stay silent this week
            # Same shape as manual /remind_dues: group summary (money post,
            # always sends) + individual DMs.
            from bot_state import send_md_fallback
            await send_md_fallback(chat_id, "🗓 Weekly dues reminder\n\n" + result["announcement"])
            from handlers.dues import _send_dues_dms
            asyncio.create_task(_send_dues_dms(chat_id, result)).add_done_callback(_log_task_exc)
            logging.info(f"[periodic] weekly dues nudge sent for chat {chat_id}")
        except Exception:
            logging.exception(f"[periodic] weekly dues nudge failed for chat {chat_id}")


# ── Weekly dues report ────────────────────────────────────────────────────────

async def _weekly_dues_report() -> None:
    import db as _db

    for chat_id in _db.get_all_chat_ids_with_dues_report():
        tz = _tz_for(chat_id)
        now = datetime.now(tz)
        if now.weekday() != 6 or now.hour < 20:  # Sunday >= 20:00 local
            continue
        week = now.strftime("%G-W%V")
        stamp_key = f"dues_report:{chat_id}"
        if _db.get_system_config(stamp_key) == week:
            continue
        _db.set_system_config(stamp_key, week)

        try:
            from services import dues as dues_svc
            from bot_state import send_md_fallback
            snapshot = dues_svc.dues_snapshot(chat_id)
            text = "📋 *Weekly dues snapshot*\n\n" + snapshot["text"]
            await send_md_fallback(chat_id, text)
            logging.info(f"[periodic] weekly dues report sent for chat {chat_id}")
        except Exception:
            logging.exception(f"[periodic] weekly dues report failed for chat {chat_id}")


# ── Monthly digests ───────────────────────────────────────────────────────────

def _prev_month_window(now_local: datetime):
    """Return (start_utc_str, end_utc_str, label) for the previous calendar month."""
    first_this = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    label = first_prev.strftime("%B %Y")
    return _utc_str(first_prev), _utc_str(first_this), label


async def _monthly_digests() -> None:
    import db as _db

    since = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d %H:%M:%S")
    for chat_id in _db.get_active_group_chat_ids(since):
        tz = _tz_for(chat_id)
        now = datetime.now(tz)
        if now.day != 1 or now.hour < 9:
            continue
        month = now.strftime("%Y-%m")
        stamp_key = f"monthly_digest:{chat_id}"
        if _db.get_system_config(stamp_key) == month:
            continue
        _db.set_system_config(stamp_key, month)

        start_s, end_s, label = _prev_month_window(now)

        # Season wrap-up card — celebratory, respects shh mode.
        try:
            from rollcall_manager import manager
            if not manager.get_shh_mode(chat_id):
                sessions = _db.get_rollcalls_between(chat_id, start_s, end_s)
                if len(sessions) >= 2:
                    attendance = _db.get_attendance_between(chat_id, start_s, end_s)
                    total = sum(int(s.get("in_count") or 0) for s in sessions)
                    avg = round(total / len(sessions), 1)
                    chat_row = _db.get_or_create_chat(chat_id)
                    from utils.card_gen import month_wrapup_card
                    buf = month_wrapup_card(
                        chat_row.get("group_name") or "Our Group",
                        label, len(sessions), avg,
                        [{"name": a["name"], "attended": a["attended"]} for a in attendance[:5]],
                    )
                    await bot.send_photo(
                        chat_id, buf,
                        caption=f"🏆 {label} wrap-up — {len(sessions)} games, "
                                f"{avg} players/game. Share it!",
                    )
        except Exception:
            logging.exception(f"[periodic] wrap-up card failed for chat {chat_id}")

        # Dues statement — money post, always sends (ledger durability rule).
        try:
            chat_row = _db.get_or_create_chat(chat_id)
            if chat_row.get("dues_enabled"):
                txns = _db.get_fund_transactions_between(chat_id, start_s, end_s)
                if txns:
                    inflow = sum(t["amount"] for t in txns if t["amount"] > 0)
                    outflow = sum(-t["amount"] for t in txns if t["amount"] < 0)
                    balance = _db.get_fund_balance(chat_id)
                    await bot.send_message(
                        chat_id,
                        f"🏦 Treasury statement — {label}\n\n"
                        f"⬆️ In: ₹{inflow}\n"
                        f"⬇️ Out: ₹{outflow}\n"
                        f"💰 Fund balance: ₹{balance}\n\n"
                        f"Full history: /fund_history",
                    )
        except Exception:
            logging.exception(f"[periodic] dues statement failed for chat {chat_id}")


# ── Idle-group re-engagement ──────────────────────────────────────────────────

async def _idle_reengagement() -> None:
    import db as _db

    # Once per day (IST anchor) for the whole sweep.
    today = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    if _db.get_system_config("idle_check_last") == today:
        return
    _db.set_system_config("idle_check_last", today)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=IDLE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Bot-wide, unscoped-by-design join — offloaded off the event loop on
    # SQLite (the prod path) so this once-a-day scheduler sweep doesn't
    # stall Telegram polling/REST for its whole duration. Postgres pool
    # thread-safety wasn't verified, so it stays on the direct call.
    if _db.db_type == "sqlite":
        idle_chats = await asyncio.get_running_loop().run_in_executor(
            _db._stats_executor, _db.get_idle_chats, cutoff
        )
    else:
        idle_chats = _db.get_idle_chats(cutoff)

    for row in idle_chats:
        chat_id = row["chat_id"]
        try:
            last_nudge = row.get("last_idle_nudge")
            if last_nudge:
                try:
                    nudged_at = datetime.fromisoformat(str(last_nudge).replace("Z", "+00:00"))
                    if nudged_at.tzinfo is None:
                        nudged_at = nudged_at.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - nudged_at < timedelta(days=IDLE_RENUDGE_DAYS):
                        continue
                except (ValueError, TypeError):
                    pass

            actor = _db.get_last_admin_actor(chat_id)
            if not actor or not actor.get("admin_id"):
                continue
            templates = _db.get_templates(chat_id)
            if not templates:
                continue
            tmpl_name = templates[0].get("name") or "template"
            group_name = row.get("group_name") or "your group"

            from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton(
                f"▶️ Start '{tmpl_name}' now",
                callback_data=f"idle_start_{chat_id}_{actor['admin_id']}",
            ))

            # Stamp BEFORE the DM attempt: an unreachable admin (never started
            # the bot) must not cause a failed-DM retry every single day.
            _db.update_chat_settings(chat_id, last_idle_nudge=now_iso)

            await bot.send_message(
                actor["admin_id"],
                f"👋 {group_name} hasn't had a game in {IDLE_DAYS}+ days.\n"
                f"Want to get one going? One tap starts a rollcall from your "
                f"'{tmpl_name}' template.",
                reply_markup=markup,
            )
            logging.info(f"[periodic] idle nudge DM sent for chat {chat_id}")
        except Exception:
            logging.exception(f"[periodic] idle nudge failed for chat {chat_id}")
