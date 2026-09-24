"""Is the bot actually RECEIVING anything — and will Telegram still deliver?

Sept 2026: Telegram's stored allowed_updates lost callback_query, so button
presses were never delivered. Typed commands worked. /health was green, the
Docker healthcheck was green, the watchdog stayed quiet, and the container
reported (healthy) for three days — because get_me() succeeds perfectly well
on a bot that cannot hear anything. The same week, a stray webhook caused an
identical silence from an entirely different cause.

Two complementary signals, doing different jobs on purpose:

  update_ages()            DIAGNOSES. Per update type, so "commands 2m ago,
                           button taps 3d ago" names the fault outright. Does
                           NOT alarm per-type: a group that never taps buttons
                           is indistinguishable from one whose buttons broke.

  check_update_delivery()  ALARMS. Asks Telegram what it will actually deliver
                           and compares against what we asked for. Unambiguous,
                           so it can page a human without crying wolf.
"""
import asyncio
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        import bot_state
        self.bs = bot_state
        self.bs._last_update_state.clear()
        self.bs._last_update_persist["at"] = 0.0
        self.bs._last_delivery_check["at"] = 0.0
        self.bs._delivery_state.update(checked_at=None, missing=[], ok=None)


class TestPerTypeAges(_Base):
    def test_ages_are_tracked_per_update_type(self):
        """One combined timestamp would have looked healthy all through the
        outage, because messages never stopped arriving."""
        now = time.time()
        self.bs._last_update_state.update(
            {"message": now - 120, "callback_query": now - 3 * 86400})
        ages = self.bs.update_ages()
        self.assertLess(ages["message"], 200)
        self.assertGreater(ages["callback_query"], 2 * 86400)

    def test_most_recent_type_is_listed_first(self):
        now = time.time()
        self.bs._last_update_state.update(
            {"callback_query": now - 9000, "message": now - 10})
        self.assertEqual(list(self.bs.update_ages())[0], "message")

    def test_seconds_since_any_update_uses_the_freshest(self):
        now = time.time()
        self.bs._last_update_state.update(
            {"message": now - 60, "callback_query": now - 86400})
        self.assertLess(self.bs.seconds_since_any_update(), 120)

    def test_none_when_nothing_has_arrived(self):
        """A fresh install must not look broken."""
        self.assertIsNone(self.bs.seconds_since_any_update())
        self.assertEqual(self.bs.update_ages(), {})

    def test_note_update_records_and_never_raises(self):
        with patch.object(self.bs, "set_system_config", create=True):
            self.bs.note_update("callback_query")
        self.assertIn("callback_query", self.bs._last_update_state)

    def test_a_db_failure_never_costs_the_update(self):
        """This runs on the hot path for every single update."""
        import db
        with patch.object(db, "set_system_config", side_effect=RuntimeError("down")):
            self.bs.note_update("message")   # must not raise
        self.assertIn("message", self.bs._last_update_state)

    def test_persistence_is_throttled(self):
        """In-memory updates every time; the DB write does not."""
        import db
        with patch.object(db, "set_system_config") as w:
            for _ in range(50):
                self.bs.note_update("message")
        self.assertEqual(w.call_count, 1, "should persist once, not per update")

    def test_restored_stamps_never_overwrite_live_ones(self):
        import db, json
        now = time.time()
        self.bs._last_update_state["message"] = now
        with patch.object(db, "get_system_config",
                          return_value=json.dumps({"message": int(now - 99999),
                                                   "callback_query": int(now - 500)})):
            self.bs.load_update_state()
        self.assertAlmostEqual(self.bs._last_update_state["message"], now, delta=2)
        self.assertIn("callback_query", self.bs._last_update_state)

    def test_restore_survives_a_corrupt_value(self):
        import db
        with patch.object(db, "get_system_config", return_value="{not json"):
            self.bs.load_update_state()   # must not raise


class TestMiddlewareStampsEveryUpdate(unittest.TestCase):
    def test_stamp_happens_before_any_early_return(self):
        """Member tracking skips DMs and bots. 'Did an update reach us' is
        true for those too, so the stamp must come first."""
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "bot_state.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        fn = body.split("async def pre_process")[1].split("async def post_process")[0]
        self.assertIn("note_update", fn)
        self.assertLess(fn.index("note_update"), fn.index("try:"),
                        "stamp must precede the guarded body and its early returns")


class TestDeliveryCheck(_Base):
    def _check(self, stored, wanted=("message", "callback_query")):
        info = MagicMock()
        info.allowed_updates = stored
        with patch.object(self.bs, "bot") as b:
            b.get_webhook_info = AsyncMock(return_value=info)
            with patch("runner.allowed_updates", return_value=list(wanted)):
                return _run(self.bs.check_update_delivery(force=True))

    def test_missing_type_is_flagged(self):
        """The literal Sept 2026 configuration."""
        state = self._check(["message", "edited_message",
                             "channel_post", "edited_channel_post"])
        self.assertEqual(state["missing"], ["callback_query"])
        self.assertFalse(state["ok"])

    def test_everything_present_is_ok(self):
        state = self._check(["message", "callback_query", "edited_message"])
        self.assertEqual(state["missing"], [])
        self.assertTrue(state["ok"])

    def test_empty_stored_list_is_telegrams_default_not_a_fault(self):
        """[] means 'all types except chat_member' — delivers everything we
        want, so flagging it would be a permanent false alarm."""
        state = self._check([])
        self.assertEqual(state["missing"], [])

    def test_an_api_failure_is_unknown_not_broken(self):
        with patch.object(self.bs, "bot") as b:
            b.get_webhook_info = AsyncMock(side_effect=RuntimeError("timeout"))
            state = _run(self.bs.check_update_delivery(force=True))
        self.assertIsNone(state["ok"])
        self.assertEqual(state["missing"], [])

    def test_the_check_is_throttled(self):
        info = MagicMock(); info.allowed_updates = ["message", "callback_query"]
        with patch.object(self.bs, "bot") as b:
            b.get_webhook_info = AsyncMock(return_value=info)
            with patch("runner.allowed_updates", return_value=["message"]):
                _run(self.bs.check_update_delivery(force=True))
                for _ in range(10):
                    _run(self.bs.check_update_delivery())
            self.assertEqual(b.get_webhook_info.await_count, 1)


class TestHealthWiring(unittest.TestCase):
    def _runner_src(self):
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "runner.py")
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_total_silence_is_degraded(self):
        self.assertIn("no_updates_", self._runner_src())

    def test_undelivered_types_are_degraded(self):
        self.assertIn("undelivered_", self._runner_src())

    def test_a_stale_single_type_does_NOT_alarm(self):
        """Deliberate. A group that never taps buttons must not page anyone —
        an alert that cries wolf gets muted, and then catches nothing at all."""
        src = self._runner_src()
        block = src.split("def health_check")[1].split("async def ping")[0]
        self.assertNotIn("for k, v in _ages.items()\n        if v >", block)
        self.assertIn("seconds_since_any_update", block)

    def test_never_a_503(self):
        """A bot receiving nothing is still serving web and REST fine, and
        restarting it does not fix Telegram's delivery config."""
        src = self._runner_src()
        line = [l for l in src.splitlines() if "status_code = 503" in l]
        self.assertEqual(len(line), 1)
        self.assertNotIn("no_updates", line[0])
        self.assertNotIn("undelivered", line[0])

    def test_threshold_is_generous_and_configurable(self):
        import runner
        self.assertGreaterEqual(runner.UPDATE_STALE_SECONDS, 12 * 3600,
                                "a quiet overnight group must never trip this")


if __name__ == "__main__":
    unittest.main()
