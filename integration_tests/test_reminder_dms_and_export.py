"""
Real-DB integration tests for reminder DMs (/remind_before_close,
/remind_after_open) and /export_stats.

Both trigger paths live inside check_reminders.check() — the same
per-chat loop that already handles auto-close and /auto_buzz — so these
tests also cover a real regression found while building this: check()
was reading chat settings via manager.get_chat(chat_id), whose cache dict
only ever holds a fixed 6-field subset (not auto_buzz_hours or either new
reminder field), so the setting always read back as unset/0 regardless of
what an admin configured. Fixed to use db.get_or_create_chat (a fresh
read) instead -- test_auto_buzz_actually_fires below is the regression
test that catches this class of bug for auto-buzz specifically, since no
prior test exercised it.
"""
import asyncio
import pytz
from datetime import datetime, timedelta

import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID
from mock_helpers import get_mock_bot
import services.stats as stats_svc


class TestReminderDMs(IntegrationBase):

    async def _warm_up_member(self, user, chat_id=CHAT_ID):
        """get_non_responders only considers users who've voted at least
        once before (db.get_active_members) -- register `user` as one via
        a throwaway rollcall."""
        await self.start_roll_call(self.msg("/src Warmup", ADMIN_USER))
        await self.vote_in(user)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    async def _run_check_once(self, timeout=1.0):
        from check_reminders import check
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        try:
            await asyncio.wait_for(check(rollcalls, "Asia/Kolkata", CHAT_ID), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    def _dm_recipients(self):
        """user_ids DM'd (send_message calls whose first arg isn't the
        group chat_id)."""
        bot = get_mock_bot()
        return [
            args[0] for args, kwargs in bot.send_message.call_args_list
            if args and args[0] != CHAT_ID
        ]

    async def test_before_close_dms_non_voters_once(self):
        await self._warm_up_member(USERS[1])  # becomes a known non-voter target
        await self.remind_before_close_command(self.msg("/remind_before_close 2", ADMIN_USER))

        await self.start_rc("Friday Football")
        await self.vote_in(USERS[0])
        # USERS[1] doesn't vote this time.

        rc = self.rc(0)
        tz = pytz.timezone("Asia/Kolkata")
        rc.finalizeDate = datetime.now(tz) + timedelta(minutes=90)  # within the 2h window
        rc.save()

        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        recipients = self._dm_recipients()
        self.assertIn(USERS[1]["id"], recipients)
        self.assertNotIn(USERS[0]["id"], recipients)  # voted -- not a non-responder

        # One-shot: a second pass must not re-DM.
        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        self.assertEqual(self._dm_recipients(), [])

    async def test_after_open_does_not_fire_before_the_window_elapses(self):
        await self._warm_up_member(USERS[1])
        await self.remind_after_open_command(self.msg("/remind_after_open 1", ADMIN_USER))

        await self.start_rc("Friday Football")
        await self.vote_in(USERS[0])

        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        self.assertEqual(self._dm_recipients(), [])  # only seconds have elapsed, not 1h

    async def test_after_open_fires_once_window_elapsed(self):
        await self._warm_up_member(USERS[1])
        await self.remind_after_open_command(self.msg("/remind_after_open 1", ADMIN_USER))

        await self.start_rc("Friday Football")
        await self.vote_in(USERS[0])

        rc = self.rc(0)
        # createdDate is a naive UTC system timestamp -- backdate it the
        # same way, not via chat-local time (that was the second bug this
        # feature surfaced: createdDate must never go through
        # _ensure_aware, which assumes naive input is chat-local wall time).
        rc.createdDate = datetime.utcnow() - timedelta(hours=2)

        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        recipients = self._dm_recipients()
        self.assertIn(USERS[1]["id"], recipients)

        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        self.assertEqual(self._dm_recipients(), [])  # one-shot

    async def test_reminders_off_by_default_no_dms(self):
        await self._warm_up_member(USERS[1])
        await self.start_rc("Friday Football")
        await self.vote_in(USERS[0])
        rc = self.rc(0)
        tz = pytz.timezone("Asia/Kolkata")
        rc.finalizeDate = datetime.now(tz) + timedelta(minutes=5)
        rc.save()

        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        self.assertEqual(self._dm_recipients(), [])

    async def test_auto_buzz_actually_fires(self):
        """Regression for the manager.get_chat cache-staleness bug found
        while building the reminder-DM feature -- /auto_buzz shares the
        exact same (buggy, until fixed) settings-read pattern and had zero
        prior test coverage of whether it fires at all."""
        from handlers.lists import auto_buzz_command
        await self._warm_up_member(USERS[1])
        await auto_buzz_command(self.msg("/auto_buzz 2", ADMIN_USER))

        await self.start_rc("Friday Football")
        await self.vote_in(USERS[0])
        rc = self.rc(0)
        tz = pytz.timezone("Asia/Kolkata")
        rc.finalizeDate = datetime.now(tz) + timedelta(minutes=90)
        rc.save()

        get_mock_bot().send_message.reset_mock()
        await self._run_check_once()
        texts = self.sent_texts()
        self.assertTrue(any("closes in" in t.lower() for t in texts))


class TestExportStats(IntegrationBase):

    async def test_export_stats_sends_document_with_real_data(self):
        await self.start_rc("Game 1")
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        get_mock_bot().send_document.reset_mock()
        await self.export_stats_command(self.msg("/export_stats", ADMIN_USER))

        get_mock_bot().send_document.assert_called_once()
        _, kwargs = get_mock_bot().send_document.call_args
        self.assertIn("stats_export_", kwargs.get("visible_file_name", ""))

    async def test_export_stats_non_admin_rejected(self):
        get_mock_bot().send_document.reset_mock()
        await self.export_stats_command(self.msg("/export_stats", USERS[0]))
        get_mock_bot().send_document.assert_not_called()

    async def test_export_stats_matches_direct_service_call(self):
        await self.start_rc("Game 1")
        await self.vote_in(USERS[0])
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        csv_str = stats_svc.export_stats_csv(CHAT_ID)
        self.assertIn(USERS[0]["first_name"], csv_str)
        self.assertIn(str(USERS[0]["id"]), csv_str)
