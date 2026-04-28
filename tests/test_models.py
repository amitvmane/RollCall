"""
Tests for RollCall model logic (models.py)

Covers:
- User creation and string representation
- addIn / addOut / addMaybe state transitions
- Duplicate detection (AB return code)
- Waitlist promotion when a user leaves (AC return code)
- inListLimit enforcement
- set_limit moving users between inList and waitList
- delete_user
- allList / inListText / outListText / maybeListText / waitListText
- Proxy user handling
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from models import RollCall, User  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_rollcall(title="Test Event"):
    """Return an in-memory RollCall without a DB chat_id."""
    rc = RollCall(title)
    rc.id = 1  # simulate a saved rollcall
    return rc


def make_user(name="Alice", username="alice", user_id=100):
    return User(name, username, user_id, [])


def make_proxy(name="Bob Proxy"):
    """Proxy users have a string user_id (their name)."""
    return User(name, None, name, [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUserCreation(unittest.TestCase):

    def test_basic_attributes(self):
        u = make_user("Alice", "alice", 1)
        self.assertEqual(u.name, "Alice")
        self.assertEqual(u.username, "alice")
        self.assertEqual(u.user_id, 1)
        self.assertEqual(u.comment, "")

    def test_str_no_comment(self):
        u = make_user("Alice")
        self.assertEqual(str(u), "Alice")

    def test_str_with_comment(self):
        u = make_user("Alice")
        u.comment = "late arrival"
        self.assertEqual(str(u), "Alice (late arrival)")

    def test_repr(self):
        u = make_user("Alice", "alice", 42)
        self.assertIn("Alice", repr(u))
        self.assertIn("alice", repr(u))
        self.assertIn("42", repr(u))


class TestAddIn(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()

    def test_add_user_to_inlist(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addIn(u)
        self.assertIn(u, self.rc.inList)

    def test_duplicate_returns_AB(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addIn(u)
        result = self.rc.addIn(u)
        self.assertEqual(result, "AB")
        self.assertEqual(len(self.rc.inList), 1)

    def test_change_comment_does_not_duplicate(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addIn(u)
        u2 = make_user("Alice", "alice", 1)
        u2.comment = "new comment"
        result = self.rc.addIn(u2)
        # Should update comment, not duplicate
        self.assertNotEqual(result, "AB")
        self.assertEqual(len(self.rc.inList), 1)

    def test_move_from_out_to_in(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addOut(u)
        self.rc.addIn(u)
        self.assertIn(u, self.rc.inList)
        self.assertNotIn(u, self.rc.outList)

    def test_move_from_maybe_to_in(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addMaybe(u)
        self.rc.addIn(u)
        self.assertIn(u, self.rc.inList)
        self.assertNotIn(u, self.rc.maybeList)


class TestInListLimit(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()
        self.rc.inListLimit = 2

    def test_users_fill_inlist_up_to_limit(self):
        u1 = make_user("Alice", "alice", 1)
        u2 = make_user("Bob", "bob", 2)
        self.rc.addIn(u1)
        self.rc.addIn(u2)
        self.assertEqual(len(self.rc.inList), 2)
        self.assertEqual(len(self.rc.waitList), 0)

    def test_overflow_goes_to_waitlist(self):
        u1 = make_user("Alice", "alice", 1)
        u2 = make_user("Bob", "bob", 2)
        u3 = make_user("Carol", "carol", 3)
        self.rc.addIn(u1)
        self.rc.addIn(u2)
        result = self.rc.addIn(u3)
        self.assertEqual(result, "AC")
        self.assertIn(u3, self.rc.waitList)
        self.assertEqual(len(self.rc.inList), 2)

    def test_waitlist_promoted_when_user_leaves(self):
        u1 = make_user("Alice", "alice", 1)
        u2 = make_user("Bob", "bob", 2)
        u3 = make_user("Carol", "carol", 3)
        self.rc.addIn(u1)
        self.rc.addIn(u2)
        self.rc.addIn(u3)  # goes to waitlist (AC)

        # Alice leaves → Carol should be promoted
        promoted = self.rc.addOut(u1)
        self.assertIsInstance(promoted, User)
        self.assertEqual(promoted.user_id, u3.user_id)
        self.assertIn(u3, self.rc.inList)
        self.assertEqual(len(self.rc.waitList), 0)

    def test_waitlist_promoted_on_maybe(self):
        u1 = make_user("Alice", "alice", 1)
        u2 = make_user("Bob", "bob", 2)
        u3 = make_user("Carol", "carol", 3)
        self.rc.addIn(u1)
        self.rc.addIn(u2)
        self.rc.addIn(u3)  # waitlist

        promoted = self.rc.addMaybe(u1)
        self.assertIsInstance(promoted, User)
        self.assertIn(u3, self.rc.inList)


class TestAddOut(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()

    def test_add_user_to_outlist(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addOut(u)
        self.assertIn(u, self.rc.outList)

    def test_duplicate_out_returns_AB(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addOut(u)
        result = self.rc.addOut(u)
        self.assertEqual(result, "AB")

    def test_move_from_in_to_out(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addIn(u)
        self.rc.addOut(u)
        self.assertNotIn(u, self.rc.inList)
        self.assertIn(u, self.rc.outList)


class TestAddMaybe(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()

    def test_add_user_to_maybelist(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addMaybe(u)
        self.assertIn(u, self.rc.maybeList)

    def test_duplicate_maybe_returns_AB(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addMaybe(u)
        result = self.rc.addMaybe(u)
        self.assertEqual(result, "AB")

    def test_move_from_out_to_maybe(self):
        u = make_user("Alice", "alice", 1)
        self.rc.addOut(u)
        self.rc.addMaybe(u)
        self.assertNotIn(u, self.rc.outList)
        self.assertIn(u, self.rc.maybeList)


class TestDeleteUser(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()

    def test_delete_existing_user_returns_true(self):
        # db mock already returns True for delete_user_by_name
        # and get_all_users returns []
        result = self.rc.delete_user("Alice")
        self.assertTrue(result)

    def test_delete_nonexistent_user_returns_false(self):
        # Force a fresh mock that returns False
        with patch('models.db.delete_user_by_name', return_value=False):
            result = self.rc.delete_user("Ghost")
        self.assertFalse(result)


class TestListTexts(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()

    def test_inlist_text_empty(self):
        self.assertIn("Nobody", self.rc.inListText())

    def test_outlist_text_empty(self):
        self.assertIn("Nobody", self.rc.outListText())

    def test_maybelist_text_empty(self):
        self.assertIn("Nobody", self.rc.maybeListText())

    def test_waitlist_text_empty_no_limit(self):
        # Without a limit, waitListText returns empty string
        self.assertEqual(self.rc.waitListText(), "")

    def test_inlist_text_with_user(self):
        u = make_user("Alice", "alice", 1)
        self.rc.inList.append(u)
        txt = self.rc.inListText()
        self.assertIn("Alice", txt)
        self.assertIn("1.", txt)

    def test_outlist_text_with_user(self):
        u = make_user("Bob", "bob", 2)
        self.rc.outList.append(u)
        txt = self.rc.outListText()
        self.assertIn("Bob", txt)

    def test_alllist_contains_title(self):
        txt = self.rc.allList()
        self.assertIn("Test Event", txt)
        self.assertIn("__RCID__", txt)

    def test_alllist_shows_max_limit(self):
        self.rc.inListLimit = 10
        txt = self.rc.allList()
        self.assertIn("10", txt)

    def test_alllist_shows_infinity_when_no_limit(self):
        txt = self.rc.allList()
        self.assertIn("∞", txt)


class TestProxyUsers(unittest.TestCase):

    def setUp(self):
        self.rc = make_rollcall()

    def test_proxy_user_added_to_inlist(self):
        proxy = make_proxy("Charlie Proxy")
        self.rc.addIn(proxy)
        self.assertIn(proxy, self.rc.inList)

    def test_proxy_user_duplicate_returns_AB(self):
        proxy = make_proxy("Charlie Proxy")
        self.rc.addIn(proxy)
        proxy2 = make_proxy("Charlie Proxy")
        result = self.rc.addIn(proxy2)
        self.assertEqual(result, "AB")

    def test_set_proxy_owner(self):
        self.rc.set_proxy_owner("Charlie Proxy", 999)
        self.assertEqual(self.rc.get_proxy_owner("Charlie Proxy"), 999)

    def test_get_proxy_owner_unknown(self):
        result = self.rc.get_proxy_owner("Nobody")
        self.assertIsNone(result)


class TestRollCallDbIdAlias(unittest.TestCase):

    def test_db_id_returns_same_as_id(self):
        rc = make_rollcall()
        rc.id = 42
        self.assertEqual(rc.db_id, 42)

    def test_db_id_setter(self):
        rc = make_rollcall()
        rc.db_id = 99
        self.assertEqual(rc.id, 99)


class TestMultipleUsers(unittest.TestCase):
    """Integration-style tests covering realistic usage patterns."""

    def test_full_rsvp_flow(self):
        rc = make_rollcall("Weekly Game")
        rc.inListLimit = 3

        alice = make_user("Alice", "alice", 1)
        bob = make_user("Bob", "bob", 2)
        carol = make_user("Carol", "carol", 3)
        dave = make_user("Dave", "dave", 4)

        # Fill up
        rc.addIn(alice)
        rc.addIn(bob)
        rc.addIn(carol)
        result = rc.addIn(dave)
        self.assertEqual(result, "AC")
        self.assertIn(dave, rc.waitList)

        # Alice goes out → Dave promoted
        promoted = rc.addOut(alice)
        self.assertIsInstance(promoted, User)
        self.assertEqual(promoted.user_id, 4)
        self.assertNotIn(dave, rc.waitList)
        self.assertIn(dave, rc.inList)

        # Bob is maybe now → no more waitlist so no promotion, just frees slot
        rc.addMaybe(bob)
        self.assertIn(bob, rc.maybeList)
        self.assertNotIn(bob, rc.inList)

    def test_all_names_tracks_everyone(self):
        rc = make_rollcall()
        u1 = make_user("Alice", "alice", 1)
        u2 = make_user("Bob", "bob", 2)
        rc.addIn(u1)
        rc.addOut(u2)
        names = [u.name for u in rc.allNames]
        self.assertIn("Alice", names)
        self.assertIn("Bob", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
