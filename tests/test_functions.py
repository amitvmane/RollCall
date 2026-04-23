"""
Tests for utility functions in functions.py

Covers:
- roll_call_not_started
- send_list
- auto_complete_timezone
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import functions  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRollCallNotStarted(unittest.TestCase):

    def _make_message(self, chat_id=123):
        msg = MagicMock()
        msg.chat.id = chat_id
        return msg

    def test_returns_false_when_no_rollcalls(self):
        manager = MagicMock()
        manager.get_rollcalls.return_value = []
        msg = self._make_message()
        self.assertFalse(functions.roll_call_not_started(msg, manager))

    def test_returns_true_when_rollcall_exists(self):
        manager = MagicMock()
        manager.get_rollcalls.return_value = [MagicMock()]
        msg = self._make_message()
        self.assertTrue(functions.roll_call_not_started(msg, manager))

    def test_returns_false_on_exception(self):
        manager = MagicMock()
        manager.get_rollcalls.side_effect = Exception("DB error")
        msg = self._make_message()
        self.assertFalse(functions.roll_call_not_started(msg, manager))


class TestSendList(unittest.TestCase):

    def _make_message(self, chat_id=456):
        msg = MagicMock()
        msg.chat.id = chat_id
        return msg

    def test_returns_true_when_not_shh(self):
        manager = MagicMock()
        manager.get_shh_mode.return_value = False
        msg = self._make_message()
        self.assertTrue(functions.send_list(msg, manager))

    def test_returns_false_when_shh(self):
        manager = MagicMock()
        manager.get_shh_mode.return_value = True
        msg = self._make_message()
        self.assertFalse(functions.send_list(msg, manager))


class TestAutoCompleteTimezone(unittest.TestCase):

    def test_exact_match(self):
        result = functions.auto_complete_timezone("Asia/Kolkata")
        self.assertIsNotNone(result)
        self.assertIn("Asia", result)

    def test_india_alias(self):
        result = functions.auto_complete_timezone("Asia/India")
        self.assertIsNotNone(result)
        # Should resolve to Asia/Calcutta or similar
        self.assertIn("Asia", result)

    def test_invalid_timezone_returns_none(self):
        result = functions.auto_complete_timezone("Nowhere/Invalid")
        self.assertIsNone(result)

    def test_missing_slash_returns_none(self):
        result = functions.auto_complete_timezone("NoSlashHere")
        self.assertIsNone(result)

    def test_fuzzy_match_london(self):
        result = functions.auto_complete_timezone("Europe/Lundon")  # typo
        # Should fuzzy match to Europe/London
        self.assertIsNotNone(result)
        self.assertIn("Europe", result)

    def test_america_new_york(self):
        result = functions.auto_complete_timezone("America/New_York")
        self.assertIsNotNone(result)
        self.assertIn("America", result)

    def test_case_insensitive(self):
        result = functions.auto_complete_timezone("asia/kolkata")
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
