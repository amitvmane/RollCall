"""
Shared base class and factories for integration tests.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mock_helpers import get_mock_bot, reset_db, _next_msg_id

# Lazy imports — modules only available after conftest runs
def _import_all():
    import bot_state
    import rollcall_manager
    from handlers.lifecycle import (
        start_roll_call, end_roll_call, callback_handler,
        get_status_keyboard, show_panel_for_rollcall, set_title, show_panel,
    )
    from handlers.voting import in_user, out_user, maybe_user
    from handlers.proxy import set_in_for, set_out_for, set_maybe_for
    from handlers.lists import whos_in, whos_out, whos_maybe, whos_waiting, buzz_command, history_command
    from handlers.ghost import (
        toggle_ghost_tracking, set_absent_limit, clear_absent, mark_absent,
        ghost_callback_handler,
    )
    from handlers.admin import delete_user, set_status_override, audit_log_command, audit_pagination_callback
    from handlers.templates import (
        list_templates, set_template, start_template,
        delete_template_command, schedules_command, schedule_template_cmd, schedules_toggle_callback,
    )
    from handlers.settings import (
        shh, louder, wait_limit, event_fee, individual_fee, when, set_location,
        set_rollcall_time, reminder,
    )
    from handlers.core import (
        set_admins, unset_admins, welcome_and_explanation, help_commands,
        broadcast, config_timezone, version_command, show_reminders,
    )
    from handlers.stats import stats_command
    return locals()


# 10 simulated group members
CHAT_ID = -100_000_001
ADMIN_ID = 999

USERS = [
    {"id": i, "first_name": f"User{i}", "last_name": None, "username": f"user{i}"}
    for i in range(1, 11)
]
ADMIN_USER = {"id": ADMIN_ID, "first_name": "Admin", "last_name": None, "username": "admin_user"}


def make_message(text="/cmd", user=None, chat_id=CHAT_ID):
    if user is None:
        user = ADMIN_USER
    msg = MagicMock()
    msg.text = text
    msg.chat.id = chat_id
    msg.from_user.id = user["id"]
    msg.from_user.first_name = user["first_name"]
    msg.from_user.last_name = user.get("last_name")
    msg.from_user.username = user.get("username")
    return msg


def make_call(data, user=None, chat_id=CHAT_ID, message_id=500):
    if user is None:
        user = ADMIN_USER
    call = MagicMock()
    call.data = data
    call.id = f"cbq_{data}"
    call.message.chat.id = chat_id
    call.message.message_id = message_id
    call.from_user.id = user["id"]
    call.from_user.first_name = user["first_name"]
    call.from_user.last_name = user.get("last_name")
    call.from_user.username = user.get("username")
    return call


class IntegrationBase(unittest.IsolatedAsyncioTestCase):
    """
    Base for all integration tests.
    - Real SQLite db, truncated before each test
    - Real RollCallManager with cache cleared
    - Real handlers
    - Mocked Telegram bot API
    """

    @classmethod
    def setUpClass(cls):
        h = _import_all()
        cls.bs = h["bot_state"]
        cls.mgr = h["rollcall_manager"].manager
        cls.start_roll_call = staticmethod(h["start_roll_call"])
        cls.end_roll_call = staticmethod(h["end_roll_call"])
        cls.callback_handler = staticmethod(h["callback_handler"])
        cls.show_panel_for_rollcall = staticmethod(h["show_panel_for_rollcall"])
        cls.get_status_keyboard = staticmethod(h["get_status_keyboard"])
        cls.set_title = staticmethod(h["set_title"])
        cls.show_panel = staticmethod(h["show_panel"])
        cls.in_user = staticmethod(h["in_user"])
        cls.out_user = staticmethod(h["out_user"])
        cls.maybe_user = staticmethod(h["maybe_user"])
        cls.set_in_for = staticmethod(h["set_in_for"])
        cls.set_out_for = staticmethod(h["set_out_for"])
        cls.set_maybe_for = staticmethod(h["set_maybe_for"])
        cls.whos_in = staticmethod(h["whos_in"])
        cls.whos_out = staticmethod(h["whos_out"])
        cls.whos_maybe = staticmethod(h["whos_maybe"])
        cls.whos_waiting = staticmethod(h["whos_waiting"])
        cls.buzz_command = staticmethod(h["buzz_command"])
        cls.toggle_ghost_tracking = staticmethod(h["toggle_ghost_tracking"])
        cls.set_absent_limit = staticmethod(h["set_absent_limit"])
        cls.clear_absent = staticmethod(h["clear_absent"])
        cls.mark_absent = staticmethod(h["mark_absent"])
        cls.ghost_callback_handler = staticmethod(h["ghost_callback_handler"])
        cls.delete_user = staticmethod(h["delete_user"])
        cls.set_status_override = staticmethod(h["set_status_override"])
        cls.audit_log_command = staticmethod(h["audit_log_command"])
        cls.list_templates = staticmethod(h["list_templates"])
        cls.set_template = staticmethod(h["set_template"])
        cls.start_template = staticmethod(h["start_template"])
        cls.delete_template_command = staticmethod(h["delete_template_command"])
        cls.schedules_command = staticmethod(h["schedules_command"])
        cls.shh = staticmethod(h["shh"])
        cls.louder = staticmethod(h["louder"])
        cls.wait_limit = staticmethod(h["wait_limit"])
        cls.event_fee = staticmethod(h["event_fee"])
        cls.individual_fee = staticmethod(h["individual_fee"])
        cls.when = staticmethod(h["when"])
        cls.set_location = staticmethod(h["set_location"])
        cls.set_admins = staticmethod(h["set_admins"])
        cls.unset_admins = staticmethod(h["unset_admins"])
        cls.welcome_and_explanation = staticmethod(h["welcome_and_explanation"])
        cls.help_commands = staticmethod(h["help_commands"])
        cls.broadcast = staticmethod(h["broadcast"])
        cls.config_timezone = staticmethod(h["config_timezone"])
        cls.version_command = staticmethod(h["version_command"])
        cls.show_reminders = staticmethod(h["show_reminders"])
        cls.history_command = staticmethod(h["history_command"])
        cls.stats_command = staticmethod(h["stats_command"])
        cls.set_rollcall_time = staticmethod(h["set_rollcall_time"])
        cls.reminder = staticmethod(h["reminder"])
        cls.audit_pagination_callback = staticmethod(h["audit_pagination_callback"])
        cls.schedule_template_cmd = staticmethod(h["schedule_template_cmd"])
        cls.schedules_toggle_callback = staticmethod(h["schedules_toggle_callback"])

    def setUp(self):
        reset_db()
        self.mgr.clear_cache()
        # Clear all bot_state dicts
        self.bs._ghost_selections.clear()
        self.bs._pending_reconf.clear()
        self.bs._pending_deletes.clear()
        self.bs._pending_overrides.clear()
        self.bs._rate_limits.clear()
        self.bs._buzz_cooldowns.clear()
        self.bs._panel_msg_ids.clear()
        self.bs._pending_panel_updates.clear()
        # Reset bot mock call history
        bot = get_mock_bot()
        bot.send_message.reset_mock()
        bot.send_message.side_effect = lambda *a, **kw: _make_sent_msg()
        bot.edit_message_text.reset_mock()
        bot.edit_message_reply_markup.reset_mock()
        bot.answer_callback_query.reset_mock()
        bot.get_chat_member.return_value.status = "administrator"

    # ── convenience helpers ──────────────────────────────────────────────────

    def msg(self, text="/cmd", user=None):
        return make_message(text, user)

    def call(self, data, user=None, message_id=500):
        return make_call(data, user, message_id=message_id)

    def sent_texts(self):
        """All text args sent via bot.send_message."""
        bot = get_mock_bot()
        results = []
        for args, kwargs in bot.send_message.call_args_list:
            if len(args) >= 2:
                results.append(str(args[1]))
            else:
                results.append(str(kwargs.get("text", "")))
        return results

    def sent_count(self):
        return get_mock_bot().send_message.call_count

    def rc(self, n=0):
        """Get rollcall n (0-based) for the test chat."""
        return self.mgr.get_rollcall(CHAT_ID, n)

    async def start_rc(self, title="Test Event"):
        """Start a rollcall and return it."""
        await self.start_roll_call(self.msg(f"/src {title}", ADMIN_USER))
        return self.rc(0)

    def _clear_rate(self, user):
        """Remove rate-limit entry so the next vote isn't blocked."""
        self.bs._rate_limits.pop((CHAT_ID, user["id"]), None)

    async def vote_in(self, user, comment="", rc_suffix=""):
        self._clear_rate(user)
        text = f"/in{' ' + comment if comment else ''}{' ' + rc_suffix if rc_suffix else ''}"
        await self.in_user(self.msg(text, user))

    async def vote_out(self, user, rc_suffix=""):
        self._clear_rate(user)
        text = f"/out{' ' + rc_suffix if rc_suffix else ''}"
        await self.out_user(self.msg(text, user))

    async def vote_maybe(self, user, rc_suffix=""):
        self._clear_rate(user)
        text = f"/maybe{' ' + rc_suffix if rc_suffix else ''}"
        await self.maybe_user(self.msg(text, user))


def _make_sent_msg():
    m = MagicMock()
    m.message_id = _next_msg_id()
    return m
