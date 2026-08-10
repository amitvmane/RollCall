"""
Unit tests for the repeat= shortcut (handlers/lifecycle.py) and the
recurrence-matching helpers backing /calendar (check_reminders.py).

check_reminders is globally mocked in tests/ (see conftest.py) — pure
functions from it are loaded via the same importlib bypass pattern
tests/test_bug_fixes.py already uses (_load_real_module).
"""
import os
import sys
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytz

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _load_real_check_reminders():
    import importlib.util
    module_path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "check_reminders.py")
    spec = importlib.util.spec_from_file_location("_real_check_reminders_rc", module_path)
    mod = importlib.util.module_from_spec(spec)
    mod_deps = dict(sys.modules)
    mod_deps["telebot"] = MagicMock()
    mod_deps["telebot.async_telebot"] = MagicMock()
    mod_deps["config"] = sys.modules["config"]
    mod_deps["db"] = sys.modules["db"]
    with patch.dict("sys.modules", mod_deps):
        spec.loader.exec_module(mod)
    return mod


class TestMatchesRecurrence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_real_check_reminders()

    def test_weekly_matches_correct_weekday_only(self):
        monday = date(2026, 8, 10)  # confirmed Monday
        tuesday = date(2026, 8, 11)
        self.assertTrue(self.mod._matches_recurrence(monday, "monday", "weekly", None))
        self.assertFalse(self.mod._matches_recurrence(tuesday, "monday", "weekly", None))

    def test_daily_always_matches(self):
        self.assertTrue(self.mod._matches_recurrence(date(2026, 8, 10), None, "daily", None))
        self.assertTrue(self.mod._matches_recurrence(date(2026, 12, 25), None, "daily", "2026-08-01"))

    def test_biweekly_never_fired_matches_next_weekday_like_weekly(self):
        """No last_scheduled_date yet -> behaves like weekly until it fires once."""
        monday = date(2026, 8, 10)
        self.assertTrue(self.mod._matches_recurrence(monday, "monday", "biweekly", None))

    def test_biweekly_recently_fired_blocks_within_14_days(self):
        last_fired = "2026-08-03"  # a Monday
        next_monday = date(2026, 8, 10)  # 7 days later -- too soon
        self.assertFalse(self.mod._matches_recurrence(next_monday, "monday", "biweekly", last_fired))

    def test_biweekly_fires_again_after_14_days(self):
        last_fired = "2026-08-03"
        two_weeks_later = date(2026, 8, 17)  # 14 days later
        self.assertTrue(self.mod._matches_recurrence(two_weeks_later, "monday", "biweekly", last_fired))

    def test_monthly_matches_target_day(self):
        self.assertTrue(self.mod._matches_recurrence(date(2026, 8, 15), "15", "monthly", None))
        self.assertFalse(self.mod._matches_recurrence(date(2026, 8, 16), "15", "monthly", None))

    def test_monthly_clamps_to_last_day_of_short_month(self):
        """schedule_day=31 in a 30-day month fires on the 30th, not skipped."""
        self.assertTrue(self.mod._matches_recurrence(date(2026, 9, 30), "31", "monthly", None))
        self.assertFalse(self.mod._matches_recurrence(date(2026, 9, 29), "31", "monthly", None))

    def test_monthly_february_clamp(self):
        # 2026 is not a leap year -> Feb has 28 days.
        self.assertTrue(self.mod._matches_recurrence(date(2026, 2, 28), "31", "monthly", None))

    def test_is_due_now_still_delegates_correctly(self):
        """Regression: refactoring _is_due_now to call _matches_recurrence
        must not change its externally-observed behavior."""
        tz = pytz.timezone("Asia/Kolkata")
        now = tz.localize(datetime(2026, 8, 10, 18, 5))  # Monday, just past 18:00
        self.assertTrue(self.mod._is_due_now("18:00", "monday", None, now, "weekly"))
        self.assertFalse(self.mod._is_due_now("18:00", "tuesday", None, now, "weekly"))
        # Already fired today -> never again same day.
        self.assertFalse(self.mod._is_due_now("18:00", "monday", "2026-08-10", now, "weekly"))


class TestNextOccurrenceDatetime(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_real_check_reminders()
        cls.tz = pytz.timezone("Asia/Kolkata")

    def test_weekly_same_day_future_time_returns_today(self):
        # Freeze "now" via a fixed reference isn't available without patching
        # datetime.now inside the module, so instead assert structurally:
        # the result must be a Monday at 09:00 in the given tz.
        with patch.object(self.mod, "datetime") as mock_dt:
            mock_dt.now.return_value = self.tz.localize(datetime(2026, 8, 10, 8, 0))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = self.mod.next_occurrence_datetime(self.tz, "09:00", "monday", "weekly")
        self.assertEqual(result.date(), date(2026, 8, 10))
        self.assertEqual((result.hour, result.minute), (9, 0))

    def test_weekly_time_already_passed_rolls_to_next_week(self):
        with patch.object(self.mod, "datetime") as mock_dt:
            mock_dt.now.return_value = self.tz.localize(datetime(2026, 8, 10, 10, 0))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = self.mod.next_occurrence_datetime(self.tz, "09:00", "monday", "weekly")
        self.assertEqual(result.date(), date(2026, 8, 17))

    def test_monthly_next_occurrence_respects_clamp(self):
        with patch.object(self.mod, "datetime") as mock_dt:
            mock_dt.now.return_value = self.tz.localize(datetime(2026, 9, 1, 0, 0))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = self.mod.next_occurrence_datetime(self.tz, "10:00", "31", "monthly")
        self.assertEqual(result.date(), date(2026, 9, 30))

    def test_malformed_time_returns_none(self):
        result = self.mod.next_occurrence_datetime(self.tz, "not-a-time", "monday", "weekly")
        self.assertIsNone(result)


class TestExtractRepeatFlag(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import handlers.lifecycle as lc
        cls.lc = lc

    def test_no_flag_present(self):
        tokens, repeat_type = self.lc._extract_repeat_flag(["Friday", "Football"])
        self.assertEqual(tokens, ["Friday", "Football"])
        self.assertIsNone(repeat_type)

    def test_trailing_flag_stripped(self):
        tokens, repeat_type = self.lc._extract_repeat_flag(["Friday", "Football", "repeat=weekly"])
        self.assertEqual(tokens, ["Friday", "Football"])
        self.assertEqual(repeat_type, "weekly")

    def test_flag_is_case_insensitive_and_lowercased(self):
        tokens, repeat_type = self.lc._extract_repeat_flag(["Title", "REPEAT=Weekly"])
        self.assertEqual(tokens, ["Title"])
        self.assertEqual(repeat_type, "weekly")

    def test_flag_anywhere_in_tokens(self):
        tokens, repeat_type = self.lc._extract_repeat_flag(["repeat=monthly", "Friday", "Football"])
        self.assertEqual(tokens, ["Friday", "Football"])
        self.assertEqual(repeat_type, "monthly")

    def test_empty_tokens(self):
        tokens, repeat_type = self.lc._extract_repeat_flag([])
        self.assertEqual(tokens, [])
        self.assertIsNone(repeat_type)

    def test_invalid_repeat_type_is_still_extracted_unvalidated(self):
        """Validation is the caller's job (fail before creating anything) —
        extraction itself doesn't reject bad values."""
        tokens, repeat_type = self.lc._extract_repeat_flag(["Title", "repeat=fortnightly"])
        self.assertEqual(repeat_type, "fortnightly")


class TestApplyRepeatFlag(unittest.IsolatedAsyncioTestCase):

    async def test_upserts_template_and_schedules_weekly(self):
        import handlers.lifecycle as lc

        rc = MagicMock()
        rc.title = "Friday Football"
        rc.timezone = "Asia/Kolkata"
        rc.inListLimit = 20
        rc.location = "Turf"
        rc.event_fee = "500"

        with patch("services.templates.upsert_template") as mock_upsert, \
             patch("services.templates.set_schedule") as mock_set_sched:
            result = await lc._apply_repeat_flag(-100, rc, "weekly", 1, "Admin")

        mock_upsert.assert_called_once()
        self.assertEqual(mock_upsert.call_args.args[:2], (-100, "friday-football"))
        self.assertEqual(mock_upsert.call_args.kwargs["title"], "Friday Football")
        self.assertEqual(mock_upsert.call_args.kwargs["limit"], 20)
        self.assertEqual(mock_upsert.call_args.kwargs["location"], "Turf")
        self.assertEqual(mock_upsert.call_args.kwargs["fee"], "500")

        mock_set_sched.assert_called_once()
        self.assertEqual(mock_set_sched.call_args.kwargs["recurrence_type"], "weekly")
        self.assertIn("schedule_day", mock_set_sched.call_args.kwargs)
        self.assertIn("schedule_time", mock_set_sched.call_args.kwargs)
        self.assertNotIn("monthly_day", mock_set_sched.call_args.kwargs)

        self.assertIsNotNone(result)
        self.assertIn("Repeats", result)

    async def test_monthly_passes_monthly_day_not_schedule_day(self):
        import handlers.lifecycle as lc

        rc = MagicMock()
        rc.title = "Monthly Meetup"
        rc.timezone = "Asia/Kolkata"
        rc.inListLimit = None
        rc.location = None
        rc.event_fee = None

        with patch("services.templates.upsert_template"), \
             patch("services.templates.set_schedule") as mock_set_sched:
            await lc._apply_repeat_flag(-100, rc, "monthly", 1, "Admin")

        self.assertIn("monthly_day", mock_set_sched.call_args.kwargs)
        self.assertNotIn("schedule_day", mock_set_sched.call_args.kwargs)

    async def test_title_slugified_for_template_name(self):
        import handlers.lifecycle as lc

        rc = MagicMock()
        rc.title = "  Friday's BIG Football!! Match  "
        rc.timezone = "Asia/Kolkata"
        rc.inListLimit = None
        rc.location = None
        rc.event_fee = None

        with patch("services.templates.upsert_template") as mock_upsert, \
             patch("services.templates.set_schedule"):
            await lc._apply_repeat_flag(-100, rc, "weekly", 1, "Admin")

        slug = mock_upsert.call_args.args[1]
        self.assertRegex(slug, r"^[a-z0-9-]+$")
        self.assertNotIn("--", slug)

    async def test_scheduling_failure_is_best_effort_returns_none(self):
        """A scheduling failure must not raise -- the rollcall itself was
        already created and announced by the time this runs."""
        import handlers.lifecycle as lc

        rc = MagicMock()
        rc.title = "Friday Football"
        rc.timezone = "Asia/Kolkata"
        rc.inListLimit = None
        rc.location = None
        rc.event_fee = None

        with patch("services.templates.upsert_template", side_effect=RuntimeError("db down")):
            result = await lc._apply_repeat_flag(-100, rc, "weekly", 1, "Admin")

        self.assertIsNone(result)

    async def test_bad_timezone_falls_back_to_kolkata(self):
        import handlers.lifecycle as lc

        rc = MagicMock()
        rc.title = "Friday Football"
        rc.timezone = "Not/A/Real/Zone"
        rc.inListLimit = None
        rc.location = None
        rc.event_fee = None

        with patch("services.templates.upsert_template"), \
             patch("services.templates.set_schedule"):
            result = await lc._apply_repeat_flag(-100, rc, "weekly", 1, "Admin")

        self.assertIsNotNone(result)
        self.assertIn("Asia/Kolkata", result)


if __name__ == "__main__":
    unittest.main()
