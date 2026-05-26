"""
Integration: settings commands — /shh, /louder, /sl, /ef, /if, /w, /loc.
"""
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestQuietMode(IntegrationBase):

    async def test_shh_enables_quiet_mode(self):
        await self.shh(self.msg("/shh", ADMIN_USER))
        self.assertTrue(self.mgr.get_shh_mode(CHAT_ID))

    async def test_louder_disables_quiet_mode(self):
        await self.shh(self.msg("/shh", ADMIN_USER))
        await self.louder(self.msg("/louder", ADMIN_USER))
        self.assertFalse(self.mgr.get_shh_mode(CHAT_ID))

    async def test_shh_suppresses_vote_messages(self):
        await self.start_rc()
        await self.shh(self.msg("/shh", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(USERS[0])
        # In shh mode, no message should be sent on vote
        self.assertEqual(self.sent_count(), 0)

    async def test_shh_sends_confirmation(self):
        await self.shh(self.msg("/shh", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("quiet" in t.lower() or "silent" in t.lower() or "shh" in t.lower() for t in texts))

    async def test_louder_sends_confirmation(self):
        await self.louder(self.msg("/louder", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("hear" in t.lower() or "ok" in t.lower() for t in texts))


class TestWaitLimit(IntegrationBase):

    async def test_sl_sets_limit(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 5", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(rc.inListLimit, 5)

    async def test_sl_no_rollcall_sends_error(self):
        await self.wait_limit(self.msg("/sl 5", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_sl_zero_sends_error(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 0", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("positive" in t.lower() or "missing" in t.lower() for t in texts))

    async def test_sl_non_numeric_sends_error(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl abc", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("positive" in t.lower() or "missing" in t.lower() for t in texts))

    async def test_sl_enforces_waitlist_at_new_limit(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for user in USERS[:4]:
            await self.vote_in(user)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 3)
        self.assertEqual(len(rc.waitList), 1)


class TestEventFee(IntegrationBase):

    async def test_ef_sets_fee(self):
        await self.start_rc()
        await self.event_fee(self.msg("/ef $10", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(rc.event_fee, "$10")

    async def test_ef_no_rollcall_sends_error(self):
        await self.event_fee(self.msg("/ef $10", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_ef_invalid_value_sends_error(self):
        await self.start_rc()
        await self.event_fee(self.msg("/ef nomoney", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("correct format" in t.lower() or "integer" in t.lower() for t in texts))

    async def test_ef_sends_confirmation(self):
        await self.start_rc()
        get_mock_bot().send_message.reset_mock()
        await self.event_fee(self.msg("/ef $20", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("$20" in t or "fee" in t.lower() for t in texts))


class TestIndividualFee(IntegrationBase):
    """individual_fee calculates per-person cost from event_fee / inList count."""

    async def test_if_no_rollcall_sends_error(self):
        await self.individual_fee(self.msg("/if", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_if_no_event_fee_sends_error(self):
        await self.start_rc()
        await self.individual_fee(self.msg("/if", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no event fee" in t.lower() or "event_fee" in t.lower() for t in texts))

    async def test_if_calculates_per_person_fee(self):
        await self.start_rc()
        await self.event_fee(self.msg("/ef 100", ADMIN_USER))
        for user in USERS[:4]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.individual_fee(self.msg("/if", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("25" in t or "fee" in t.lower() for t in texts))


class TestWhen(IntegrationBase):
    """/when displays the finalizeDate; errors if none is set."""

    async def test_w_no_rollcall_sends_error(self):
        await self.when(self.msg("/w", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_w_no_finalize_date_sends_error(self):
        await self.start_rc()
        await self.when(self.msg("/w", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no start time" in t.lower() or "start time" in t.lower() for t in texts))


class TestLocation(IntegrationBase):

    async def test_loc_sets_location(self):
        await self.start_rc()
        await self.set_location(self.msg("/loc Central Park", ADMIN_USER))
        rc = self.rc(0)
        self.assertIn("Central Park", rc.location or "")

    async def test_loc_no_rollcall_sends_error(self):
        await self.set_location(self.msg("/loc Central Park", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_loc_no_args_sends_error(self):
        await self.start_rc()
        await self.set_location(self.msg("/loc", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("correct format" in t.lower() or "location" in t.lower() for t in texts))

    async def test_loc_updates_existing_location(self):
        await self.start_rc()
        await self.set_location(self.msg("/loc Field 1", ADMIN_USER))
        await self.set_location(self.msg("/loc Field 2", ADMIN_USER))
        rc = self.rc(0)
        self.assertIn("Field 2", rc.location or "")


class TestSetRollcallTime(IntegrationBase):
    """Tests for /srt (set_rollcall_time)."""

    def _future_date_str(self, days=365):
        from datetime import datetime, timedelta
        return (datetime.now() + timedelta(days=days)).strftime("%d-%m-%Y %H:%M")

    async def test_srt_no_rollcall_sends_error(self):
        await self.set_rollcall_time(self.msg("/srt 01-01-2099 10:00", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_srt_no_args_sends_error(self):
        await self.start_rc()
        await self.set_rollcall_time(self.msg("/srt", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("format" in t.lower() or "datetime" in t.lower() for t in texts))

    async def test_srt_sets_finalize_date(self):
        await self.start_rc()
        future = self._future_date_str()
        await self.set_rollcall_time(self.msg(f"/srt {future}", ADMIN_USER))
        rc = self.rc(0)
        self.assertIsNotNone(rc.finalizeDate)

    async def test_srt_past_date_sends_error(self):
        await self.start_rc()
        await self.set_rollcall_time(self.msg("/srt 01-01-2000 10:00", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("future" in t.lower() or "valid" in t.lower() for t in texts))

    async def test_srt_cancel_clears_finalize_date(self):
        await self.start_rc()
        future = self._future_date_str()
        await self.set_rollcall_time(self.msg(f"/srt {future}", ADMIN_USER))
        self.assertIsNotNone(self.rc(0).finalizeDate)
        get_mock_bot().send_message.reset_mock()
        await self.set_rollcall_time(self.msg("/srt cancel", ADMIN_USER))
        self.assertIsNone(self.rc(0).finalizeDate)


class TestSetRollcallReminder(IntegrationBase):
    """Tests for /srr (set_rollcall_reminder)."""

    def _future_date_str(self, days=365):
        from datetime import datetime, timedelta
        return (datetime.now() + timedelta(days=days)).strftime("%d-%m-%Y %H:%M")

    async def test_srr_no_rollcall_sends_error(self):
        await self.reminder(self.msg("/srr 2", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_srr_no_finalize_date_sends_error(self):
        await self.start_rc()
        await self.reminder(self.msg("/srr 2", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("finalize" in t.lower() or "first" in t.lower() for t in texts))

    async def test_srr_sets_reminder(self):
        await self.start_rc()
        future = self._future_date_str(days=10)
        await self.set_rollcall_time(self.msg(f"/srt {future}", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.reminder(self.msg("/srr 2", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(rc.reminder, 2)

    async def test_srr_cancel_clears_reminder(self):
        await self.start_rc()
        future = self._future_date_str(days=10)
        await self.set_rollcall_time(self.msg(f"/srt {future}", ADMIN_USER))
        await self.reminder(self.msg("/srr 2", ADMIN_USER))
        self.assertEqual(self.rc(0).reminder, 2)
        await self.reminder(self.msg("/srr cancel", ADMIN_USER))
        self.assertIsNone(self.rc(0).reminder)

    async def test_srr_zero_hours_sends_error(self):
        await self.start_rc()
        future = self._future_date_str(days=10)
        await self.set_rollcall_time(self.msg(f"/srt {future}", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.reminder(self.msg("/srr 0", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("hour" in t.lower() or "higher" in t.lower() or "format" in t.lower() for t in texts))
