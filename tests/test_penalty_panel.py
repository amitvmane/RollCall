"""
Tests for the penalty panel's cross-tier player exclusivity (a player selected
or applied in one tier must not be selectable in another tier until freed).
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _call(data, chat_id=100, msg_id=5, user_id=1):
    c = MagicMock()
    c.data = data
    c.id = "cbq1"
    c.message.chat.id = chat_id
    c.message.message_id = msg_id
    c.from_user.id = user_id
    c.from_user.first_name = "Admin"
    c.from_user.username = "admin"
    return c


def _members():
    return [
        {"user_id": 1, "member_name": "Alice", "_identity": 1},
        {"user_id": 2, "member_name": "Bob", "_identity": 2},
    ]


class TestPenaltyPanelTierExclusivity(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.answer_callback_query = AsyncMock()
        bot_state.bot.send_message = AsyncMock()

        from handlers import penalty_panel as pp
        self.pp = pp
        pp._sessions.clear()

        self.mgr = MagicMock()
        self.mgr.get_admin_rights.return_value = False  # skip admin gate
        lock_ctx = MagicMock()
        lock_ctx.__aenter__ = AsyncMock(return_value=None)
        lock_ctx.__aexit__ = AsyncMock(return_value=False)
        self.mgr.get_chat_write_lock.return_value = lock_ctx

        self.tiers = [
            {"name": "late", "amount": 50, "description": "", "is_ditch": False},
            {"name": "ditch", "amount": 200, "description": "no-show", "is_ditch": True},
        ]

        self.session = pp._PenaltySession(
            chat_id=100, rollcall_id=1, title="Sunday",
            members=_members(), ghost_eligible=True,
        )
        pp._sessions[(100, 5)] = self.session

        patcher1 = patch("handlers.penalty_panel.db.get_penalty_tiers", return_value=self.tiers)
        patcher1.start()
        self.addCleanup(patcher1.stop)
        patcher2 = patch("handlers.penalty_panel.manager", self.mgr)
        patcher2.start()
        self.addCleanup(patcher2.stop)
        patcher3 = patch("handlers.penalty_panel.safe_edit_text", new=AsyncMock())
        patcher3.start()
        self.addCleanup(patcher3.stop)
        patcher4 = patch("handlers.penalty_panel.safe_edit_markup", new=AsyncMock())
        patcher4.start()
        self.addCleanup(patcher4.stop)

    async def test_pending_selection_locks_other_tiers(self):
        # Select Alice (idx 0) in "late" tier
        self.session.active_tier = "late"
        await self.pp.penalty_panel_callback(_call("pen_g:1:0"))
        self.assertIn(0, self.session.selections["late"])

        # Switching to "ditch" tier and trying to toggle idx 0 should be rejected
        self.session.active_tier = "ditch"
        await self.pp.penalty_panel_callback(_call("pen_g:1:0"))
        self.assertNotIn(0, self.session.selections.get("ditch", set()))
        # rejection came with an alert
        call_kwargs = self.bot_state.bot.answer_callback_query.call_args
        self.assertTrue(call_kwargs.kwargs.get("show_alert") or (len(call_kwargs.args) > 2 and call_kwargs.args[2]))

    async def test_unselecting_frees_player_for_other_tiers(self):
        self.session.active_tier = "late"
        self.session.selections["late"] = {0}

        # Untoggle Alice in "late"
        await self.pp.penalty_panel_callback(_call("pen_g:1:0"))
        self.assertNotIn(0, self.session.selections["late"])

        # Now Alice should be selectable again in "ditch"
        self.session.active_tier = "ditch"
        await self.pp.penalty_panel_callback(_call("pen_g:1:0"))
        self.assertIn(0, self.session.selections["ditch"])

    async def test_applied_player_stays_locked_out_of_other_tiers(self):
        with patch("handlers.penalty_panel.dues_svc.mark_penalty") as mp:
            self.session.active_tier = "late"
            self.session.selections["late"] = {0}
            await self.pp.penalty_panel_callback(_call("pen_a:1"))
            mp.assert_called_once()

        self.assertIn(0, self.session.applied_indices["late"])

        # Alice (idx 0) is now locked out of the ditch tier
        self.session.active_tier = "ditch"
        await self.pp.penalty_panel_callback(_call("pen_g:1:0"))
        self.assertNotIn(0, self.session.selections.get("ditch", set()))

    async def test_locked_callback_does_not_mutate_state(self):
        self.session.selections["late"] = {0}
        self.session.active_tier = "ditch"
        await self.pp.penalty_panel_callback(_call("pen_locked:1:0"))
        self.assertNotIn(0, self.session.selections.get("ditch", set()))

    async def test_non_curated_mark_penalty_failure_does_not_leak_raw_exception(self):
        # Regression: a non-curated exception from dues_svc.mark_penalty used
        # to be interpolated verbatim (f"{m['member_name']}: {exc}") straight
        # into a group-chat message. It must now be replaced with the same
        # generic message reply_error() uses elsewhere.
        with patch("handlers.penalty_panel.dues_svc.mark_penalty",
                   side_effect=RuntimeError("db connection reset by peer")):
            self.session.active_tier = "late"
            self.session.selections["late"] = {0}
            await self.pp.penalty_panel_callback(_call("pen_a:1"))

        sent_texts = [c.args[1] for c in self.bot_state.bot.send_message.call_args_list]
        joined = " ".join(sent_texts)
        self.assertNotIn("db connection reset by peer", joined)
        self.assertIn("logged", joined.lower())

    async def test_curated_mark_penalty_failure_still_shown_verbatim(self):
        from exceptions import incorrectParameter
        with patch("handlers.penalty_panel.dues_svc.mark_penalty",
                   side_effect=incorrectParameter("Tier 'late' no longer exists")):
            self.session.active_tier = "late"
            self.session.selections["late"] = {0}
            await self.pp.penalty_panel_callback(_call("pen_a:1"))

        sent_texts = [c.args[1] for c in self.bot_state.bot.send_message.call_args_list]
        joined = " ".join(sent_texts)
        self.assertIn("no longer exists", joined)
        self.bot_state.bot.answer_callback_query.assert_awaited()


class TestSendPenaltyPanelReturn(unittest.IsolatedAsyncioTestCase):
    """send_penalty_panel signals whether the panel opened so the settle flow
    can hand off to the confirm card instead of dead-ending (flow-audit #6)."""

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        sent = MagicMock()
        sent.message_id = 5
        bot_state.bot.send_message = AsyncMock(return_value=sent)
        from handlers import penalty_panel as pp
        self.pp = pp
        pp._sessions.clear()

    async def test_no_tiers_returns_false_and_hints_setup(self):
        with patch("handlers.penalty_panel.db.get_penalty_tiers", return_value=[]):
            opened = await self.pp.send_penalty_panel(100, 1, "Sunday")
        self.assertFalse(opened)
        hint = self.bot_state.bot.send_message.call_args[0][1]
        self.assertIn("/add_penalty", hint)

    async def test_no_members_returns_false(self):
        tiers = [{"name": "late", "amount": 50, "description": "", "is_ditch": False}]
        with patch("handlers.penalty_panel.db.get_penalty_tiers", return_value=tiers), \
             patch("handlers.penalty_panel._members_for_rollcall", return_value=[]):
            opened = await self.pp.send_penalty_panel(100, 1, "Sunday")
        self.assertFalse(opened)

    async def test_panel_opened_returns_true(self):
        tiers = [{"name": "late", "amount": 50, "description": "", "is_ditch": False}]
        with patch("handlers.penalty_panel.db.get_penalty_tiers", return_value=tiers), \
             patch("handlers.penalty_panel._members_for_rollcall", return_value=_members()):
            opened = await self.pp.send_penalty_panel(100, 1, "Sunday")
        self.assertTrue(opened)
        self.assertIn((100, 5), self.pp._sessions)


if __name__ == "__main__":
    unittest.main()
