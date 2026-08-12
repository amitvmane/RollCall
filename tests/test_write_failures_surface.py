"""
Regression: a failed DB write must never be reported to the user as success.

Every one of these writers used to `except Exception: return False`, and every
caller discarded that result — so a write that never landed looked exactly like
one that did, and the handler went on to announce "done". That is precisely how
the Postgres boolean-coercion bug stayed invisible: /enable_dues reported
success on every single call while writing nothing at all.

Two things are asserted here, and both matter:
  1. the writers RAISE rather than return a falsy value, and
  2. the message that reaches the user is the curated generic one — a DB error
     string must never be echoed into a group chat.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from exceptions import databaseError  # noqa: E402


# The writers whose result was discarded at every call site. Keep this list in
# sync with the raise sites in db.py — a writer that quietly goes back to
# returning False re-opens the whole class of bug.
_MUST_RAISE = [
    "update_chat_settings",
    "update_rollcall",
    "end_rollcall",
    "add_or_update_proxy_user",
    "upsert_penalty_tier",
    "update_game_closure_collector",
    "delete_game_closure",
]


def _msg(text="/cmd", chat_id=100, user_id=1):
    m = MagicMock()
    m.text = text
    m.chat.id = chat_id
    m.from_user.id = user_id
    m.from_user.first_name = "Admin"
    m.from_user.username = "admin"
    return m


class TestWritersRaiseInsteadOfSwallowing(unittest.TestCase):
    """Source-level guard: each writer must raise databaseError on failure."""

    def test_each_writer_raises_databaseerror_on_failure(self):
        import re
        src = open(os.path.join(os.path.dirname(__file__), "..",
                                "rollCall", "db.py")).read()
        lines = src.split("\n")

        # map each target function to its body
        spans, cur, start = {}, None, 0
        for i, line in enumerate(lines):
            m = re.match(r"def (\w+)\(", line)
            if m:
                if cur:
                    spans[cur] = (start, i)
                cur, start = m.group(1), i
        if cur:
            spans[cur] = (start, len(lines))

        for fn in _MUST_RAISE:
            self.assertIn(fn, spans, f"{fn} no longer exists in db.py")
            s, e = spans[fn]
            body = "\n".join(lines[s:e])
            self.assertIn(
                "raise databaseError", body,
                f"db.{fn} must raise databaseError on write failure — "
                f"returning a falsy value makes a failed write look like success",
            )

    def test_databaseerror_is_user_facing_but_generic(self):
        """It must be in the curated set (so the user gets a real message,
        not 'something went wrong'), while carrying no driver detail."""
        import bot_state
        self.assertIn(databaseError, bot_state._USER_FACING_EXCEPTIONS)


class TestFailedWriteReachesTheUser(unittest.IsolatedAsyncioTestCase):
    """Behavioural counterpart: the handler must not announce success."""

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()

    def _said(self):
        return self.bot_state.bot.send_message.call_args_list[0][0][1]

    async def test_enable_dues_does_not_claim_success_when_write_fails(self):
        from handlers.dues import enable_dues
        with patch("handlers.dues.admin_rights", new=AsyncMock(return_value=True)), \
             patch("handlers.dues.manager", MagicMock()), \
             patch("handlers.dues._db.get_or_create_chat", return_value={"dues_enabled": 0}), \
             patch("handlers.dues._db.update_chat_settings",
                   side_effect=databaseError("⚠️ Couldn't save that setting — please try again.")), \
             patch("handlers.dues.dues_svc.stamp_dues_epoch"), \
             patch("handlers.dues.dues_svc.seed_default_penalty_tiers"):
            await enable_dues(_msg("/enable_dues"))

        said = self._said()
        self.assertNotIn("enabled", said.lower(),
                         "handler announced success despite the write failing")
        self.assertIn("couldn't save", said.lower())

    async def test_failed_write_message_leaks_no_driver_detail(self):
        from handlers.dues import enable_dues
        with patch("handlers.dues.admin_rights", new=AsyncMock(return_value=True)), \
             patch("handlers.dues.manager", MagicMock()), \
             patch("handlers.dues._db.get_or_create_chat", return_value={"dues_enabled": 0}), \
             patch("handlers.dues._db.update_chat_settings",
                   side_effect=databaseError("⚠️ Couldn't save that setting — please try again.")), \
             patch("handlers.dues.dues_svc.stamp_dues_epoch"), \
             patch("handlers.dues.dues_svc.seed_default_penalty_tiers"):
            await enable_dues(_msg("/enable_dues"))

        said = self._said().lower()
        for leak in ("psycopg", "sqlite", "traceback", "syntax error",
                     "column", "relation", "constraint"):
            self.assertNotIn(leak, said, f"DB internals leaked into chat: {leak!r}")


class TestEndRollcallKeepsMemoryAndDbConsistent(unittest.TestCase):
    """The specific consistency bug the end_rollcall swallow allowed."""

    def test_failed_db_end_does_not_pop_the_rollcall_from_memory(self):
        """remove_rollcall pops from its cache right after db.end_rollcall.
        When that write was swallowed the rollcall vanished from memory while
        staying active in the DB — and reappeared on the next restart."""
        import rollcall_manager
        mgr = rollcall_manager.manager

        rc = MagicMock()
        rc.db_id = 4242
        chat = {"rollCalls": [rc]}
        with patch.object(mgr, "get_chat", return_value=chat), \
             patch("rollcall_manager.db.end_rollcall",
                   side_effect=databaseError("nope")):
            with self.assertRaises(databaseError):
                mgr.remove_rollcall(-100123, 0)

        self.assertEqual(len(chat["rollCalls"]), 1,
                         "rollcall was dropped from memory even though the DB write failed")


if __name__ == "__main__":
    unittest.main()
