"""
End-to-end verification of the guided dues flow as shipped in 9.4 —
every handoff from game end to closed books, through the REAL handlers
and REAL database:

  /erc → pinned settle nudge (Settle-now button)
       → settle_now tap → penalty panel (or straight to confirm card
         when no tiers — the dead-end fix)
       → pen_d Done → confirm/subsidy card
       → settle_confirm tap → financial close
       → nudge unpinned + de-buttoned, ledger written.

Plus the /enable_dues setup card and its reply-to-set-UPI flow.
"""
from unittest.mock import AsyncMock

import db
from services import dues as dues_svc

from helpers import IntegrationBase, CHAT_ID, ADMIN_USER, USERS
from mock_helpers import get_mock_bot


def _enable_dues(chat_id=CHAT_ID, upi="test@upi", step=10, tiers=True):
    db.get_or_create_chat(chat_id)
    db.update_chat_settings(chat_id, dues_enabled=1, dues_round_step=step, upi_vpa=upi)
    if tiers:
        dues_svc.seed_default_penalty_tiers(chat_id)


class GuidedFlowBase(IntegrationBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from handlers.dues import (
            settle_now_callback, dues_setup, dues_setup_check_callback,
            dues_setup_upi_callback, dues_setup_upi_reply,
            _settle_nudge_msgs, _pending_upi_input,
        )
        cls.settle_now_callback = staticmethod(settle_now_callback)
        cls.dues_setup = staticmethod(dues_setup)
        cls.dues_setup_check_callback = staticmethod(dues_setup_check_callback)
        cls.dues_setup_upi_callback = staticmethod(dues_setup_upi_callback)
        cls.dues_setup_upi_reply = staticmethod(dues_setup_upi_reply)
        cls.settle_nudge_msgs = _settle_nudge_msgs
        cls.pending_upi_input = _pending_upi_input

    def setUp(self):
        super().setUp()
        self.settle_nudge_msgs.clear()
        self.pending_upi_input.clear()
        bot = get_mock_bot()
        bot.pin_chat_message = AsyncMock()
        bot.unpin_chat_message = AsyncMock()

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _play_game(self, title="Sunday Game", fee="300", n_in=3):
        rc = await self.start_rc(title)
        rc.event_fee = fee
        for u in USERS[:n_in]:
            await self.vote_in(u)
        return rc

    def _nudge_state(self):
        """(rollcall_id, message_id) of the tracked settle nudge, or None."""
        for (cid, rc_id), mid in self.settle_nudge_msgs.items():
            if cid == CHAT_ID:
                return rc_id, mid
        return None

    def _sent_markups(self):
        return [kw.get("reply_markup")
                for _a, kw in get_mock_bot().send_message.call_args_list]

    def _buttons_with_prefix(self, prefix):
        datas = []
        for m in self._sent_markups():
            for b in getattr(m, "keyboard", []) or []:
                d = getattr(b, "callback_data", "")
                if isinstance(d, str) and d.startswith(prefix):
                    datas.append(d)
        return datas


class TestGuidedSettleViaNudge(GuidedFlowBase):
    """The full happy path: end game → pinned nudge → button → panel →
    Done → confirm → close → nudge cleared."""

    async def test_full_chain_end_to_close(self):
        _enable_dues()
        await self._play_game(fee="300", n_in=3)

        # 1. /erc ends the game → persistent nudge appears
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        state = self._nudge_state()
        self.assertIsNotNone(state, "settle nudge not tracked after /erc")
        rc_id, nudge_mid = state
        self.assertTrue(any(f"settle_now:{rc_id}" == d
                            for d in self._buttons_with_prefix("settle_now:")),
                        "nudge missing Settle-now button")
        get_mock_bot().pin_chat_message.assert_awaited_once()
        pin_args = get_mock_bot().pin_chat_message.await_args
        self.assertEqual(pin_args[0][1], nudge_mid)  # pinned the nudge itself

        # 2. Tap Settle now → penalty panel opens, scoped to this game
        await self.settle_now_callback(self.call(f"settle_now:{rc_id}", ADMIN_USER,
                                                 message_id=nudge_mid))
        self.assertTrue(any(cid == CHAT_ID for (cid, _m) in self.penalty_panel_sessions),
                        "penalty panel session did not open from the nudge button")

        # 3. Done on the panel → confirm/subsidy card
        panel_rc_id = await self.tap_penalty_done()
        self.assertEqual(panel_rc_id, rc_id)

        # 4. Confirm with no subsidy → books closed
        await self.dues_settle_confirm_callback(
            self.call(f"settle_confirm:{rc_id}:0", ADMIN_USER))

        closure = db.get_game_closure(rc_id)
        self.assertIsNotNone(closure, "no game_closures row after confirm tap")
        self.assertEqual(closure["per_head"], 100)  # 300 / 3
        balances = db.get_all_dues_balances(CHAT_ID)
        self.assertEqual(len(balances), 3)

        # 5. Nudge cleared: unpinned + forgotten
        get_mock_bot().unpin_chat_message.assert_awaited_once_with(CHAT_ID, nudge_mid)
        self.assertIsNone(self._nudge_state(), "nudge still tracked after close")

    async def test_settle_dues_command_itself_never_nudges(self):
        """/settle_dues on an active game ends it and opens the panel — the
        nudge would be noise there (settle_nudge=False path)."""
        _enable_dues()
        await self._play_game()
        await self.dues_settle_dues(self.msg("/settle_dues", ADMIN_USER))
        self.assertIsNone(self._nudge_state())
        get_mock_bot().pin_chat_message.assert_not_awaited()
        # ...and the penalty panel opened directly instead
        self.assertTrue(any(cid == CHAT_ID for (cid, _m) in self.penalty_panel_sessions))

    async def test_settle_now_after_already_settled_is_safe(self):
        _enable_dues()
        await self._play_game(fee="300", n_in=3)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        rc_id, nudge_mid = self._nudge_state()

        # Someone settles via the fast path while the nudge sits pinned
        await self.dues_settle_dues(self.msg("/settle_dues 0", ADMIN_USER))
        self.assertIsNotNone(db.get_game_closure(rc_id))

        # Late tap on the stale nudge → polite no-op, no second closure
        await self.settle_now_callback(self.call(f"settle_now:{rc_id}", ADMIN_USER,
                                                 message_id=nudge_mid))
        self.assertTrue(any("Already settled" in t for t in self.sent_texts()))
        with db._cursor() as cur:
            ph = "?"
            cur.execute(f"SELECT COUNT(*) FROM game_closures WHERE rollcall_id = {ph}", (rc_id,))
            self.assertEqual(cur.fetchone()[0], 1)


class TestNoTierDeadEndFixed(GuidedFlowBase):
    """Dues enabled but zero tiers: the guided flow must skip the panel and
    reach the confirm card instead of silently stopping (9.4 fix)."""

    async def test_settle_now_without_tiers_reaches_confirm_card(self):
        _enable_dues(tiers=False)
        await self._play_game(fee="200", n_in=2)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        rc_id, nudge_mid = self._nudge_state()

        await self.settle_now_callback(self.call(f"settle_now:{rc_id}", ADMIN_USER,
                                                 message_id=nudge_mid))

        # No panel (nothing to mark), but the confirm card MUST appear
        self.assertFalse(any(cid == CHAT_ID for (cid, _m) in self.penalty_panel_sessions))
        confirms = self._buttons_with_prefix("settle_confirm:")
        self.assertIn(f"settle_confirm:{rc_id}:0", confirms,
                      "confirm card did not appear — guided flow dead-ended")
        # ...and the admin is told why the penalty step was skipped
        self.assertTrue(any("/add_penalty" in t for t in self.sent_texts()))

        # Closing still works end-to-end
        await self.dues_settle_confirm_callback(
            self.call(f"settle_confirm:{rc_id}:0", ADMIN_USER))
        self.assertIsNotNone(db.get_game_closure(rc_id))
        self.assertEqual(db.get_game_closure(rc_id)["per_head"], 100)  # 200 / 2


class TestDuesSetupCard(GuidedFlowBase):
    """/enable_dues guided setup: status card, Set-UPI reply flow, re-check."""

    async def test_enable_dues_shows_card_and_upi_reply_configures(self):
        # Fresh group: dues off, no UPI
        db.get_or_create_chat(CHAT_ID)
        await self.dues_enable(self.msg("/enable_dues", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("Dues & Treasury enabled" in t for t in texts))
        card = next((t for t in texts if "setup" in t), None)
        self.assertIsNotNone(card, "setup card not sent after /enable_dues")
        self.assertIn("not set", card)  # UPI flagged missing
        self.assertIn("dues_setup_upi", self._buttons_with_prefix("dues_setup_upi"))

        # Tap 💳 Set UPI now → prompt armed
        card_mid = 777
        await self.dues_setup_upi_callback(
            self.call("dues_setup_upi", ADMIN_USER, message_id=card_mid))
        self.assertIn((CHAT_ID, ADMIN_USER["id"]), self.pending_upi_input)

        # Reply with the VPA → stored, pending consumed, card refreshed in place
        await self.dues_setup_upi_reply(self.msg("group@okbank", ADMIN_USER))
        self.assertEqual(db.get_or_create_chat(CHAT_ID).get("upi_vpa"), "group@okbank")
        self.assertNotIn((CHAT_ID, ADMIN_USER["id"]), self.pending_upi_input)
        self.assertTrue(any("group@okbank" in t for t in self.edited_texts()),
                        "setup card was not refreshed with the new UPI")

    async def test_invalid_upi_reply_keeps_prompt_armed(self):
        _enable_dues(upi=None)
        await self.dues_setup_upi_callback(
            self.call("dues_setup_upi", ADMIN_USER, message_id=777))
        await self.dues_setup_upi_reply(self.msg("not a valid vpa @", ADMIN_USER))
        self.assertIsNone(db.get_or_create_chat(CHAT_ID).get("upi_vpa"))
        self.assertIn((CHAT_ID, ADMIN_USER["id"]), self.pending_upi_input)

    async def test_dues_setup_command_reopens_card_all_green(self):
        _enable_dues(upi="ready@upi")
        await self.dues_setup(self.msg("/dues_setup", ADMIN_USER))
        card = next((t for t in self.sent_texts() if "setup" in t), None)
        self.assertIsNotNone(card)
        self.assertIn("ready@upi", card)
        self.assertIn("All set", card)


if __name__ == "__main__":
    import unittest
    unittest.main()
