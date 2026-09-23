"""A closing time we were given but could not read must be an error.

The failure this prevents: start_rollcall accepted `finalize_at="tomorrow at
6pm"` (or `event_day="Freitag"`, or a day with no time) and simply skipped
setting the close time. The rollcall opened, looked completely normal in every
list and panel, and then never closed — no reminders, no auto-close, nothing
until somebody noticed days later and ran /erc by hand.

That is the same symptom as the 10.4 template-offset bug, and the same reason
it was expensive: nothing is visible at the moment it happens. `api/schemas/
web.py` carries no validators, so the HTTP path reached the service unchanged.

The fix lives in the SERVICE, not in a request schema, because the service is
the platform-agnostic core — the Telegram handlers and any future adapter call
it directly and would not be covered by a FastAPI validator. It also keeps the
rule in one place rather than adding a second copy that can drift.

The distinction these tests pin: asking for NO close time is valid and makes
an open-ended rollcall. What is refused is asking for one we then fail to set.
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from exceptions import incorrectParameter  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        from services import rollcalls as rc_svc
        self.svc = rc_svc
        self.chat_id = -100999

        # The real model (chat_id=None -> no DB write), so serialize_rollcall
        # sees every field it expects instead of a hand-rolled stub that has
        # to be chased every time the model grows one.
        from models import RollCall
        self.rc = RollCall("G")
        self.rc.save = lambda *a, **k: None
        self.mgr = patch.object(rc_svc, "manager").start()
        self.mgr.get_rollcalls.return_value = []
        self.mgr.add_rollcall.return_value = self.rc
        self.mgr.get_chat.return_value = {"timezone": "Asia/Kolkata"}
        patch.object(rc_svc, "log_admin_action", lambda *a, **k: None).start()
        self.addCleanup(patch.stopall)

    def _start(self, **kw):
        return _run(self.svc.start_rollcall(
            self.chat_id, "G", started_by_user_id=1,
            started_by_name="A", started_by_username="a", **kw))


class TestUnreadableCloseTimeIsRefused(_Base):
    def test_malformed_finalize_at_raises(self):
        for bad in ("2026-10-01T18:30:00ZZ", "tomorrow at 6pm", "   ", "18:30"):
            with self.subTest(bad=bad):
                with self.assertRaises(incorrectParameter) as ctx:
                    self._start(finalize_at=bad)
                # The message must name what it could not read, or the person
                # fixing it is guessing.
                self.assertIn(bad.strip() or bad, str(ctx.exception))
                self.assertIsNone(self.rc.finalizeDate)

    def test_unknown_weekday_raises(self):
        with self.assertRaises(incorrectParameter):
            self._start(event_day="Freitag", event_time="18:30")

    def test_malformed_time_raises(self):
        for bad in ("6.30pm", "half six", "18-30"):
            with self.subTest(bad=bad):
                with self.assertRaises(incorrectParameter):
                    self._start(event_day="Friday", event_time=bad)

    def test_out_of_range_time_raises_curated_not_500(self):
        """'25:99' parses as two fine ints and only blew up inside datetime(),
        escaping as a bare ValueError -> HTTP 500. It is bad input, not a bug."""
        with self.assertRaises(incorrectParameter):
            self._start(event_day="Friday", event_time="25:99")

    def test_half_a_weekly_slot_raises(self):
        """Previously these just failed the `and` and skipped the whole block."""
        with self.assertRaises(incorrectParameter):
            self._start(event_day="Friday")
        with self.assertRaises(incorrectParameter):
            self._start(event_time="18:30")


class TestValidInputIsUnchanged(_Base):
    def test_no_close_time_at_all_is_valid(self):
        """The regression that would matter most: an open-ended rollcall is a
        deliberate, supported thing and must not start raising."""
        self._start()
        self.assertIsNone(self.rc.finalizeDate)

    def test_iso_finalize_at_is_applied(self):
        self._start(finalize_at="2026-10-01T18:30:00Z")
        self.assertIsNotNone(self.rc.finalizeDate)
        self.assertEqual(self.rc.finalizeDate.tzinfo.zone, "Asia/Kolkata")

    def test_weekday_pair_is_applied(self):
        self._start(event_day="Friday", event_time="18:30")
        self.assertIsNotNone(self.rc.finalizeDate)
        self.assertEqual(self.rc.finalizeDate.weekday(), 4)
        self.assertEqual((self.rc.finalizeDate.hour, self.rc.finalizeDate.minute), (18, 30))

    def test_finalize_at_still_wins_over_the_weekday_pair(self):
        """Documented precedence — an exact one-off beats 'next Xday'. It must
        keep winning even when the pair alongside it is nonsense, or adding
        these guards would have quietly changed the contract."""
        self._start(finalize_at="2026-10-01T18:30:00Z", event_day="Freitag", event_time="99:99")
        self.assertIsNotNone(self.rc.finalizeDate)


class TestNothingIsLeftBehindWhenRefused(_Base):
    """A refusal must create nothing.

    manager.add_rollcall() writes a row AND appends to the chat cache, so
    validating after it would leave a phantom rollcall: open-ended, listed in
    /rollcalls, never closing — exactly the state these checks exist to
    prevent, reached by the checks themselves. The close time is therefore
    resolved before anything is created.
    """

    def test_no_rollcall_is_created_on_a_bad_close_time(self):
        for kw in ({"finalize_at": "tomorrow at 6pm"},
                   {"event_day": "Freitag", "event_time": "18:30"},
                   {"event_day": "Friday", "event_time": "25:99"},
                   {"event_day": "Friday"}):
            with self.subTest(**kw):
                self.mgr.add_rollcall.reset_mock()
                with self.assertRaises(incorrectParameter):
                    self._start(**kw)
                self.mgr.add_rollcall.assert_not_called()

    def test_a_good_close_time_still_creates_one(self):
        self.mgr.add_rollcall.reset_mock()
        self._start(event_day="Friday", event_time="18:30")
        self.mgr.add_rollcall.assert_called_once()


class TestWeekdayHelperContract(unittest.TestCase):
    """get_next_weekday_datetime promises None for anything unreadable. It kept
    that promise for a bad weekday and a bad split, but not for an in-shape,
    out-of-range time — which reached datetime() and raised."""

    def setUp(self):
        import pytz
        from functions import get_next_weekday_datetime
        self.fn = get_next_weekday_datetime
        self.tz = pytz.timezone("Asia/Kolkata")

    def test_returns_none_never_raises(self):
        for day, time_ in [("Freitag", "18:30"), ("Friday", "6.30pm"),
                           ("Friday", "25:99"), ("Friday", "12:60"),
                           ("Friday", "-1:00"), ("", "")]:
            with self.subTest(day=day, time=time_):
                self.assertIsNone(self.fn(self.tz, day, time_))

    def test_valid_input_still_resolves(self):
        got = self.fn(self.tz, "Friday", "18:30")
        self.assertIsNotNone(got)
        self.assertEqual(got.weekday(), 4)


if __name__ == "__main__":
    unittest.main()
