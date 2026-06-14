"""
Regression tests for _ensure_aware's DST cutover handling.

The bot defaults to Asia/Kolkata where DST has never been observed
(India dropped DST in 1945), so these edge cases don't fire for the
primary user base. But chats can /timezone to anywhere, and the
previous code silently fired ambiguous times at the LATER occurrence
of the two valid wall-time interpretations — which is off by an hour
from a user's natural reading of "the rollcall starts at 1:30am".

Uses America/New_York which has both fall-back (early November) and
spring-forward (early March) cutovers, both well-documented in the
IANA TZ database.
"""
import unittest
from datetime import datetime

import pytz

from check_reminders import _ensure_aware


class TestEnsureAwareUnambiguous(unittest.TestCase):
    """Unambiguous times: 99.99% of calls. No DST cutover involved."""

    def test_naive_localizes_to_aware(self):
        tz = pytz.timezone("America/New_York")
        naive = datetime(2026, 6, 14, 12, 0, 0)
        aware = _ensure_aware(naive, tz)
        self.assertIsNotNone(aware.tzinfo)
        self.assertEqual(aware.hour, 12)

    def test_already_aware_returns_unchanged(self):
        tz = pytz.timezone("America/New_York")
        aware_in = tz.localize(datetime(2026, 6, 14, 12, 0, 0))
        aware_out = _ensure_aware(aware_in, tz)
        self.assertEqual(aware_in, aware_out)

    def test_none_returns_none(self):
        tz = pytz.timezone("America/New_York")
        self.assertIsNone(_ensure_aware(None, tz))

    def test_kolkata_no_dst_always_unambiguous(self):
        """India has no DST. Every naive time localizes uniquely."""
        tz = pytz.timezone("Asia/Kolkata")
        # Pick a time that would be ambiguous in NYC (1:30am on fall-back).
        # In Kolkata it's just an ordinary minute.
        naive = datetime(2026, 11, 1, 1, 30, 0)
        aware = _ensure_aware(naive, tz)
        self.assertEqual(aware.hour, 1)
        self.assertEqual(aware.minute, 30)


class TestEnsureAwareDSTFallback(unittest.TestCase):
    """Fall-back cutover (November in US/EU): the local 1:30am occurs
    twice. The old code defaulted to the SECOND (standard-time)
    occurrence; the new code picks the FIRST (still daylight-time),
    matching user expectation of "the 1:30am closest to midnight"."""

    # 2026-11-01: US clocks fall back from 2:00 EDT (UTC-4) to 1:00 EST (UTC-5).
    # 1:30am on that morning corresponds to both 5:30 UTC (first occurrence,
    # still EDT) and 6:30 UTC (second occurrence, now EST).
    AMBIGUOUS_TIME = datetime(2026, 11, 1, 1, 30, 0)

    def test_ambiguous_time_picks_earlier_occurrence(self):
        tz = pytz.timezone("America/New_York")
        aware = _ensure_aware(self.AMBIGUOUS_TIME, tz)
        # The EARLIER occurrence is in DST (EDT, UTC-4): 1:30am EDT = 5:30 UTC.
        utc = aware.astimezone(pytz.UTC)
        self.assertEqual(utc.hour, 5, "ambiguous fall-back must pick EARLIER (EDT) occurrence, not LATER (EST)")
        self.assertEqual(utc.minute, 30)

    def test_ambiguous_time_does_not_raise(self):
        tz = pytz.timezone("America/New_York")
        # Must not propagate AmbiguousTimeError.
        try:
            _ensure_aware(self.AMBIGUOUS_TIME, tz)
        except pytz.AmbiguousTimeError:
            self.fail("_ensure_aware must handle AmbiguousTimeError, not propagate it")


class TestEnsureAwareDSTSpringForward(unittest.TestCase):
    """Spring-forward (March): the local 2:30am never exists because
    clocks jump from 2:00 EST to 3:00 EDT. Old behavior: bare-except
    fallback to is_dst=False — silently picked "would-have-been EST"
    interpretation. New behavior: same numerical fallback BUT with an
    explicit warning logged so operators can see it happened."""

    # 2026-03-08: US clocks spring forward — 2:00 EST → 3:00 EDT.
    # 2:30am EST/EDT on that morning never occurred.
    NONEXISTENT_TIME = datetime(2026, 3, 8, 2, 30, 0)

    def test_nonexistent_time_does_not_raise(self):
        tz = pytz.timezone("America/New_York")
        try:
            aware = _ensure_aware(self.NONEXISTENT_TIME, tz)
        except pytz.NonExistentTimeError:
            self.fail("_ensure_aware must handle NonExistentTimeError, not propagate it")
        self.assertIsNotNone(aware.tzinfo)


if __name__ == "__main__":
    unittest.main()
