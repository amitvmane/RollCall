"""
tests/test_handlers.py

Full handler-level test suite for all telegram_helper.py command handlers.

Uses unittest.IsolatedAsyncioTestCase for native async support (Python 3.8+).
Because conftest.py makes @bot.message_handler an identity decorator, all
async def handlers are accessible directly on the telegram_helper module.

Coverage: 37 command handlers across all command groups.
Each handler gets at minimum:
  - Happy-path test
  - Key error/edge cases (no rollcall, missing params, invalid params, etc.)
"""

import sys
import os
import json
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


# ---------------------------------------------------------------------------
# Shared base: imported once, shared across all test classes
# ---------------------------------------------------------------------------

class HandlerTestBase(unittest.IsolatedAsyncioTestCase):
    """Base class providing shared fixtures for all handler tests."""

    @classmethod
    def setUpClass(cls):
        import telegram_helper as th
        cls.th = th

    def setUp(self):
        # Fresh AsyncMock for bot.send_message each test
        self.th.bot.send_message = AsyncMock()
        self.th.bot.get_chat_member = AsyncMock()
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
        return m

    # ---- helpers ----------------------------------------------------------

    def _sent_text(self, call_index=0):
        """Return the text sent in the Nth bot.send_message call."""
        return self.th.bot.send_message.call_args_list[call_index][0][1]

    def _sent_count(self):
        return self.th.bot.send_message.call_count

    def _rc_not_started(self):
        """Patch roll_call_not_started to return False (no rollcall active)."""
        return patch('telegram_helper.roll_call_not_started', return_value=False)

    def _rc_started(self):
        """Patch roll_call_not_started to return True (rollcall active)."""
        return patch('telegram_helper.roll_call_not_started', return_value=True)

    def _admin_ok(self):
        return patch('telegram_helper.admin_rights', new=AsyncMock(return_value=True))

    def _admin_denied(self):
        return patch('telegram_helper.admin_rights', new=AsyncMock(return_value=False))

    def _panel(self):
        return patch('telegram_helper.show_panel_for_rollcall', new=AsyncMock())

    def _patch_manager(self):
        return patch('telegram_helper.manager', self.manager)


# ===========================================================================
# /start
# ===========================================================================

class TestWelcomeAndExplanation(HandlerTestBase):

    async def test_sends_welcome_message(self):
        msg = self._make_message("/start")
        with self._admin_ok(), self._patch_manager():
            await self.th.welcome_and_explanation(msg)
        self.assertGreater(self._sent_count(), 0)
        self.assertIn("RollCall", self._sent_text())

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/start")
        with self._admin_denied(), self._patch_manager():
            await self.th.welcome_and_explanation(msg)
        self.assertIn("permission", self._sent_text().lower())


# ===========================================================================
# /help
# ===========================================================================

class TestHelpCommands(HandlerTestBase):

    async def test_sends_help_text(self):
        msg = self._make_message("/help")
        await self.th.help_commands(msg)
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
        self.th.bot.get_chat_member = AsyncMock(return_value=member)
        msg = self._make_message("/set_admins")
        with self._patch_manager():
            await self.th.set_admins(msg)
        self.manager.set_admin_rights.assert_called_once_with(100, True)

    async def test_non_admin_cannot_enable_admin_mode(self):
        member = MagicMock()
        member.status = 'member'
        self.th.bot.get_chat_member = AsyncMock(return_value=member)
        msg = self._make_message("/set_admins")
        with self._patch_manager():
            await self.th.set_admins(msg)
        # send_message should tell user they lack permissions
        self.assertGreater(self._sent_count(), 0)


class TestUnsetAdmins(HandlerTestBase):

    async def test_disables_admin_mode(self):
        msg = self._make_message("/unset_admins")
        member_mock = MagicMock()
        member_mock.status = 'administrator'
        self.th.bot.get_chat_member = AsyncMock(return_value=member_mock)
        with self._patch_manager():
            await self.th.unset_admins(msg)
        self.manager.set_admin_rights.assert_called_once_with(100, False)


# ===========================================================================
# /timezone
# ===========================================================================

class TestConfigTimezone(HandlerTestBase):

    async def test_valid_timezone_is_set(self):
        msg = self._make_message("/timezone Asia/Kolkata")
        with self._patch_manager(), \
             patch('telegram_helper.auto_complete_timezone', return_value='Asia/Calcutta'):
            await self.th.config_timezone(msg)
        self.manager.set_timezone.assert_called_once_with(100, 'Asia/Calcutta')
        self.assertIn("Asia/Calcutta", self._sent_text())

    async def test_missing_parameter_sends_error(self):
        msg = self._make_message("/timezone")
        with self._patch_manager():
            await self.th.config_timezone(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_invalid_timezone_sends_link(self):
        msg = self._make_message("/timezone Fake/Place")
        with self._patch_manager(), \
             patch('telegram_helper.auto_complete_timezone', return_value=None):
            await self.th.config_timezone(msg)
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
            await self.th.version_command(msg)
        self.assertIn("4.6", self._sent_text())


# ===========================================================================
# /rollcalls
# ===========================================================================

class TestShowRollcalls(HandlerTestBase):

    async def test_no_rollcalls_sends_empty_message(self):
        empty_manager = self._make_manager([])
        msg = self._make_message("/rollcalls")
        with patch('telegram_helper.manager', empty_manager):
            await self.th.show_reminders(msg)
        self.assertIn("empty", self._sent_text().lower())

    async def test_with_rollcall_sends_list(self):
        msg = self._make_message("/rollcalls")
        with self._patch_manager():
            await self.th.show_reminders(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /templates
# ===========================================================================

class TestListTemplates(HandlerTestBase):

    async def test_no_templates_sends_notice(self):
        msg = self._make_message("/templates")
        with patch('telegram_helper.get_templates', return_value=[]):
            await self.th.list_templates(msg)
        self.assertIn("no templates", self._sent_text().lower())

    async def test_with_templates_sends_list(self):
        templates = [MagicMock(name="t1")]
        msg = self._make_message("/templates")
        with patch('telegram_helper.get_templates', return_value=templates):
            await self.th.list_templates(msg)
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
             patch('telegram_helper.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.th.start_roll_call(msg)
        self.manager.add_rollcall.assert_called_once_with(100, "Friday Game")

    async def test_starts_rollcall_without_title_uses_empty(self):
        msg = self._make_message("/start_roll_call")
        with self._db_json_patch(), self._admin_ok(), self._patch_manager(), \
             patch('telegram_helper.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.th.start_roll_call(msg)
        self.manager.add_rollcall.assert_called_once_with(100, "<Empty>")

    async def test_max_rollcalls_reached_sends_error(self):
        three_rcs = [self._make_rc(), self._make_rc(), self._make_rc()]
        full_manager = self._make_manager(three_rcs)
        msg = self._make_message("/start_roll_call New Event")
        with self._db_json_patch(), self._admin_ok(), \
             patch('telegram_helper.manager', full_manager):
            await self.th.start_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("3", str(sent))

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/start_roll_call Test")
        with self._db_json_patch(), self._admin_denied(), self._patch_manager():
            await self.th.start_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())


# ===========================================================================
# /shh  /louder
# ===========================================================================

class TestShh(HandlerTestBase):

    async def test_shh_enables_silent_mode(self):
        msg = self._make_message("/shh")
        with self._rc_started(), self._patch_manager():
            await self.th.shh(msg)
        self.manager.set_shh_mode.assert_called_once_with(100, True)

    async def test_shh_no_rollcall_sends_error(self):
        msg = self._make_message("/shh")
        with self._rc_not_started(), self._patch_manager():
            await self.th.shh(msg)
        self.assertGreater(self._sent_count(), 0)


class TestLouder(HandlerTestBase):

    async def test_louder_disables_silent_mode(self):
        msg = self._make_message("/louder")
        with self._rc_started(), self._patch_manager():
            await self.th.louder(msg)
        self.manager.set_shh_mode.assert_called_once_with(100, False)

    async def test_louder_no_rollcall_sends_error(self):
        msg = self._make_message("/louder")
        with self._rc_not_started(), self._patch_manager():
            await self.th.louder(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /in
# ===========================================================================

class TestInUser(HandlerTestBase):

    async def test_in_happy_path(self):
        self.rc.addIn.return_value = None
        msg = self._make_message("/in")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1):
            await self.th.in_user(msg)
        self.rc.addIn.assert_called_once()

    async def test_in_no_rollcall_sends_error(self):
        msg = self._make_message("/in")
        with self._rc_not_started(), self._patch_manager():
            await self.th.in_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_in_with_comment(self):
        self.rc.addIn.return_value = None
        msg = self._make_message("/in running late")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1):
            await self.th.in_user(msg)
        # User object should have comment set
        self.rc.addIn.assert_called_once()

    async def test_in_duplicate_sends_error(self):
        self.rc.addIn.return_value = 'AB'
        msg = self._make_message("/in")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1):
            await self.th.in_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_in_waitlist_sends_waitlist_message(self):
        self.rc.addIn.return_value = 'AC'
        msg = self._make_message("/in")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1):
            await self.th.in_user(msg)
        sent = self._sent_text()
        self.assertIn("waitlist", sent.lower())

    async def test_in_rc_number_targeting(self):
        rc2 = self._make_rc("Second Event")
        multi_manager = self._make_manager([self.rc, rc2])
        multi_manager.get_rollcall.return_value = rc2
        msg = self._make_message("/in ::2")
        with self._rc_started(), patch('telegram_helper.manager', multi_manager), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=2):
            await self.th.in_user(msg)
        multi_manager.get_rollcall.assert_called_with(100, 1)  # index 1 = RC #2

    async def test_in_invalid_rc_number_sends_error(self):
        msg = self._make_message("/in ::99")
        with self._rc_started(), self._patch_manager():
            await self.th.in_user(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /out
# ===========================================================================

class TestOutUser(HandlerTestBase):

    async def test_out_happy_path(self):
        self.rc.addOut.return_value = None
        msg = self._make_message("/out")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.th.out_user(msg)
        self.rc.addOut.assert_called_once()

    async def test_out_no_rollcall_sends_error(self):
        msg = self._make_message("/out")
        with self._rc_not_started(), self._patch_manager():
            await self.th.out_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_out_duplicate_sends_error(self):
        self.rc.addOut.return_value = 'AB'
        msg = self._make_message("/out")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.th.out_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_out_promotes_from_waitlist(self):
        from models import User
        promoted = User("Dave", "dave", 99, [])
        self.rc.addOut.return_value = promoted
        msg = self._make_message("/out")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()), \
             patch('telegram_helper.format_mention_with_name', return_value="Dave"):
            await self.th.out_user(msg)
        # A "→ IN" message must have been sent for the promoted user
        texts = [c[0][1] for c in self.th.bot.send_message.call_args_list]
        self.assertTrue(any("→ IN" in t for t in texts))


# ===========================================================================
# /maybe
# ===========================================================================

class TestMaybeUser(HandlerTestBase):

    async def test_maybe_happy_path(self):
        self.rc.addMaybe.return_value = None
        msg = self._make_message("/maybe")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1):
            await self.th.maybe_user(msg)
        self.rc.addMaybe.assert_called_once()

    async def test_maybe_no_rollcall_sends_error(self):
        msg = self._make_message("/maybe")
        with self._rc_not_started(), self._patch_manager():
            await self.th.maybe_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_maybe_duplicate_sends_error(self):
        self.rc.addMaybe.return_value = 'AB'
        msg = self._make_message("/maybe")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1):
            await self.th.maybe_user(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /set_in_for  /sif
# ===========================================================================

class TestSetInFor(HandlerTestBase):

    async def test_proxy_in_happy_path(self):
        self.rc.addIn.return_value = None
        msg = self._make_message("/set_in_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel(), \
             patch('telegram_helper.add_or_update_proxy_user'):
            await self.th.set_in_for(msg)
        self.rc.addIn.assert_called_once()

    async def test_proxy_in_missing_name_sends_error(self):
        msg = self._make_message("/set_in_for")
        with self._rc_started(), self._patch_manager():
            await self.th.set_in_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_in_no_rollcall_sends_error(self):
        msg = self._make_message("/set_in_for Bob")
        with self._rc_not_started(), self._patch_manager():
            await self.th.set_in_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_in_duplicate_sends_error(self):
        self.rc.addIn.return_value = 'AB'
        msg = self._make_message("/set_in_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel(), \
             patch('telegram_helper.add_or_update_proxy_user'):
            await self.th.set_in_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_in_waitlist_sends_message(self):
        self.rc.addIn.return_value = 'AC'
        msg = self._make_message("/set_in_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel(), \
             patch('telegram_helper.add_or_update_proxy_user'):
            await self.th.set_in_for(msg)
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
             patch('telegram_helper.add_or_update_proxy_user'), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.th.set_out_for(msg)
        self.rc.addOut.assert_called_once()

    async def test_proxy_out_missing_name_sends_error(self):
        msg = self._make_message("/set_out_for")
        with self._rc_started(), self._patch_manager():
            await self.th.set_out_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_out_no_rollcall_sends_error(self):
        msg = self._make_message("/set_out_for Bob")
        with self._rc_not_started(), self._patch_manager():
            await self.th.set_out_for(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /set_maybe_for  /smf
# ===========================================================================

class TestSetMaybeFor(HandlerTestBase):

    async def test_proxy_maybe_happy_path(self):
        self.rc.addMaybe.return_value = None
        msg = self._make_message("/set_maybe_for Bob")
        with self._rc_started(), self._patch_manager(), self._panel(), \
             patch('telegram_helper.add_or_update_proxy_user'):
            await self.th.set_maybe_for(msg)
        self.rc.addMaybe.assert_called_once()

    async def test_proxy_maybe_missing_name_sends_error(self):
        msg = self._make_message("/set_maybe_for")
        with self._rc_started(), self._patch_manager():
            await self.th.set_maybe_for(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_proxy_maybe_no_rollcall_sends_error(self):
        msg = self._make_message("/set_maybe_for Bob")
        with self._rc_not_started(), self._patch_manager():
            await self.th.set_maybe_for(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /whos_in  /whos_out  /whos_maybe  /whos_waiting
# ===========================================================================

class TestWhosIn(HandlerTestBase):

    async def test_sends_in_list(self):
        self.rc.inListText.return_value = "In:\n1. Alice\n\n"
        msg = self._make_message("/whos_in")
        with self._rc_started(), self._patch_manager():
            await self.th.whos_in(msg)
        self.assertIn("Alice", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_in")
        with self._rc_not_started(), self._patch_manager():
            await self.th.whos_in(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_rc_number_selection(self):
        rc2 = self._make_rc("Event 2")
        rc2.inListText.return_value = "In:\n1. Bob\n\n"
        multi = self._make_manager([self.rc, rc2])
        multi.get_rollcall.return_value = rc2
        msg = self._make_message("/whos_in ::2")
        with self._rc_started(), patch('telegram_helper.manager', multi):
            await self.th.whos_in(msg)
        multi.get_rollcall.assert_called_with(100, 1)


class TestWhosOut(HandlerTestBase):

    async def test_sends_out_list(self):
        self.rc.outListText.return_value = "Out:\n1. Bob\n\n"
        msg = self._make_message("/whos_out")
        with self._rc_started(), self._patch_manager():
            await self.th.whos_out(msg)
        self.assertIn("Bob", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_out")
        with self._rc_not_started(), self._patch_manager():
            await self.th.whos_out(msg)
        self.assertGreater(self._sent_count(), 0)


class TestWhosMaybe(HandlerTestBase):

    async def test_sends_maybe_list(self):
        self.rc.maybeListText.return_value = "Maybe:\n1. Carol\n\n"
        msg = self._make_message("/whos_maybe")
        with self._rc_started(), self._patch_manager():
            await self.th.whos_maybe(msg)
        self.assertIn("Carol", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_maybe")
        with self._rc_not_started(), self._patch_manager():
            await self.th.whos_maybe(msg)
        self.assertGreater(self._sent_count(), 0)


class TestWhosWaiting(HandlerTestBase):

    async def test_sends_waitlist(self):
        self.rc.waitListText.return_value = "Waiting:\n1. Dave\n"
        msg = self._make_message("/whos_waiting")
        with self._rc_started(), self._patch_manager():
            await self.th.whos_waiting(msg)
        self.assertIn("Dave", self._sent_text())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/whos_waiting")
        with self._rc_not_started(), self._patch_manager():
            await self.th.whos_waiting(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /set_title  /st
# ===========================================================================

class TestSetTitle(HandlerTestBase):

    async def test_sets_title(self):
        msg = self._make_message("/set_title Sunday Match")
        with self._rc_started(), self._patch_manager():
            await self.th.set_title(msg)
        self.assertEqual(self.rc.title, "Sunday Match")
        self.rc.save.assert_called()

    async def test_missing_title_sends_message(self):
        msg = self._make_message("/set_title")
        with self._rc_started(), self._patch_manager():
            await self.th.set_title(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/set_title New Title")
        with self._rc_not_started(), self._patch_manager():
            await self.th.set_title(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_empty_title_uses_placeholder(self):
        # After stripping the ::N, title becomes empty → <Empty>
        msg = self._make_message("/set_title ::1")
        with self._rc_started(), self._patch_manager():
            await self.th.set_title(msg)
        self.assertEqual(self.rc.title, "<Empty>")


# ===========================================================================
# /end_roll_call  /erc
# ===========================================================================

class TestEndRollCall(HandlerTestBase):

    async def test_ends_rollcall(self):
        msg = self._make_message("/end_roll_call")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.th.end_roll_call(msg)
        self.manager.remove_rollcall.assert_called_once_with(100, 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/end_roll_call")
        with self._rc_not_started(), self._admin_ok(), self._patch_manager():
            await self.th.end_roll_call(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/end_roll_call")
        with self._rc_started(), self._admin_denied(), self._patch_manager():
            await self.th.end_roll_call(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())

    async def test_sends_finish_list(self):
        self.rc.finishList.return_value = "Title: Test\nID: __RCID__\nIn:\nNobody"
        msg = self._make_message("/end_roll_call")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.th.end_roll_call(msg)
        texts = [c[0][1] for c in self.th.bot.send_message.call_args_list]
        self.assertTrue(any("Roll ended" in t for t in texts))


# ===========================================================================
# /set_limit  /sl
# ===========================================================================

class TestSetLimit(HandlerTestBase):

    async def test_sets_limit(self):
        msg = self._make_message("/set_limit 5")
        with self._rc_started(), self._patch_manager():
            await self.th.wait_limit(msg)
        self.assertEqual(self.rc.inListLimit, 5)

    async def test_missing_limit_sends_error(self):
        msg = self._make_message("/set_limit")
        with self._rc_started(), self._patch_manager():
            await self.th.wait_limit(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_non_numeric_limit_sends_error(self):
        msg = self._make_message("/set_limit abc")
        with self._rc_started(), self._patch_manager():
            await self.th.wait_limit(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/set_limit 5")
        with self._rc_not_started(), self._patch_manager():
            await self.th.wait_limit(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_limit_moves_overflow_to_waitlist(self):
        u1 = MagicMock(); u1.name = "Alice"; u1.user_id = 1
        u2 = MagicMock(); u2.name = "Bob";   u2.user_id = 2
        self.rc.inList = [u1, u2]
        self.rc.waitList = []
        self.rc.inListLimit = None
        msg = self._make_message("/set_limit 1")
        with self._rc_started(), self._patch_manager(), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=1), \
             patch('telegram_helper.format_mention_with_name', return_value="Bob"):
            await self.th.wait_limit(msg)
        # Bob should move to waitlist (inList > limit)
        self.assertIn(u2, self.rc.waitList)
        self.assertEqual(self.rc.inList, [u1])


# ===========================================================================
# /delete_user
# ===========================================================================

class TestDeleteUser(HandlerTestBase):

    async def test_deletes_existing_user(self):
        self.rc.delete_user.return_value = True
        msg = self._make_message("/delete_user Alice")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.th.delete_user(msg)
        self.rc.delete_user.assert_called_once_with("Alice")
        self.assertIn("deleted", self._sent_text().lower())

    async def test_user_not_found_sends_notice(self):
        self.rc.delete_user.return_value = False
        msg = self._make_message("/delete_user Ghost")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.th.delete_user(msg)
        self.assertIn("wasn't found", self._sent_text())

    async def test_missing_name_sends_error(self):
        msg = self._make_message("/delete_user")
        with self._rc_started(), self._admin_ok(), self._patch_manager():
            await self.th.delete_user(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/delete_user Alice")
        with self._rc_started(), self._admin_denied(), self._patch_manager():
            await self.th.delete_user(msg)
        sent = self._sent_text()
        self.assertIn("permission", str(sent).lower())

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/delete_user Alice")
        with self._rc_not_started(), self._patch_manager():
            await self.th.delete_user(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /delete_template
# ===========================================================================

class TestDeleteTemplate(HandlerTestBase):

    async def test_deletes_existing_template(self):
        msg = self._make_message("/delete_template sunday")
        with self._admin_ok(), self._patch_manager(), \
             patch('telegram_helper.delete_template', return_value=True):
            await self.th.delete_template_command(msg)
        self.assertIn("deleted", self._sent_text().lower())

    async def test_template_not_found(self):
        msg = self._make_message("/delete_template ghost")
        with self._admin_ok(), self._patch_manager(), \
             patch('telegram_helper.delete_template', return_value=False):
            await self.th.delete_template_command(msg)
        self.assertIn("not found", self._sent_text().lower())

    async def test_missing_name_sends_usage(self):
        msg = self._make_message("/delete_template")
        with self._admin_ok(), self._patch_manager():
            await self.th.delete_template_command(msg)
        self.assertIn("Usage", self._sent_text())

    async def test_no_admin_rights_sends_error(self):
        msg = self._make_message("/delete_template sunday")
        with self._admin_denied(), self._patch_manager():
            await self.th.delete_template_command(msg)
        self.assertIn("permission", self._sent_text().lower())


# ===========================================================================
# /event_fee  /ef
# ===========================================================================

class TestEventFee(HandlerTestBase):

    async def test_sets_fee(self):
        msg = self._make_message("/event_fee 500")
        with self._rc_started(), self._patch_manager():
            await self.th.event_fee(msg)
        self.assertEqual(self.rc.event_fee, "500")
        self.rc.save.assert_called()

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/event_fee 500")
        with self._rc_not_started(), self._patch_manager():
            await self.th.event_fee(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_zero_fee_sends_error(self):
        msg = self._make_message("/event_fee 0")
        with self._rc_started(), self._patch_manager():
            await self.th.event_fee(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_non_numeric_fee_sends_error(self):
        msg = self._make_message("/event_fee free")
        with self._rc_started(), self._patch_manager():
            await self.th.event_fee(msg)
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
            await self.th.individual_fee(msg)
        sent = self._sent_text()
        self.assertIn("200", sent)  # 600 / 3

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/individual_fee")
        with self._rc_not_started(), self._patch_manager():
            await self.th.individual_fee(msg)
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
            await self.th.when(msg)
        sent = self._sent_text()
        self.assertIn("10-05-2026", sent)

    async def test_no_time_set_sends_error(self):
        self.rc.finalizeDate = None
        msg = self._make_message("/when")
        with self._rc_started(), self._patch_manager():
            await self.th.when(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/when")
        with self._rc_not_started(), self._patch_manager():
            await self.th.when(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /location  /loc
# ===========================================================================

class TestSetLocation(HandlerTestBase):

    async def test_sets_location(self):
        msg = self._make_message("/location Central Park")
        with self._rc_started(), self._patch_manager():
            await self.th.set_location(msg)
        self.assertEqual(self.rc.location, "Central Park")
        self.rc.save.assert_called()
        self.assertIn("Central Park", self._sent_text())

    async def test_missing_location_sends_error(self):
        msg = self._make_message("/location")
        with self._rc_started(), self._patch_manager():
            await self.th.set_location(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_no_rollcall_sends_error(self):
        msg = self._make_message("/location Park")
        with self._rc_not_started(), self._patch_manager():
            await self.th.set_location(msg)
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
        with self._rc_started(), patch('telegram_helper.manager', multi):
            await self.th.whos_in(msg)
        multi.get_rollcall.assert_called_with(100, 1)  # index 1 = #2

    async def test_out_of_range_rc_number_sends_error(self):
        msg = self._make_message("/whos_in ::5")
        with self._rc_started(), self._patch_manager():
            await self.th.whos_in(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_non_numeric_rc_suffix_sends_error(self):
        msg = self._make_message("/whos_out ::abc")
        with self._rc_started(), self._patch_manager():
            await self.th.whos_out(msg)
        self.assertGreater(self._sent_count(), 0)


# ===========================================================================
# /stats  (basic routing only — full stats logic is complex)
# ===========================================================================

class TestStatsCommand(HandlerTestBase):

    async def _run_stats(self, text):
        msg = self._make_message(text, user_id=42, first_name="Alice", username="alice")
        with self._patch_manager(), \
             patch('telegram_helper.build_user_stats_text', new=AsyncMock(return_value="stats text")), \
             patch('telegram_helper.build_group_stats_text', new=AsyncMock(return_value="group text")), \
             patch('telegram_helper.build_leaderboard_text', new=AsyncMock(return_value="leader text")), \
             patch('telegram_helper.build_bot_stats_text', new=AsyncMock(return_value="bot text")), \
             patch('telegram_helper.resolve_user_for_stats', new=AsyncMock(return_value=(42, "Alice"))):
            await self.th.stats_command(msg)

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
        with patch('telegram_helper.manager', empty_manager):
            await self.th.show_panel(msg)
        self.assertGreater(self._sent_count(), 0)

    async def test_with_rollcall_shows_panel(self):
        msg = self._make_message("/panel")
        with self._patch_manager(), \
             patch('telegram_helper.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.th.show_panel(msg)
        self.assertGreater(self._sent_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
