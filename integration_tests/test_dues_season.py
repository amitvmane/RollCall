"""
Dues-v2 integration: dues epoch, /new_season reset, collector UPI memory —
real handlers, real database.
"""
from unittest.mock import AsyncMock, patch

import db
from services import dues as dues_svc

from helpers import IntegrationBase, CHAT_ID, ADMIN_USER, USERS
from mock_helpers import get_mock_bot


def _enable(upi="test@upi"):
    db.get_or_create_chat(CHAT_ID)
    db.update_chat_settings(CHAT_ID, dues_enabled=1, dues_round_step=10, upi_vpa=upi)
    dues_svc.seed_default_penalty_tiers(CHAT_ID)


def _bump_ended_at(rollcall_id, seconds=120):
    """Shift a game's ended_at relative to the (second-resolution) epoch —
    real games end hours apart from enable/reset moments; tests run inside
    one second. Positive = clearly after the epoch, negative = clearly before."""
    sign = "+" if seconds >= 0 else "-"
    with db._cursor(commit=True) as cur:
        if db.db_type == "postgresql":
            # datetime() is SQLite-only; ended_at is a real TIMESTAMP here
            cur.execute(
                "UPDATE rollcalls SET ended_at = ended_at + %s::interval WHERE id = %s",
                (f"{sign}{abs(seconds)} seconds", rollcall_id),
            )
        else:
            cur.execute(
                "UPDATE rollcalls SET ended_at = datetime(ended_at, ?) WHERE id = ?",
                (f"{sign}{abs(seconds)} seconds", rollcall_id),
            )


class SeasonBase(IntegrationBase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from handlers.dues import (
            new_season_cmd, new_season_confirm_callback, new_season_cancel_callback,
            pick_collector_callback, collector_upi_callback,
            _settle_nudge_msgs, _pending_colupi,
        )
        cls.new_season_cmd = staticmethod(new_season_cmd)
        cls.new_season_confirm_callback = staticmethod(new_season_confirm_callback)
        cls.new_season_cancel_callback = staticmethod(new_season_cancel_callback)
        cls.pick_collector_callback = staticmethod(pick_collector_callback)
        cls.collector_upi_callback = staticmethod(collector_upi_callback)
        cls.settle_nudge_msgs = _settle_nudge_msgs
        cls.pending_colupi = _pending_colupi

    def setUp(self):
        super().setUp()
        self.settle_nudge_msgs.clear()
        self.pending_colupi.clear()
        bot = get_mock_bot()
        bot.pin_chat_message = AsyncMock()
        bot.unpin_chat_message = AsyncMock()

    async def _play_and_end(self, title="Game", fee="300", n_in=3, bump=120):
        rc = await self.start_rc(title)
        rc.event_fee = fee
        rc_db_id = rc.db_id
        for u in USERS[:n_in]:
            await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        _bump_ended_at(rc_db_id, bump)
        return rc_db_id

    async def _settle(self, subsidy=0):
        await self.dues_settle_dues(self.msg(f"/settle_dues {subsidy}", ADMIN_USER))


class TestDuesEpoch(SeasonBase):

    async def test_games_played_while_disabled_never_surface(self):
        """The Mumbai FC scenario: cash-period games must not become
        settleable on re-enable."""
        _enable()
        rc1 = await self._play_and_end("January Game")
        await self._settle()
        self.assertIsNotNone(db.get_game_closure(rc1))

        # Disable → play a cash game → its rollcall ends with no closure
        # (ended_at backdated well before the re-enable epoch below)
        db.update_chat_settings(CHAT_ID, dues_enabled=0)
        rc2 = await self._play_and_end("March Cash Game", bump=-3600)
        self.assertIsNone(db.get_game_closure(rc2))

        # Re-enable via the real handler → epoch stamped now
        await self.dues_enable(self.msg("/enable_dues", ADMIN_USER))
        self.assertTrue(db.get_or_create_chat(CHAT_ID).get("dues_epoch"))

        # The cash game is invisible to every settle path
        self.assertEqual(db.get_unsettled_rollcalls(CHAT_ID), [])
        self.assertIsNone(db.get_latest_closeable_rollcall(CHAT_ID))
        get_mock_bot().send_message.reset_mock()
        await self.dues_settle_dues(self.msg("/settle_dues", ADMIN_USER))
        self.assertTrue(any("No game to close" in t for t in self.sent_texts()),
                        self.sent_texts())

        # ...but a NEW game after re-enable is settleable as normal
        rc3 = await self._play_and_end("May Game")
        unsettled = db.get_unsettled_rollcalls(CHAT_ID)
        self.assertEqual([g["id"] for g in unsettled], [rc3])

    async def test_first_enable_hides_pre_dues_history(self):
        # Group used the bot for attendance only, then turns dues on
        db.get_or_create_chat(CHAT_ID)
        old = await self._play_and_end("Pre-dues Game", bump=-3600)
        await self.dues_enable(self.msg("/enable_dues", ADMIN_USER))
        db.update_chat_settings(CHAT_ID, upi_vpa="x@y")
        self.assertEqual(db.get_unsettled_rollcalls(CHAT_ID), [])
        self.assertIsNone(db.get_game_closure(old))  # history untouched, just not offered

    async def test_reenable_while_on_does_not_move_epoch(self):
        _enable()
        await self.dues_enable(self.msg("/enable_dues", ADMIN_USER))  # first stamp? no —
        # _enable() set the flag directly, so the handler sees enabled→enabled
        epoch_before = db.get_or_create_chat(CHAT_ID).get("dues_epoch")
        await self.dues_enable(self.msg("/enable_dues", ADMIN_USER))
        self.assertEqual(db.get_or_create_chat(CHAT_ID).get("dues_epoch"), epoch_before)


class TestNewSeasonFlow(SeasonBase):

    async def _build_balances(self):
        _enable()
        await self._play_and_end("Game A", fee="300", n_in=3)   # 3 × ₹100 owed
        await self._settle()
        db.add_fund_transaction(CHAT_ID, None, "topup", 500, "seed", 999, "Admin")

    async def test_full_reset_carry_fund(self):
        await self._build_balances()
        entries_before = len(db.get_dues_entries(CHAT_ID, limit=100))
        self.assertEqual(sum(b["balance"] for b in db.get_all_dues_balances(CHAT_ID)), 300)

        await self.new_season_cmd(self.msg("/new_season", ADMIN_USER))
        self.assertTrue(any("forgiven" in t for t in self.sent_texts()))

        await self.new_season_confirm_callback(self.call("season_go:carry", ADMIN_USER))

        # Every balance zero; original entries untouched (append-only)
        for b in db.get_all_dues_balances(CHAT_ID):
            self.assertEqual(b["balance"], 0)
        entries_after = db.get_dues_entries(CHAT_ID, limit=100)
        self.assertEqual(len(entries_after), entries_before + 3)  # 3 adjustments appended
        # Fund carried forward
        self.assertEqual(db.get_fund_balance(CHAT_ID), 500)
        # Epoch stamped
        self.assertTrue(db.get_or_create_chat(CHAT_ID).get("dues_epoch"))
        # Announcement posted
        self.assertTrue(any("Season closed" in t for t in self.sent_texts()))

    async def test_full_reset_zero_fund(self):
        await self._build_balances()
        await self.new_season_cmd(self.msg("/new_season", ADMIN_USER))
        await self.new_season_confirm_callback(self.call("season_go:zero", ADMIN_USER))
        self.assertEqual(db.get_fund_balance(CHAT_ID), 0)

    async def test_blocked_by_unsettled_game(self):
        _enable()
        await self._play_and_end("Unsettled Game")
        get_mock_bot().send_message.reset_mock()
        await self.new_season_cmd(self.msg("/new_season", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("still unsettled" in t for t in texts), texts)
        self.assertFalse(any("Close the season?" in t for t in texts))

    async def test_cancel_leaves_everything_untouched(self):
        await self._build_balances()
        await self.new_season_cmd(self.msg("/new_season", ADMIN_USER))
        await self.new_season_cancel_callback(self.call("season_cancel", ADMIN_USER))
        self.assertEqual(sum(b["balance"] for b in db.get_all_dues_balances(CHAT_ID)), 300)
        self.assertEqual(db.get_fund_balance(CHAT_ID), 500)

    async def test_non_admin_cannot_confirm(self):
        await self._build_balances()
        await self.new_season_cmd(self.msg("/new_season", ADMIN_USER))
        with patch("handlers.dues.is_chat_admin", new=AsyncMock(return_value=False)):
            await self.new_season_confirm_callback(self.call("season_go:zero", USERS[0]))
        self.assertEqual(sum(b["balance"] for b in db.get_all_dues_balances(CHAT_ID)), 300)


class TestCollectorUpiMemoryE2E(SeasonBase):

    async def test_returning_collector_gets_offer_and_use_applies(self):
        _enable()
        # Game 1: User1 collects with a personal UPI, game settles → history
        rc1 = await self.start_rc("Game 1")
        rc1.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_set_collector(self.msg("/set_collector user1 user1@okbank", ADMIN_USER))
        await self._settle()
        self.assertEqual(db.get_last_collector_upi(CHAT_ID, USERS[0]["id"]), "user1@okbank")

        # Game 2: pick the same collector from the panel → memory card appears
        rc2 = await self.start_rc("Game 2")
        rc2.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        get_mock_bot().send_message.reset_mock()
        await self.pick_collector_callback(
            self.call(f"pickcol_0_{USERS[0]['id']}", ADMIN_USER))
        offer = next((t for t in self.sent_texts() if "user1@okbank" in t), None)
        self.assertIsNotNone(offer, f"UPI memory card not offered: {self.sent_texts()}")
        (card_key,) = [k for k in self.pending_colupi if k[0] == CHAT_ID]

        # Tap ✅ Use it → this game's collector UPI is set
        await self.collector_upi_callback(
            self.call("colupi_use", ADMIN_USER, message_id=card_key[1]))
        self.assertEqual(getattr(self.rc(0), "collector_upi", None), "user1@okbank")

    async def test_first_time_collector_no_card(self):
        _enable()
        rc = await self.start_rc("Game 1")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        get_mock_bot().send_message.reset_mock()
        await self.pick_collector_callback(
            self.call(f"pickcol_0_{USERS[1]['id']}", ADMIN_USER))
        self.assertEqual(self.pending_colupi, {})
        self.assertFalse(any("usual UPI" in t for t in self.sent_texts()))


if __name__ == "__main__":
    import unittest
    unittest.main()
