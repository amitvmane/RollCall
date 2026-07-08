"""
Regression tests for messages sent with parse_mode="Markdown" that mention
underscore-containing command names as bare text.

Telegram's legacy Markdown parser treats a lone, unescaped `_` as an
italic-entity opener. A command name like /settle_dues or /enable_dues used as
plain text (not backtick-wrapped, not backslash-escaped) breaks entity
pairing and Telegram rejects the whole message with a 400 "can't parse
entities" error — which the caller may not surface to the chat at all if the
send happens in a fire-and-forget context. Each case here previously crashed
in production (see /src's dues-active hint and /enable_dues).
"""
import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _code_span_ranges(text: str):
    """(start, end) index ranges covered by `...` code spans (exclusive of backticks)."""
    ticks = [m.start() for m in re.finditer("`", text)]
    return [(ticks[i] + 1, ticks[i + 1]) for i in range(0, len(ticks) - 1, 2)]


def _bare_and_unescaped(text: str, command: str) -> bool:
    """True if `command` (e.g. "/settle_dues") appears in `text` without being
    inside a backtick code span or backslash-escaped underscores."""
    escaped_variant = command.replace("_", "\\_")
    if escaped_variant in text:
        return False
    spans = _code_span_ranges(text)
    for m in re.finditer(re.escape(command), text):
        start, end = m.start(), m.end()
        if any(s <= start and end <= e for s, e in spans):
            continue
        return True
    return False


def _msg(text="/cmd", chat_id=100, user_id=1, first_name="Admin", username="admin"):
    m = MagicMock()
    m.text = text
    m.chat.id = chat_id
    m.from_user.id = user_id
    m.from_user.first_name = first_name
    m.from_user.username = username
    return m


def _make_lock_manager():
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


class TestEnableDisableDuesMarkdown(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()
        self.mgr = _make_lock_manager()

    def _sent_text(self):
        return self.bot_state.bot.send_message.call_args_list[0][0][1]

    async def test_enable_dues_command_mentions_are_backtick_wrapped(self):
        from handlers.dues import enable_dues
        with _admin_ok(), patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues.dues_svc.seed_default_penalty_tiers"), \
             patch("handlers.dues._db.update_chat_settings"):
            await enable_dues(_msg("/enable_dues"))
        text = self._sent_text()
        for cmd in ("/add_penalty", "/remove_penalty", "/set_upi",
                    "/set_treasury_upi", "/set_round_step", "/settle_dues"):
            self.assertFalse(
                _bare_and_unescaped(text, cmd),
                f"{cmd!r} appears unescaped/unwrapped in /enable_dues text — "
                f"will break Telegram Markdown parsing: {text!r}",
            )

    async def test_disable_dues_command_mention_is_backtick_wrapped(self):
        from handlers.dues import disable_dues
        with _admin_ok(), patch("handlers.dues.manager", self.mgr), \
             patch("handlers.dues._db.update_chat_settings"):
            await disable_dues(_msg("/disable_dues"))
        text = self._sent_text()
        self.assertFalse(_bare_and_unescaped(text, "/enable_dues"), text)


class TestStartRollCallDuesHintMarkdown(unittest.IsolatedAsyncioTestCase):

    async def test_dues_active_hint_settle_dues_mention_is_safe(self):
        import bot_state
        bot_state.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
        mgr = _make_lock_manager()
        mgr.get_rollcall.return_value = MagicMock()

        with patch("handlers.lifecycle.admin_rights", new=AsyncMock(return_value=True)), \
             patch("handlers.lifecycle.manager", mgr), \
             patch("handlers.lifecycle.rollcalls_svc.start_rollcall",
                   new=AsyncMock(return_value={"number": 1, "rc_index": 0})), \
             patch("handlers.lifecycle.get_status_keyboard", new=AsyncMock(return_value=MagicMock())), \
             patch("handlers.lifecycle._build_panel_text", return_value="panel"), \
             patch("handlers.lifecycle._persist_panel_msg_id"), \
             patch("handlers.lifecycle.db.get_or_create_chat",
                   return_value={"dues_enabled": True}):
            from handlers.lifecycle import start_roll_call
            await start_roll_call(_msg("/src Sunday"))

        calls = bot_state.bot.send_message.call_args_list
        dues_hint_calls = [c for c in calls if "Dues active" in c.args[1]]
        self.assertEqual(len(dues_hint_calls), 1)
        text = dues_hint_calls[0].args[1]
        self.assertFalse(_bare_and_unescaped(text, "/settle_dues"), text)


class TestPenaltyPanelTierViewMarkdown(unittest.TestCase):
    """The default seeded tiers are named "late_short"/"late_long" — a tier
    name with an underscore shown unescaped in the "applied" summary line
    broke the panel's Markdown edit on every Apply, which then made "Done"
    (and any later tier tap) appear frozen since safe_edit_text swallows the
    parse failure instead of re-raising."""

    def test_applied_tier_name_with_underscore_is_escaped(self):
        from handlers.penalty_panel import _tier_view, _PenaltySession

        session = _PenaltySession(
            chat_id=100, rollcall_id=1, title="Test",
            members=[], ghost_eligible=False,
            applied={"late_short": 2},
        )
        text, _ = _tier_view(session, tiers=[
            {"name": "late_short", "amount": 50, "is_ditch": False},
        ])
        self.assertFalse(_bare_and_unescaped(text, "late_short"), text)


class TestMyDuesMarkdown(unittest.IsolatedAsyncioTestCase):
    """entry_type values like "late_short" (a penalty tier name) and
    admin-typed memos can contain underscores — shown unescaped this broke
    /my_dues with a raw bot.send_message(parse_mode="Markdown") call that had
    no fallback at all."""

    async def test_entry_type_and_memo_are_escaped(self):
        import bot_state
        bot_state.bot.send_message = AsyncMock()
        mgr = _make_lock_manager()

        svc_result = {
            "balance": 50,
            "entries": [{"amount": 50, "entry_type": "late_short", "memo": "some_note"}],
        }
        with patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._require_dues_enabled"), \
             patch("handlers.dues.dues_svc.my_dues", return_value=svc_result), \
             patch("handlers.dues.dues_svc.get_dues_settings", return_value={}):
            from handlers.dues import my_dues
            await my_dues(_msg("/my_dues"))

        text = bot_state.bot.send_message.call_args_list[0][0][1]
        self.assertFalse(_bare_and_unescaped(text, "late_short"), text)
        self.assertFalse(_bare_and_unescaped(text, "some_note"), text)


class TestDuesLedgerMarkdown(unittest.IsolatedAsyncioTestCase):
    """Proxy/member names can contain underscores (e.g. a name like
    "team_b") — shown unescaped this broke /dues the same way as /my_dues."""

    async def test_member_name_is_escaped(self):
        import bot_state
        bot_state.bot.send_message = AsyncMock()
        mgr = _make_lock_manager()

        svc_result = {"balances": [{"member_name": "team_b", "balance": 100}]}
        with _admin_ok(), patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._require_dues_enabled"), \
             patch("handlers.dues.dues_svc.all_dues", return_value=svc_result):
            from handlers.dues import dues
            await dues(_msg("/dues"))

        text = bot_state.bot.send_message.call_args_list[0][0][1]
        self.assertFalse(_bare_and_unescaped(text, "team_b"), text)


class TestFundHistoryMarkdown(unittest.IsolatedAsyncioTestCase):

    async def test_txn_type_and_description_are_escaped(self):
        import bot_state
        bot_state.bot.send_message = AsyncMock()
        mgr = _make_lock_manager()

        svc_result = {
            "transactions": [{
                "amount": 40, "txn_type": "adjustment_entry",
                "description": "rounding_surplus", "created_at": "2026-07-08",
            }],
            "total": 1,
        }
        with patch("handlers.dues.manager", mgr), \
             patch("handlers.dues._require_dues_enabled"), \
             patch("handlers.dues.dues_svc.fund_history", return_value=svc_result):
            from handlers.dues import fund_history
            await fund_history(_msg("/fund_history"))

        text = bot_state.bot.send_message.call_args_list[0][0][1]
        self.assertFalse(_bare_and_unescaped(text, "adjustment_entry"), text)
        self.assertFalse(_bare_and_unescaped(text, "rounding_surplus"), text)


class TestPenaltiesListMarkdown(unittest.TestCase):

    def test_default_tier_names_are_escaped(self):
        from services.dues import list_penalty_tiers
        tiers = [
            {"name": "late_short", "amount": 50, "description": "under 15 min late"},
            {"name": "late_long", "amount": 100, "description": None},
        ]
        with patch("services.dues.db.get_penalty_tiers", return_value=tiers):
            result = list_penalty_tiers(1)
        text = result["announcement"]
        self.assertFalse(_bare_and_unescaped(text, "late_short"), text)
        self.assertFalse(_bare_and_unescaped(text, "late_long"), text)


if __name__ == "__main__":
    unittest.main()
