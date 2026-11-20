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
  ghost auto-forgive  — daily sweep; sessions nobody ever marked up get
                        treated as "everyone attended" after a grace window
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import pytz

from bot_state import bot, _log_task_exc

IDLE_DAYS = 14
IDLE_RENUDGE_DAYS = 30

# Ghost auto-forgive: how long an ended session may sit unmarked before we
# treat it as "everyone who was IN showed up". 0 disables the sweep.
#
# Why this exists: ghost counts are only ever INCREMENTED by an admin
# explicitly marking no-shows, but they are only ever DECREMENTED by that
# same admin answering the post-/erc "any ghosts?" prompt. Punishment is
# therefore automatic while forgiveness needs a button tap after every single
# game — an asymmetry that guarantees drift. Someone who ghosted once and has
# attended ever since stays flagged forever, and gets a reconfirmation prompt
# on every /in, purely because nobody tapped the prompt.
#
# This sweep can only ever forgive: a session that was never marked never
# added a ghost to anyone, so processing it as "all attended" strictly
# decrements. There is no input that makes it punish someone.
GHOST_AUTOFORGIVE_DAYS = int(os.environ.get("GHOST_AUTOFORGIVE_DAYS", "7"))

# Outer bound for the lookback, matching the /mark_absent panel's own window —
# keeps a first run after upgrade from churning through ancient history.
_AUTOFORGIVE_MAX_LOOKBACK_DAYS = 30


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
    try:
        await _ghost_auto_forgive()
    except Exception:
        logging.exception("[periodic] ghost auto-forgive sweep failed")
    try:
        await _vote_digests()
    except Exception:
        logging.exception("[periodic] vote digest sweep failed")
    try:
        await asyncio.wait_for(_weather_alerts(), timeout=_WEATHER_TICK_BUDGET)
    except asyncio.TimeoutError:
        logging.warning("[periodic] weather alert lookup took over %ss — skipped this tick",
                        _WEATHER_TICK_BUDGET)
    except Exception:
        logging.exception("[periodic] weather alert sweep failed")
    try:
        _prune_expired_config()
    except Exception:
        logging.exception("[periodic] system_config prune failed")


# ── Bundled vote notices ──────────────────────────────────────────────────────

async def _vote_digests() -> None:
    """Post each bundled group's summary when its interval is up — and only
    if something on the list moved. services.vote_digest decides; this
    only delivers, outside any chat lock (the send may be paced)."""
    import db as _db
    from services.vote_digest import due_digest

    for chat_id in _db.get_chat_ids_with_vote_digest():
        try:
            text = due_digest(chat_id)
            if text:
                await bot.send_message(chat_id, text)
        except Exception:
            logging.exception("[periodic] vote digest failed for chat %s", chat_id)


# ── Weather alerts ────────────────────────────────────────────────────────────

# Two looks per game: the evening-before window and a final check shortly
# before. The early one posts only when rain is expected; the late one also
# posts "now looks clear" if the early one had warned — otherwise a group
# warned the night before would never hear the all-clear.
_WEATHER_EARLY_HOURS = 18
_WEATHER_LATE_HOURS = 3

# This runs in the minute tick that also fires scheduled rollcalls, and a
# forecast lookup is outbound HTTP. So each tick looks up at most ONE game
# (any other game due is picked up on the next tick, a minute later), and
# the whole step is capped — a hanging weather service costs one tick at
# most this long, never the schedule. The game is stamped before the lookup,
# so a capped lookup is not retried every minute.
_WEATHER_TICK_BUDGET = 20


async def _weather_alerts() -> None:
    import db as _db
    from services import weather as weather_svc
    from rollcall_manager import manager

    now = datetime.now(timezone.utc)
    for chat_id in _db.get_chat_ids_with_weather_alerts():
        for rc in manager.get_rollcalls(chat_id):
            try:
                when = weather_svc.game_time(rc)
                if when is None or not rc.location or rc.id is None:
                    continue
                hours_left = (when - now).total_seconds() / 3600
                if hours_left <= 0:
                    continue
                if hours_left <= _WEATHER_LATE_HOURS:
                    stage = "late"
                elif hours_left <= _WEATHER_EARLY_HOURS:
                    stage = "early"
                else:
                    continue
                stamp = f"weather_{stage}:{rc.id}"
                if _db.get_system_config(stamp):
                    continue
                # Stamp first: a forecast service that keeps failing must not
                # be retried (and logged) every minute until the game.
                _db.set_system_config(stamp, now.isoformat(timespec="seconds"))
                result = await weather_svc.rollcall_weather(rc)
                if result["ok"]:
                    weather_svc.remember(rc.id, result["text"])
                    warned_early = bool(_db.get_system_config(f"weather_warned:{rc.id}"))
                    rainy = result["result"]["rainy"] > 0
                    if rainy or (stage == "late" and warned_early):
                        if rainy:
                            _db.set_system_config(f"weather_warned:{rc.id}", "1")
                        await bot.send_message(chat_id, "🌦 " + result["text"])
            except Exception:
                logging.exception("[periodic] weather alert failed for chat %s rollcall %s",
                                  chat_id, getattr(rc, "id", None))
            else:
                return  # reached only after a lookup: one per tick (_WEATHER_TICK_BUDGET)


# ── system_config housekeeping ────────────────────────────────────────────────

# Per-rollcall weather stamps/forecasts and per-place geocoder results are
# written to system_config and nothing else ever removes them — one set per
# game, for the life of the deployment. All are useless well before this
# age: forecasts only cover games ≤6 days out and the geocoder cache's own
# TTL is 7 days. (Run-once stamps for jobs are keyed per chat, not per game,
# so they are bounded and stay.)
_EXPIRING_CONFIG_PREFIXES = ("weather_early:", "weather_late:", "weather_warned:",
                             "weather_last:", "geo:")
_EXPIRING_CONFIG_DAYS = 8


def _prune_expired_config() -> None:
    import db as _db
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _db.get_system_config("config_prune_last") == today:
        return
    _db.set_system_config("config_prune_last", today)
    cutoff = _utc_str(datetime.now(timezone.utc) - timedelta(days=_EXPIRING_CONFIG_DAYS))
    removed = _db.delete_system_config_before(_EXPIRING_CONFIG_PREFIXES, cutoff)
    if removed:
        logging.info("[periodic] pruned %d expired system_config rows", removed)


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
                        # Mark guests. A one-off name in the members list reads
                        # like a stranger crept into the stats; saying "guest"
                        # is both honest and how the group already thinks of
                        # someone added with /sif.
                        [{"name": a["name"] + (" (guest)" if a.get("kind") == "proxy" else ""),
                          "attended": a["attended"]} for a in attendance[:5]],
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

def _parse_ended_at(value):
    """ended_at is a datetime on Postgres and an ISO-ish string on SQLite."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _ghost_auto_forgive() -> None:
    """Treat long-unmarked sessions as 'everyone attended' and forgive.

    See GHOST_AUTOFORGIVE_DAYS above for why. Strictly decrements — an
    unmarked session never added a ghost to anyone in the first place.
    """
    if GHOST_AUTOFORGIVE_DAYS <= 0:
        return

    import db as _db
    from handlers.ghost import _decrement_attended

    # Once per day for the whole sweep, same persistent-stamp pattern as the
    # idle sweep so a restart can't make it run repeatedly.
    today = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    if _db.get_system_config("ghost_autoforgive_last") == today:
        return
    _db.set_system_config("ghost_autoforgive_last", today)

    cutoff = datetime.now(timezone.utc) - timedelta(days=GHOST_AUTOFORGIVE_DAYS)
    swept = 0

    for chat_id in _db.get_all_chat_ids():
        try:
            # Ghost tracking off → the whole concept doesn't apply here.
            if not _db.get_or_create_chat(chat_id).get("ghost_tracking_enabled"):
                continue

            stale = [
                rc for rc in _db.get_unprocessed_rollcalls(
                    chat_id, days=_AUTOFORGIVE_MAX_LOOKBACK_DAYS)
                if (ended := _parse_ended_at(rc.get("ended_at"))) and ended <= cutoff
            ]
            for rc in stale:
                rc_id = rc["id"]
                in_users = _db.get_rollcall_in_users(rc_id)
                # selected=set() == "nobody ghosted", the same call the
                # "✅ No, all showed up" button makes.
                _decrement_attended(chat_id, in_users, set())
                _db.mark_rollcall_absent_done(rc_id)
                swept += 1
                logging.info(
                    "[periodic] ghost auto-forgive: chat=%s rollcall=%s (%r) "
                    "unmarked for >%sd — forgave %d attendee(s)",
                    chat_id, rc_id, rc.get("title"), GHOST_AUTOFORGIVE_DAYS,
                    len(in_users),
                )
        except Exception:
            logging.exception("[periodic] ghost auto-forgive failed for chat %s", chat_id)

    if swept:
        logging.info("[periodic] ghost auto-forgive: processed %d stale session(s)", swept)


async def _idle_reengagement() -> None:
    import db as _db

    # Once per day (IST anchor) for the whole sweep.
    today = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    if _db.get_system_config("idle_check_last") == today:
        return
    _db.set_system_config("idle_check_last", today)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=IDLE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Bot-wide, unscoped-by-design join — offloaded off the event loop so this
    # once-a-day scheduler sweep doesn't stall Telegram polling/REST for its
    # whole duration. Applies to BOTH backends: this used to be SQLite-only
    # because psycopg2's SimpleConnectionPool isn't thread-safe, which left
    # Postgres deployments blocking the loop here. db.py now uses
    # ThreadedConnectionPool, so both take the same path.
    idle_chats = await asyncio.get_running_loop().run_in_executor(
        _db._stats_executor, _db.get_idle_chats, cutoff
    )

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
            # Never offer a hidden one-off (__once_…) template: it isn't the
            # group's, and starting it would hand out an internal name.
            from services.templates import is_reserved_once_template_name
            templates = [t for t in _db.get_templates(chat_id)
                         if not is_reserved_once_template_name(t.get("name"))]
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
