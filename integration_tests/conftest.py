"""
Integration test conftest.

Uses a REAL SQLite database and the REAL handler functions.
Only mocks the Telegram bot API (no real network calls) and
aiohttp/check_reminders (no background loops in CI).

This file is NOT related to tests/conftest.py — it lives in a sibling
directory so the unit-test mocks never interfere with these tests.
"""
import sys
import os

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))  # helpers, conftest importable as top-level
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

# ── 1. telebot: mock the API layer, keep types semi-realistic ─────────────────
from unittest.mock import MagicMock, AsyncMock

class _Markup:
    """Minimal InlineKeyboardMarkup stand-in."""
    def __init__(self, row_width=3):
        self.keyboard = []
    def add(self, *buttons):
        self.keyboard.extend(buttons)
    def row(self, *buttons):
        self.keyboard.extend(buttons)

class _Button:
    """Minimal InlineKeyboardButton stand-in."""
    def __init__(self, text="", callback_data="", url=None):
        self.text = text
        self.callback_data = callback_data

_telebot = MagicMock()
_async_telebot = MagicMock()
_telebot_types = MagicMock()
_telebot_types.InlineKeyboardMarkup = _Markup
_telebot_types.InlineKeyboardButton = _Button
_telebot.async_telebot = _async_telebot
_telebot.types = _telebot_types

sys.modules["telebot"] = _telebot
sys.modules["telebot.async_telebot"] = _async_telebot
sys.modules["telebot.types"] = _telebot_types

# ── 2. Bot mock: realistic async returns ──────────────────────────────────────
_msg_id_counter = [1000]

def _next_msg_id():
    _msg_id_counter[0] += 1
    return _msg_id_counter[0]

def _make_sent(mid=None):
    m = MagicMock()
    m.message_id = mid or _next_msg_id()
    return m

def _noop_decorator(**kwargs):
    """Pass-through: preserves the decorated async function unchanged."""
    def inner(f):
        return f
    return inner

mock_bot = MagicMock()
mock_bot.send_message = AsyncMock(side_effect=lambda *a, **kw: _make_sent())
mock_bot.edit_message_text = AsyncMock(return_value=MagicMock())
mock_bot.edit_message_reply_markup = AsyncMock(return_value=MagicMock())
mock_bot.answer_callback_query = AsyncMock(return_value=None)
mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
mock_bot.set_my_commands = AsyncMock()
# Pass-through decorators — prevents @bot.message_handler from replacing async fns
mock_bot.message_handler = _noop_decorator
mock_bot.callback_query_handler = _noop_decorator
_async_telebot.AsyncTeleBot.return_value = mock_bot

# ── 3. config: real values, SQLite path resolved at import time ───────────────
import tempfile

_DB_FILE = os.path.join(tempfile.gettempdir(), "rollcall_integration.db")

_config = MagicMock()
_config.TELEGRAM_TOKEN = "test:token"
_config.ADMINS = [999]
_config.DATABASE_URL = f"sqlite:///{_DB_FILE}"
_config.DEFAULT_ABSENT_LIMIT = 1
sys.modules["config"] = _config

# ── 4. Silence noisy deps ─────────────────────────────────────────────────────
sys.modules["aiohttp"] = MagicMock()

# ── 5. Bootstrap the real database once ──────────────────────────────────────
import db as _db_module
_db_module.init_db()

# ── 6. Import check_reminders for real, patch out background loop ─────────────
# We need the real _auto_start_from_template function available to tests.
# Patching `start` to a no-op AsyncMock prevents infinite reminder loops in CI.
import check_reminders as _cr_real
_cr_real.start = AsyncMock()

# Expose helpers for tests to import
def get_mock_bot():
    return mock_bot

def reset_db():
    """Truncate all rows between tests. Keeps schema intact."""
    conn = _db_module.get_connection()
    cur = conn.cursor()
    for tbl in [
        "admin_actions", "ghost_events", "ghost_records", "ghost_selections",
        "chat_members", "proxy_users", "rollcall_stats", "user_stats",
        "users", "rollcalls", "templates", "chats",
    ]:
        try:
            cur.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass
    conn.commit()
    cur.close()
