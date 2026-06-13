"""
Unit tests for the scheduled-template catch-up window in
rollCall/check_reminders.py::_is_due_now.

Production bug being prevented:
  Sunday 9am scheduled template silently skipped its weekly run. Root
  cause: the old loop used exact-minute string equality
  (now.strftime('%H:%M') == schedule_time). Any iteration that drifted
  past the target minute — event-loop pressure, slow telegram API in a
  previous iteration, a bot restart landing just after the schedule
  time — silently dropped the entire week's run.

  _is_due_now replaces that with a 30-minute catch-up window: fire if
  the chat clock has crossed the scheduled minute, haven't fired today,
  and the day-of-week (or day-of-month for monthly) matches.

  The dedupe on last_scheduled_date == today prevents firing twice
  within the same catch-up window.
"""

import unittest
from datetime import datetime, timedelta

# integration_tests/conftest.py puts rollCall/ on sys.path AND leaves
# check_reminders unmocked (only mocks the telegram surface), so the
# real _is_due_now / _parse_hhmm functions are imported here.
from check_reminders import _is_due_now, _parse_hhmm  # noqa: E402


class TestParseHHMM(unittest.TestCase):

    def test_zero_padded(self):
        self.assertEqual(_parse_hhmm("09:00"), (9, 0))

    def test_not_zero_padded(self):
        # User-typed "9:00" must also be accepted — the schedule_template
        # handler validates the time but stores it raw. We can't change
        # the stored format retroactively without a migration; accept both.
        self.assertEqual(_parse_hhmm("9:00"), (9, 0))

    def test_with_whitespace(self):
        self.assertEqual(_parse_hhmm("  09:30  "), (9, 30))

    def test_invalid_returns_none(self):
        self.assertIsNone(_parse_hhmm(""))
        self.assertIsNone(_parse_hhmm(None))
        self.assertIsNone(_parse_hhmm("garbage"))
        self.assertIsNone(_parse_hhmm("25:00"))
        self.assertIsNone(_parse_hhmm("09:99"))
        self.assertIsNone(_parse_hhmm("9"))


class TestIsDueNowWeekly(unittest.TestCase):
    """Each test runs with a deterministic clock that mimics the chat
    timezone (naive datetimes are fine — _is_due_now compares now to a
    timezone-stripped scheduled_dt because both come from the same source)."""

    # 2026-06-14 is a Sunday — used as the canonical schedule day below.
    SUNDAY_9AM     = datetime(2026, 6, 14, 9, 0, 0)
    SUNDAY_855AM   = datetime(2026, 6, 14, 8, 55, 0)
    SUNDAY_905AM   = datetime(2026, 6, 14, 9, 5, 0)
    SUNDAY_910AM   = datetime(2026, 6, 14, 9, 10, 0)
    SUNDAY_935AM   = datetime(2026, 6, 14, 9, 35, 0)  # past the 30-min window
    SUNDAY_1130AM  = datetime(2026, 6, 14, 11, 30, 0)
    SATURDAY_9AM   = datetime(2026, 6, 13, 9, 0, 0)

    def test_exactly_at_scheduled_time_fires(self):
        self.assertTrue(_is_due_now("09:00", "sunday", None, self.SUNDAY_9AM, "weekly"))

    def test_five_minutes_late_still_fires(self):
        """The original bug: loop drifted past the exact :00 second."""
        self.assertTrue(_is_due_now("09:00", "sunday", None, self.SUNDAY_905AM, "weekly"))

    def test_ten_minutes_late_still_fires(self):
        self.assertTrue(_is_due_now("09:00", "sunday", None, self.SUNDAY_910AM, "weekly"))

    def test_thirty_five_minutes_late_does_not_fire(self):
        """Outside the catch-up window — too stale to be useful."""
        self.assertFalse(_is_due_now("09:00", "sunday", None, self.SUNDAY_935AM, "weekly"))

    def test_too_early_does_not_fire(self):
        self.assertFalse(_is_due_now("09:00", "sunday", None, self.SUNDAY_855AM, "weekly"))

    def test_wrong_day_does_not_fire(self):
        self.assertFalse(_is_due_now("09:00", "sunday", None, self.SATURDAY_9AM, "weekly"))

    def test_already_fired_today_does_not_fire(self):
        """Dedupe — even within the catch-up window, never fire twice."""
        today = self.SUNDAY_9AM.strftime("%Y-%m-%d")
        self.assertFalse(_is_due_now("09:00", "sunday", today, self.SUNDAY_905AM, "weekly"))

    def test_not_zero_padded_time_still_fires(self):
        """User entered '9:00' instead of '09:00' — must still match."""
        self.assertTrue(_is_due_now("9:00", "sunday", None, self.SUNDAY_905AM, "weekly"))

    def test_case_insensitive_day(self):
        self.assertTrue(_is_due_now("09:00", "Sunday", None, self.SUNDAY_9AM, "weekly"))
        self.assertTrue(_is_due_now("09:00", "SUNDAY", None, self.SUNDAY_9AM, "weekly"))


class TestIsDueNowBiweekly(unittest.TestCase):

    SUNDAY_9AM = datetime(2026, 6, 14, 9, 5, 0)

    def test_fires_when_last_fire_was_14_days_ago(self):
        last = (self.SUNDAY_9AM - timedelta(days=14)).strftime("%Y-%m-%d")
        self.assertTrue(_is_due_now("09:00", "sunday", last, self.SUNDAY_9AM, "biweekly"))

    def test_does_not_fire_when_last_fire_was_only_7_days_ago(self):
        last = (self.SUNDAY_9AM - timedelta(days=7)).strftime("%Y-%m-%d")
        self.assertFalse(_is_due_now("09:00", "sunday", last, self.SUNDAY_9AM, "biweekly"))

    def test_fires_when_no_previous_fire(self):
        self.assertTrue(_is_due_now("09:00", "sunday", None, self.SUNDAY_9AM, "biweekly"))


class TestIsDueNowMonthly(unittest.TestCase):

    def test_fires_on_target_day_of_month(self):
        # 2026-06-15 is the 15th of a month
        now = datetime(2026, 6, 15, 9, 5, 0)
        self.assertTrue(_is_due_now("09:00", "15", None, now, "monthly"))

    def test_does_not_fire_on_wrong_day_of_month(self):
        now = datetime(2026, 6, 14, 9, 5, 0)
        self.assertFalse(_is_due_now("09:00", "15", None, now, "monthly"))

    def test_catchup_window_applies_to_monthly_too(self):
        now = datetime(2026, 6, 15, 9, 28, 0)  # within 30-min window
        self.assertTrue(_is_due_now("09:00", "15", None, now, "monthly"))


class TestAutoCloseUsesGteComparison(unittest.TestCase):
    """The auto-CLOSE path in check_reminders.check() must use `>=` against
    the rollcall's finalize_dt, NOT exact-minute equality. Auto-START's
    silently-skipped-on-drift bug must not have a sibling on the close
    side: if a rollcall is scheduled to close at 17:00 and the check loop
    iteration lands at 17:05 (or bot was down until 17:30), the rollcall
    must still end. The `>=` comparison gives this for free — exact
    equality (the old auto-start bug) would NOT.

    This is a source-inspection regression to lock in the invariant — the
    check() function is an infinite loop, harder to drive directly from a
    test, so we assert the invariant at the source level.
    """

    def test_check_uses_gte_for_finalize_dt(self):
        import inspect
        import check_reminders
        src = inspect.getsource(check_reminders.check)
        # Must use >= so a check loop iteration that lands AFTER the
        # finalize moment (drift, restart, slow previous iter) still ends
        # the rollcall.
        self.assertIn("now_date >= finalize_dt", src,
            "auto-close comparison must be >= so drift/restart can't skip "
            "overdue rollcalls")
        # Must NOT use exact equality on the formatted time string — that
        # would silently miss minutes the same way auto-START did before
        # b8ee99c.
        self.assertNotIn('now_date_string == finalize_dt', src)

    def test_check_uses_gte_for_reminder_time(self):
        import inspect
        import check_reminders
        src = inspect.getsource(check_reminders.check)
        # The pre-close reminder ("event is N hours away") must also use
        # >= so it doesn't silently skip when the loop drifts past the
        # exact reminder minute.
        self.assertIn("now_date >= reminder_time", src)


class TestRegressionMissedSundayRollcall(unittest.TestCase):
    """The exact production scenario reported: Sunday 9am scheduled, the
    loop drifted past the minute, and the schedule was silently skipped."""

    def test_sunday_9am_fires_even_when_loop_iteration_lands_at_905(self):
        """Loop hit the iteration at 9:05 instead of 9:00 (drift, restart,
        slow previous iteration). New catch-up window catches it."""
        now = datetime(2026, 6, 14, 9, 5, 30)  # Sunday 09:05:30
        self.assertTrue(_is_due_now("09:00", "sunday", None, now, "weekly"))

    def test_old_exact_minute_behavior_would_have_failed(self):
        """Document the regression: old code did
            now.strftime('%H:%M') == schedule_time
        which evaluates False for 09:05 vs 09:00. Confirm we no longer
        rely on that comparison."""
        now = datetime(2026, 6, 14, 9, 5, 0)
        # Old logic equivalent — would have skipped:
        self.assertNotEqual(now.strftime("%H:%M"), "09:00")
        # New logic — fires anyway thanks to the catch-up window:
        self.assertTrue(_is_due_now("09:00", "sunday", None, now, "weekly"))


if __name__ == "__main__":
    unittest.main()
