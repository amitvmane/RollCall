"""
conftest.py - pytest fixtures and module mocks applied before all tests.

All external dependencies (telebot, db, config) are mocked here so tests
run without a real Telegram token or database.
"""

import sys
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# telebot – must be mocked before any rollCall module is imported
# ---------------------------------------------------------------------------
telebot_mock = MagicMock()
async_telebot_mock = MagicMock()
telebot_mock.async_telebot = async_telebot_mock
telebot_mock.TeleBot = MagicMock()
sys.modules["telebot"] = telebot_mock
sys.modules["telebot.async_telebot"] = async_telebot_mock

# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------
db_mock = MagicMock()
db_mock.create_rollcall.return_value = 1
db_mock.ensure_rollcall_stats.return_value = None
db_mock.update_rollcall.return_value = None
db_mock.add_or_update_user.return_value = None
db_mock.add_or_update_proxy_user.return_value = None
db_mock.delete_user_by_name.return_value = True
db_mock.get_all_users.return_value = []
db_mock.get_proxy_users_by_status.return_value = []
db_mock.get_or_create_chat.return_value = {
    "shh_mode": False,
    "admin_rights": False,
    "timezone": "Asia/Calcutta",
}
db_mock.get_active_rollcalls.return_value = []
db_mock.end_rollcall.return_value = None
db_mock.update_chat_settings.return_value = None
db_mock.get_rollcall.return_value = None
db_mock.db_type = "sqlite"
sys.modules["db"] = db_mock

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
config_mock = MagicMock()
config_mock.TELEGRAM_TOKEN = "test_token"
config_mock.ADMINS = []
config_mock.DATABASE_URL = "sqlite:///:memory:"
sys.modules["config"] = config_mock

# ---------------------------------------------------------------------------
# aiohttp (used by runner / check_reminders)
# ---------------------------------------------------------------------------
sys.modules["aiohttp"] = MagicMock()

# ---------------------------------------------------------------------------
# check_reminders
# ---------------------------------------------------------------------------
check_reminders_mock = MagicMock()
check_reminders_mock.start = MagicMock()
sys.modules["check_reminders"] = check_reminders_mock
