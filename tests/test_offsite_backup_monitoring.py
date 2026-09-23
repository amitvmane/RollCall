"""The off-site copy is monitored, not just the container that makes it.

`backup_freshness()` answers "is a backup being taken". Nothing answered "is
it getting off this machine" — the sync sidecar ran `rclone copy … || true`,
so a remote that had been rejecting writes for weeks logged into a container
nobody reads and otherwise looked identical to one that worked, while
`make status` showed a green tick for the container merely being up.

That is the shape of the sidecar which sat dead from 2026-08-03 to
2026-08-24, and it matters more here: this is the copy that survives losing
the machine, and it holds an append-only financial ledger.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.stamp = os.path.join(self.dir, "last-success")

    def _freshness(self, remote="gdrive:rollcall", age_hours=None, write=True):
        import backup_status
        if write:
            with open(self.stamp, "w") as fh:
                fh.write("2026-09-23T11:00:00Z")
            if age_hours is not None:
                old = time.time() - age_hours * 3600
                os.utime(self.stamp, (old, old))
        env = {"BACKUP_SYNC_STATE_DIR": self.dir}
        env["RCLONE_REMOTE"] = remote
        with patch.dict(os.environ, env, clear=False):
            if not remote:
                os.environ["RCLONE_REMOTE"] = ""
            return backup_status.remote_sync_freshness()


class TestRemoteSyncFreshness(_Base):
    def test_recent_success_is_ok(self):
        r = self._freshness(age_hours=1)
        self.assertEqual(r["status"], "OK")
        self.assertIn("ok", r["label"])

    def test_old_success_is_stale(self):
        r = self._freshness(age_hours=48)
        self.assertEqual(r["status"], "STALE")
        self.assertGreater(r["age_hours"], 24)

    def test_never_succeeded_is_missing_not_ok(self):
        """The dangerous case: sidecar up, copies never landing."""
        r = self._freshness(write=False)
        self.assertEqual(r["status"], "MISSING")

    def test_no_remote_configured_is_not_a_fault(self):
        """Off-site backup is a choice. An operator who hasn't made it must
        not be shown a red signal they didn't cause."""
        r = self._freshness(remote="", write=False)
        self.assertEqual(r["status"], "NA")

    def test_threshold_is_configurable(self):
        import importlib, backup_status
        with patch.dict(os.environ, {"BACKUP_SYNC_MAX_AGE_HOURS": "100"}):
            importlib.reload(backup_status)
            try:
                r = self._freshness(age_hours=48)
                self.assertEqual(r["status"], "OK")
            finally:
                importlib.reload(backup_status)

    def test_stamp_contents_are_surfaced_and_bounded(self):
        with open(self.stamp, "w") as fh:
            fh.write("X" * 5000)
        with patch.dict(os.environ, {"BACKUP_SYNC_STATE_DIR": self.dir,
                                     "RCLONE_REMOTE": "r:b"}):
            import backup_status
            r = backup_status.remote_sync_freshness()
        self.assertLessEqual(len(r["at"] or ""), 32)


class TestWiredIntoHealth(unittest.TestCase):
    def _read(self, rel):
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", *rel.split("/"))
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_http_health_reports_offsite(self):
        body = self._read("runner.py")
        self.assertIn("remote_sync_freshness", body)
        self.assertIn("offsite=", body)

    def test_a_broken_offsite_is_never_a_503(self):
        """A stale remote does not mean the bot is unhealthy, and a 503 would
        make Docker restart a working bot — which fixes nothing about the
        remote. Same reasoning the local backup signal already follows."""
        body = self._read("runner.py")
        code_line = [l for l in body.splitlines() if "status_code = 503" in l]
        self.assertEqual(len(code_line), 1)
        self.assertNotIn("offsite", code_line[0])

    def test_bot_health_command_reports_offsite(self):
        body = self._read("handlers/core.py")
        self.assertIn("remote_sync_freshness", body)
        self.assertIn("Off-site", body)


class TestSidecarStampsOnlyOnSuccess(unittest.TestCase):
    """The stamp is the whole signal — if the sidecar writes it unconditionally
    the check reports healthy forever and is worse than nothing."""

    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")
        with open(path, encoding="utf-8") as fh:
            self.compose = fh.read()
        self.sync = self.compose.split("backup-sync:")[1]

    def test_copy_failure_is_not_swallowed(self):
        self.assertNotIn("--log-level INFO || true", self.sync)

    def test_stamp_is_inside_the_success_branch(self):
        block = self.sync.split("if rclone copy")[1].split("else")[0]
        self.assertIn("last-success", block)

    def test_failure_branch_says_so_and_writes_nothing(self):
        block = self.sync.split("else")[1].split("fi")[0]
        self.assertIn("FAILED", block)
        self.assertNotIn("last-success", block)

    def test_backups_stay_read_only_to_the_sync_sidecar(self):
        """It ships snapshots; it must never be able to alter them. That is
        why the stamp needs a mount of its own."""
        self.assertIn("/backups:ro", self.sync)
        self.assertIn(":/state", self.sync)


if __name__ == "__main__":
    unittest.main()
