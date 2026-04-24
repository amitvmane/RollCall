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
telebot_types_mock = MagicMock()
telebot_mock.async_telebot = async_telebot_mock
telebot_mock.TeleBot = MagicMock()
telebot_mock.types = telebot_types_mock
sys.modules["telebot"] = telebot_mock
sys.modules["telebot.async_telebot"] = async_telebot_mock
sys.modules["telebot.types"] = telebot_types_mock

# Make @bot.message_handler(...) an identity decorator so the actual
# async handler functions remain accessible on the telegram_helper module.
def _message_handler_identity(*args, **kwargs):
    def decorator(func):
        return func
    return decorator

mock_bot_instance = MagicMock()
mock_bot_instance.message_handler.side_effect = _message_handler_identity
mock_bot_instance.callback_query_handler.side_effect = _message_handler_identity
async_telebot_mock.AsyncTeleBot.return_value = mock_bot_instance

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
    "absent_limit": 1,
    "ghost_tracking_enabled": True,
}
db_mock.get_ghost_count.return_value = 0
db_mock.increment_ghost_count.return_value = True
db_mock.reset_ghost_count.return_value = True
db_mock.get_ghost_leaderboard.return_value = []
db_mock.get_user_ghost_count_by_name.return_value = None
db_mock.mark_rollcall_absent_done.return_value = True
db_mock.get_unprocessed_rollcalls.return_value = []
db_mock.add_ghost_event.return_value = True
db_mock.get_rollcall_in_users.return_value = []
db_mock.get_active_rollcalls.return_value = []
db_mock.end_rollcall.return_value = None
db_mock.update_chat_settings.return_value = None
db_mock.get_rollcall.return_value = None
db_mock.get_all_chat_ids.return_value = []
db_mock.db_type = "sqlite"
sys.modules["db"] = db_mock

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
config_mock = MagicMock()
config_mock.TELEGRAM_TOKEN = "test_token"
config_mock.ADMINS = []
config_mock.DATABASE_URL = "sqlite:///:memory:"
config_mock.DEFAULT_ABSENT_LIMIT = 1
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
