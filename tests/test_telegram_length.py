"""
Telegram refuses a message over 4096 characters (UTF-16 units, after the
markup is parsed) — the whole message, nothing is delivered. Two places
crossed it in production with no test noticing, because every layer below
the real API mocks the bot:

- /help admin renders to ~6.7k characters and sent nothing at all.
- a vote comment was uncapped from chat commands, so one long /in pushed
  the panel over the limit and every later panel edit failed for the group.

The functional test now refuses any over-long send; these pin the pieces.
"""

import os
import re
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))
os.environ.setdefault("TELEGRAM_TOKEN", "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY")

from utils.text import TG_TEXT_LIMIT, split_for_telegram  # noqa: E402


def _u16(s):
    return len(s.encode("utf-16-le")) // 2


def _nonblank_lines(text):
    return [ln for ln in text.split("\n") if ln.strip()]


class TestSplitForTelegram(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(split_for_telegram("hello\n\nworld"), ["hello\n\nworld"])

    def test_long_text_splits_at_lines_and_loses_nothing(self):
        text = "\n\n".join(
            "*Category %d*\n" % c + "\n".join(f"/cmd_{c}_{i} — does a thing" for i in range(30))
            for c in range(12)
        )
        self.assertGreater(_u16(text), TG_TEXT_LIMIT)
        pieces = split_for_telegram(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(_u16(p) <= TG_TEXT_LIMIT for p in pieces))
        self.assertEqual(_nonblank_lines("\n".join(pieces)), _nonblank_lines(text))

    def test_counts_emoji_as_two_units(self):
        # 3,000 emoji is 3,000 Python characters but 6,000 Telegram units.
        text = "\n".join(["🔴" * 10] * 300)
        pieces = split_for_telegram(text)
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(_u16(p) <= TG_TEXT_LIMIT for p in pieces))

    def test_single_overlong_line_is_hard_cut_without_loss(self):
        text = "a" * 9000
        pieces = split_for_telegram(text)
        self.assertTrue(all(_u16(p) <= TG_TEXT_LIMIT for p in pieces))
        self.assertEqual("".join(pieces), text)

    def test_admin_help_splits_without_breaking_markdown(self):
        from handlers import core
        from commands_registry import ADMIN_CATEGORY_ORDER
        text = core._render_command_list({"user", "admin", "super_admin"},
                                         ADMIN_CATEGORY_ORDER, "⚙️ *RollCall — Admin Commands*")
        pieces = split_for_telegram(text)
        self.assertGreater(len(pieces), 1, "admin help fits again — fine, but re-check this test")
        for p in pieces:
            self.assertLessEqual(_u16(p), TG_TEXT_LIMIT)
            # Every * / ` entity opens and closes inside the same piece.
            for mark in ("*", "`"):
                self.assertEqual(len(re.findall(r"(?<!\\)" + re.escape(mark), p)) % 2, 0,
                                 f"unbalanced {mark} in a piece starting {p[:40]!r}")


class TestChunkedSends(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        self._orig = bot_state.bot.send_message
        bot_state.bot.send_message = AsyncMock(side_effect=lambda *a, **k: MagicMock(message_id=len(a[1])))

    def tearDown(self):
        self.bot_state.bot.send_message = self._orig

    def _long(self):
        return "\n".join(f"🔴 Member {i}: ₹{i * 10}" for i in range(400))

    async def test_send_long_puts_buttons_on_the_last_piece_only(self):
        markup = object()
        await self.bot_state.send_long(-1, self._long(), parse_mode="Markdown", reply_markup=markup)
        calls = self.bot_state.bot.send_message.await_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(_u16(c.args[1]) <= TG_TEXT_LIMIT for c in calls))
        self.assertTrue(all(c.kwargs.get("parse_mode") == "Markdown" for c in calls))
        self.assertEqual([c.kwargs.get("reply_markup") for c in calls],
                         [None] * (len(calls) - 1) + [markup])

    async def test_ledger_announcement_over_the_limit_is_still_delivered(self):
        # send_md_fallback carries the ledger announcements that are the
        # recovery source if data is lost — a too-long one must not vanish.
        await self.bot_state.send_md_fallback(-1, self._long())
        calls = self.bot_state.bot.send_message.await_args_list
        self.assertGreater(len(calls), 1)
        self.assertTrue(all(_u16(c.args[1]) <= TG_TEXT_LIMIT for c in calls))
        self.assertEqual(_nonblank_lines("\n".join(c.args[1] for c in calls)),
                         _nonblank_lines(self._long()))

    async def test_short_message_is_one_send(self):
        await self.bot_state.send_md_fallback(-1, "🧾 Dues adjusted")
        self.assertEqual(self.bot_state.bot.send_message.await_count, 1)


class TestCallbackKeys(unittest.TestCase):
    """Telegram caps callback_data at 64 bytes and refuses the whole message
    over it. Names went in raw: a long non-ASCII proxy or web-guest name
    killed the /sif ghost warning and that game's absence-review card."""

    GUEST = "सुब्रमण्यम वेंकटेश्वर रामकृष्ण नारायण"  # 37 chars, 105 bytes

    def test_ghost_toggle_button_fits_for_any_allowed_name(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from bot_state import _ghost_row
        for name in (self.GUEST, "x" * 64, "🏏" * 64):
            # telebot is mocked in unit tests; a plain stand-in keeps the fields.
            with patch("bot_state.InlineKeyboardButton",
                       lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data)):
                btn = _ghost_row(2_147_483_647, {"proxy_name": name}, set())
            self.assertLessEqual(len(btn.callback_data.encode()), 64, name)
            self.assertIn(name, btn.text)  # the label still shows the full name

    def test_key_maps_back_and_raw_names_still_work(self):
        from utils.text import callback_key, match_callback_name
        names = ["Kedar", self.GUEST, "kedar"]
        self.assertEqual(match_callback_name(callback_key(self.GUEST), names), self.GUEST)
        # Case-exact: selections hold the exact stored name.
        self.assertEqual(match_callback_name(callback_key("kedar"), names), "kedar")
        # A button posted before keys existed carries the raw name.
        self.assertEqual(match_callback_name("Kedar", names), "Kedar")
        # A name that isn't a candidate any more resolves to nothing.
        self.assertIsNone(match_callback_name(callback_key("Gone"), names))

    def test_proxy_confirm_resolves_only_this_admins_pending(self):
        from bot_state import _pending_proxy_add
        from handlers.ghost import _pending_proxy_name
        from utils.text import callback_key
        _pending_proxy_add.clear()
        _pending_proxy_add[(-1, 10, self.GUEST)] = {"_ts": 0}
        try:
            self.assertEqual(_pending_proxy_name(-1, 10, callback_key(self.GUEST)), self.GUEST)
            self.assertIsNone(_pending_proxy_name(-1, 11, callback_key(self.GUEST)))  # another admin
            self.assertIsNone(_pending_proxy_name(-2, 10, callback_key(self.GUEST)))  # another chat
        finally:
            _pending_proxy_add.clear()


class TestCommentCap(unittest.TestCase):
    def test_comment_is_capped_for_every_surface(self):
        from services.common import MAX_COMMENT_LEN, clean_comment
        self.assertEqual(MAX_COMMENT_LEN, 100)  # same as the web/REST schemas
        self.assertEqual(clean_comment("x" * 4000), "x" * 100)
        self.assertEqual(clean_comment(None), "")
        self.assertEqual(clean_comment("late by 10"), "late by 10")


if __name__ == "__main__":
    unittest.main()
