"""Downtime measurement: the bot's own record of how long it was gone.

The watchdog can only report outages it was awake for. When the host loses
power or its network, the watchdog container goes down with the bot and the
outage leaves no trace anywhere. `uptime.py` is the second observer — a
heartbeat stamp in the database, compared against the clock on the next boot.

These tests pin the properties that make that record trustworthy:
  - a first-ever boot is not an outage
  - a backwards clock is not an outage
  - the log is bounded (it is one rewritten row, so it can only be bounded here)
  - availability is measured over the window actually observed, never implied
  - routine restarts are recorded but not announced
"""

import importlib
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class _FakeConfig:
    """Stand-in for the system_config key/value table."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value


def _load_uptime(fake, now, min_minutes="5"):
    """Import a fresh uptime module bound to `fake` with PROCESS_STARTED_AT=now.

    Reimported per test because DOWNTIME_MIN_SECONDS and PROCESS_STARTED_AT are
    both resolved at import — which is the right design for the real process
    (one boot, one measurement) and simply means tests re-import.
    """
    import db as _db
    with patch.dict(os.environ, {"DOWNTIME_MIN_MINUTES": min_minutes}):
        sys.modules.pop("uptime", None)
        import uptime as u
        importlib.reload(u)
    u.PROCESS_STARTED_AT = now
    _db.get_system_config.side_effect = fake.get
    _db.set_system_config.side_effect = fake.set
    return u


class TestFormatDuration(unittest.TestCase):
    def test_two_units_and_no_false_precision(self):
        fake = _FakeConfig()
        u = _load_uptime(fake, 1_000_000.0)
        cases = {
            0: "0s", 5: "5s", 59: "59s",
            60: "1m", 95: "1m", 3599: "59m",
            3600: "1h", 3720: "1h 2m",
            86400: "1d", 90000: "1d 1h", 172800: "2d",
        }
        for seconds, expected in cases.items():
            self.assertEqual(u.format_duration(seconds), expected, seconds)

    def test_unknown_and_negative_never_crash(self):
        u = _load_uptime(_FakeConfig(), 1_000_000.0)
        self.assertEqual(u.format_duration(None), "unknown")
        self.assertEqual(u.format_duration(-5), "0s")

    def test_matches_the_watchdog_shell_formatter(self):
        """Same figure, same reader, same wording.

        There are deliberately two copies of this formatter — Python for the
        bot, POSIX sh for the watchdog container, which has no Python — so the
        only thing keeping them honest is running both and comparing. The
        alternative to the duplication is a Python dependency in the one
        component whose job is to work when the bot does not.
        """
        import re
        import shutil
        import subprocess

        sh = shutil.which("sh")
        if not sh:  # pragma: no cover - POSIX only
            self.skipTest("no POSIX shell available")

        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "watchdog.sh")
        with open(path) as fh:
            source = fh.read()
        match = re.search(r"^human_duration\(\) \{.*?^\}", source, re.M | re.S)
        self.assertIsNotNone(match, "human_duration() missing from watchdog.sh")

        probes = [0, 5, 59, 60, 95, 3599, 3600, 3720, 86399, 86400, 90000, 172800]
        script = match.group(0) + "\n" + "\n".join(
            f'human_duration {v}' for v in probes
        )
        out = subprocess.run([sh, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

        u = _load_uptime(_FakeConfig(), 1_000_000.0)
        shell_values = out.stdout.splitlines()  # values contain spaces ('1h 2m')
        self.assertEqual(len(shell_values), len(probes))
        for probe, from_shell in zip(probes, shell_values):
            self.assertEqual(u.format_duration(probe), from_shell,
                             f"formatters disagree at {probe}s")


class TestBootGap(unittest.TestCase):
    def test_first_ever_boot_is_not_an_outage(self):
        fake = _FakeConfig()
        u = _load_uptime(fake, 1_000_000.0)
        self.assertIsNone(u.record_boot_gap())
        self.assertEqual(u.read_log(), [])
        # ...but tracking now starts, so the next boot can be measured.
        self.assertIsNotNone(fake.get(u.SINCE_KEY))

    def test_gap_is_measured_logged_and_flagged_for_announcement(self):
        fake = _FakeConfig()
        fake.set("uptime_heartbeat", "1000000")
        u = _load_uptime(fake, 1_000_000.0 + 3600)

        gap = u.record_boot_gap()
        self.assertEqual(gap["sec"], 3600)
        self.assertTrue(gap["announce"])
        self.assertEqual(u.boot_gap()["sec"], 3600)

        log = json.loads(fake.get(u.DOWNTIME_KEY))
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["sec"], 3600)
        # "announce" is a decision for this boot only — it must not be
        # persisted, or a later threshold change would rewrite history.
        self.assertNotIn("announce", log[0])

    def test_routine_restart_is_recorded_but_not_announced(self):
        """A `make build` deploy is a ~30s gap that happened on purpose.
        Paging for those trains the operator to ignore the channel that also
        carries real outages — so it is logged, and silent."""
        fake = _FakeConfig()
        fake.set("uptime_heartbeat", "1000000")
        u = _load_uptime(fake, 1_000_000.0 + 40)

        gap = u.record_boot_gap()
        self.assertEqual(gap["sec"], 40)
        self.assertFalse(gap["announce"])
        self.assertEqual(len(json.loads(fake.get(u.DOWNTIME_KEY))), 1)

    def test_threshold_is_configurable(self):
        fake = _FakeConfig()
        fake.set("uptime_heartbeat", "1000000")
        u = _load_uptime(fake, 1_000_000.0 + 120, min_minutes="1")
        self.assertTrue(u.record_boot_gap()["announce"])

    def test_clock_moving_backwards_is_not_an_outage(self):
        """An NTP step or a restore of an older database leaves a stamp in the
        future. Recording that as a negative outage would poison every later
        average, so it is dropped outright."""
        fake = _FakeConfig()
        fake.set("uptime_heartbeat", "1000500")
        u = _load_uptime(fake, 1_000_000.0)
        self.assertIsNone(u.record_boot_gap())
        self.assertEqual(u.read_log(), [])

    def test_unreadable_log_is_treated_as_empty_not_fatal(self):
        fake = _FakeConfig()
        fake.set("uptime_heartbeat", "1000000")
        fake.set("uptime_downtime", "{not json")
        u = _load_uptime(fake, 1_000_000.0 + 3600)
        self.assertEqual(u.read_log(), [])
        self.assertEqual(u.record_boot_gap()["sec"], 3600)

    def test_a_failed_stamp_read_never_blocks_the_boot(self):
        import db as _db
        fake = _FakeConfig()
        u = _load_uptime(fake, 1_000_000.0)
        _db.get_system_config.side_effect = RuntimeError("db down")
        self.assertIsNone(u.record_boot_gap())


class TestLogIsBounded(unittest.TestCase):
    def test_log_never_exceeds_max(self):
        """One row, rewritten in place — nothing else can bound it. Same
        unbounded-growth class the 2026-09-06 audit fixed in four other spots."""
        fake = _FakeConfig()
        for i in range(u_max := 120):
            fake.set("uptime_heartbeat", str(1_000_000 + i * 10_000))
            u = _load_uptime(fake, 1_000_000.0 + i * 10_000 + 3600)
            u.record_boot_gap()

        log = json.loads(fake.get("uptime_downtime"))
        self.assertLessEqual(len(log), u._MAX_LOG)
        self.assertEqual(len(log), u._MAX_LOG)
        self.assertLess(u_max, 10_000)  # sanity: the loop really did overflow it


class TestSummary(unittest.TestCase):
    def _summary_with(self, entries, now, since):
        fake = _FakeConfig()
        fake.set("uptime_downtime", json.dumps(entries))
        fake.set("uptime_since", str(int(since)))
        u = _load_uptime(fake, now)
        with patch("uptime.time.time", return_value=now):
            return u, u.summary(days=30)

    def test_window_is_what_was_observed_not_what_was_asked_for(self):
        """Reporting 100% over 30 days when the feature has been live for two
        is a lie told by arithmetic."""
        now = 2_000_000.0
        two_days = 2 * 86400
        _, s = self._summary_with([], now, since=now - two_days)
        self.assertAlmostEqual(s["window_sec"], two_days, delta=1)
        self.assertEqual(s["availability"], 100.0)

    def test_downtime_reduces_availability(self):
        now = 2_000_000.0
        window = 10 * 86400
        entries = [{"from": now - 3600, "to": now, "sec": 3600}]
        _, s = self._summary_with(entries, now, since=now - window)
        self.assertEqual(s["outages"], 1)
        self.assertEqual(s["downtime_sec"], 3600)
        self.assertAlmostEqual(s["availability"], 100 * (1 - 3600 / window), places=4)

    def test_outages_outside_the_window_are_excluded(self):
        now = 2_000_000.0
        ancient = now - 60 * 86400
        entries = [{"from": ancient, "to": ancient + 3600, "sec": 3600}]
        _, s = self._summary_with(entries, now, since=now - 90 * 86400)
        self.assertEqual(s["outages"], 0)
        self.assertEqual(s["downtime_sec"], 0)

    def test_outage_straddling_the_window_edge_counts_only_its_tail(self):
        now = 2_000_000.0
        edge = now - 30 * 86400
        # Two hours long, but only the last hour falls inside the 30d window.
        entries = [{"from": edge - 3600, "to": edge + 3600, "sec": 7200}]
        _, s = self._summary_with(entries, now, since=now - 90 * 86400)
        self.assertEqual(s["downtime_sec"], 3600)

    def test_availability_is_clamped_to_a_sane_range(self):
        now = 2_000_000.0
        entries = [{"from": now - 10 ** 7, "to": now, "sec": 10 ** 7}]
        _, s = self._summary_with(entries, now, since=now - 86400)
        self.assertGreaterEqual(s["availability"], 0.0)
        self.assertLessEqual(s["availability"], 100.0)

    def test_malformed_entries_are_skipped_not_fatal(self):
        now = 2_000_000.0
        entries = [{"to": "nonsense", "sec": "x"}, {"from": now - 60, "to": now, "sec": 60}]
        _, s = self._summary_with(entries, now, since=now - 86400)
        self.assertEqual(s["downtime_sec"], 60)


class TestHeartbeatIsWired(unittest.TestCase):
    def test_the_minute_tick_writes_a_heartbeat(self):
        """The stamp is only as good as the loop that writes it. If the tick
        stops calling beat(), every later gap silently measures the wrong
        thing — so assert the call site, not just the function."""
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "check_reminders.py")
        with open(path) as fh:
            body = fh.read()
        self.assertIn("_uptime.beat()", body)

    def test_beat_writes_the_key_record_boot_gap_reads(self):
        fake = _FakeConfig()
        u = _load_uptime(fake, 1_000_000.0)
        with patch("uptime.time.time", return_value=1_234_567.0):
            u.beat()
        self.assertEqual(fake.get(u.HEARTBEAT_KEY), "1234567")

    def test_announcement_waits_for_telegram_not_for_boot(self):
        """A long gap is usually a network outage — announcing it at boot
        sends it into the void. It has to go out on reconnect."""
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "runner.py")
        with open(path) as fh:
            body = fh.read()
        self.assertIn("async def _announce_downtime", body)
        setup = body.split("async def _post_connect_setup")[1]
        self.assertIn("_announce_downtime()", setup.split("\nasync def")[0])


if __name__ == "__main__":
    unittest.main()
