"""
Regressions for the October 2026 scheduling audit (real DB, real
check_reminders; only the Telegram API is mocked).

1. set_template_schedule wiped last_scheduled_date on every save, so
   `/src Football repeat=weekly` at the slot time opened a second Football
   a minute later, and any web template edit re-armed a schedule that had
   already fired today / dropped biweekly's 14-day anchor.
2. Weekly catch-up stamped the run date but deduped on the missed date, so
   a Sunday miss fired Monday AND Tuesday; a same-day miss (>30 min late)
   was never caught up that day.
3. Rollcalls started via the service layer (web/REST) never started the
   per-chat auto-close loop.
4. /remind_after_open never fired for rollcalls without a close time — the
   loop never started / exited immediately for them.
5. /srr reminder: fired after the event, and a failed send blocked
   auto-close forever.
6. A one-off that hit the 3-rollcall cap orphaned its hidden __once_
   template, which the idle nudge could then offer to start.
"""
import asyncio
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import AsyncMock, patch

import pytz

import db
import check_reminders
from check_reminders import _is_due_now, _missed_within_days, _loop_needed
from helpers import IntegrationBase, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot

TZ = pytz.timezone("Asia/Kolkata")  # default chat timezone


class _Base(IntegrationBase):
    def setUp(self):
        super().setUp()
        check_reminders._active_loops.discard(CHAT_ID)

    def tearDown(self):
        check_reminders._active_loops.discard(CHAT_ID)
        super().tearDown()


# ── 1. last_scheduled_date survives schedule saves ───────────────────────────

class TestScheduleSavePreservesLastFire(_Base):

    def _mk(self, name="sg"):
        from services import templates as tsvc
        tsvc.upsert_template(CHAT_ID, name, 1, "Admin", title="SG")
        return tsvc

    async def test_src_repeat_counts_as_todays_fire(self):
        """The reported bug: /src Football repeat=weekly at the slot time,
        then the scheduler tick opened a second Football."""
        await self.start_roll_call(self.msg("/src Football repeat=weekly", ADMIN_USER))
        t = db.get_template(CHAT_ID, "football")
        self.assertIsNotNone(t)
        now = datetime.now(TZ)
        self.assertEqual(t["last_scheduled_date"], now.strftime("%Y-%m-%d"))
        self.assertFalse(_is_due_now(t["schedule_time"], t["schedule_day"],
                                     t["last_scheduled_date"], now, "weekly"))
        self.assertFalse(_missed_within_days(t["schedule_day"], t["schedule_time"],
                                             t["last_scheduled_date"], now, 2))

    async def test_resaving_unchanged_schedule_keeps_todays_fire(self):
        """The web template editor PUTs /schedule on every save."""
        tsvc = self._mk()
        now = datetime.now(TZ)
        day, hhmm = now.strftime("%A").lower(), now.strftime("%H:%M")
        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="weekly",
                          schedule_day=day, schedule_time=hhmm)
        today = now.strftime("%Y-%m-%d")
        db.update_template_last_scheduled_date(CHAT_ID, "sg", today)

        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="weekly",
                          schedule_day=day, schedule_time=hhmm)
        t = db.get_template(CHAT_ID, "sg")
        self.assertEqual(t["last_scheduled_date"], today)
        self.assertFalse(_is_due_now(hhmm, day, t["last_scheduled_date"], now, "weekly"))

    async def test_changed_time_never_refires_same_day(self):
        tsvc = self._mk()
        now = datetime.now(TZ)
        day = now.strftime("%A").lower()
        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="daily",
                          schedule_time="00:00")
        today = now.strftime("%Y-%m-%d")
        db.update_template_last_scheduled_date(CHAT_ID, "sg", today)
        # Move it to "now" — inside the catch-up window, but it already ran today.
        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="weekly",
                          schedule_day=day, schedule_time=now.strftime("%H:%M"))
        t = db.get_template(CHAT_ID, "sg")
        self.assertEqual(t["last_scheduled_date"], today)

    async def test_biweekly_anchor_survives_time_change(self):
        tsvc = self._mk()
        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="biweekly",
                          schedule_day="sunday", schedule_time="09:00")
        anchor = (datetime.now(TZ) - timedelta(days=7)).strftime("%Y-%m-%d")
        db.update_template_last_scheduled_date(CHAT_ID, "sg", anchor)
        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="biweekly",
                          schedule_day="sunday", schedule_time="10:30")
        self.assertEqual(db.get_template(CHAT_ID, "sg")["last_scheduled_date"], anchor)

    async def test_new_weekly_slot_does_not_owe_a_catchup(self):
        """Setting a weekly schedule for yesterday's weekday must not fire a
        'missed' run immediately — the slot didn't exist yesterday."""
        tsvc = self._mk()
        now = datetime.now(TZ)
        yday = (now - timedelta(days=1)).strftime("%A").lower()
        tsvc.set_schedule(CHAT_ID, "sg", 1, "Admin", recurrence_type="weekly",
                          schedule_day=yday, schedule_time="09:00")
        t = db.get_template(CHAT_ID, "sg")
        self.assertFalse(_missed_within_days(yday, "09:00", t["last_scheduled_date"], now, 2))


# ── 2. weekly catch-up fires each missed occurrence exactly once ─────────────

class TestWeeklyCatchupOnce(_Base):
    # 2026-06-14 is a Sunday.
    SUN_10 = datetime(2026, 6, 14, 10, 0)
    MON_00 = datetime(2026, 6, 15, 0, 0)
    TUE_00 = datetime(2026, 6, 16, 0, 0)

    def test_sunday_miss_fires_monday_not_again_tuesday(self):
        self.assertTrue(_missed_within_days("sunday", "09:00", "2026-06-07", self.MON_00, 2))
        # Catch-up stamps the run date (Monday) — Tuesday must see it as done.
        self.assertFalse(_missed_within_days("sunday", "09:00", "2026-06-15", self.TUE_00, 2))

    def test_same_day_miss_is_caught_up_once(self):
        # Bot down 08:30-10:00 for a 09:00 slot: outside the 30-min window.
        self.assertFalse(_is_due_now("09:00", "sunday", "2026-06-07", self.SUN_10, "weekly"))
        self.assertTrue(_missed_within_days("sunday", "09:00", "2026-06-07", self.SUN_10, 2))
        self.assertFalse(_missed_within_days("sunday", "09:00", "2026-06-14", self.SUN_10, 2))

    def test_not_yet_due_today_is_not_a_miss(self):
        early = datetime(2026, 6, 14, 8, 0)
        self.assertFalse(_missed_within_days("sunday", "09:00", "2026-06-07", early, 2))


# ── 3 + 4. every start path kicks the reminder loop ──────────────────────────

class TestReminderLoopStarts(_Base):

    async def _started_chats(self, coro_fn):
        started = []

        async def _fake_start(rollcalls, tz, chat_id):
            started.append(chat_id)

        with patch("check_reminders.start", side_effect=_fake_start):
            await coro_fn()
            await asyncio.sleep(0)
        return started

    async def test_service_start_rollcall_with_close_time_starts_loop(self):
        """web_start_rollcall / REST start route go through this service."""
        from services import rollcalls as rc_svc
        future = (datetime.now(dt_timezone.utc) + timedelta(hours=2)).isoformat()
        started = await self._started_chats(lambda: rc_svc.start_rollcall(
            CHAT_ID, "Web Game", 1, "Admin", finalize_at=future))
        self.assertEqual(started, [CHAT_ID])

    async def test_service_start_template_with_close_time_starts_loop(self):
        """web_start_template / REST start_template go through this service."""
        from services import templates as tsvc
        tsvc.upsert_template(CHAT_ID, "fri", 1, "Admin", title="Fri", offset_hours=3)
        started = await self._started_chats(lambda: tsvc.start_template(
            chat_id=CHAT_ID, name="fri", admin_user_id=1, admin_name="Admin"))
        self.assertEqual(started, [CHAT_ID])

    async def test_open_ended_rollcall_without_after_open_needs_no_loop(self):
        started = await self._started_chats(lambda: self.start_rc("Open"))
        self.assertEqual(started, [])

    async def test_after_open_reminder_starts_loop_without_close_time(self):
        db.get_or_create_chat(CHAT_ID)
        db.update_chat_settings(CHAT_ID, reminder_after_open_hours=1)
        started = await self._started_chats(lambda: self.start_rc("Open"))
        self.assertEqual(started, [CHAT_ID])
        self.assertTrue(_loop_needed(CHAT_ID, self.mgr.get_rollcalls(CHAT_ID)))

    async def test_enabling_after_open_kicks_loop_for_already_open_rollcall(self):
        await self.start_rc("Open")
        started = await self._started_chats(lambda: self.remind_after_open_command(
            self.msg("/remind_after_open 2", ADMIN_USER)))
        self.assertEqual(started, [CHAT_ID])

    async def test_check_keeps_running_while_after_open_pending(self):
        """It used to break on the first pass when no rollcall had a close
        time, before the after-open window ever elapsed."""
        db.get_or_create_chat(CHAT_ID)
        db.update_chat_settings(CHAT_ID, reminder_after_open_hours=1)
        await self.start_rc("Open")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(
                check_reminders.check(self.mgr.get_rollcalls(CHAT_ID), "Asia/Kolkata", CHAT_ID),
                timeout=0.5,
            )

    async def test_resume_on_boot_covers_after_open(self):
        db.get_or_create_chat(CHAT_ID)
        db.update_chat_settings(CHAT_ID, reminder_after_open_hours=1)
        await self.start_rc("Open")
        self.mgr.clear_cache()
        started = await self._started_chats(check_reminders.resume_reminder_loops)
        self.assertIn(CHAT_ID, started)


# ── 5. /srr reminder bounds ──────────────────────────────────────────────────

class TestSrrReminder(_Base):

    async def _check_once(self):
        try:
            await asyncio.wait_for(
                check_reminders.check(self.mgr.get_rollcalls(CHAT_ID), "Asia/Kolkata", CHAT_ID),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pass

    def _reminders_sent(self):
        return [a[1] for a, _ in get_mock_bot().send_message.call_args_list
                if len(a) > 1 and "Gentle reminder" in str(a[1])]

    async def test_stale_reminder_after_outage_is_skipped_and_rollcall_closes(self):
        rc = await self.start_rc("Late")
        rc.finalizeDate = datetime.now(TZ) - timedelta(minutes=5)
        rc.reminder = 2
        rc.save()
        get_mock_bot().send_message.reset_mock()
        await self._check_once()
        self.assertEqual(self._reminders_sent(), [])
        self.assertEqual(self.mgr.get_rollcalls(CHAT_ID), [])

    async def test_failed_reminder_send_still_lets_autoclose_run(self):
        rc = await self.start_rc("Flaky")
        rc.finalizeDate = datetime.now(TZ) + timedelta(minutes=30)
        rc.reminder = 2
        rc.save()
        ok_send = get_mock_bot().send_message.side_effect
        get_mock_bot().send_message.side_effect = Exception("telegram down")
        try:
            await self._check_once()
        finally:
            get_mock_bot().send_message.side_effect = ok_send
        self.assertIsNone(rc.reminder)
        # Persisted too, so a restart doesn't re-arm it.
        self.assertIsNone(db.get_rollcall(rc.id).get("reminder_hours"))

        rc.finalizeDate = datetime.now(TZ) - timedelta(minutes=1)
        await self._check_once()
        self.assertEqual(self.mgr.get_rollcalls(CHAT_ID), [])


# ── 6. capped one-off cleans up its hidden template ──────────────────────────

class TestCappedOneOff(_Base):

    async def test_capped_one_off_deletes_reserved_template(self):
        from services import templates as tsvc
        past = (datetime.now(dt_timezone.utc) - timedelta(minutes=1)).isoformat()
        res = tsvc.schedule_once(CHAT_ID, fire_at_iso=past, title="Holiday",
                                 admin_user_id=1, admin_name="Admin")
        name = res["template_name"]
        self.assertTrue(tsvc.is_reserved_once_template_name(name))
        for i in range(3):
            await self.start_rc(f"Game {i}")
        await check_reminders._fire_scheduled_rollcalls()
        self.assertIsNone(db.get_template(CHAT_ID, name))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 3)

    async def test_idle_nudge_never_offers_reserved_template(self):
        from services import templates as tsvc
        import periodic_jobs
        future = (datetime.now(dt_timezone.utc) + timedelta(days=3)).isoformat()
        tsvc.schedule_once(CHAT_ID, fire_at_iso=future, title="Hidden",
                           admin_user_id=1, admin_name="Admin")
        bot = get_mock_bot()
        with patch.object(db, "get_idle_chats", return_value=[{"chat_id": CHAT_ID, "group_name": "G"}]), \
             patch.object(db, "get_last_admin_actor", return_value={"admin_id": 999}), \
             patch.object(db, "get_system_config", return_value=None):
            bot.send_message.reset_mock()
            await periodic_jobs._idle_reengagement()
            self.assertFalse(any("__once_" in str(c) for c in bot.send_message.call_args_list))
            self.assertEqual(bot.send_message.call_count, 0)  # nothing real to offer

            tsvc.upsert_template(CHAT_ID, "friday", 1, "Admin", title="Friday")
            db.update_chat_settings(CHAT_ID, last_idle_nudge=None)
            bot.send_message.reset_mock()
            await periodic_jobs._idle_reengagement()
            texts = " ".join(str(c) for c in bot.send_message.call_args_list)
            self.assertIn("friday", texts)
            self.assertNotIn("__once_", texts)
