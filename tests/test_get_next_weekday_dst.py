"""
Regression test: functions.get_next_weekday_datetime must not carry a stale
UTC offset across a DST boundary. Adding a timedelta directly to an
already-localized pytz datetime keeps the original date's offset — correct
only when the target date is in the same DST regime as "now". Whenever the
day-arithmetic crosses a DST transition, the result was off by the DST
delta (typically 1 hour) even though the displayed wall-clock hour:minute
looked unchanged. Fixed by doing the day-arithmetic on a naive datetime and
localizing fresh for the landing date.
"""

import sys
import os
import datetime as _dt
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import pytz


class _FixedNow(_dt.datetime):
    """Subclasses the real datetime so the constructor keeps working
    normally; only .now() is overridden to return a fixed instant."""
    _fixed = None

    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return cls._fixed.astimezone(tz)
        return cls._fixed


class TestGetNextWeekdayDatetimeDST(unittest.TestCase):

    def test_crossing_spring_forward_gets_correct_offset(self):
        import functions

        ny = pytz.timezone("America/New_York")
        # Thursday, 2026-03-05, 09:00 EST — three days before "now" the
        # next Sunday (2026-03-08) is the US spring-forward DST transition.
        _FixedNow._fixed = ny.localize(_dt.datetime(2026, 3, 5, 9, 0))

        with patch.object(functions, "datetime", _FixedNow):
            candidate = functions.get_next_weekday_datetime(ny, "sunday", "09:00")

        self.assertEqual((candidate.year, candidate.month, candidate.day), (2026, 3, 8))
        self.assertEqual((candidate.hour, candidate.minute), (9, 0))
        # Must carry the correct EDT offset for March 8, not the stale EST
        # offset from March 5.
        self.assertEqual(candidate.utcoffset(), _dt.timedelta(hours=-4))

    def test_same_dst_regime_unaffected(self):
        """Sanity check: when the target date doesn't cross a DST
        boundary, behavior is unchanged (both old and new code agree)."""
        import functions

        ny = pytz.timezone("America/New_York")
        # Thursday, 2026-01-08 — next Friday (Jan 9) is deep in EST, no
        # DST transition anywhere nearby.
        _FixedNow._fixed = ny.localize(_dt.datetime(2026, 1, 8, 9, 0))

        with patch.object(functions, "datetime", _FixedNow):
            candidate = functions.get_next_weekday_datetime(ny, "friday", "09:00")

        self.assertEqual((candidate.year, candidate.month, candidate.day), (2026, 1, 9))
        self.assertEqual(candidate.utcoffset(), _dt.timedelta(hours=-5))


if __name__ == "__main__":
    unittest.main()
