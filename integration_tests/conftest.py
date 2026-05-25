"""
Integration test conftest.
Uses a REAL SQLite database and the REAL handler functions.
Only mocks the Telegram bot API (no real network calls) and
aiohttp/check_reminders (no background loops in CI).
This file is NOT related to tests/conftest.py -- it lives in a sibling
directory so the unit-test mocks never interfere with these tests.
"""
import sys
import os
import tempfile
from unittest.mock import MagicMock, AsyncMock

# -- Path setup ---------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))  # helpers, conftest importable as top-level
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

# -- 1. telebot: mock the API layer, keep types semi-realistic ----------------
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

# -- 2. Import mock_bot from mock_helpers (single source of truth) ------------
# All tests import get_mock_bot from mock_helpers or conftest -- they must
# receive the SAME object. mock_helpers owns mock_bot; we wire it here.
from mock_helpers import mock_bot, get_mock_bot, reset_db  # noqa: F401

# Wire AsyncTeleBot() to return the shared mock_bot from mock_helpers
_async_telebot.AsyncTeleBot.return_value = mock_bot

# -- 3. config: real values, SQLite path resolved at import time --------------
_DB_FILE = os.path.join(tempfile.gettempdir(), "rollcall_integration.db")
_config = MagicMock()
_config.TELEGRAM_TOKEN = "test:token"
_config.ADMINS = [999]
_config.DATABASE_URL = f"sqlite:///{_DB_FILE}"
_config.DEFAULT_ABSENT_LIMIT = 1
sys.modules["config"] = _config

# -- 4. Silence noisy deps ----------------------------------------------------
sys.modules["aiohttp"] = MagicMock()

# -- 5. Bootstrap the real database once --------------------------------------
import db as _db_module
_db_module.init_db()

# -- 6. Import check_reminders for real, patch out background loop ------------
import check_reminders as _cr_real
_cr_real.start = AsyncMock()
