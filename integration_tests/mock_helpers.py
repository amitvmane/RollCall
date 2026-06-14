"""
Bot mock state shared by conftest.py and test modules.

Imported as `mock_helpers` — never as `conftest` — so the root conftest.py
(pytest guard) cannot shadow this module.
"""
import sys, os

# Ensure the integration_tests dir is in sys.path (conftest.py also does this,
# but this module may be imported before conftest runs).
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

from unittest.mock import MagicMock, AsyncMock


class _Markup:
    def __init__(self, row_width=3):
        self.keyboard = []
    def add(self, *buttons):
        self.keyboard.extend(buttons)
    def row(self, *buttons):
        self.keyboard.extend(buttons)


class _Button:
    def __init__(self, text="", callback_data="", url=None):
        self.text = text
        self.callback_data = callback_data


_msg_id_counter = [1000]


def _next_msg_id():
    _msg_id_counter[0] += 1
    return _msg_id_counter[0]


def _make_sent(mid=None):
    m = MagicMock()
    m.message_id = mid or _next_msg_id()
    return m


def _noop_decorator(**kwargs):
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
mock_bot.message_handler = _noop_decorator
mock_bot.callback_query_handler = _noop_decorator


def get_mock_bot():
    return mock_bot


def reset_db():
    """Truncate all rows between tests. Keeps schema intact."""
    import db as _db
    conn = _db.get_connection()
    cur = conn.cursor()
    for tbl in [
        "admin_actions", "ghost_events", "ghost_records", "ghost_selections",
        "chat_members", "proxy_users", "rollcall_stats", "user_stats",
        "proxy_stats",
        "users", "rollcalls", "templates", "chats",
    ]:
        try:
            cur.execute(f"DELETE FROM {tbl}")
        except Exception:
            pass
    conn.commit()
    cur.close()
