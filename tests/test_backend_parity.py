"""SQLite and Postgres must behave the same, not merely both work.

Both backends are first-class, but parity used to be asserted only by the
integration suite. Two gaps existed as of 2026-08-21:

  1. Two hot paths (bot-wide /stats, the daily idle-chat sweep) offloaded their
     query to a thread pool on SQLite but ran it inline on Postgres, because
     psycopg2's SimpleConnectionPool is not thread-safe. Postgres deployments
     therefore blocked the event loop — stalling Telegram polling, the REST API
     and the scheduler — on the two slowest queries in the app. Fixed by moving
     to ThreadedConnectionPool.
  2. scripts/functional_test.py hard-assigned DATABASE_URL to SQLite, so the
     only layer driving real telebot routing had never run against Postgres.

These tests pin both so neither silently regresses.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestPoolIsThreadSafe(unittest.TestCase):

    def test_uses_threaded_connection_pool(self):
        """SimpleConnectionPool is documented as NOT thread-safe. db.py hands
        connections to _stats_executor threads, so it must use the threaded
        variant or those offloads are unsafe."""
        src = _read("rollCall", "db.py")
        self.assertIn("ThreadedConnectionPool(", src)
        self.assertNotIn("SimpleConnectionPool(", src,
                         "SimpleConnectionPool is not thread-safe — see module docstring")


class TestOffloadParity(unittest.TestCase):
    """Neither hot path may branch on backend when deciding to offload."""

    def _assert_offloads_unconditionally(self, relpath, marker):
        src = _read(*relpath.split("/"))
        idx = src.index(marker)
        # Span BOTH sides: run_in_executor(...) wraps the marker, so it sits
        # just above it, and a reintroduced dialect branch would sit above too.
        window = src[max(0, idx - 700):idx + 300]
        self.assertIn("run_in_executor", window,
                      f"{relpath} should offload {marker!r} off the event loop")
        # A dialect check inside the offload decision is exactly the asymmetry
        # this test exists to prevent.
        offending = re.search(r'db_type\s*==\s*["\'](sqlite|postgresql)["\']', window)
        self.assertIsNone(
            offending,
            f"{relpath} branches on db_type near {marker!r} — both backends must "
            f"take the same offloaded path (found: {offending.group(0) if offending else ''})"
        )

    def test_bot_stats_offloads_on_both_backends(self):
        self._assert_offloads_unconditionally(
            "rollCall/handlers/stats.py", "stats_svc.bot_stats")

    def test_idle_sweep_offloads_on_both_backends(self):
        self._assert_offloads_unconditionally(
            "rollCall/periodic_jobs.py", "get_idle_chats")


class TestFunctionalTestCanTargetPostgres(unittest.TestCase):

    def test_database_url_is_not_hard_assigned(self):
        """A hard assignment silently discards an inherited DATABASE_URL, which
        is how the functional layer went un-run against Postgres."""
        src = _read("scripts", "functional_test.py")
        self.assertIn('os.environ.setdefault("DATABASE_URL"', src)
        self.assertNotIn('os.environ["DATABASE_URL"] = f"sqlite', src)

    def test_ci_runs_functional_against_postgres(self):
        ci = _read(".github", "workflows", "ci.yml")
        postgres_job = ci[ci.index("  postgres:"):]
        self.assertIn("scripts/functional_test.py", postgres_job,
                      "the Postgres CI job should run the functional suite too")


class TestSmokeCanTargetPostgres(unittest.TestCase):
    """Booting the app builds the schema, so smoke-on-Postgres is the only
    thing that proves the postgresql arm of db.py — every CREATE TABLE,
    ALTER TABLE and partial index, plus pool construction — actually runs."""

    def test_database_url_is_not_hard_assigned(self):
        src = _read("scripts", "smoke_test.py")
        self.assertIn('os.environ.setdefault("DATABASE_URL"', src)
        self.assertNotIn('os.environ["DATABASE_URL"] = f"sqlite', src)

    def test_ci_runs_smoke_against_postgres(self):
        ci = _read(".github", "workflows", "ci.yml")
        postgres_job = ci[ci.index("  postgres:"):]
        self.assertIn("scripts/smoke_test.py", postgres_job,
                      "the Postgres CI job should boot the app against Postgres")


if __name__ == "__main__":
    unittest.main()
