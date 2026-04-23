"""
Tests for RollCallManager (rollcall_manager.py)

Covers:
- get_chat initialises and caches correctly
- add_rollcall / remove_rollcall
- get_rollcall by index
- shh mode get/set
- admin rights get/set
- set_timezone propagates to rollcalls
- reload_chat clears and reloads cache
- clear_cache
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from rollcall_manager import RollCallManager  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRollCallManagerBasics(unittest.TestCase):

    def setUp(self):
        self.mgr = RollCallManager()
        sys.modules['db'].get_or_create_chat.return_value = {
            "shh_mode": False,
            "admin_rights": False,
            "timezone": "Asia/Calcutta",
        }
        sys.modules['db'].get_active_rollcalls.return_value = []

    def test_get_chat_creates_entry(self):
        chat = self.mgr.get_chat(1001)
        self.assertIn("rollCalls", chat)
        self.assertIn("shh", chat)
        self.assertIn("adminRights", chat)

    def test_get_chat_caches(self):
        sys.modules['db'].get_or_create_chat.reset_mock()
        self.mgr.get_chat(1001)
        self.mgr.get_chat(1001)
        # Should only call DB once (second call hits cache)
        call_count = sys.modules['db'].get_or_create_chat.call_count
        self.assertEqual(call_count, 1)

    def test_get_rollcalls_empty(self):
        self.assertEqual(self.mgr.get_rollcalls(1002), [])

    def test_add_rollcall(self):
        rc = self.mgr.add_rollcall(1003, "Event")
        rollcalls = self.mgr.get_rollcalls(1003)
        self.assertEqual(len(rollcalls), 1)
        self.assertEqual(rc.title, "Event")

    def test_get_rollcall_by_index(self):
        rc = self.mgr.add_rollcall(1004, "Yoga Session")
        result = self.mgr.get_rollcall(1004, 0)
        self.assertEqual(result.title, "Yoga Session")

    def test_get_rollcall_out_of_range_returns_none(self):
        self.mgr.add_rollcall(1005, "Test")
        result = self.mgr.get_rollcall(1005, 99)
        self.assertIsNone(result)

    def test_remove_rollcall(self):
        self.mgr.add_rollcall(1006, "Badminton")
        self.mgr.remove_rollcall(1006, 0)
        self.assertEqual(len(self.mgr.get_rollcalls(1006)), 0)

    def test_remove_rollcall_calls_db_end(self):
        sys.modules['db'].end_rollcall.reset_mock()
        self.mgr.add_rollcall(1007, "Cricket")
        self.mgr.remove_rollcall(1007, 0)
        sys.modules['db'].end_rollcall.assert_called_once()

    def test_remove_rollcall_out_of_range_raises(self):
        with self.assertRaises(IndexError):
            self.mgr.remove_rollcall(1008, 5)


class TestShhMode(unittest.TestCase):

    def setUp(self):
        self.mgr = RollCallManager()
        sys.modules['db'].get_or_create_chat.return_value = {
            "shh_mode": False,
            "admin_rights": False,
            "timezone": "Asia/Calcutta",
        }
        sys.modules['db'].get_active_rollcalls.return_value = []

    def test_shh_default_false(self):
        self.assertFalse(self.mgr.get_shh_mode(2001))

    def test_set_shh_true(self):
        self.mgr.set_shh_mode(2001, True)
        self.assertTrue(self.mgr.get_shh_mode(2001))

    def test_set_shh_false(self):
        self.mgr.set_shh_mode(2001, True)
        self.mgr.set_shh_mode(2001, False)
        self.assertFalse(self.mgr.get_shh_mode(2001))

    def test_shh_persists_to_db(self):
        sys.modules['db'].update_chat_settings.reset_mock()
        self.mgr.set_shh_mode(2002, True)
        sys.modules['db'].update_chat_settings.assert_called_with(2002, shh_mode=True)


class TestAdminRights(unittest.TestCase):

    def setUp(self):
        self.mgr = RollCallManager()
        sys.modules['db'].get_or_create_chat.return_value = {
            "shh_mode": False,
            "admin_rights": False,
            "timezone": "Asia/Calcutta",
        }
        sys.modules['db'].get_active_rollcalls.return_value = []

    def test_admin_rights_default_false(self):
        self.assertFalse(self.mgr.get_admin_rights(3001))

    def test_set_admin_rights_true(self):
        self.mgr.set_admin_rights(3001, True)
        self.assertTrue(self.mgr.get_admin_rights(3001))

    def test_admin_rights_persists_to_db(self):
        sys.modules['db'].update_chat_settings.reset_mock()
        self.mgr.set_admin_rights(3002, True)
        sys.modules['db'].update_chat_settings.assert_called_with(3002, admin_rights=True)


class TestTimezone(unittest.TestCase):

    def setUp(self):
        self.mgr = RollCallManager()
        sys.modules['db'].get_or_create_chat.return_value = {
            "shh_mode": False,
            "admin_rights": False,
            "timezone": "Asia/Calcutta",
        }
        sys.modules['db'].get_active_rollcalls.return_value = []

    def test_set_timezone_updates_chat_cache(self):
        self.mgr.set_timezone(4001, "Europe/London")
        chat = self.mgr.get_chat(4001)
        self.assertEqual(chat["timezone"], "Europe/London")

    def test_set_timezone_persists_to_db(self):
        sys.modules['db'].update_chat_settings.reset_mock()
        self.mgr.set_timezone(4002, "America/New_York")
        sys.modules['db'].update_chat_settings.assert_called_with(4002, timezone="America/New_York")

    def test_set_timezone_propagates_to_rollcalls(self):
        rc = self.mgr.add_rollcall(4003, "Test Event")
        self.mgr.set_timezone(4003, "America/Chicago")
        self.assertEqual(rc.timezone, "America/Chicago")


class TestCacheManagement(unittest.TestCase):

    def setUp(self):
        self.mgr = RollCallManager()
        sys.modules['db'].get_or_create_chat.return_value = {
            "shh_mode": False,
            "admin_rights": False,
            "timezone": "Asia/Calcutta",
        }
        sys.modules['db'].get_active_rollcalls.return_value = []

    def test_clear_cache(self):
        self.mgr.get_chat(5001)
        self.mgr.clear_cache()
        self.assertEqual(len(self.mgr._cache), 0)

    def test_reload_chat_refreshes_data(self):
        self.mgr.get_chat(5002)
        sys.modules['db'].get_or_create_chat.reset_mock()
        self.mgr.reload_chat(5002)
        sys.modules['db'].get_or_create_chat.assert_called_once_with(5002)


if __name__ == "__main__":
    unittest.main(verbosity=2)
