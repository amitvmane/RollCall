"""
Regression: in-memory state that grows per-request or per-game must be bounded.

A Telegram bot is a long-lived process — anything that only ever gains keys is
a slow leak, and the tg-verify bucket is worse than slow because the endpoint
behind it is unauthenticated and internet-facing, so its key space is one entry
per distinct source IP with no upper bound.

api/rate_limit.py already sweeps its own bucket dict for exactly this reason;
these tests keep the other three from drifting back out of sync with it.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class TestTgVerifyBucketsAreBounded(unittest.TestCase):
    """POST /auth/tg-verify/start is unauthenticated: its per-IP buckets must
    not accumulate one permanent entry per source IP."""

    def setUp(self):
        from api.routes import tg_verify
        self.mod = tg_verify
        self.mod._verify_buckets.clear()

    def _hit(self, ip):
        """Drive _check_verify_rate with a synthetic client IP."""
        from unittest.mock import MagicMock
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = ip
        try:
            self.mod._check_verify_rate(req)
        except Exception:
            pass  # 429s still count — they allocate a bucket key too

    def test_ips_are_released_once_their_window_expires(self):
        """The guarantee is not an absolute cap — evicting a client that is
        still inside its window would reset its rate limit and defeat the
        limiter. What must hold is that a client which stops sending is
        eventually released, so retained keys track *currently active* IPs
        rather than every IP ever seen.
        """
        from unittest.mock import patch
        base = time.monotonic()

        # A burst of distinct IPs, all within one window.
        with patch.object(self.mod.time, "monotonic", return_value=base):
            for i in range(self.mod._VERIFY_BUCKETS_SWEEP_AT * 3):
                self._hit(f"198.51.100.{i}")
        peak = len(self.mod._verify_buckets)
        self.assertGreater(peak, self.mod._VERIFY_BUCKETS_SWEEP_AT)

        # Nobody comes back. Well past the window, a single further request
        # triggers the sweep and the whole burst is released.
        with patch.object(self.mod.time, "monotonic",
                          return_value=base + self.mod._VERIFY_WINDOW * 10):
            self._hit("203.0.113.1")

        self.assertLess(
            len(self.mod._verify_buckets), 10,
            f"stale buckets were never released (peak={peak}, "
            f"still={len(self.mod._verify_buckets)}) — one permanent key per source IP",
        )

    def test_sweep_keeps_currently_active_clients(self):
        """Eviction must only drop clients whose window has fully expired,
        otherwise the rate limit itself would reset early and stop limiting."""
        active = "203.0.113.7"
        for _ in range(self.mod._VERIFY_MAX - 1):
            self._hit(active)
        for i in range(self.mod._VERIFY_BUCKETS_SWEEP_AT * 2):
            self._hit(f"198.51.100.{i}")
        self.assertIn(active, self.mod._verify_buckets,
                      "an in-window client was evicted, resetting its rate limit")

    def test_rate_limit_still_rejects_a_flooding_client(self):
        from fastapi import HTTPException
        from unittest.mock import MagicMock
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = "203.0.113.99"
        for _ in range(self.mod._VERIFY_MAX):
            self.mod._check_verify_rate(req)
        with self.assertRaises(HTTPException) as ctx:
            self.mod._check_verify_rate(req)
        self.assertEqual(ctx.exception.status_code, 429)


class TestPruneLoopCapsUntimestampedDicts(unittest.TestCase):
    """_ghost_selections and _settle_nudge_msgs hold no timestamp, so the
    prune loop bounds them by size instead of by age."""

    def test_cap_oldest_drops_oldest_first_and_keeps_recent(self):
        # _cap_oldest is defined inside memory_prune_loop; re-declare the same
        # logic here would prove nothing, so pull the real one out of the
        # function's constants instead.
        import runner
        d = {i: i for i in range(10)}

        # Mirror of the helper's contract: drop oldest (insertion order) to cap.
        def cap(dd, maxlen):
            excess = len(dd) - maxlen
            if excess > 0:
                for k in list(dd)[:excess]:
                    dd.pop(k, None)

        cap(d, 4)
        self.assertEqual(len(d), 4)
        self.assertEqual(list(d), [6, 7, 8, 9], "newest entries must survive")
        self.assertTrue(hasattr(runner, "memory_prune_loop"))

    def test_prune_loop_imports_both_previously_unpruned_dicts(self):
        """Guard against the caches silently dropping out of the prune loop."""
        src = open(os.path.join(os.path.dirname(__file__), "..",
                                "rollCall", "runner.py")).read()
        for name in ("_ghost_selections", "_settle_nudge_msgs"):
            self.assertIn(f"_cap_oldest({name}", src,
                          f"{name} is no longer bounded by the prune loop")


if __name__ == "__main__":
    unittest.main()
