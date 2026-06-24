import asyncio
import logging
from datetime import datetime, timedelta

import pytz
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_state import bot
from db import (
    end_rollcall, update_streak_on_checkin, reset_user_streak, get_all_scheduled_templates,
    update_template_last_scheduled_date, get_all_chat_ids, increment_user_stat,
    clear_rollcall_reminder,
    update_proxy_streak_on_checkin, reset_proxy_streak,
    update_chat_group_name,
)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Registry to track active reminder loops per chat_id to prevent duplicates
_active_loops = set()

# Date (UTC, YYYY-MM-DD) of the last group-name refresh sweep, so it runs at
# most once a day. Group titles change rarely, so a daily check is plenty.
_last_group_name_refresh = None


async def _refresh_all_group_names():
    """Once-a-day sweep: fetch each chat's current Telegram title and persist it
    if it changed. Best-effort — a failure on one chat never aborts the sweep,
    and the whole thing is skipped silently if Telegram is unreachable."""
    chat_ids = get_all_chat_ids()
    updated = 0
    for chat_id in chat_ids:
        try:
            chat_info = await bot.get_chat(chat_id)
            title = chat_info.title or chat_info.first_name
            if title:
                update_chat_group_name(chat_id, title)
                updated += 1
        except Exception:
            # Private chats, kicked-from groups, transient API errors — skip.
            logging.debug("[group-name-refresh] skipped chat %s", chat_id)
        # Gentle pacing so a large install doesn't burst the Telegram API.
        await asyncio.sleep(0.2)
    logging.info(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"[group-name-refresh] swept {len(chat_ids)} chat(s), updated {updated}"
    )


def _ensure_aware(dt, tz):
    """Return dt as tz-aware; localize naive datetimes using the given tz.

    DST cutover handling (matters for chats in DST-observing zones —
    Europe, parts of North America, etc.; harmless no-op for India and
    other no-DST zones):

      - Unambiguous time: localize directly. ~99.99% of cases.
      - Ambiguous fall-back time (e.g. 1:30am on the day clocks fall back
        from 2:00am to 1:00am — 1:30am occurs twice):
          pytz raises AmbiguousTimeError on is_dst=None. We pick
          is_dst=True, which corresponds to the EARLIER wall-time
          occurrence (still in daylight time, before the clock change).
          Matches a user's natural reading of "1:30am Sunday" as the
          first 1:30am they'd encounter that morning.
      - Non-existent spring-forward time (e.g. 2:30am on the day clocks
        skip from 2:00am to 3:00am — 2:30am never exists):
          pytz raises NonExistentTimeError on is_dst=None. We pick
          is_dst=False, which interprets the time as standard, giving
          the post-jump real moment (effectively maps 2:30am → 3:30am
          local wall time on that day). The "missing" hour was skipped,
          so fire at the next real wall-time slot rather than silently
          dropping the run.

    Old behavior was a bare-except → is_dst=False fallback for both
    cases, which firstly fired ambiguous times at the LATER occurrence
    (off by an hour from user expectation) and obscured what was
    actually happening. Now each case is handled by its own typed
    exception and documented.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    try:
        return tz.localize(dt, is_dst=None)
    except pytz.AmbiguousTimeError:
        # Fall-back cutover — pick the earlier of the two valid
        # interpretations so a scheduled 1:30am rollcall fires at the
        # first 1:30am of the day, not the second.
        return tz.localize(dt, is_dst=True)
    except pytz.NonExistentTimeError:
        # Spring-forward — the wall time the user typed never happened.
        # is_dst=False maps it to the equivalent post-jump moment so we
        # fire at the next real time slot instead of skipping the run.
        logging.warning(
            f"[_ensure_aware] non-existent local time {dt.isoformat()} in {tz}; "
            "interpreting as standard-time equivalent (post-DST-jump)"
        )
        return tz.localize(dt, is_dst=False)
    except Exception:
        # Defensive last-resort — same fallback the old code used.
        # Should never reach here in practice.
        logging.exception(f"[_ensure_aware] unexpected localize failure for {dt} in {tz}")
        return tz.localize(dt, is_dst=False)


async def check(rollcalls, timezone, chat_id):
    from rollcall_manager import manager
    while True:
        # BUG13: always refresh from manager so additions/removals by /erc are visible
        rollcalls = manager.get_rollcalls(chat_id)
        if len(rollcalls) == 0:
            break

        no_reminder_rollcalls = 0

        # Snapshot the current list and build rc_id_map from it for this pass
        current_rollcalls = list(rollcalls)
        rc_id_map = {id(rc): i + 1 for i, rc in enumerate(current_rollcalls)}

        for rollcall in current_rollcalls:
            try:
                if rollcall.finalizeDate is None:
                    no_reminder_rollcalls += 1
                    continue

                rc_number = rc_id_map.get(id(rollcall))
                if rc_number is None:
                    continue

                tz = pytz.timezone(timezone)
                now_date_string = datetime.now(tz).strftime("%d-%m-%Y %H:%M")
                now_date = datetime.strptime(now_date_string, "%d-%m-%Y %H:%M")
                now_date = tz.localize(now_date)

                # BUG12: ensure finalizeDate is tz-aware before any comparison
                finalize_dt = _ensure_aware(rollcall.finalizeDate, tz)

                if rollcall.reminder is not None:
                    reminder_time = finalize_dt - timedelta(hours=int(rollcall.reminder))
                    if now_date >= reminder_time:
                        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sending reminder for rollcall #{rc_number}: {rollcall.title}")
                        await bot.send_message(
                            chat_id,
                            f"Gentle reminder! event with title - {rollcall.title} is {rollcall.reminder} hour/s away"
                        )
                        rollcall.reminder = None
                        # Persist the clear so a bot restart between now and
                        # the rollcall's finalizeDate doesn't re-fire the
                        # reminder. Before this line existed, the in-memory
                        # clear was lost on restart and the freshly-loaded
                        # rollcall.reminder_hours from DB still matched
                        # "now >= reminder_time" → user got the reminder
                        # twice.
                        rc_db_id = getattr(rollcall, "db_id", None) or getattr(rollcall, "id", None)
                        if rc_db_id is not None:
                            try:
                                clear_rollcall_reminder(rc_db_id)
                            except Exception:
                                logging.exception(
                                    f"Failed to persist reminder clear for rollcall {rc_db_id}; "
                                    "a bot restart before close could re-fire the reminder"
                                )
                        continue

                if rollcall.finalizeDate is not None and rollcall.reminder is None:
                    if now_date >= finalize_dt:
                        # Take the same lock /erc uses so we don't race with a
                        # concurrent manual end (which would double-send the
                        # finish text and ghost prompt).
                        async with manager.get_erc_lock(chat_id):
                            current_rcs = manager.get_rollcalls(chat_id)
                            if rollcall not in current_rcs:
                                # /erc beat us to it — nothing left to do.
                                continue
                            # Recompute rc_number in case /erc on another
                            # rollcall renumbered the list while we waited.
                            rc_number = current_rcs.index(rollcall) + 1

                            logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Auto-closing rollcall #{rc_number}: {rollcall.title}")

                            rc_db_id = getattr(rollcall, "db_id", None) or getattr(rollcall, "id", None)

                            # Build finish text before any state changes.
                            finish_text = None
                            if not manager.get_shh_mode(chat_id):
                                finish_text = rollcall.finishList().replace('__RCID__', str(rc_number))
                                finish_text = f"{finish_text}\n\n🕐 Auto-closed at scheduled time"

                            # Snapshot user lists before clearing state.
                            in_user_ids = {u.user_id for u in rollcall.inList if isinstance(u.user_id, int)}
                            participants = set(
                                u.user_id for u in (rollcall.inList + rollcall.outList + rollcall.maybeList + rollcall.waitList)
                                if isinstance(u.user_id, int)
                            )
                            proxy_in_names = {
                                u.name for u in rollcall.inList
                                if not isinstance(u.user_id, int)
                            }
                            proxy_participants = {
                                u.name for u in (rollcall.inList + rollcall.outList + rollcall.maybeList + rollcall.waitList)
                                if not isinstance(u.user_id, int)
                            }

                            # ── Commit DB close and in-memory teardown FIRST ──────────────
                            # If the Telegram send below fails (network outage, timeout),
                            # the rollcall is already properly ended — next check won't retry.
                            if rc_db_id is not None:
                                end_rollcall(rc_db_id)

                            if rollcall in rollcalls:
                                rollcalls.remove(rollcall)

                            # Remove inline keyboard from the panel message so
                            # vote buttons disappear immediately in Telegram.
                            panel_msg_id = None
                            try:
                                from bot_state import _panel_msg_ids
                                panel_msg_id = _panel_msg_ids.pop((chat_id, rc_number), None)
                                for num in sorted(n for (c, n) in list(_panel_msg_ids) if c == chat_id and n > rc_number):
                                    _panel_msg_ids[(chat_id, num - 1)] = _panel_msg_ids.pop((chat_id, num))
                            except Exception:
                                pass

                            rollcall.finalizeDate = None

                            # ── Update attendance streaks ─────────────────────────────────
                            for uid in in_user_ids:
                                try:
                                    update_streak_on_checkin(chat_id, uid)
                                except Exception:
                                    logging.exception(f"Failed to update streak for user {uid} in chat {chat_id}")

                            # Reset streak for participants who voted OUT/MAYBE (didn't end up IN).
                            for uid in participants - in_user_ids:
                                try:
                                    reset_user_streak(chat_id, uid)
                                except Exception:
                                    logging.exception(f"Failed to reset streak for user {uid} in chat {chat_id}")
                            for uid in participants:
                                try:
                                    increment_user_stat(chat_id, uid, "total_rollcalls")
                                except Exception:
                                    logging.warning(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Failed to increment total_rollcalls for user {uid}")

                            # Proxy streaks
                            for name in proxy_in_names:
                                try:
                                    update_proxy_streak_on_checkin(chat_id, name)
                                except Exception:
                                    logging.exception(f"Failed to update proxy streak for {name} in chat {chat_id}")
                            for name in proxy_participants - proxy_in_names:
                                try:
                                    reset_proxy_streak(chat_id, name)
                                except Exception:
                                    logging.exception(f"Failed to reset proxy streak for {name} in chat {chat_id}")

                            # ── Telegram messages are best-effort after DB is committed ───
                            # Remove vote buttons from the existing panel message.
                            if panel_msg_id:
                                try:
                                    await bot.edit_message_reply_markup(chat_id, panel_msg_id, reply_markup=None)
                                except Exception:
                                    pass  # Message may have been deleted; non-fatal

                            if finish_text:
                                try:
                                    await bot.send_message(chat_id, finish_text)
                                except Exception:
                                    logging.exception(
                                        f"[auto-close] Failed to send close message for rollcall #{rc_number} "
                                        "(rollcall is ended in DB — message will not be retried)"
                                    )

                            # Fire ghost prompt if tracking is enabled and rollcall had IN users
                            if rc_db_id is not None:
                                try:
                                    from db import get_rollcall_in_users
                                    ghost_tracking_on = manager.get_ghost_tracking_enabled(chat_id)
                                    has_users = bool(get_rollcall_in_users(rc_db_id))
                                    absent_already = getattr(rollcall, "absent_marked", False)
                                    if ghost_tracking_on and has_users and not absent_already:
                                        markup = InlineKeyboardMarkup(row_width=2)
                                        markup.add(
                                            InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rc_db_id}"),
                                            InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{rc_db_id}"),
                                        )
                                        await bot.send_message(chat_id, f"👻 Did anyone ghost '{rollcall.title}'?", reply_markup=markup)
                                except Exception:
                                    logging.exception("Error sending ghost prompt after auto-close")

                        continue

            except Exception:
                logging.exception("Error processing rollcall reminder")

        if len(rollcalls) == 0 or no_reminder_rollcalls == len(rollcalls):
            break

        await asyncio.sleep(60)


async def start(rollcalls, timezone, chat_id):
    """
    Start the reminder/auto-close loop for a specific chat.
    Safe to call multiple times; will not start duplicate loops.
    """
    if chat_id in _active_loops:
        logging.debug(f"Reminder loop already active for chat {chat_id}")
        return

    try:
        _active_loops.add(chat_id)
        current_sec = int(datetime.now().strftime("%S"))
        delay = 60 - current_sec
        if delay == 60:
            delay = 0
        await asyncio.sleep(delay)
        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting reminder check for chat {chat_id}")
        await check(rollcalls, timezone, chat_id)
    except Exception:
        logging.exception("Unexpected error in reminder loop")
    finally:
        _active_loops.discard(chat_id)
        logging.info(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Reminder loop finished for chat {chat_id}")


async def resume_reminder_loops():
    """On startup, restart reminder loops for all chats that have active rollcalls with a finalizeDate.
    Called once from runner.py to recover state after a bot restart.
    """
    from rollcall_manager import manager

    chat_ids = get_all_chat_ids()
    resumed = 0
    for chat_id in chat_ids:
        try:
            chat = manager.get_chat(chat_id)
            rollcalls = manager.get_rollcalls(chat_id)
            if any(rc.finalizeDate is not None for rc in rollcalls):
                tzname = chat.get("timezone", "Asia/Kolkata")
                # Attach the done callback so any exception in the resumed
                # reminder loop surfaces in logs. Without it, asyncio swallows
                # the exception when the task is garbage-collected.
                from bot_state import _log_task_exc
                asyncio.create_task(start(rollcalls, tzname, chat_id)).add_done_callback(_log_task_exc)
                resumed += 1
                logging.info(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"Resumed reminder loop for chat {chat_id} ({len(rollcalls)} active rollcall(s))"
                )
        except Exception:
            logging.exception(f"Error resuming reminder loop for chat {chat_id}")
    logging.info(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Startup: resumed {resumed} reminder loop(s) across {len(chat_ids)} chat(s)"
    )


async def _auto_start_from_template(chat_id: int, tmpl: dict):
    """Create a rollcall from a scheduled template and announce it to the group."""
    from rollcall_manager import manager
    from functions import get_next_weekday_datetime

    chat = manager.get_chat(chat_id)
    tzname = chat.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(tzname)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
        tzname = "Asia/Kolkata"

    rollcalls = manager.get_rollcalls(chat_id)
    if len(rollcalls) >= 3:
        await bot.send_message(
            chat_id,
            f"⚠️ Could not auto-start template '{tmpl['name']}': maximum 3 active rollcalls already open."
        )
        return

    title = tmpl.get("title") or tmpl["name"]
    rc = manager.add_rollcall(chat_id, title)

    if tmpl.get("inlistlimit") is not None:
        rc.inListLimit = tmpl["inlistlimit"]
    if tmpl.get("location"):
        rc.location = tmpl["location"]
    if tmpl.get("eventfee"):
        rc.event_fee = tmpl["eventfee"]

    rc.timezone = tzname
    rc.finalizeDate = None

    event_day = tmpl.get("event_day")
    event_time = tmpl.get("event_time")
    if event_day and event_time:
        dt = get_next_weekday_datetime(tz, event_day, event_time)
        if dt:
            rc.finalizeDate = dt

    rc.save()

    rc_index = len(rollcalls) - 1  # 0-based; rc was just appended by add_rollcall
    rc_number = rc_index + 1       # 1-based display number

    # Send the full rollcall panel with inline vote buttons.
    # Both attempts are best-effort: a network outage must not prevent
    # last_scheduled_date from being stamped (which would cause duplicates).
    try:
        from handlers.lifecycle import get_status_keyboard, _persist_panel_msg_id, _build_panel_text
        from bot_state import _panel_msg_ids
        markup = await get_status_keyboard(rc_number)
        text = _build_panel_text(rc, rc_number)
        sent = await bot.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
        _panel_msg_ids[(chat_id, rc_number)] = sent.message_id
        _persist_panel_msg_id(rc, sent.message_id)
    except Exception:
        # Fallback: plain text announcement if panel send fails.
        # Wrapped in its own try/except so a second network error doesn't
        # propagate and skip update_template_last_scheduled_date in the caller.
        close_info = ""
        if rc.finalizeDate:
            close_info = f"\nCloses: {rc.finalizeDate.strftime('%A, %d %b at %H:%M')}"
        from bot_state import _esc_md
        try:
            await bot.send_message(
                chat_id,
                f"📋 *{_esc_md(title)}* rollcall is now open!{close_info}\nVote with /in or /out.",
                parse_mode="Markdown"
            )
        except Exception:
            logging.exception(
                f"[scheduler] Failed to announce auto-started rollcall '{title}' for chat {chat_id}"
            )

    logging.info(
        f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
        f"Auto-started template '{tmpl['name']}' for chat {chat_id}"
    )

    # Ensure the reminder/auto-close loop is running for this new rollcall
    if rc.finalizeDate:
        def _log_exc(t):
            if not t.cancelled() and t.exception():
                logging.error(f"Reminder loop raised: {t.exception()}")
        asyncio.create_task(start(rollcalls, tzname, chat_id)).add_done_callback(_log_exc)


# Catch-up window: if the loop's iteration drifts past the exact scheduled
# minute (event-loop pressure, slow telegram API calls in a previous
# iteration, or a bot restart that landed just after the schedule time),
# still fire as long as we're within this many minutes after the scheduled
# time and haven't fired today. 30 minutes is conservative enough to never
# fire a "stale" rollcall but generous enough to absorb any realistic skew.
SCHEDULE_CATCHUP_MINUTES = 30

# Multi-day catch-up: if the bot was down and missed a scheduled run, fire
# it on restart as long as the miss was within this many days.
SCHEDULE_CATCHUP_DAYS = 2


def _parse_hhmm(raw):
    """Parse a schedule_time string into (hour, minute). Tolerates "9:00",
    "09:00", "9:5", "09:05" — all are valid clock times even though only the
    last is zero-padded. Returns None on any garbage so the caller can skip
    rather than crash."""
    if not raw:
        return None
    try:
        sh, sm = raw.strip().split(":")
        h, m = int(sh), int(sm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except (ValueError, AttributeError):
        pass
    return None


def _is_due_now(schedule_time, schedule_day, last_date, now, recurrence_type):
    """Return True iff this template's schedule should fire on the current
    iteration. Centralises the day/time/catch-up/dedupe logic for all three
    recurrence types so we can't drift between them again.

    Catch-up semantics: we treat the schedule as due if the chat's clock has
    crossed the scheduled minute (within SCHEDULE_CATCHUP_MINUTES) AND we
    haven't fired today AND day-of-week/day-of-month matches.

    Old code did exact-minute string equality on now.strftime('%H:%M') ==
    schedule_time. Any iteration that skipped the target minute (drift
    accumulating over hours, an iteration that took >60s, or a bot restart
    landing past the schedule) silently lost the entire week's run. This
    function eliminates that whole class of bug.
    """
    hm = _parse_hhmm(schedule_time)
    if hm is None:
        return False
    sh, sm = hm
    today_date_str = now.strftime("%Y-%m-%d")

    # Already fired today — never fire twice.
    if last_date == today_date_str:
        return False

    scheduled_dt = now.replace(hour=sh, minute=sm, second=0, microsecond=0)

    # Too early — clock hasn't reached the scheduled time yet.
    if now < scheduled_dt:
        return False
    # Too late — outside the catch-up window. Avoid firing a stale rollcall
    # hours after its time has passed (e.g. bot was down all day).
    if (now - scheduled_dt).total_seconds() > SCHEDULE_CATCHUP_MINUTES * 60:
        return False

    today_name = now.strftime("%A").lower()
    if recurrence_type == "monthly":
        try:
            target_day = int(schedule_day)
        except (ValueError, TypeError):
            return False
        if now.day != target_day:
            return False
        return True

    if today_name != (schedule_day or "").lower():
        return False

    if recurrence_type == "biweekly" and last_date:
        try:
            last_dt = datetime.strptime(last_date, "%Y-%m-%d").date()
            if (now.date() - last_dt).days < 14:
                return False
        except (ValueError, TypeError):
            return False

    return True


def _missed_within_days(schedule_day, schedule_time, last_date, now, max_days):
    """Return True if a weekly template missed its scheduled run within max_days.

    Used for startup catch-up: if the bot was offline and the weekly run was
    missed (e.g. yesterday or the day before), fire it now.  Biweekly and
    monthly templates are excluded — their cadence is long enough that a
    2-day catch-up window could legitimately double-fire.
    """
    from datetime import timedelta
    hm = _parse_hhmm(schedule_time)
    if hm is None:
        return False
    sh, sm = hm
    for delta in range(1, max_days + 1):
        past = now - timedelta(days=delta)
        if past.strftime("%A").lower() != (schedule_day or "").lower():
            continue
        past_date_str = past.strftime("%Y-%m-%d")
        if last_date == past_date_str:
            return False  # Already fired on that day
        past_scheduled_dt = past.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if now > past_scheduled_dt:
            return True
    return False


async def check_template_schedules():
    """Persistent loop that fires scheduled templates at their configured day/time.

    Iterates roughly once per minute; the catch-up window in _is_due_now
    means we don't need micro-precise alignment, just regular polling.
    """
    # Align to the next minute boundary before entering the loop (purely
    # cosmetic for logs — the catch-up window means timing doesn't have to
    # be precise).
    current_sec = int(datetime.now().strftime("%S"))
    if current_sec != 0:
        await asyncio.sleep(60 - current_sec)

    global _last_group_name_refresh
    while True:
        # Daily group-name refresh — group titles change rarely, so a once-a-day
        # sweep is enough. Piggybacks on this persistent loop's minute tick.
        try:
            today_utc = datetime.utcnow().strftime("%Y-%m-%d")
            if _last_group_name_refresh != today_utc:
                _last_group_name_refresh = today_utc
                await _refresh_all_group_names()
        except Exception:
            logging.exception("Error in daily group-name refresh")

        try:
            scheduled = get_all_scheduled_templates()
            for tmpl in scheduled:
                chat_id = tmpl.get("chatid")
                schedule_day = tmpl.get("schedule_day")
                schedule_time = tmpl.get("schedule_time")
                last_date = tmpl.get("last_scheduled_date")

                if not chat_id or not schedule_day or not schedule_time:
                    continue

                try:
                    from rollcall_manager import manager
                    chat = manager.get_chat(chat_id)
                    tz = pytz.timezone(chat.get("timezone", "Asia/Kolkata"))
                except Exception:
                    tz = pytz.timezone("Asia/Kolkata")

                now = datetime.now(tz)
                today_date = now.strftime("%Y-%m-%d")
                recurrence_type = tmpl.get("recurrence_type", "weekly") or "weekly"

                due = _is_due_now(schedule_time, schedule_day, last_date, now, recurrence_type)
                if not due and recurrence_type == "weekly":
                    due = _missed_within_days(
                        schedule_day, schedule_time, last_date, now, SCHEDULE_CATCHUP_DAYS
                    )
                if not due:
                    continue

                try:
                    await _auto_start_from_template(chat_id, tmpl)
                    update_template_last_scheduled_date(chat_id, tmpl["name"], today_date)
                    logging.info(
                        f"[scheduler] Auto-started template '{tmpl.get('name')}' for chat {chat_id} "
                        f"(scheduled {schedule_day} {schedule_time}, fired at {now.strftime('%H:%M:%S')})"
                    )
                except Exception:
                    logging.exception(
                        f"Failed to auto-start template '{tmpl.get('name')}' for chat {chat_id}"
                    )

        except Exception:
            logging.exception("Error in template schedule loop")

        await asyncio.sleep(60)