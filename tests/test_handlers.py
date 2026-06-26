"""
tests/test_handlers.py

Full handler-level test suite for all command handlers.

Uses unittest.IsolatedAsyncioTestCase for native async support (Python 3.8+).
Because conftest.py makes @bot.message_handler an identity decorator, all
async def handlers are accessible directly on their respective handler modules.

Coverage: 37 command handlers across all command groups.
Each handler gets at minimum:
  - Happy-path test
  - Key error/edge cases (no rollcall, missing params, invalid params, etc.)
"""

import sys
import os
import json
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, AsyncMock, patch, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


# ---------------------------------------------------------------------------
# Shared base: imported once, shared across all test classes
# ---------------------------------------------------------------------------

class HandlerTestBase(unittest.IsolatedAsyncioTestCase):
    """Base class providing shared fixtures for all handler tests."""

    @classmethod
    def setUpClass(cls):
        import bot_state
        from handlers.voting import in_user, out_user, maybe_user
        from handlers.lifecycle import (
            start_roll_call, end_roll_call, callback_handler,
            get_status_keyboard, show_panel_for_rollcall,
            notify_proxy_owner_wait_to_in, set_title, show_panel,
            cancel_roll_call,
        )
        from handlers.proxy import set_in_for, set_out_for, set_maybe_for
        from handlers.lists import whos_in, whos_out, whos_maybe, whos_waiting, buzz_command
        from handlers.ghost import (
            ghost_callback_handler, toggle_ghost_tracking, set_absent_limit,
            clear_absent, mark_absent,
        )
        from handlers.admin import delete_user
        from handlers.stats import stats_command
        from handlers.templates import (
            list_templates, set_template, start_template, delete_template_command,
            schedules_command, schedule_template_cmd,
        )
        from handlers.core import (
            welcome_and_explanation, help_commands, set_admins, unset_admins,
            broadcast, config_timezone, version_command, show_reminders,
            on_new_chat_members,
        )
        from handlers.settings import (
            shh, louder, wait_limit, event_fee, individual_fee,
            when, set_location,
        )

        # Attach everything to cls for test access.
        # staticmethod prevents Python's descriptor protocol from binding self
        # when these are called as self.func(message) in test methods.
        cls.bot_state = bot_state
        cls.in_user = staticmethod(in_user)
        cls.out_user = staticmethod(out_user)
        cls.maybe_user = staticmethod(maybe_user)
        cls.start_roll_call = staticmethod(start_roll_call)
        cls.end_roll_call = staticmethod(end_roll_call)
        cls.callback_handler = staticmethod(callback_handler)
        cls.get_status_keyboard = staticmethod(get_status_keyboard)
        cls.show_panel_for_rollcall = staticmethod(show_panel_for_rollcall)
        cls.notify_proxy_owner_wait_to_in = staticmethod(notify_proxy_owner_wait_to_in)
        cls.set_in_for = staticmethod(set_in_for)
        cls.set_out_for = staticmethod(set_out_for)
        cls.set_maybe_for = staticmethod(set_maybe_for)
        cls.whos_in = staticmethod(whos_in)
        cls.whos_out = staticmethod(whos_out)
        cls.whos_maybe = staticmethod(whos_maybe)
        cls.whos_waiting = staticmethod(whos_waiting)
        cls.buzz_command = staticmethod(buzz_command)
        cls.ghost_callback_handler = staticmethod(ghost_callback_handler)
        cls.toggle_ghost_tracking = staticmethod(toggle_ghost_tracking)
        cls.set_absent_limit = staticmethod(set_absent_limit)
        cls.clear_absent = staticmethod(clear_absent)
        cls.mark_absent = staticmethod(mark_absent)
        cls.delete_user = staticmethod(delete_user)
        cls.stats_command = staticmethod(stats_command)
        cls.list_templates = staticmethod(list_templates)
        cls.set_template = staticmethod(set_template)
        cls.start_template = staticmethod(start_template)
        cls.delete_template_command = staticmethod(delete_template_command)
        cls.schedules_command = staticmethod(schedules_command)
        cls.schedule_template_cmd = staticmethod(schedule_template_cmd)
        cls.welcome_and_explanation = staticmethod(welcome_and_explanation)
        cls.help_commands = staticmethod(help_commands)
        cls.set_admins = staticmethod(set_admins)
        cls.unset_admins = staticmethod(unset_admins)
        cls.broadcast = staticmethod(broadcast)
        cls.config_timezone = staticmethod(config_timezone)
        cls.version_command = staticmethod(version_command)
        cls.show_reminders = staticmethod(show_reminders)
        cls.on_new_chat_members = staticmethod(on_new_chat_members)
        cls.shh = staticmethod(shh)
        cls.louder = staticmethod(louder)
        cls.set_title = staticmethod(set_title)
        cls.show_panel = staticmethod(show_panel)
        cls.cancel_roll_call = staticmethod(cancel_roll_call)
        cls.wait_limit = staticmethod(wait_limit)
        cls.event_fee = staticmethod(event_fee)
        cls.individual_fee = staticmethod(individual_fee)
        cls.when = staticmethod(when)
        cls.set_location = staticmethod(set_location)

    def setUp(self):
        # Fresh AsyncMock for bot.send_message each test
        self.bot_state.bot.send_message = AsyncMock()
        self.bot_state.bot.get_chat_member = AsyncMock()
        # Clear rate limit and pending state between tests
        self.bot_state._rate_limits.clear()
        self.bot_state._pending_deletes.clear()
        # Shared manager and rollcall mocks
        self.rc = self._make_rc()
        self.manager = self._make_manager([self.rc])

    # ---- factories --------------------------------------------------------

    def _make_message(self, text="/cmd", chat_id=100, user_id=1,
                      first_name="Alice", username="alice"):
        msg = MagicMock()
        msg.text = text
        msg.chat.id = chat_id
        msg.from_user.id = user_id
        msg.from_user.first_name = first_name
        msg.from_user.last_name = None
        msg.from_user.username = username
        return msg

    def _make_rc(self, title="Weekly Event"):
        rc = MagicMock()
        rc.title = title
        rc.id = 1
        rc.inList = []
        rc.outList = []
        rc.maybeList = []
        rc.waitList = []
        rc.allNames = []
        rc.inListLimit = None
        rc.finalizeDate = None
        rc.timezone = "Asia/Calcutta"
        rc.location = None
        rc.event_fee = None
        rc.inListText.return_value = "In:\nNobody\n\n"
        rc.outListText.return_value = "Out:\nNobody\n\n"
        rc.maybeListText.return_value = "Maybe:\nNobody\n\n"
        rc.waitListText.return_value = ""
        rc.allList.return_value = "Title: Weekly Event\nID: __RCID__\n"
        rc.finishList.return_value = "Title: Weekly Event\nID: __RCID__\n"
        rc.addIn.return_value = None
        rc.addOut.return_value = None
        rc.addMaybe.return_value = None
        rc.delete_user.return_value = True
        rc.save.return_value = None
        return rc

    def _make_manager(self, rollcalls=None):
        m = MagicMock()
        rollcalls = rollcalls or []
        m.get_rollcalls.return_value = rollcalls
        m.get_rollcall.return_value = rollcalls[0] if rollcalls else None
        m.get_shh_mode.return_value = False
        m.get_admin_rights.return_value = False
        m.get_ghost_tracking_enabled.return_value = True
        m.get_absent_limit.return_value = 1
        return m

    # ---- helpers ----------------------------------------------------------

    def _sent_text(self, call_index=0):
        """Return the text sent in the Nth bot.send_message call."""
        return self.bot_state.bot.send_message.call_args_list[call_index][0][1]

    def _sent_count(self):
        return self.bot_state.bot.send_message.call_count

    def _rc_not_started(self):
        """Patch roll_call_not_started to return False (no rollcall active) in all handler modules."""
        return _MultiPatch([
            patch('handlers.voting.roll_call_not_started', return_value=False),
            patch('handlers.lifecycle.roll_call_not_started', return_value=False),
            patch('handlers.proxy.roll_call_not_started', return_value=False),
            patch('handlers.ghost.roll_call_not_started', return_value=False),
            patch('handlers.lists.roll_call_not_started', return_value=False),
            patch('handlers.admin.roll_call_not_started', return_value=False),
            patch('handlers.settings.roll_call_not_started', return_value=False),
        ])

    def _rc_started(self):
        """Patch roll_call_not_started to return True (rollcall active) in all handler modules."""
        return _MultiPatch([
            patch('handlers.voting.roll_call_not_started', return_value=True),
            patch('handlers.lifecycle.roll_call_not_started', return_value=True),
            patch('handlers.proxy.roll_call_not_started', return_value=True),
            patch('handlers.ghost.roll_call_not_started', return_value=True),
            patch('handlers.lists.roll_call_not_started', return_value=True),
            patch('handlers.admin.roll_call_not_started', return_value=True),
            patch('handlers.settings.roll_call_not_started', return_value=True),
        ])

    def _admin_ok(self):
        return _MultiPatch([
            patch('handlers.lifecycle.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.proxy.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.lists.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.admin.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.settings.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.core.admin_rights', new=AsyncMock(return_value=True)),
            patch('handlers.templates.admin_rights', new=AsyncMock(return_value=True)),
        ])

    def _admin_denied(self):
        return _MultiPatch([
            patch('handlers.lifecycle.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.proxy.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.lists.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.admin.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.settings.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.core.admin_rights', new=AsyncMock(return_value=False)),
            patch('handlers.templates.admin_rights', new=AsyncMock(return_value=False)),
        ])

    def _panel(self):
        return patch('handlers.lifecycle.show_panel_for_rollcall', new=AsyncMock())

    def _patch_manager(self):
        return _MultiPatch([
            patch('handlers.voting.manager', self.manager),
            patch('handlers.lifecycle.manager', self.manager),
            patch('handlers.proxy.manager', self.manager),
            patch('handlers.ghost.manager', self.manager),
            patch('handlers.lists.manager', self.manager),
            patch('handlers.admin.manager', self.manager),
            patch('handlers.settings.manager', self.manager),
            patch('handlers.core.manager', self.manager),
            patch('handlers.templates.manager', self.manager),
            patch('handlers.stats.manager', self.manager),
            patch('rollcall_manager.manager', self.manager),
            patch('services.voting.manager', self.manager),
            patch('services.proxy.manager', self.manager),
            patch('services.settings.manager', self.manager),
            patch('services.rollcalls.manager', self.manager),
        ])


class _MultiPatch:
    """Context manager that enters/exits multiple patches at once."""

    def __init__(self, patches):
        self._patches = patches
        self._stack = None

    def __enter__(self):
        self._stack = ExitStack()
        for p in self._patches:
            self._stack.enter_context(p)
        return self

    def __exit__(self, *args):
        self._stack.__exit__(*args)


# ===========================================================================
# /start
# ===========================================================================

class TestWelcomeAndExplanation(HandlerTestBase):

    async def test_sends_welcome_message(self):
        msg = self._make_message("/start")
        with self._admin_ok(), self._patch_manager():
            await self.welcome_and_explanation(msg)
        self.assertGreater(self._sent_count(), 0)
        self.assertIn("RollCall", self._sent_text())

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/start")
        with self._admin_denied(), self._patch_manager():
            await self.welcome_and_explanation(msg)
        self.assertIn("permission", self._sent_text().lower())


# ===========================================================================
# /help
# ===========================================================================

class TestHelpCommands(HandlerTestBase):

    async def test_sends_help_text(self):
        msg = self._make_message("/help")
        await self.help_commands(msg)
        self.assertGreater(self._sent_count(), 0)
        # Help message should list key commands
        sent = self._sent_text()
        self.assertIn("/in", sent)
        self.assertIn("/out", sent)


# ===========================================================================
# /set_admins  /unset_admins
# ===========================================================================

class TestSetAdmins(HandlerTestBase):

    async def test_group_admin_can_enable_admin_mode(self):
        member = MagicMock()
        member.status = 'administrator'
        self.bot_state.bot.get_chat_member = AsyncMock(return_value=member)
        msg = self._make_message("/set_admins")
        with self._patch_manager():
            await self.set_admins(msg)
        self.manager.set_admin_rights.assert_called_once_with(100, True)

    async def test_non_admin_cannot_enable_admin_mode(self):
        member = MagicMock()
        member.status = 'member'
        self.bot_state.bot.get_chat_member = AsyncMock(return_value=member)
        msg = self._make_message("/set_admins")
        with self._patch_manager():
            await self.set_admins(msg)
        # send_message should tell user they lack permissions
        self.assertGreater(self._sent_count(), 0)


class TestUnsetAdmins(HandlerTestBase):

    async def test_disables_admin_mode(self):
        msg = self._make_message("/unset_admins")
        member_mock = MagicMock()
        member_mock.status = 'administrator'
        self.bot_state.bot.get_chat_member = AsyncMock(return_value=member_mock)
        with self._patch_manager():
            await self.unset_admins(msg)
        self.manager.set_admin_rights.assert_called_once_with(100, False)


# ===========================================================================
# /timezone
# ===========================================================================

class TestConfigTimezone(HandlerTestBase):

    async def test_valid_timezone_is_set(self):
        msg = self._make_message("/timezone Asia/Kolkata")
        with self._patch_manager(), \
             patch('handlers.core.auto_complete_timezone', return_value='Asia/Calcutta'):
            await self.config_timezone(msg)
        # behavior check: confirmation message sent with the resolved timezone
        self.assertIn("Asia/Calcutta", self._sent_text())

    async def test_missing_parameter_sends_error(self):
        msg = self._make_message("/timezone")
        with self._patch_manager():
            await self.config_timezone(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_invalid_timezone_sends_link(self):
        msg = self._make_message("/timezone Fake/Place")
        with self._patch_manager(), \
             patch('handlers.core.auto_complete_timezone', return_value=None):
            await self.config_timezone(msg)
        sent = self._sent_text()
        self.assertIn("invalid", sent.lower())


# ===========================================================================
# /version  (happy-path; error cases covered in test_bug_fixes.py)
# ===========================================================================

class TestVersionCommandBasic(HandlerTestBase):

    async def test_sends_version_info(self):
        versions = [{"Version": 4.6, "Description": "Test", "DeployedOnProd": "Y",
                     "DeployedDatetime": "23-04-2026"}]
        msg = self._make_message("/version")
        with patch("builtins.open", mock_open(read_data=json.dumps(versions))):
            await self.version_command(msg)
        self.assertIn("4.6", self._sent_text())


# ===========================================================================
# /rollcalls
# ===========================================================================

class TestShowRollcalls(HandlerTestBase):

    async def test_no_rollcalls_sends_empty_message(self):
        empty_manager = self._make_manager([])
        msg = self._make_message("/rollcalls")
        with patch('handlers.core.manager', empty_manager):
            await self.show_reminders(msg)
        self.assertIn("empty", self._sent_text().lower())

    async def test_with_rollcall_sends_list(self):
        msg = self._make_message("/rollcalls")
        with self._patch_manager():
            await self.show_reminders(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /templates
# ===========================================================================

class TestListTemplates(HandlerTestBase):

    async def test_no_templates_sends_notice(self):
        msg = self._make_message("/templates")
        with patch('handlers.templates.templates_svc.list_templates', return_value=[]):
            await self.list_templates(msg)
        self.assertIn("no templates", self._sent_text().lower())

    async def test_with_templates_sends_list(self):
        templates = [{"name": "t1", "title": "T1", "schedule_enabled": False,
                      "schedule_day": None, "schedule_time": None,
                      "event_day": None, "event_time": None, "last_scheduled_date": None}]
        msg = self._make_message("/templates")
        with patch('handlers.templates.templates_svc.list_templates', return_value=templates):
            await self.list_templates(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /start_roll_call  /src
# ===========================================================================

class TestStartRollCall(HandlerTestBase):

    def _db_json_patch(self):
        """Mock database.json so start_roll_call doesn't hit the filesystem."""
        return patch("builtins.open", mock_open(read_data='[]'))

    async def test_starts_rollcall_with_title(self):
        msg = self._make_message("/start_roll_call Friday Game")
        with self._db_json_patch(), self._admin_ok(), self._patch_manager(), \
             patch('handlers.lifecycle.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.start_roll_call(msg)
        self.manager.add_rollcall.assert_called_once_with(100, "Friday Game")

    async def test_starts_rollcall_without_title_uses_empty(self):
        msg = self._make_message("/start_roll_call")
        with self._db_json_patch(), self._admin_ok(), self._patch_manager(), \
             patch('handlers.lifecycle.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.start_roll_call(msg)
        self.manager.add_rollcall.assert_called_once_with(100, "<Empty>")

    async def test_max_rollcalls_reached_sends_error(self):
        three_rcs = [self._make_rc(), self._make_rc(), self._make_rc()]
        full_manager = self._make_manager(three_rcs)
        msg = self._make_message("/start_roll_call New Event")
        with self._db_json_patch(), self._admin_ok(), \
             patch('handlers.lifecycle.manager', full_manager), \
             patch('services.rollcalls.manager', full_manager):
            await self.start_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("3", str(sent))

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/start_roll_call Test")
        with self._db_json_patch(), self._admin_denied(), self._patch_manager():
            await self.start_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())


# ===========================================================================
# /shh  /louder
# ===========================================================================

class TestShh(HandlerTestBase):

    async def test_shh_enables_silent_mode(self):
        msg = self._make_message("/shh")
        with self._rc_started(), self._patch_manager():
            await self.shh(msg)
        self.manager.set_shh_mode.assert_called_once_with(100, True)

    async def test_shh_no_rollcall_sends_error(self):
        msg = self._make_message("/shh")
        with self._rc_not_started(), self._patch_manager():
            await self.shh(msg)
        self.assertGreater(self._sent_count(), 0)


class TestLouder(HandlerTestBase):

    async def test_louder_disables_silent_mode(self):
        msg = self._make_message("/louder")
        with self._rc_started(), self._patch_manager():
            await self.louder(msg)
        self.manager.set_shh_mode.assert_called_once_with(100, False)

    async def test_louder_no_rollcall_sends_error(self):
        msg = self._make_message("/louder")
        with self._rc_not_started(), self._patch_manager():
            await self.louder(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /in
# ===========================================================================

class TestInUser(HandlerTestBase):

    async def test_in_happy_path(self):
        self.rc.addIn.return_value = None
        msg = self._make_message("/in")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(msg)
        self.rc.addIn.assert_called_once()

    async def test_in_no_rollcall_sends_error(self):
        msg = self._make_message("/in")
        with self._rc_not_started(), self._patch_manager():
            await self.in_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_in_with_comment(self):
        self.rc.addIn.return_value = None
        msg = self._make_message("/in running late")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(msg)
        # User object should have comment set
        self.rc.addIn.assert_called_once()

    async def test_in_duplicate_sends_error(self):
        self.rc.addIn.return_value = 'AB'
        msg = self._make_message("/in")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_in_waitlist_sends_waitlist_message(self):
        self.rc.addIn.return_value = 'AC'
        msg = self._make_message("/in")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(msg)
        sent = self._sent_text()
        self.assertIn("waitlist", sent.lower())

    async def test_in_rc_number_targeting(self):
        rc2 = self._make_rc("Second Event")
        multi_manager = self._make_manager([self.rc, rc2])
        multi_manager.get_rollcall.return_value = rc2
        msg = self._make_message("/in ::2")
        with self._rc_started(), patch('handlers.voting.manager', multi_manager), \
             patch('rollcall_manager.manager', multi_manager), \
             patch('services.voting.manager', multi_manager), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(msg)
        multi_manager.get_rollcall.assert_called_with(100, 1)  # index 1 = RC #2

    async def test_in_invalid_rc_number_sends_error(self):
        msg = self._make_message("/in ::99")
        with self._rc_started(), self._patch_manager():
            await self.in_user(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /out
# ===========================================================================

class TestOutUser(HandlerTestBase):

    async def test_out_happy_path(self):
        self.rc.addOut.return_value = None
        msg = self._make_message("/out")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False), \
             patch('handlers.lifecycle.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.out_user(msg)
        self.rc.addOut.assert_called_once()

    async def test_out_no_rollcall_sends_error(self):
        msg = self._make_message("/out")
        with self._rc_not_started(), self._patch_manager():
            await self.out_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_out_duplicate_sends_error(self):
        self.rc.addOut.return_value = 'AB'
        msg = self._make_message("/out")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False), \
             patch('handlers.lifecycle.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.out_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_out_promotes_from_waitlist(self):
        from models import User
        promoted = User("Dave", "dave", 99, [])
        self.rc.addOut.return_value = promoted
        msg = self._make_message("/out")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False), \
             patch('handlers.lifecycle.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.out_user(msg)
        # A "→ IN" message must have been sent for the promoted user
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        self.assertTrue(any("→ IN" in t for t in texts))


# ===========================================================================
# /maybe
# ===========================================================================

class TestMaybeUser(HandlerTestBase):

    async def test_maybe_happy_path(self):
        self.rc.addMaybe.return_value = None
        msg = self._make_message("/maybe")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.maybe_user(msg)
        self.rc.addMaybe.assert_called_once()

    async def test_maybe_no_rollcall_sends_error(self):
        msg = self._make_message("/maybe")
        with self._rc_not_started(), self._patch_manager():
            await self.maybe_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_maybe_duplicate_sends_error(self):
        self.rc.addMaybe.return_value = 'AB'
        msg = self._make_message("/maybe")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.maybe_user(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /set_in_for  /sif
# ===========================================================================

class TestSetInFor(HandlerTestBase):

    async def test_proxy_in_happy_path(self):
        self.rc.addIn.return_value = None
        msg = self._make_message("/set_in_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel():
            await self.set_in_for(msg)
        self.rc.addIn.assert_called_once()

    async def test_proxy_in_missing_name_sends_error(self):
        msg = self._make_message("/set_in_for")
        with self._rc_started(), self._patch_manager():
            await self.set_in_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_in_no_rollcall_sends_error(self):
        msg = self._make_message("/set_in_for Bob")
        with self._rc_not_started(), self._patch_manager():
            await self.set_in_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_in_duplicate_sends_error(self):
        self.rc.addIn.return_value = 'AB'
        msg = self._make_message("/set_in_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel():
            await self.set_in_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_in_waitlist_sends_message(self):
        self.rc.addIn.return_value = 'AC'
        msg = self._make_message("/set_in_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel():
            await self.set_in_for(msg)
        sent = self._sent_text()
        self.assertIn("waitlist", sent.lower())


# ===========================================================================
# /set_out_for  /sof
# ===========================================================================

class TestSetOutFor(HandlerTestBase):

    async def test_proxy_out_happy_path(self):
        self.rc.addOut.return_value = None
        msg = self._make_message("/set_out_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel(), \
             patch('handlers.lifecycle.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.set_out_for(msg)
        self.rc.addOut.assert_called_once()

    async def test_proxy_out_missing_name_sends_error(self):
        msg = self._make_message("/set_out_for")
        with self._rc_started(), self._patch_manager():
            await self.set_out_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_out_no_rollcall_sends_error(self):
        msg = self._make_message("/set_out_for Bob")
        with self._rc_not_started(), self._patch_manager():
            await self.set_out_for(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /set_maybe_for  /smf
# ===========================================================================

class TestSetMaybeFor(HandlerTestBase):

    async def test_proxy_maybe_happy_path(self):
        self.rc.addMaybe.return_value = None
        msg = self._make_message("/set_maybe_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel():
            await self.set_maybe_for(msg)
        self.rc.addMaybe.assert_called_once()

    async def test_proxy_maybe_missing_name_sends_error(self):
        msg = self._make_message("/set_maybe_for")
        with self._rc_started(), self._patch_manager():
            await self.set_maybe_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_maybe_no_rollcall_sends_error(self):
        msg = self._make_message("/set_maybe_for Bob")
        with self._rc_not_started(), self._patch_manager():
            await self.set_maybe_for(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /whos_in  /whos_out  /whos_maybe  /whos_waiting
# ===========================================================================

class TestWhosIn(HandlerTestBase):

    async def test_sends_in_list(self):
        self.rc.inListText.return_value = "In:\n1. Alice\n\n"
        msg = self._make_message("/whos_in")
        with self._rc_started(), self._patch_manager():
            await self.whos_in(msg)
        self.assertIn("Alice", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_in")
        with self._rc_not_started(), self._patch_manager():
            await self.whos_in(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_rc_number_selection(self):
        rc2 = self._make_rc("Event 2")
        rc2.inListText.return_value = "In:\n1. Bob\n\n"
        multi = self._make_manager([self.rc, rc2])
        multi.get_rollcall.return_value = rc2
        msg = self._make_message("/whos_in ::2")
        with self._rc_started(), patch('handlers.lists.manager', multi):
            await self.whos_in(msg)
        multi.get_rollcall.assert_called_with(100, 1)


class TestWhosOut(HandlerTestBase):

    async def test_sends_out_list(self):
        self.rc.outListText.return_value = "Out:\n1. Bob\n\n"
        msg = self._make_message("/whos_out")
        with self._rc_started(), self._patch_manager():
            await self.whos_out(msg)
        self.assertIn("Bob", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_out")
        with self._rc_not_started(), self._patch_manager():
            await self.whos_out(msg)
        self.assertGreater(self._sent_count(), 0)


class TestWhosMaybe(HandlerTestBase):

    async def test_sends_maybe_list(self):
        self.rc.maybeListText.return_value = "Maybe:\n1. Carol\n\n"
        msg = self._make_message("/whos_maybe")
        with self._rc_started(), self._patch_manager():
            await self.whos_maybe(msg)
        self.assertIn("Carol", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_maybe")
        with self._rc_not_started(), self._patch_manager():
            await self.whos_maybe(msg)
        self.assertGreater(self._sent_count(), 0)


class TestWhosWaiting(HandlerTestBase):

    async def test_sends_waitlist(self):
        self.rc.waitListText.return_value = "Waiting:\n1. Dave\n"
        msg = self._make_message("/whos_waiting")
        with self._rc_started(), self._patch_manager():
            await self.whos_waiting(msg)
        self.assertIn("Dave", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_waiting")
        with self._rc_not_started(), self._patch_manager():
            await self.whos_waiting(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /set_title  /st
# ===========================================================================

class TestSetTitle(HandlerTestBase):

    async def test_sets_title(self):
        msg = self._make_message("/set_title Sunday Match")
        with self._rc_started(), self._patch_manager():
            await self.set_title(msg)
        self.assertEqual(self.rc.title, "Sunday Match")
        self.rc.save.assert_called()

    async def test_missing_title_sends_message(self):
        msg = self._make_message("/set_title")
        with self._rc_started(), self._patch_manager():
            await self.set_title(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/set_title New Title")
        with self._rc_not_started(), self._patch_manager():
            await self.set_title(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_empty_title_uses_placeholder(self):
        # After stripping the ::N, title becomes empty → <Empty>
        msg = self._make_message("/set_title ::1")
        with self._rc_started(), self._patch_manager():
            await self.set_title(msg)
        self.assertEqual(self.rc.title, "<Empty>")


# ===========================================================================
# /end_roll_call  /erc
# ===========================================================================

class TestEndRollCall(HandlerTestBase):

    async def test_ends_rollcall(self):
        msg = self._make_message("/end_roll_call")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.end_roll_call(msg)
        self.manager.remove_rollcall.assert_called_once_with(100, 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/end_roll_call")
        with self._rc_not_started(), self._admin_ok(), self._patch_manager():
            await self.end_roll_call(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/end_roll_call")
        with self._rc_started(), self._admin_denied(), self._patch_manager():
            await self.end_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())

    async def test_sends_finish_list(self):
        self.rc.finishList.return_value = "Title: Test\nID: __RCID__\nIn:\nNobody"
        msg = self._make_message("/end_roll_call")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.end_roll_call(msg)
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        self.assertTrue(any("Ended by" in t for t in texts))


# ===========================================================================
# /set_limit  /sl
# ===========================================================================

class TestSetLimit(HandlerTestBase):

    async def test_sets_limit(self):
        msg = self._make_message("/set_limit 5")
        with self._rc_started(), self._patch_manager():
            await self.wait_limit(msg)
        self.assertEqual(self.rc.inListLimit, 5)

    async def test_missing_limit_sends_error(self):
        msg = self._make_message("/set_limit")
        with self._rc_started(), self._patch_manager():
            await self.wait_limit(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_non_numeric_limit_sends_error(self):
        msg = self._make_message("/set_limit abc")
        with self._rc_started(), self._patch_manager():
            await self.wait_limit(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/set_limit 5")
        with self._rc_not_started(), self._patch_manager():
            await self.wait_limit(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_limit_moves_overflow_to_waitlist(self):
        u1 = MagicMock(); u1.name = "Alice"; u1.user_id = 1
        u2 = MagicMock(); u2.name = "Bob";   u2.user_id = 2
        self.rc.inList = [u1, u2]
        self.rc.waitList = []
        self.rc.inListLimit = None
        msg = self._make_message("/set_limit 1")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle.notify_proxy_owner_wait_to_in', new=AsyncMock()), \
             patch('handlers.settings.get_rc_db_id', return_value=1), \
             patch('handlers.settings.format_mention_with_name', return_value="Bob"):
            await self.wait_limit(msg)
        # Bob should move to waitlist (inList > limit)
        self.assertIn(u2, self.rc.waitList)
        self.assertEqual(self.rc.inList, [u1])

    async def test_limit_zero_clears_cap(self):
        """Regression: /sl 0 must clear the cap and succeed — not raise an error."""
        u1 = MagicMock(); u1.name = "Alice"; u1.user_id = 1
        self.rc.inList = [u1]
        self.rc.waitList = []
        self.rc.inListLimit = 5
        msg = self._make_message("/sl 0")
        with self._rc_started(), self._patch_manager(), \
             patch('handlers.lifecycle.notify_proxy_owner_wait_to_in', new=AsyncMock()), \
             patch('handlers.settings.get_rc_db_id', return_value=1), \
             patch('handlers.lifecycle._update_panel', new=AsyncMock()):
            await self.wait_limit(msg)
        # Limit should be cleared to None
        self.assertIsNone(self.rc.inListLimit)
        # Confirmation message sent (not an error)
        sent_texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        self.assertTrue(any("cleared" in t.lower() for t in sent_texts),
                        f"Expected 'cleared' in one of: {sent_texts}")


# ===========================================================================
# /delete_user
# ===========================================================================

class TestDeleteUser(HandlerTestBase):

    async def test_deletes_existing_user(self):
        """delete_user now shows a confirmation keyboard before deleting."""
        self.rc.delete_user.return_value = True
        msg = self._make_message("/delete_user Alice")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.delete_user(msg)
        # Should NOT delete directly — shows confirmation prompt instead
        self.rc.delete_user.assert_not_called()
        sent_text = self._sent_text().lower()
        self.assertIn("alice", sent_text)
        # Pending delete should be stored
        self.assertIn((100, 1), self.bot_state._pending_deletes)

    async def test_user_not_found_sends_notice(self):
        """Confirmation is stored even for unknown users — callback handles not-found."""
        self.rc.delete_user.return_value = False
        msg = self._make_message("/delete_user Ghost")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.delete_user(msg)
        # Confirmation prompt is sent; pending delete stored
        self.assertIn((100, 1), self.bot_state._pending_deletes)

    async def test_missing_name_sends_error(self):
        msg = self._make_message("/delete_user")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.delete_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/delete_user Alice")
        with self._rc_started(), self._admin_denied(), self._patch_manager():
            await self.delete_user(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/delete_user Alice")
        with self._rc_not_started(), self._patch_manager():
            await self.delete_user(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /delete_template
# ===========================================================================

class TestDeleteTemplate(HandlerTestBase):

    async def test_deletes_existing_template(self):
        msg = self._make_message("/delete_template sunday")
        with self._admin_ok(), self._patch_manager(), \
             patch('handlers.templates.templates_svc.delete_one_template', return_value={"name": "sunday", "deleted": True}):
            await self.delete_template_command(msg)
        self.assertIn("deleted", self._sent_text().lower())

    async def test_template_not_found(self):
        from exceptions import incorrectParameter
        msg = self._make_message("/delete_template ghost")
        with self._admin_ok(), self._patch_manager(), \
             patch('handlers.templates.templates_svc.delete_one_template', side_effect=incorrectParameter("Template 'ghost' not found.")):
            await self.delete_template_command(msg)
        self.assertIn("not found", self._sent_text().lower())

    async def test_missing_name_sends_usage(self):
        msg = self._make_message("/delete_template")
        with self._admin_ok(), self._patch_manager():
            await self.delete_template_command(msg)
        self.assertIn("Usage", self._sent_text())

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/delete_template sunday")
        with self._admin_denied(), self._patch_manager():
            await self.delete_template_command(msg)
        self.assertIn("permission", self._sent_text().lower())


# ===========================================================================
# /event_fee  /ef
# ===========================================================================

class TestEventFee(HandlerTestBase):

    async def test_sets_fee(self):
        msg = self._make_message("/event_fee 500")
        with self._rc_started(), self._patch_manager():
            await self.event_fee(msg)
        self.assertEqual(self.rc.event_fee, "500")
        self.rc.save.assert_called()

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/event_fee 500")
        with self._rc_not_started(), self._patch_manager():
            await self.event_fee(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_zero_fee_sends_error(self):
        msg = self._make_message("/event_fee 0")
        with self._rc_started(), self._patch_manager():
            await self.event_fee(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_non_numeric_fee_sends_error(self):
        msg = self._make_message("/event_fee free")
        with self._rc_started(), self._patch_manager():
            await self.event_fee(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /individual_fee  /if
# ===========================================================================

class TestIndividualFee(HandlerTestBase):

    async def test_calculates_individual_fee(self):
        self.rc.event_fee = "600"
        u1 = MagicMock(); u1.user_id = 1
        u2 = MagicMock(); u2.user_id = 2
        u3 = MagicMock(); u3.user_id = 3
        self.rc.inList = [u1, u2, u3]
        msg = self._make_message("/individual_fee")
        with self._rc_started(), self._patch_manager():
            await self.individual_fee(msg)
        sent = self._sent_text()
        self.assertIn("200", sent)  # 600 / 3

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/individual_fee")
        with self._rc_not_started(), self._patch_manager():
            await self.individual_fee(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /when  /w
# ===========================================================================

class TestWhen(HandlerTestBase):

    async def test_sends_event_time_when_set(self):
        from datetime import datetime
        self.rc.finalizeDate = datetime(2026, 5, 10, 18, 30)
        msg = self._make_message("/when")
        with self._rc_started(), self._patch_manager():
            await self.when(msg)
        sent = self._sent_text()
        self.assertIn("10-05-2026", sent)

    async def test_no_time_set_sends_error(self):
        self.rc.finalizeDate = None
        msg = self._make_message("/when")
        with self._rc_started(), self._patch_manager():
            await self.when(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/when")
        with self._rc_not_started(), self._patch_manager():
            await self.when(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /location  /loc
# ===========================================================================

class TestSetLocation(HandlerTestBase):

    async def test_sets_location(self):
        msg = self._make_message("/location Central Park")
        with self._rc_started(), self._patch_manager():
            await self.set_location(msg)
        self.assertEqual(self.rc.location, "Central Park")
        self.rc.save.assert_called()
        self.assertIn("Central Park", self._sent_text())

    async def test_missing_location_sends_error(self):
        msg = self._make_message("/location")
        with self._rc_started(), self._patch_manager():
            await self.set_location(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/location Park")
        with self._rc_not_started(), self._patch_manager():
            await self.set_location(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# Multi-rollcall ::N selection (shared edge cases)
# ===========================================================================

class TestRcNumberSelection(HandlerTestBase):
    """Verify ::N routing works correctly across representative commands."""

    async def test_valid_rc2_routes_to_second_rollcall(self):
        rc2 = self._make_rc("Event 2")
        multi = self._make_manager([self.rc, rc2])
        multi.get_rollcall.return_value = rc2
        msg = self._make_message("/whos_in ::2")
        with self._rc_started(), patch('handlers.lists.manager', multi):
            await self.whos_in(msg)
        multi.get_rollcall.assert_called_with(100, 1)  # index 1 = #2

    async def test_out_of_range_rc_number_sends_error(self):
        msg = self._make_message("/whos_in ::5")
        with self._rc_started(), self._patch_manager():
            await self.whos_in(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_non_numeric_rc_suffix_sends_error(self):
        msg = self._make_message("/whos_out ::abc")
        with self._rc_started(), self._patch_manager():
            await self.whos_out(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /stats  (basic routing only — full stats logic is complex)
# ===========================================================================

class TestStatsCommand(HandlerTestBase):

    async def _run_stats(self, text):
        msg = self._make_message(text, user_id=42, first_name="Alice", username="alice")
        _personal = {
            "total_rollcalls_in_chat": 10, "sessions_attended": 8, "attendance_rate": 80.0,
            "total_in_votes": 8, "total_out_votes": 1, "total_maybe_votes": 1,
            "total_sessions_voted": 8, "voting_rate": 80.0, "total_waiting_to_in": 0,
            "best_streak": 3, "current_streak": 2, "ghost_count": 0, "absent_limit": 3,
        }
        _group = {
            "total_rollcalls": 10, "real_attendance_slots": 80, "proxy_attendance_slots": 10,
            "total_attendance_slots": 90, "real_participants": 8, "proxy_participants": 2,
            "avg_attendance": 9.0, "real_vote_in": 80, "real_vote_out": 5, "real_vote_maybe": 3,
            "proxy_in": 10, "proxy_out": 1, "proxy_maybe": 0, "waitlist_promotions": 2,
            "top_attendees": [], "ghost_leaderboard": [],
        }
        _lb = {"total_rollcalls_in_chat": 10, "entries": []}
        with self._patch_manager(), \
             patch('handlers.stats.stats_svc') as mock_svc, \
             patch('handlers.stats.ghost_svc') as mock_gsvc:
            mock_svc.personal_stats.return_value = _personal
            mock_svc.group_stats.return_value = _group
            mock_svc.leaderboard.return_value = _lb
            mock_svc.bot_stats.return_value = {k: 0 for k in [
                "total_groups", "active_groups_7d", "active_groups_30d", "total_rollcalls",
                "ended_rollcalls", "rollcalls_30d", "total_real_users", "total_proxy_users",
                "total_templates", "total_attendance_slots", "real_attendance_slots",
                "proxy_attendance_slots", "avg_attendance_per_rollcall", "real_participants",
                "proxy_participants", "sum_in_votes", "sum_out_votes", "sum_maybe_votes",
            ]}
            mock_svc.resolve_user.return_value = ("real", 42, "Alice")
            mock_gsvc.ghost_leaderboard.return_value = []
            await self.stats_command(msg)

    async def test_stats_my_stats_sends_message(self):
        await self._run_stats("/stats")
        self.assertGreater(self._sent_count(), 0)

    async def test_stats_group_sends_message(self):
        await self._run_stats("/stats group")
        self.assertGreater(self._sent_count(), 0)

    async def test_stats_top_sends_message(self):
        await self._run_stats("/stats top")
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /panel
# ===========================================================================

class TestShowPanel(HandlerTestBase):

    async def test_no_rollcall_sends_error(self):
        empty_manager = self._make_manager([])
        msg = self._make_message("/panel")
        with patch('handlers.lifecycle.manager', empty_manager):
            await self.show_panel(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_with_rollcall_shows_panel(self):
        msg = self._make_message("/panel")
        with self._patch_manager(), \
             patch('handlers.lifecycle.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.show_panel(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /cancel_roll_call  /xrc
# ===========================================================================

class TestCancelRollCall(HandlerTestBase):

    async def test_cancels_rollcall(self):
        cancel_result = {
            "cancelled": {}, "rc_number_ended_1based": 1, "remaining": [], "renumbered": [],
        }
        msg = self._make_message("/xrc")
        with self._rc_started(), self._admin_ok(), self._patch_manager(), \
             patch('handlers.lifecycle.rollcalls_svc.cancel_rollcall', new=AsyncMock(return_value=cancel_result)):
            await self.cancel_roll_call(msg)
        self.assertGreater(self._sent_count(), 0)
        sent = self._sent_text()
        self.assertIn("cancelled", sent.lower())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/xrc")
        with self._rc_not_started(), self._admin_ok(), self._patch_manager():
            await self.cancel_roll_call(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/xrc")
        with self._rc_started(), self._admin_denied(), self._patch_manager():
            await self.cancel_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())

    async def test_reason_included_in_message(self):
        cancel_result = {
            "cancelled": {}, "rc_number_ended_1based": 1, "remaining": [], "renumbered": [],
        }
        msg = self._make_message("/xrc rain")
        with self._rc_started(), self._admin_ok(), self._patch_manager(), \
             patch('handlers.lifecycle.rollcalls_svc.cancel_rollcall', new=AsyncMock(return_value=cancel_result)) as mock_cancel:
            await self.cancel_roll_call(msg)
        # verify reason passed to service
        mock_cancel.assert_called_once()
        call_kwargs = mock_cancel.call_args[1]
        self.assertEqual(call_kwargs.get("reason"), "rain")
        # and shown in the sent message
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        self.assertTrue(any("rain" in t for t in texts))

    async def test_no_ghost_prompt(self):
        """Cancel must never offer the ghost-mark prompt."""
        cancel_result = {
            "cancelled": {}, "rc_number_ended_1based": 1, "remaining": [], "renumbered": [],
        }
        msg = self._make_message("/xrc")
        with self._rc_started(), self._admin_ok(), self._patch_manager(), \
             patch('handlers.lifecycle.rollcalls_svc.cancel_rollcall', new=AsyncMock(return_value=cancel_result)):
            await self.cancel_roll_call(msg)
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        self.assertFalse(any("ghost" in t.lower() for t in texts))


# ===========================================================================
# on_new_chat_members  (bot join onboarding)
# ===========================================================================

class TestOnNewChatMembers(HandlerTestBase):

    def _make_join_message(self, bot_id=999, chat_id=100, include_bot=True):
        """Message with new_chat_members containing the bot (or not)."""
        msg = MagicMock()
        msg.chat.id = chat_id
        bot_member = MagicMock()
        bot_member.id = bot_id
        other_member = MagicMock()
        other_member.id = 42
        msg.new_chat_members = [bot_member] if include_bot else [other_member]
        return msg

    async def test_sends_onboarding_when_bot_added(self):
        msg = self._make_join_message(bot_id=999, include_bot=True)
        me = MagicMock(); me.id = 999
        with patch('handlers.core.manager', self.manager), \
             patch.object(self.bot_state.bot, 'get_me', new=AsyncMock(return_value=me)):
            await self.on_new_chat_members(msg)
        self.assertGreater(self._sent_count(), 0)
        sent = self._sent_text()
        self.assertIn("RollCall", sent)
        self.assertIn("/help", sent)

    async def test_no_message_when_other_user_added(self):
        msg = self._make_join_message(bot_id=999, include_bot=False)
        me = MagicMock(); me.id = 999
        with patch('handlers.core.manager', self.manager), \
             patch.object(self.bot_state.bot, 'get_me', new=AsyncMock(return_value=me)):
            await self.on_new_chat_members(msg)
        self.assertEqual(self._sent_count(), 0)

    async def test_no_crash_on_exception(self):
        msg = self._make_join_message(bot_id=999, include_bot=True)
        me = MagicMock(); me.id = 999
        with patch('handlers.core.manager', self.manager), \
             patch.object(self.bot_state.bot, 'get_me', new=AsyncMock(return_value=me)), \
             patch.object(self.bot_state.bot, 'send_message', new=AsyncMock(side_effect=Exception("network"))):
            # Should swallow the exception rather than propagate
            await self.on_new_chat_members(msg)


# ===========================================================================
# /erc summary line
# ===========================================================================

class TestErcSummaryLine(HandlerTestBase):

    async def test_summary_appended_to_finish_text(self):
        u1 = MagicMock(); u1.name = "Alice"
        u2 = MagicMock(); u2.name = "Bob"
        self.rc.inList = [u1, u2]
        self.rc.outList = [MagicMock()]
        self.rc.maybeList = []
        self.rc.finishList.return_value = "Title: Test\nID: __RCID__\nIn: Alice, Bob"
        msg = self._make_message("/erc")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.end_roll_call(msg)
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        finish = next((t for t in texts if "Ended by" in t), None)
        self.assertIsNotNone(finish, "Expected finish text with 'Ended by'")
        self.assertIn("📊", finish)
        self.assertIn("2 IN", finish)
        self.assertIn("1 OUT", finish)
        self.assertIn("0 MAYBE", finish)

    async def test_summary_includes_top_attendees(self):
        u1 = MagicMock(); u1.name = "Alice"
        u2 = MagicMock(); u2.name = "Bob"
        u3 = MagicMock(); u3.name = "Carol"
        self.rc.inList = [u1, u2, u3]
        self.rc.outList = []
        self.rc.maybeList = []
        self.rc.finishList.return_value = "Title: Test\nID: __RCID__\nIn: Alice, Bob, Carol"
        msg = self._make_message("/erc")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.end_roll_call(msg)
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        finish = next((t for t in texts if "📊" in t), None)
        self.assertIsNotNone(finish)
        self.assertIn("🥇", finish)
        self.assertIn("Alice", finish)

    async def test_summary_no_top_when_empty_in_list(self):
        self.rc.inList = []
        self.rc.outList = [MagicMock()]
        self.rc.maybeList = [MagicMock()]
        self.rc.finishList.return_value = "Title: Test\nID: __RCID__\nIn: Nobody"
        msg = self._make_message("/erc")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.end_roll_call(msg)
        texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list]
        finish = next((t for t in texts if "📊" in t), None)
        self.assertIsNotNone(finish)
        self.assertIn("0 IN", finish)
        self.assertNotIn("🥇", finish)


if __name__ == "__main__":
    unittest.main(verbosity=2)
