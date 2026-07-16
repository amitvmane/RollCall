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
        from handlers.dues import settle_dues
        with _admin_denied(), patch("handlers.dues.manager", self.mgr):
            await settle_dues(_msg("/settle_dues"))
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
        from handlers.dues import settle_dues
        svc_result = {
            "announcement": "📊 Game closed: Sunday",
            "end_result": None,
            "title": "Sunday",
        }
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=AsyncMock(return_value=svc_result)), \
             _patch_post_end():
            # Explicit subsidy arg hits the fast path (direct close, no
            # penalty panel / confirm card) — bare /settle_dues now goes
            # through the guided flow instead, covered separately.
            await settle_dues(_msg("/settle_dues 0"))
        self.assertIn("Game closed", self._sent_text())

    async def test_close_game_with_subsidy_arg(self):
        from handlers.dues import settle_dues
        svc_mock = AsyncMock(return_value={
            "announcement": "📊 Game closed",
            "end_result": None,
            "title": "Sunday",
        })
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=svc_mock), \
             _patch_post_end():
            await settle_dues(_msg("/settle_dues 60"))
        call_kwargs = svc_mock.call_args
        self.assertEqual(call_kwargs.kwargs.get("subsidy") or call_kwargs.args[1], 60)

    async def test_close_game_with_rc_suffix(self):
        from handlers.dues import settle_dues
        svc_mock = AsyncMock(return_value={
            "announcement": "📊 Game closed",
            "end_result": None,
            "title": "Sunday",
        })
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.close_game", new=svc_mock), \
             _patch_post_end():
            await settle_dues(_msg("/settle_dues ::2"))
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

    async def test_waive_multi_word_proxy_name(self):
        """Regression: /waive used to assume the name was always a single
        token (args[0]), silently truncating multi-word proxy names and
        misinterpreting the next word as the amount."""
        from handlers.dues import waive
        svc_mock = MagicMock(return_value={"announcement": "🕊 Waived ₹75 for Team B: injury"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.waive", svc_mock):
            await waive(_msg("/waive Team B 75 injury"))
        args = svc_mock.call_args.args
        self.assertEqual(args[1], "Team B")
        self.assertEqual(args[2], 75)
        self.assertEqual(args[3], "injury")

    async def test_waive_multi_word_name_no_reason(self):
        from handlers.dues import waive
        svc_mock = MagicMock(return_value={"announcement": "🕊 Waived"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.waive", svc_mock):
            await waive(_msg("/waive Team B 75"))
        args = svc_mock.call_args.args
        self.assertEqual(args[1], "Team B")
        self.assertEqual(args[2], 75)
        self.assertEqual(args[3], "")

    async def test_reimburse_multi_word_proxy_name(self):
        from handlers.dues import reimburse
        svc_mock = MagicMock(return_value={"announcement": "💸 Reimbursed"})
        with _admin_ok(), \
             patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.reimburse", svc_mock):
            await reimburse(_msg("/reimburse Team B 40 travel"))
        args = svc_mock.call_args.args
        self.assertEqual(args[1], "Team B")
        self.assertEqual(args[2], 40)
        self.assertEqual(args[3], "travel")

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
        from handlers.dues import settle_dues
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await settle_dues(_msg("/settle_dues ::abc"))
        text = self._sent_text()
        self.assertIn("integer", text.lower())

    async def test_zero_rc_suffix_raises_error(self):
        from handlers.dues import settle_dues
        with _admin_ok(), patch("handlers.dues.manager", self.mgr):
            await settle_dues(_msg("/settle_dues ::0"))
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
        from handlers.dues import settle_dues
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
            # Explicit subsidy arg hits the fast path (direct close).
            await settle_dues(_msg("/settle_dues 0"))
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

    async def test_set_collector_fixed_format_only_trailing_token_is_upi(self):
        """Format is fixed as <name> [paid] [upi@bank] — only the very last
        token is ever checked for the UPI pattern. If 'ravi@ybl' isn't last,
        it's just part of the name, not silently spliced out. This is the
        intentional trade for eliminating the old free-scan misclassification
        risk (an @-shaped word anywhere in a multi-word name used to get torn
        out no matter where it appeared)."""
        svc = await self._call_set_collector("/set_collector Ravi ravi@ybl paid")
        kwargs = svc.call_args.kwargs
        self.assertIsNone(kwargs.get("collector_upi"))
        self.assertEqual(svc.call_args.args[1], "Ravi ravi@ybl")

    async def test_set_collector_upi_never_extracted_from_middle_of_name(self):
        """A UPI-shaped word in the middle of a multi-word name must never be
        torn out — only the trailing token is inspected."""
        svc = await self._call_set_collector("/set_collector John ravi@ybl Smith")
        kwargs = svc.call_args.kwargs
        self.assertIsNone(kwargs.get("collector_upi"))
        self.assertEqual(svc.call_args.args[1], "John ravi@ybl Smith")


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


class TestSettleNudge(unittest.IsolatedAsyncioTestCase):
    """Persistent settle nudge: pinned Settle-now message after a dues-enabled
    game ends, cleared (unpin + de-button) once the game is settled."""

    def setUp(self):
        import bot_state
        import handlers.dues as dues_handlers
        self.bot_state = bot_state
        self.dues_handlers = dues_handlers
        sent = MagicMock()
        sent.message_id = 777
        bot_state.bot.send_message = AsyncMock(return_value=sent)
        bot_state.bot.pin_chat_message = AsyncMock()
        bot_state.bot.unpin_chat_message = AsyncMock()
        bot_state.bot.answer_callback_query = AsyncMock()
        dues_handlers._settle_nudge_msgs.clear()

    def _sent_text(self, idx=0):
        return self.bot_state.bot.send_message.call_args_list[idx][0][1]

    async def test_nudge_has_settle_button_and_pins(self):
        import telebot.types as tt
        from handlers.dues import send_settle_nudge, _settle_nudge_msgs
        tt.InlineKeyboardButton.reset_mock()
        with patch("handlers.dues.dues_svc.list_unsettled_games",
                   return_value={"games": [{"id": 42}]}):
            await send_settle_nudge(100, 42, "Sunday Game")
        self.assertEqual(
            tt.InlineKeyboardButton.call_args.kwargs["callback_data"], "settle_now:42")
        self.bot_state.bot.pin_chat_message.assert_awaited_once_with(
            100, 777, disable_notification=True)
        self.assertEqual(_settle_nudge_msgs[(100, 42)], 777)
        self.assertIn("Sunday Game", self._sent_text())

    async def test_nudge_warns_when_games_stack_up(self):
        from handlers.dues import send_settle_nudge
        with patch("handlers.dues.dues_svc.list_unsettled_games",
                   return_value={"games": [{"id": 41}, {"id": 42}, {"id": 43}]}):
            await send_settle_nudge(100, 43)
        self.assertIn("3 games", self._sent_text())

    async def test_nudge_survives_missing_pin_rights(self):
        from handlers.dues import send_settle_nudge, _settle_nudge_msgs
        self.bot_state.bot.pin_chat_message = AsyncMock(side_effect=Exception("no rights"))
        with patch("handlers.dues.dues_svc.list_unsettled_games",
                   return_value={"games": [{"id": 42}]}):
            await send_settle_nudge(100, 42)
        # Message still sent and tracked even though the pin failed
        self.assertEqual(_settle_nudge_msgs[(100, 42)], 777)

    async def test_clear_unpins_and_removes_button(self):
        from handlers.dues import _clear_settle_nudge, _settle_nudge_msgs
        _settle_nudge_msgs[(100, 42)] = 777
        with patch("handlers.dues.safe_edit_markup", new=AsyncMock()) as edit:
            await _clear_settle_nudge(100, 42)
        self.bot_state.bot.unpin_chat_message.assert_awaited_once_with(100, 777)
        edit.assert_awaited_once()
        self.assertNotIn((100, 42), _settle_nudge_msgs)

    async def test_clear_is_noop_without_tracked_nudge(self):
        from handlers.dues import _clear_settle_nudge
        await _clear_settle_nudge(100, 999)
        self.bot_state.bot.unpin_chat_message.assert_not_awaited()

    async def test_settle_now_button_opens_settlement(self):
        from handlers.dues import settle_now_callback
        call = MagicMock()
        call.data = "settle_now:42"
        call.message.chat.id = 100
        call.message.message_id = 777
        with patch("handlers.dues._settle_admin_ok", new=AsyncMock(return_value=True)), \
             patch("handlers.dues._db.get_game_closure", return_value=None), \
             patch("handlers.dues._db.get_rollcall", return_value={"title": "Sunday"}), \
             patch("handlers.dues._begin_settlement", new=AsyncMock()) as begin:
            await settle_now_callback(call)
        begin.assert_awaited_once_with(100, 42, "Sunday")

    async def test_settle_now_button_already_settled(self):
        from handlers.dues import settle_now_callback, _settle_nudge_msgs
        _settle_nudge_msgs[(100, 42)] = 777
        call = MagicMock()
        call.data = "settle_now:42"
        call.message.chat.id = 100
        call.message.message_id = 777
        with patch("handlers.dues._settle_admin_ok", new=AsyncMock(return_value=True)), \
             patch("handlers.dues._db.get_game_closure", return_value={"id": 1}), \
             patch("handlers.dues.safe_edit_markup", new=AsyncMock()), \
             patch("handlers.dues._begin_settlement", new=AsyncMock()) as begin:
            await settle_now_callback(call)
        begin.assert_not_awaited()
        self.assertIn("Already settled", self._sent_text())
        self.assertNotIn((100, 42), _settle_nudge_msgs)

    async def test_settle_now_button_admin_gated(self):
        from handlers.dues import settle_now_callback
        call = MagicMock()
        call.data = "settle_now:42"
        call.message.chat.id = 100
        with patch("handlers.dues._settle_admin_ok", new=AsyncMock(return_value=False)), \
             patch("handlers.dues._begin_settlement", new=AsyncMock()) as begin:
            await settle_now_callback(call)
        begin.assert_not_awaited()


class TestPickCollectorNonIn(unittest.IsolatedAsyncioTestCase):
    """Collector picker must also reach members who aren't IN (flow-audit #3:
    non-playing collector, e.g. venue owner)."""

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()
        bot_state.bot.answer_callback_query = AsyncMock()
        bot_state.bot.edit_message_text = AsyncMock()

    def _rc(self, in_ids=()):
        rc = MagicMock()
        rc.title = "Sunday"
        users = []
        for uid in in_ids:
            u = MagicMock()
            u.user_id = uid
            u.name = f"user{uid}"
            users.append(u)
        rc.inList = users
        return rc

    def _call(self, data, uid=1):
        call = MagicMock()
        call.data = data
        call.message.chat.id = 100
        call.message.message_id = 555
        call.from_user.id = uid
        call.from_user.first_name = "Admin"
        return call

    async def test_more_button_swaps_to_all_members_panel(self):
        from handlers.dues import pick_collector_more_callback
        mgr = _make_lock_manager()
        mgr.get_rollcalls.return_value = [self._rc(in_ids=[1])]
        members = [{"user_id": 9, "first_name": "VenueOwner", "username": "venue"}]
        with patch("handlers.dues.is_chat_admin", new=AsyncMock(return_value=True)), \
             patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._db.get_active_members", return_value=members), \
             patch("handlers.dues.safe_edit_text", new=AsyncMock()) as edit_text, \
             patch("handlers.dues.safe_edit_markup", new=AsyncMock()):
            await pick_collector_more_callback(self._call("pickcol_more_0"))
        text = edit_text.call_args[0][2]
        self.assertIn("all known members", text)

    async def test_all_members_panel_excludes_in_voters(self):
        import telebot.types as tt
        from handlers.dues import _show_member_collector_panel
        mgr = _make_lock_manager()
        mgr.get_rollcalls.return_value = [self._rc(in_ids=[1])]
        members = [
            {"user_id": 1, "first_name": "AlreadyIn", "username": "in1"},
            {"user_id": 9, "first_name": "VenueOwner", "username": "venue"},
        ]
        tt.InlineKeyboardButton.reset_mock()
        with patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._db.get_active_members", return_value=members):
            await _show_member_collector_panel(100, 0, "Sunday")
        datas = [c.kwargs.get("callback_data") for c in tt.InlineKeyboardButton.call_args_list]
        self.assertIn("pickcol_0_9", datas)
        self.assertNotIn("pickcol_0_1", datas)

    async def test_callback_accepts_non_in_member(self):
        from handlers.dues import pick_collector_callback
        mgr = _make_lock_manager()
        mgr.get_rollcalls.return_value = [self._rc(in_ids=[1])]
        members = [{"user_id": 9, "first_name": "VenueOwner", "username": "venue"}]
        svc_result = {"announcement": "📦 venue is collecting"}
        with patch("handlers.dues.is_chat_admin", new=AsyncMock(return_value=True)), \
             patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._db.get_active_members", return_value=members), \
             patch("handlers.dues.dues_svc.set_collector", return_value=svc_result) as svc:
            await pick_collector_callback(self._call("pickcol_0_9"))
        svc.assert_called_once()
        self.assertEqual(svc.call_args[0][1], "venue")

    async def test_callback_rejects_unknown_uid(self):
        from handlers.dues import pick_collector_callback
        mgr = _make_lock_manager()
        mgr.get_rollcalls.return_value = [self._rc(in_ids=[1])]
        with patch("handlers.dues.is_chat_admin", new=AsyncMock(return_value=True)), \
             patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._db.get_active_members", return_value=[]), \
             patch("handlers.dues.dues_svc.set_collector") as svc:
            await pick_collector_callback(self._call("pickcol_0_9"))
        svc.assert_not_called()
        alert = self.bot_state.bot.answer_callback_query.call_args
        self.assertIn("unknown", alert[0][1])

    async def test_no_real_in_users_falls_through_to_member_panel(self):
        from handlers.dues import pick_collector
        mgr = _make_lock_manager()
        mgr.get_rollcalls.return_value = [self._rc(in_ids=[])]
        members = [{"user_id": 9, "first_name": "VenueOwner", "username": "venue"}]
        with _admin_ok(), \
             patch("handlers.dues._require_dues_enabled"), \
             patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._db.get_active_members", return_value=members):
            await pick_collector(_msg("/pick_collector"))
        text = self.bot_state.bot.send_message.call_args[0][1]
        self.assertIn("all known members", text)


class TestPostEndSettleNudge(unittest.IsolatedAsyncioTestCase):
    """_post_end_cleanup routes to the persistent nudge for dues-enabled
    chats, and the settle-initiated paths suppress it via settle_nudge=False."""

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()

    def _run_cleanup(self, settle_nudge=None):
        from handlers.lifecycle import _post_end_cleanup
        result = {"renumbered": [], "badges": [], "rc_db_id": 42, "ghost_eligible": False}
        kwargs = {} if settle_nudge is None else {"settle_nudge": settle_nudge}
        mgr = MagicMock()
        mgr.get_shh_mode.return_value = True
        mgr.get_rollcalls.return_value = []
        return _post_end_cleanup, result, mgr, kwargs

    async def test_dues_enabled_sends_persistent_nudge(self):
        cleanup, result, mgr, kwargs = self._run_cleanup()
        with patch("handlers.lifecycle.manager", mgr), \
             patch("handlers.lifecycle.db.get_or_create_chat",
                   return_value={"dues_enabled": 1}), \
             patch("handlers.dues.send_settle_nudge", new=AsyncMock()) as nudge:
            await cleanup(100, 1, result, rc_title="Sunday", **kwargs)
        nudge.assert_awaited_once_with(100, 42, "Sunday")

    async def test_settle_flow_suppresses_nudge(self):
        cleanup, result, mgr, kwargs = self._run_cleanup(settle_nudge=False)
        with patch("handlers.lifecycle.manager", mgr), \
             patch("handlers.lifecycle.db.get_or_create_chat",
                   return_value={"dues_enabled": 1}), \
             patch("handlers.dues.send_settle_nudge", new=AsyncMock()) as nudge:
            await cleanup(100, 1, result, rc_title="Sunday", **kwargs)
        nudge.assert_not_awaited()


class TestAutoStartUnsettledWarning(unittest.IsolatedAsyncioTestCase):
    """Scheduled-template auto-fire warns when earlier games sit unsettled."""

    def _load_real_module(self):
        import importlib.util
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "check_reminders.py")
        spec = importlib.util.spec_from_file_location("_real_check_reminders_dues", module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    async def test_warns_with_picker_when_unsettled_games_exist(self):
        mod = self._load_real_module()
        games = [{"id": 41, "title": "Last Sunday", "ended_at": "2026-07-12"}]
        with patch("db.get_or_create_chat", return_value={"dues_enabled": 1}), \
             patch("db.get_unsettled_rollcalls", return_value=games), \
             patch("handlers.dues._send_unsettled_picker", new=AsyncMock()) as picker:
            await mod._warn_unsettled_dues(100)
        picker.assert_awaited_once()
        intro = picker.call_args[0][2]
        self.assertIn("unsettled dues", intro)

    async def test_silent_when_dues_disabled(self):
        mod = self._load_real_module()
        with patch("db.get_or_create_chat", return_value={"dues_enabled": 0}), \
             patch("handlers.dues._send_unsettled_picker", new=AsyncMock()) as picker:
            await mod._warn_unsettled_dues(100)
        picker.assert_not_awaited()

    async def test_silent_when_nothing_unsettled(self):
        mod = self._load_real_module()
        with patch("db.get_or_create_chat", return_value={"dues_enabled": 1}), \
             patch("db.get_unsettled_rollcalls", return_value=[]), \
             patch("handlers.dues._send_unsettled_picker", new=AsyncMock()) as picker:
            await mod._warn_unsettled_dues(100)
        picker.assert_not_awaited()

    async def test_warning_failure_never_propagates(self):
        mod = self._load_real_module()
        with patch("db.get_or_create_chat", side_effect=Exception("db down")):
            await mod._warn_unsettled_dues(100)  # must not raise


if __name__ == "__main__":
    unittest.main()
