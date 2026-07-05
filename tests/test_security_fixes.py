"""
Regression tests for the 2026-07 security review fixes.

1. Penalty panel callbacks reject non-admins (financial writes were open to
   any group member who tapped the buttons).
2. Ghost-marking callbacks reject non-admins (stats manipulation).
3. send_md_fallback retries as plain text when Telegram rejects Markdown
   entities — ledger announcements must never be lost to a parse error.
4. Penalty panel session store is bounded (no unbounded growth if panels
   are never dismissed).
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _call(data="pen_d:1", chat_id=100, user_id=5, message_id=42):
    c = MagicMock()
    c.data = data
    c.id = "cbq1"
    c.message.chat.id = chat_id
    c.message.message_id = message_id
    c.from_user.id = user_id
    c.from_user.first_name = "Someone"
    c.from_user.username = "someone"
    return c


def _member(status):
    m = MagicMock()
    m.status = status
    return m


class TestPenaltyPanelAdminGate(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.answer_callback_query = AsyncMock()
        bot_state.bot.get_chat_member = AsyncMock(return_value=_member("member"))
        bot_state.bot.send_message = AsyncMock()

    async def test_non_admin_tap_rejected(self):
        from handlers import penalty_panel
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = True
        with patch("handlers.penalty_panel.manager", mgr):
            await penalty_panel.penalty_panel_callback(_call("pen_t:1:ditch"))
        # Rejected with an alert; no session lookup, no writes
        args, kwargs = self.bot_state.bot.answer_callback_query.call_args
        self.assertIn("admin", args[1].lower())
        self.assertTrue(kwargs.get("show_alert"))

    async def test_admin_tap_passes_gate(self):
        from handlers import penalty_panel
        self.bot_state.bot.get_chat_member = AsyncMock(
            return_value=_member("administrator"))
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = True
        with patch("handlers.penalty_panel.manager", mgr):
            # No session registered → passes the gate, then "panel expired"
            await penalty_panel.penalty_panel_callback(_call("pen_t:1:ditch"))
        args, _ = self.bot_state.bot.answer_callback_query.call_args
        self.assertIn("expired", args[1].lower())

    async def test_admin_mode_off_skips_gate(self):
        from handlers import penalty_panel
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = False   # chat runs without admin mode
        with patch("handlers.penalty_panel.manager", mgr):
            await penalty_panel.penalty_panel_callback(_call("pen_t:1:ditch"))
        # Gate skipped → falls through to "panel expired" (no session)
        args, _ = self.bot_state.bot.answer_callback_query.call_args
        self.assertIn("expired", args[1].lower())
        self.bot_state.bot.get_chat_member.assert_not_called()


class TestGhostCallbackAdminGate(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.answer_callback_query = AsyncMock()
        bot_state.bot.get_chat_member = AsyncMock(return_value=_member("member"))

    async def test_non_admin_ghost_tap_rejected(self):
        from handlers import ghost
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = True
        with patch("handlers.ghost.manager", mgr):
            await ghost.ghost_callback_handler(_call("ghost_yes_7"))
        args, kwargs = self.bot_state.bot.answer_callback_query.call_args
        self.assertIn("admin", args[1].lower())
        self.assertTrue(kwargs.get("show_alert"))

    async def test_non_ghost_prefixes_not_gated(self):
        """reconf_/proxy_ actions are user self-service — must not be gated."""
        from handlers import ghost
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = True
        with patch("handlers.ghost.manager", mgr):
            # Malformed reconf data falls through harmlessly, but the point
            # is it must NOT be rejected by the admin gate.
            await ghost.ghost_callback_handler(_call("reconf_bogus"))
        self.bot_state.bot.get_chat_member.assert_not_called()


class TestSendMdFallback(unittest.IsolatedAsyncioTestCase):

    async def test_plain_resend_on_parse_error(self):
        import bot_state
        calls = []

        async def _send(cid, text, **kwargs):
            calls.append(kwargs)
            if kwargs.get("parse_mode") == "Markdown":
                raise Exception("Bad Request: can't parse entities: ...")
            return MagicMock()

        bot_state.bot.send_message = AsyncMock(side_effect=_send)
        await bot_state.send_md_fallback(100, "broken *name_ text")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].get("parse_mode"), "Markdown")
        self.assertNotIn("parse_mode", calls[1])

    async def test_other_errors_propagate(self):
        import bot_state
        bot_state.bot.send_message = AsyncMock(
            side_effect=Exception("Forbidden: bot was blocked"))
        with self.assertRaises(Exception):
            await bot_state.send_md_fallback(100, "hello")


class TestPenaltySessionCap(unittest.IsolatedAsyncioTestCase):

    async def test_sessions_bounded(self):
        import bot_state
        from handlers import penalty_panel

        sent = MagicMock()
        sent.message_id = 0
        counter = {"n": 0}

        async def _send(*a, **k):
            counter["n"] += 1
            m = MagicMock()
            m.message_id = counter["n"]
            return m

        bot_state.bot.send_message = AsyncMock(side_effect=_send)
        penalty_panel._sessions.clear()
        tiers = [{"name": "ditch", "amount": 200, "is_ditch": 1, "description": ""}]
        members = [{"user_id": 1, "first_name": "A", "proxy_name": None}]
        with patch.object(penalty_panel.db, "get_penalty_tiers", return_value=tiers), \
             patch.object(penalty_panel, "get_rollcall_in_users", return_value=members):
            for i in range(penalty_panel._MAX_SESSIONS + 10):
                await penalty_panel.send_penalty_panel(100, i + 1, f"Game {i}")
        self.assertLessEqual(len(penalty_panel._sessions), penalty_panel._MAX_SESSIONS)
        penalty_panel._sessions.clear()


if __name__ == "__main__":
    unittest.main()
