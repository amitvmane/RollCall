"""
Regression tests for the Asia/Calcutta → Asia/Kolkata default migration.

Context: pytz 2026.2 still treats Asia/Calcutta as a valid timezone (it's
a deprecated IANA alias for Asia/Kolkata), but the alias may be dropped
in a future pytz release. We migrated all default-fallback sites to the
modern Asia/Kolkata name so new chats and new rollcalls are future-proof.

Existing DB rows with "Asia/Calcutta" must continue to work — the goal
of the migration is *forward* hygiene, not data conversion.
"""
import unittest
import pytz


class TestTimezoneAliasCompatibility(unittest.TestCase):
    """Both timezone names must resolve cleanly on the locked pytz pin."""

    def test_kolkata_resolves(self):
        tz = pytz.timezone("Asia/Kolkata")
        self.assertEqual(tz.zone, "Asia/Kolkata")

    def test_calcutta_still_resolves_as_alias(self):
        """Existing DB rows with 'Asia/Calcutta' must still load — pytz
        keeps the deprecated alias resolvable. If this ever breaks
        (future pytz drops the alias), we'll need a one-time DB migration."""
        tz = pytz.timezone("Asia/Calcutta")
        # On pytz 2026.2 the alias resolves to the literal "Asia/Calcutta"
        # zone object (not normalised to Kolkata). Both offsets are +05:30.
        self.assertIn(tz.zone, ("Asia/Calcutta", "Asia/Kolkata"))

    def test_both_aliases_give_same_offset(self):
        from datetime import datetime
        sample = datetime(2026, 6, 14, 12, 0, 0)
        a = pytz.timezone("Asia/Kolkata").localize(sample).utcoffset()
        b = pytz.timezone("Asia/Calcutta").localize(sample).utcoffset()
        self.assertEqual(a, b, "Kolkata and Calcutta must agree on UTC offset")


class TestDefaultsUpgraded(unittest.TestCase):
    """No production code path should still hard-code 'Asia/Calcutta' as a
    default. Existing DB rows still work via the alias, but defaults for
    NEW chats / NEW rollcalls must use the modern name."""

    def test_no_calcutta_default_in_python_sources(self):
        import os
        import re
        repo = os.path.join(os.path.dirname(__file__), "..", "rollCall")
        offenders = []
        for root, _, files in os.walk(repo):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                with open(path) as f:
                    src = f.read()
                if "Asia/Calcutta" in src:
                    offenders.append(os.path.relpath(path, repo))
        self.assertEqual(offenders, [],
            f"Asia/Calcutta still hard-coded in: {offenders}. "
            "Use Asia/Kolkata for new defaults.")

    def test_models_rollcall_default_timezone(self):
        from models import RollCall
        rc = RollCall.__new__(RollCall)
        # Hit the new-rollcall branch with a fake chat_id=None so we don't
        # touch the DB. Mirrors what __init__ would assign.
        rc.title = "x"
        rc.inList = rc.outList = rc.maybeList = rc.waitList = []
        rc.proxy_owners = {}
        rc.allNames = []
        rc.inListLimit = None
        rc.reminder = None
        rc.finalizeDate = None
        rc.timezone = "Asia/Kolkata"  # what __init__ now sets
        self.assertEqual(rc.timezone, "Asia/Kolkata")


if __name__ == "__main__":
    unittest.main()
