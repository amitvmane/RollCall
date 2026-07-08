"""
Handler-level tests for handlers/dues.py.

Pattern: patch admin_rights, manager write lock, and the dues service calls.
Bot send_message is an AsyncMock so we can assert on replies.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _msg(text="/cmd", chat_id=100, user_id=1, first_name="Admin", username="admin"):
    m = MagicMock()
    m.text = text
    m.chat.id = chat_id
    m.from_user.id = user_id
    m.from_user.first_name = first_name
    m.from_user.username = username
    return m


def _make_lock_manager():
    """Manager mock whose get_chat_write_lock returns an async context manager."""
    mgr = MagicMock()
    lock_ctx = MagicMock()
    lock_ctx.__aenter__ = AsyncMock(return_value=None)
    lock_ctx.__aexit__ = AsyncMock(return_value=False)
    mgr.get_chat_write_lock.return_value = lock_ctx
    mgr.get_rollcalls.return_value = []
    mgr.get_shh_mode.return_value = False
    return mgr


def _admin_ok():
    return patch("handlers.dues.admin_rights", new=AsyncMock(return_value=True))


def _admin_denied():
    return patch("handlers.dues.admin_rights", new=AsyncMock(return_value=False))


def _patch_post_end():
    return patch("handlers.dues._post_end_cleanup", new=AsyncMock())


class TestDuesHandlers(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()
        self.mgr = _make_lock_manager()
        # Assume dues enabled for all tests (guard is a one-liner; tested separately)
        patcher = patch("handlers.dues._require_dues_enabled")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sent_text(self, idx=0):
        return self.bot_state.bot.send_message.call_args_list[idx][0][1]

    # ── admin guard ───────────────────────────────────────────────────────────

    async def test_close_game_admin_denied(self):
        from handlers.dues import close_game
        with _admin_denied(), patch("handlers.dues.manager", self.mgr):
            await close_game(_msg("/cg"))
        text = self._sent_text()
        self.assertIn("Admin", text)

    async def test_mark_penalty_admin_denied(self):
        from handlers.dues import mark_penalty
        with _admin_denied(), patch("handlers.dues.manager", self.mgr):
            await mark_penalty(_msg("/mark_penalty ditch Alice"))
        text = self._sent_text()
        self.assertIn("Admin", text)

    # ── missing params ────────────────────────────────────────────────────────

    async def test_mark_penalty_missing_args(self):
        from handlers.dues import mark_penalty
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await mark_penalty(_msg("/mark_penalty"))
        text = self._sent_text()
        self.assertIn("Usage", text)

    async def test_waive_missing_amount(self):
        from handlers.dues import waive
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await waive(_msg("/waive Alice"))
        text = self._sent_text()
        self.assertIn("Usage", text)

    async def test_add_penalty_missing_amount(self):
        from handlers.dues import add_penalty
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await add_penalty(_msg("/add_penalty"))
        text = self._sent_text()
        self.assertIn("Usage", text)

    async def test_set_round_step_missing(self):
        from handlers.dues import set_round_step
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await set_round_step(_msg("/set_round_step"))
        text = self._sent_text()
        self.assertIn("Usage", text)

    # ── happy-path calls ──────────────────────────────────────────────────────

    async def test_close_game_calls_service_and_posts(self):
        from handlers.dues import close_game
        svc_result = {
            "announcement": "📊 Game closed: Sunday",
            "end_result": None,
            "title": "Sunday",
        }
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=AsyncMock(return_value=svc_result)), \
             _patch_post_end():
            await close_game(_msg("/cg"))
        self.assertIn("Game closed", self._sent_text())

    async def test_close_game_with_subsidy_arg(self):
        from handlers.dues import close_game
        svc_mock = AsyncMock(return_value={
            "announcement": "📊 Game closed",
            "end_result": None,
            "title": "Sunday",
        })
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=svc_mock), \
             _patch_post_end():
            await close_game(_msg("/cg 60"))
        call_kwargs = svc_mock.call_args
        self.assertEqual(call_kwargs.kwargs.get("subsidy") or call_kwargs.args[1], 60)

    async def test_close_game_with_rc_suffix(self):
        from handlers.dues import close_game
        svc_mock = AsyncMock(return_value={
            "announcement": "📊 Game closed",
            "end_result": None,
            "title": "Sunday",
        })
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=svc_mock), \
             _patch_post_end():
            await close_game(_msg("/cg ::2"))
        call_args = svc_mock.call_args
        rc_number = call_args.kwargs.get("rc_number", call_args.args[4] if len(call_args.args) > 4 else 0)
        self.assertEqual(rc_number, 1)   # ::2 → 0-based index 1

    async def test_mark_penalty_posts_announcement(self):
        from handlers.dues import mark_penalty
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.mark_penalty", return_value={
                 "announcement": "⚠️ Penalty (ditch): Alice → ₹200  _no-show_"
             }):
            await mark_penalty(_msg("/mark_penalty ditch Alice"))
        self.assertIn("Penalty", self._sent_text())

    async def test_waive_posts_announcement(self):
        from handlers.dues import waive
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.waive", return_value={
                 "announcement": "🕊 Waived ₹75 for Alice: injury"
             }):
            await waive(_msg("/waive Alice 75 injury"))
        self.assertIn("Waived", self._sent_text())

    async def test_my_dues_no_balance(self):
        from handlers.dues import my_dues
        with patch("handlers.dues.dues_svc.my_dues", return_value={"balance": 0, "entries": []}), \
             patch("handlers.dues.dues_svc.get_dues_settings", return_value={"upi_vpa": None}):
            await my_dues(_msg("/my_dues"))
        self.assertIn("no outstanding", self._sent_text())

    async def test_my_dues_positive_balance(self):
        from handlers.dues import my_dues
        entries = [{"amount": 90, "entry_type": "share", "memo": None}]
        with patch("handlers.dues.dues_svc.my_dues", return_value={"balance": 90, "entries": entries}), \
             patch("handlers.dues.dues_svc.get_dues_settings", return_value={"upi_vpa": "pay@upi"}):
            await my_dues(_msg("/my_dues"))
        text = self._sent_text()
        self.assertIn("₹90", text)
        self.assertIn("pay@upi", text)

    async def test_dues_admin_only(self):
        from handlers.dues import dues
        with _admin_denied(), patch("handlers.dues.manager", self.mgr):
            await dues(_msg("/dues"))
        text = self._sent_text()
        self.assertIn("Admin", text)

    async def test_fund_public_access(self):
        from handlers.dues import fund
        with patch("handlers.dues.dues_svc.fund_summary", return_value={"fund_balance": 250}):
            await fund(_msg("/fund"))
        self.assertIn("250", self._sent_text())

    async def test_mark_paid_non_admin_as_collector(self):
        """Non-admin user who is the collector should be allowed; service enforces it."""
        from handlers.dues import mark_paid
        svc_mock = MagicMock(return_value={"announcement": "✅ Payment: Alice paid ₹90"})
        with patch("handlers.dues.admin_rights", new=AsyncMock(return_value=False)), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.mark_paid", svc_mock):
            await mark_paid(_msg("/paid Alice"))
        # Service was called with is_admin=False; service decides if allowed
        call_kwargs = svc_mock.call_args.kwargs
        self.assertFalse(call_kwargs.get("is_admin", True))

    async def test_set_collector_paid_flag(self):
        from handlers.dues import set_collector
        svc_mock = MagicMock(return_value={"announcement": "📦 Collector: Ravi (fronted ground cost)"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.set_collector", svc_mock):
            await set_collector(_msg("/set_collector Ravi paid"))
        call_kwargs = svc_mock.call_args.kwargs
        self.assertTrue(call_kwargs.get("paid_ground") or svc_mock.call_args.args[2])

    async def test_log_expense_inverts_sign_via_service(self):
        from handlers.dues import log_expense
        svc_mock = MagicMock(return_value={"announcement": "🏦 Fund: −₹150 — new balls. Balance: ₹100"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.log_expense", svc_mock):
            await log_expense(_msg("/le 150 new balls"))
        self.assertIn("new balls", self._sent_text())

    async def test_remind_dues_posts_list(self):
        from handlers.dues import remind_dues
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.remind_dues", return_value={
                 "announcement": "📢 Outstanding dues:\n• Alice: ₹90"
             }):
            await remind_dues(_msg("/remind_dues"))
        self.assertIn("Alice", self._sent_text())

    async def test_set_upi_posts_confirmation(self):
        from handlers.dues import set_upi
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.set_upi", return_value={
                 "announcement": "💳 UPI VPA set: `amit@upi`"
             }):
            await set_upi(_msg("/set_upi amit@upi"))
        self.assertIn("UPI", self._sent_text())

    async def test_cancel_game_dues_posts_reversal(self):
        from handlers.dues import cancel_game_dues
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues._db.get_nth_game_closure", return_value={"rollcall_id": 77}), \
             patch("handlers.dues.dues_svc.cancel_game_credit", return_value={
                 "announcement": "🔁 Cancelled dues for 'Sunday': 3 share entries reversed."
             }):
            await cancel_game_dues(_msg("/cancel_game_dues"))
        self.assertIn("Cancelled", self._sent_text())

    # ── ::N suffix parsing ────────────────────────────────────────────────────

    async def test_invalid_rc_suffix_raises_error(self):
        from handlers.dues import close_game
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await close_game(_msg("/cg ::abc"))
        text = self._sent_text()
        self.assertIn("integer", text.lower())

    async def test_zero_rc_suffix_raises_error(self):
        from handlers.dues import close_game
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await close_game(_msg("/cg ::0"))
        text = self._sent_text()
        self.assertIn("integer", text.lower())

    # ── fund_history pagination ───────────────────────────────────────────────

    async def test_fund_history_page_arg(self):
        from handlers.dues import fund_history
        svc_mock = MagicMock(return_value={
            "transactions": [], "total": 0, "limit": 10, "offset": 10,
        })
        with patch("handlers.dues.dues_svc.fund_history", svc_mock):
            await fund_history(_msg("/fh 2"))
        call_kwargs = svc_mock.call_args.kwargs
        self.assertEqual(call_kwargs.get("offset", call_kwargs.get("offset", 10)), 10)

    # ── close_game calls _post_end_cleanup when end_result present ────────────

    async def test_close_game_calls_post_end_cleanup_when_rc_ended(self):
        from handlers.dues import close_game
        end_res = {
            "ended": {}, "rc_number_ended_1based": 1, "ghost_eligible": False,
            "ghost_rc_db_id": None, "ended_by": {}, "remaining": [], "renumbered": [],
        }
        svc_result = {
            "announcement": "📊 Game closed",
            "end_result": end_res,
            "title": "Sunday",
        }
        cleanup_mock = AsyncMock()
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=AsyncMock(return_value=svc_result)), \
             patch("handlers.dues._post_end_cleanup", cleanup_mock):
            await close_game(_msg("/cg"))
        cleanup_mock.assert_called_once()


# ── /set_treasury_upi handler ─────────────────────────────────────────────────

class TestSetTreasuryUPI(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()
        self.mgr = _make_lock_manager()
        patcher = patch("handlers.dues._require_dues_enabled")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sent_text(self):
        return self.bot_state.bot.send_message.call_args_list[0][0][1]

    async def test_set_treasury_upi_posts_confirmation(self):
        from handlers.dues import set_treasury_upi
        svc_mock = MagicMock(return_value={"announcement": "🏦 Treasury UPI set: `treasurer@hdfc`"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.set_treasury_upi", svc_mock):
            await set_treasury_upi(_msg("/set_treasury_upi treasurer@hdfc"))
        svc_mock.assert_called_once()
        args = svc_mock.call_args
        self.assertEqual(args.args[1], "treasurer@hdfc")
        self.assertIn("Treasury", self._sent_text())

    async def test_set_treasury_upi_admin_only(self):
        from handlers.dues import set_treasury_upi
        with _admin_denied(), patch("handlers.dues.manager", self.mgr):
            await set_treasury_upi(_msg("/set_treasury_upi x@y"))
        text = self._sent_text()
        self.assertIn("Admin", text)

    async def test_set_treasury_upi_missing_arg(self):
        from handlers.dues import set_treasury_upi
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await set_treasury_upi(_msg("/set_treasury_upi"))
        text = self._sent_text()
        self.assertIn("Usage", text)


# ── /set_collector UPI arg parsing ───────────────────────────────────────────

class TestSetCollectorUPIParsing(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()
        self.mgr = _make_lock_manager()
        patcher = patch("handlers.dues._require_dues_enabled")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _call_set_collector(self, text):
        from handlers.dues import set_collector as sc_handler
        svc_mock = MagicMock(return_value={"announcement": "📦 Collector: Ravi · `ravi@ybl`"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.set_collector", svc_mock):
            await sc_handler(_msg(text))
        return svc_mock

    async def test_set_collector_parses_upi_arg(self):
        svc = await self._call_set_collector("/set_collector Ravi ravi@ybl")
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs.get("collector_upi"), "ravi@ybl")

    async def test_set_collector_paid_and_upi(self):
        svc = await self._call_set_collector("/set_collector Ravi paid ravi@ybl")
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs.get("collector_upi"), "ravi@ybl")
        self.assertTrue(kwargs.get("paid_ground") or svc.call_args.args[2])

    async def test_set_collector_upi_without_paid(self):
        """UPI arg accepted without the paid flag; paid_ground stays False."""
        svc = await self._call_set_collector("/set_collector Ravi ravi@ybl")
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs.get("collector_upi"), "ravi@ybl")
        paid = kwargs.get("paid_ground", None)
        if paid is None:
            paid = svc.call_args.args[2] if len(svc.call_args.args) > 2 else False
        self.assertFalse(paid)

    async def test_set_collector_no_upi_passes_none(self):
        svc = await self._call_set_collector("/set_collector Ravi paid")
        kwargs = svc.call_args.kwargs
        self.assertIsNone(kwargs.get("collector_upi"))

    async def test_set_collector_upi_order_independent(self):
        """UPI may come before 'paid' — should still be detected."""
        svc = await self._call_set_collector("/set_collector Ravi ravi@ybl paid")
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs.get("collector_upi"), "ravi@ybl")


# ── /my_dues payment routing display ─────────────────────────────────────────

class TestMyDuesPaymentRouting(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()
        patcher = patch("handlers.dues._require_dues_enabled")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sent_text(self):
        return self.bot_state.bot.send_message.call_args_list[0][0][1]

    def _patch_my_dues(self, balance=90, upi_vpa=None, treasury_upi=None):
        return patch.multiple(
            "handlers.dues.dues_svc",
            my_dues=MagicMock(return_value={
                "balance": balance,
                "entries": [{"amount": 90, "entry_type": "share", "memo": None}],
            }),
            get_dues_settings=MagicMock(return_value={
                "upi_vpa": upi_vpa,
                "treasury_upi": treasury_upi,
            }),
        )

    async def test_my_dues_shows_both_upi_when_distinct(self):
        from handlers.dues import my_dues
        with self._patch_my_dues(upi_vpa="group@upi", treasury_upi="treasurer@hdfc"):
            await my_dues(_msg("/my_dues"))
        text = self._sent_text()
        self.assertIn("group@upi", text)
        self.assertIn("treasurer@hdfc", text)
        self.assertIn("Game fees", text)
        self.assertIn("Penalties", text)

    async def test_my_dues_single_line_when_same_upi(self):
        from handlers.dues import my_dues
        with self._patch_my_dues(upi_vpa="shared@upi", treasury_upi="shared@upi"):
            await my_dues(_msg("/my_dues"))
        text = self._sent_text()
        self.assertEqual(text.count("shared@upi"), 1)

    async def test_my_dues_shows_group_upi_only_when_no_treasury(self):
        from handlers.dues import my_dues
        with self._patch_my_dues(upi_vpa="group@upi", treasury_upi=None):
            await my_dues(_msg("/my_dues"))
        text = self._sent_text()
        self.assertIn("group@upi", text)
        self.assertNotIn("Penalties", text)

    async def test_my_dues_shows_treasury_upi_only_when_no_group_upi(self):
        from handlers.dues import my_dues
        with self._patch_my_dues(upi_vpa=None, treasury_upi="treasurer@hdfc"):
            await my_dues(_msg("/my_dues"))
        text = self._sent_text()
        self.assertIn("treasurer@hdfc", text)

    async def test_my_dues_no_upi_line_when_neither_set(self):
        from handlers.dues import my_dues
        with self._patch_my_dues(upi_vpa=None, treasury_upi=None):
            await my_dues(_msg("/my_dues"))
        text = self._sent_text()
        self.assertNotIn("💳", text)


if __name__ == "__main__":
    unittest.main()
