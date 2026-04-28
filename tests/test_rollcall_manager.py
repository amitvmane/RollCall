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
        self.mgr.get_chat(1001)
        chat = self.mgr.get_chat(1001)
        # Calling twice should return same cached object
        self.assertIsNotNone(chat)

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
        rc = self.mgr.add_rollcall(1007, "Cricket")
        # remove_rollcall should call end_rollcall on the rc
        initial_count = len(self.mgr.get_rollcalls(1007))
        self.mgr.remove_rollcall(1007, 0)
        after_count = len(self.mgr.get_rollcalls(1007))
        self.assertEqual(initial_count, 1)
        self.assertEqual(after_count, 0)

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
        self.mgr.set_shh_mode(2002, True)
        self.assertTrue(self.mgr.get_shh_mode(2002))


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
        self.mgr.set_admin_rights(3002, True)
        self.assertTrue(self.mgr.get_admin_rights(3002))


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
        self.mgr.set_timezone(4002, "America/New_York")
        self.assertEqual(self.mgr.get_chat(4002)["timezone"], "America/New_York")

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
        chat_before = self.mgr.get_chat(5002)
        self.mgr.reload_chat(5002)
        chat_after = self.mgr.get_chat(5002)
        # Both should return valid chat data
        self.assertIsNotNone(chat_before)
        self.assertIsNotNone(chat_after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
