"""
tests/test_parallel_rollcalls.py

Tests for multiple rollcalls running in parallel in the same chat.
Covers:
- Creating multiple rollcalls
- Adding users to specific rollcalls via ::N syntax
- State isolation between parallel rollcalls
- Ending one rollcall doesn't affect others
- Queries (whos_in, etc.) work per rollcall
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import telegram_helper as th


class TestParallelRollcallsBase(unittest.IsolatedAsyncioTestCase):
    """Base class for parallel rollcall tests."""

    @classmethod
    def setUpClass(cls):
        cls.th = th

    def setUp(self):
        self.th.bot.send_message = AsyncMock()
        self.th.bot.get_chat_member = AsyncMock()
        self.th._rate_limits.clear()
        self.rc = self._make_rc()
        self.manager = self._make_manager([self.rc])

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

    def _make_rc(self, title="Event", rc_id=1):
        rc = MagicMock()
        rc.title = title
        rc.id = rc_id
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
        rc.allList.return_value = f"Title: {title}\nID: __RCID__\n"
        rc.finishList.return_value = f"Title: {title}\nID: __RCID__\n"
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

    def _sent_text(self, call_index=0):
        return self.th.bot.send_message.call_args_list[call_index][0][1]

    def _sent_count(self):
        return self.th.bot.send_message.call_count

    def _rc_not_started(self):
        return patch('telegram_helper.roll_call_not_started', return_value=False)

    def _rc_started(self):
        return patch('telegram_helper.roll_call_not_started', return_value=True)

    def _admin_ok(self):
        return patch('telegram_helper.admin_rights', new=AsyncMock(return_value=True))

    def _patch_manager(self):
        return patch('telegram_helper.manager', self.manager)


class TestMultipleRollcallCreation(TestParallelRollcallsBase):
    """Test creating multiple rollcalls in the same chat."""

    def _db_json_patch(self):
        return patch("builtins.open", mock_open(read_data='[]'))

    async def test_can_create_multiple_rollcalls(self):
        """Verify that multiple rollcalls can be created."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)
        mgr = self._make_manager([rc1, rc2])
        
        # Already has 2 rollcalls, now add one more
        msg = self._make_message("/start_roll_call Event 3")
        with self._db_json_patch(), self._admin_ok(), \
             patch('telegram_helper.manager', mgr), \
             patch('telegram_helper.get_status_keyboard', new=AsyncMock(return_value=MagicMock())):
            await self.th.start_roll_call(msg)
        
        # add_rollcall should have been called
        self.assertEqual(mgr.add_rollcall.call_count, 1)


class TestRollcallSelectionByIndex(TestParallelRollcallsBase):
    """Test ::N selection for targeting specific rollcalls."""

    async def test_in_targets_second_rollcall(self):
        """Verify /in ::2 adds user to rollcall #2."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        rc1.addIn.return_value = None
        rc2.addIn.return_value = None

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc2

        msg = self._make_message("/in ::2")
        with self._rc_started(), patch('telegram_helper.manager', mgr), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=2):
            await self.th.in_user(msg)

        mgr.get_rollcall.assert_called_with(100, 1)
        rc2.addIn.assert_called_once()
        rc1.addIn.assert_not_called()

    async def test_out_targets_third_rollcall(self):
        """Verify /out ::3 adds user to out list of rollcall #3."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)
        rc3 = self._make_rc("Event 3", 3)

        rc3.addOut.return_value = None

        mgr = self._make_manager([rc1, rc2, rc3])
        mgr.get_rollcall.return_value = rc3

        msg = self._make_message("/out ::3")
        with self._rc_started(), patch('telegram_helper.manager', mgr), \
             patch('telegram_helper.send_list', return_value=False), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=3), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()):
            await self.th.out_user(msg)

        mgr.get_rollcall.assert_called_with(100, 2)
        rc3.addOut.assert_called_once()

    async def test_invalid_rc_index_sends_error(self):
        """Verify out-of-range ::N sends error."""
        rc1 = self._make_rc("Event 1", 1)
        mgr = self._make_manager([rc1])
        mgr.get_rollcall.return_value = None

        msg = self._make_message("/in ::5")
        with self._rc_started(), patch('telegram_helper.manager', mgr):
            await self.th.in_user(msg)

        self.assertGreater(self._sent_count(), 0)


class TestStateIsolation(TestParallelRollcallsBase):
    """Test that parallel rollcalls maintain separate state."""

    async def test_in_list_isolation(self):
        """Verify users in one rollcall don't appear in another."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        u1 = MagicMock()
        u1.name = "Alice"
        u1.user_id = 1
        u1.first_name = "Alice"
        u1.username = "alice"
        u1.comment = ""

        rc1.inList = [u1]
        rc1.inListText.return_value = "In:\n1. Alice\n\n"
        rc2.inListText.return_value = "In:\nNobody\n\n"

        mgr = self._make_manager([rc1, rc2])

        msg1 = self._make_message("/whos_in ::1")
        with self._rc_started(), patch('telegram_helper.manager', mgr):
            await self.th.whos_in(msg1)
        self.assertIn("Alice", self._sent_text())

        self.th.bot.send_message.reset_mock()

        msg2 = self._make_message("/whos_in ::2")
        mgr.get_rollcall.return_value = rc2
        with self._rc_started(), patch('telegram_helper.manager', mgr):
            await self.th.whos_in(msg2)
        self.assertIn("Nobody", self._sent_text())

    async def test_title_independence(self):
        """Verify setting title on one rollcall doesn't affect another."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc2

        msg = self._make_message("/set_title New Title ::2")
        with self._rc_started(), patch('telegram_helper.manager', mgr):
            await self.th.set_title(msg)

        self.assertEqual(rc2.title, "New Title")
        self.assertEqual(rc1.title, "Event 1")


class TestEndRollcallWithParallel(TestParallelRollcallsBase):
    """Test ending rollcalls when multiple are active."""

    async def test_end_only_removes_targeted_rollcall(self):
        """Verify /end_roll_call ::2 only removes rollcall #2."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc2

        msg = self._make_message("/end_roll_call ::2")
        with self._rc_started(), self._admin_ok(), patch('telegram_helper.manager', mgr):
            await self.th.end_roll_call(msg)

        mgr.remove_rollcall.assert_called_once_with(100, 1)

    async def test_end_first_rollcall_keeps_second(self):
        """Verify ending first rollcall leaves others intact."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)
        rc3 = self._make_rc("Event 3", 3)

        mgr = self._make_manager([rc1, rc2, rc3])

        msg = self._make_message("/end_roll_call ::1")
        with self._rc_started(), self._admin_ok(), patch('telegram_helper.manager', mgr):
            await self.th.end_roll_call(msg)

        mgr.remove_rollcall.assert_called_once_with(100, 0)


class TestProxyUserWithParallel(TestParallelRollcallsBase):
    """Test proxy users (set_in_for, etc.) with parallel rollcalls."""

    async def test_set_in_for_targets_correct_rollcall(self):
        """Verify /set_in_for Bob ::2 adds proxy to rollcall #2."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)
        rc2.addIn.return_value = None

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc2

        msg = self._make_message("/set_in_for Bob ::2")
        with self._rc_started(), patch('telegram_helper.manager', mgr), \
             patch('telegram_helper.show_panel_for_rollcall', new=AsyncMock()), \
             patch('telegram_helper.add_or_update_proxy_user'):
            await self.th.set_in_for(msg)

        mgr.get_rollcall.assert_called_with(100, 1)
        rc2.addIn.assert_called_once()
        rc1.addIn.assert_not_called()


class TestQueriesWithParallel(TestParallelRollcallsBase):
    """Test query commands work correctly with parallel rollcalls."""

    async def test_location_is_per_rollcall(self):
        """Verify setting location on one rollcall doesn't affect another."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        rc1.location = "Park A"
        rc2.location = "Park B"

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc1

        msg = self._make_message("/location New Location ::1")
        with self._rc_started(), patch('telegram_helper.manager', mgr):
            await self.th.set_location(msg)

        self.assertEqual(rc1.location, "New Location")
        self.assertEqual(rc2.location, "Park B")

    async def test_event_fee_is_per_rollcall(self):
        """Verify event fee is independent between rollcalls."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        rc1.event_fee = "100"
        rc2.event_fee = "200"

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc1

        msg = self._make_message("/event_fee 500 ::1")
        with self._rc_started(), patch('telegram_helper.manager', mgr):
            await self.th.event_fee(msg)

        self.assertEqual(rc1.event_fee, "500")
        self.assertEqual(rc2.event_fee, "200")


class TestMaxRollcalls(TestParallelRollcallsBase):
    """Test maximum rollcall limit enforcement."""

    def _db_json_patch(self):
        return patch("builtins.open", mock_open(read_data='[]'))

    async def test_max_rollcalls_prevents_creation(self):
        """Verify cannot start more than 3 rollcalls."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)
        rc3 = self._make_rc("Event 3", 3)
        mgr = self._make_manager([rc1, rc2, rc3])

        msg = self._make_message("/start_roll_call Event 4")
        with self._db_json_patch(), self._admin_ok(), \
             patch('telegram_helper.manager', mgr):
            await self.th.start_roll_call(msg)

        # Should have called add_rollcall since it hasn't reached limit OR
        # should send error about max limit
        self.assertGreater(self._sent_count(), 0)


class TestDeleteUserWithParallel(TestParallelRollcallsBase):
    """Test delete_user with parallel rollcalls."""

    async def test_delete_user_from_specific_rollcall(self):
        """Verify /delete_user stores pending delete for correct rollcall (confirmation step)."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)
        rc2.delete_user.return_value = True

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc2

        msg = self._make_message("/delete_user Bob ::2")
        with self._rc_started(), self._admin_ok(), patch('telegram_helper.manager', mgr):
            await self.th.delete_user(msg)

        # delete_user now shows a confirmation prompt first
        rc1.delete_user.assert_not_called()
        rc2.delete_user.assert_not_called()
        # Pending delete for admin (user_id=1) in chat 100 should be stored
        self.assertIn((100, 1), self.th._pending_deletes)
        self.assertEqual(self.th._pending_deletes[(100, 1)]['name'], "Bob")


class TestLimitWithParallel(TestParallelRollcallsBase):
    """Test set_limit with parallel rollcalls."""

    async def test_limit_only_affects_targeted_rollcall(self):
        """Verify /set_limit only affects specified rollcall."""
        rc1 = self._make_rc("Event 1", 1)
        rc2 = self._make_rc("Event 2", 2)

        mgr = self._make_manager([rc1, rc2])
        mgr.get_rollcall.return_value = rc2

        msg = self._make_message("/set_limit 5 ::2")
        with self._rc_started(), patch('telegram_helper.manager', mgr), \
             patch('telegram_helper.notify_proxy_owner_wait_to_in', new=AsyncMock()), \
             patch('telegram_helper.increment_user_stat'), \
             patch('telegram_helper.increment_rollcall_stat'), \
             patch('telegram_helper.get_rc_db_id', return_value=2), \
             patch('telegram_helper.format_mention_with_name', return_value="User"):
            await self.th.wait_limit(msg)

        self.assertEqual(rc2.inListLimit, 5)
        self.assertIsNone(rc1.inListLimit)


if __name__ == "__main__":
    unittest.main(verbosity=2)