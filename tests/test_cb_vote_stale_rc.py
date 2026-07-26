"""
Regression test: _cb_vote's panel refresh re-fetches the rollcall after
voting_svc releases the chat write lock, so a concurrent /erc that ends (or
renumbers) that rollcall in the gap must not crash the callback with an
AttributeError on rc.title — it must degrade gracefully instead.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _make_call(chat_id=100, user_id=2, username="bob", message_id=42):
    call = MagicMock()
    call.id = "cbq1"
    call.from_user.id = user_id
    call.from_user.username = username
    call.from_user.first_name = "Bob"
    call.message.chat.id = chat_id
    call.message.message_id = message_id
    return call


class TestCbVoteStaleRollcall(unittest.IsolatedAsyncioTestCase):

    async def test_rollcall_vanished_after_vote_does_not_crash(self):
        import handlers.lifecycle as lc

        svc_result = {
            "action": "moved",
            "was_in": True,
            "promoted": None,
            "user": {"user_id": 2, "name": "Bob", "username": "bob"},
        }
        mgr = MagicMock()
        mgr.get_rollcall.return_value = None  # /erc ended it between vote and refetch
        mgr.get_shh_mode.return_value = False

        with patch.object(lc, "manager", mgr), \
             patch.object(lc, "_is_rate_limited", return_value=False), \
             patch.object(lc.voting_svc, "vote_out", new=AsyncMock(return_value=svc_result)), \
             patch.object(lc, "bot") as mock_bot:
            mock_bot.answer_callback_query = AsyncMock()
            mock_bot.send_message = AsyncMock()
            mock_bot.edit_message_text = AsyncMock()
            call = _make_call()
            # Must not raise despite rc being None.
            await lc._cb_vote(call, 100, 1, "out")

        mock_bot.answer_callback_query.assert_awaited_once()
        msg = mock_bot.answer_callback_query.await_args.args[1]
        self.assertIn("ended", msg.lower())
        # No title-bearing announcement or panel edit should have been attempted.
        mock_bot.send_message.assert_not_awaited()
        mock_bot.edit_message_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
