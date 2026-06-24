"""
Comprehensive unit tests for the entire service layer.

Each service module gets its own TestCase class. Tests use lightweight
MagicMock stand-ins for the manager and db layer so they run offline
without any database or Telegram connection.

Patching strategy:
  - `services.X.manager`         → patches the module-level manager reference
  - `rollcall_manager.manager`   → patches the local import inside
                                     resolve_rollcall_or_raise (services/common.py)
  Both are required for functions that call resolve_rollcall_or_raise.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

# ---------------------------------------------------------------------------
# Helpers shared across all test cases
# ---------------------------------------------------------------------------

def _make_user(name="Alice", username="alice", user_id=1, comment=""):
    from models import User
    u = User.__new__(User)
    u.name = name
    u.username = username
    u.user_id = user_id
    u.comment = comment
    return u


def _make_proxy_user(name="Bob Proxy"):
    return _make_user(name=name, username=None, user_id=name)


def _make_rc(title="Weekly", in_list=None, out_list=None,
             maybe_list=None, wait_list=None, limit=None, rc_id=42):
    rc = MagicMock()
    rc.title = title
    rc.id = rc_id
    rc.inList = in_list or []
    rc.outList = out_list or []
    rc.maybeList = maybe_list or []
    rc.waitList = wait_list or []
    rc.allNames = []
    rc.inListLimit = limit
    rc.finalizeDate = None
    rc.timezone = "Asia/Kolkata"
    rc.location = None
    rc.event_fee = None
    rc.individual_fee = None
    rc.reminder = None
    rc.absent_marked = False
    rc.save.return_value = None
    rc.delete_user.return_value = True
    rc._save_user_to_db.return_value = None
    rc._load_users_from_db.return_value = None
    rc.addIn.return_value = None     # "added"
    rc.addOut.return_value = None    # "added"
    rc.addMaybe.return_value = None  # "added"
    return rc


def _make_manager(rollcalls=None, ghost_on=True, absent_limit=1, shh=False):
    m = MagicMock()
    rcs = rollcalls if rollcalls is not None else []
    m.get_rollcalls.return_value = rcs
    m.get_rollcall.return_value = rcs[0] if rcs else None
    m.get_chat.return_value = {
        "timezone": "Asia/Kolkata",
        "shh_mode": shh,
        "admin_rights": False,
        "ghost_tracking_enabled": ghost_on,
        "absent_limit": absent_limit,
    }
    m.get_shh_mode.return_value = shh
    m.get_admin_rights.return_value = False
    m.get_ghost_tracking_enabled.return_value = ghost_on
    m.get_absent_limit.return_value = absent_limit
    # get_chat_write_lock returns an async-context-manager mock (no event loop needed)
    lock_ctx = MagicMock()
    lock_ctx.__aenter__ = AsyncMock(return_value=None)
    lock_ctx.__aexit__ = AsyncMock(return_value=False)
    m.get_chat_write_lock.return_value = lock_ctx
    return m


# ---------------------------------------------------------------------------
# services.common
# ---------------------------------------------------------------------------

class TestSerializeUser(unittest.TestCase):
    def _call(self, u):
        from services.common import serialize_user
        return serialize_user(u)

    def test_real_user(self):
        u = _make_user(name="Alice", username="alice", user_id=1, comment="hi")
        d = self._call(u)
        self.assertEqual(d["user_id"], 1)
        self.assertEqual(d["name"], "Alice")
        self.assertEqual(d["username"], "alice")
        self.assertEqual(d["comment"], "hi")
        self.assertFalse(d["is_proxy"])

    def test_proxy_user(self):
        u = _make_proxy_user("Charlie")
        d = self._call(u)
        self.assertEqual(d["user_id"], "Charlie")
        self.assertTrue(d["is_proxy"])

    def test_no_comment_defaults_to_empty(self):
        u = _make_user(comment=None)
        d = self._call(u)
        self.assertEqual(d["comment"], "")


class TestSerializeRollcall(unittest.TestCase):
    def _call(self, rc, idx=0):
        from services.common import serialize_rollcall
        return serialize_rollcall(rc, idx)

    def test_basic_fields(self):
        rc = _make_rc(title="Game Night", rc_id=7)
        d = self._call(rc, 0)
        self.assertEqual(d["title"], "Game Night")
        self.assertEqual(d["number"], 1)
        self.assertEqual(d["rc_index"], 0)
        self.assertEqual(d["id"], 7)
        self.assertIsNone(d["limit"])
        self.assertIsNone(d["finalize_date"])

    def test_with_limit_and_users(self):
        u = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[u], limit=5)
        d = self._call(rc, 2)
        self.assertEqual(d["in_count"], 1)
        self.assertEqual(d["limit"], 5)
        self.assertEqual(d["number"], 3)  # 0-based 2 → 1-based 3

    def test_finalize_date_isoformat(self):
        from datetime import datetime
        rc = _make_rc()
        rc.finalizeDate = datetime(2025, 12, 31, 18, 0)
        d = self._call(rc)
        self.assertIn("2025-12-31", d["finalize_date"])


class TestParseRcNumberSuffix(unittest.TestCase):
    def _call(self, text):
        from services.common import parse_rc_number_suffix
        return parse_rc_number_suffix(text)

    def test_no_suffix_returns_zero(self):
        idx, rest = self._call("/in Alice")
        self.assertEqual(idx, 0)
        self.assertEqual(rest, "/in Alice")

    def test_valid_suffix(self):
        idx, rest = self._call("/in Alice ::2")
        self.assertEqual(idx, 1)
        self.assertEqual(rest, "/in Alice")

    def test_suffix_only(self):
        idx, rest = self._call("::3")
        self.assertEqual(idx, 2)
        self.assertEqual(rest, "")

    def test_malformed_suffix_raises(self):
        with self.assertRaises(ValueError):
            self._call("::abc")

    def test_non_positive_suffix_raises(self):
        with self.assertRaises(ValueError):
            self._call("::0")

    def test_empty_text_returns_zero(self):
        idx, rest = self._call("")
        self.assertEqual(idx, 0)


class TestResolveRollcallOrRaise(unittest.TestCase):
    def _call(self, chat_id, rc_number, mgr):
        with patch("rollcall_manager.manager", mgr):
            from services.common import resolve_rollcall_or_raise
            return resolve_rollcall_or_raise(chat_id, rc_number)

    def test_no_rollcalls_raises(self):
        from exceptions import rollCallNotStarted
        mgr = _make_manager([])
        with self.assertRaises(rollCallNotStarted):
            self._call(1, 0, mgr)

    def test_out_of_range_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        mgr = _make_manager([rc])
        with self.assertRaises(incorrectParameter):
            self._call(1, 5, mgr)

    def test_returns_rollcall(self):
        rc = _make_rc()
        mgr = _make_manager([rc])
        result = self._call(1, 0, mgr)
        self.assertEqual(result, rc)


# ---------------------------------------------------------------------------
# services.rollcalls
# ---------------------------------------------------------------------------

class TestRollcallsService(unittest.IsolatedAsyncioTestCase):

    def _mgr(self, rcs=None):
        m = _make_manager(rcs or [])
        return m

    async def test_start_rollcall_happy_path(self):
        mgr = self._mgr()
        rc = _make_rc("Test")
        mgr.add_rollcall.return_value = rc

        with patch("services.rollcalls.manager", mgr), \
             patch("services.rollcalls.log_admin_action"), \
             patch("services.rollcalls.increment_user_stat"):
            from services.rollcalls import start_rollcall
            result = await start_rollcall(100, "Test", 1, "Admin")

        self.assertEqual(result["title"], "Test")
        self.assertEqual(result["number"], 1)
        mgr.add_rollcall.assert_called_once_with(100, "Test")

    async def test_start_rollcall_max_reached(self):
        from exceptions import amountOfRollCallsReached
        rcs = [_make_rc() for _ in range(3)]
        mgr = self._mgr(rcs)

        with patch("services.rollcalls.manager", mgr):
            from services.rollcalls import start_rollcall
            with self.assertRaises(amountOfRollCallsReached):
                await start_rollcall(100, "X", 1, "Admin")

    async def test_start_rollcall_empty_title_uses_empty(self):
        mgr = self._mgr()
        rc = _make_rc("<Empty>")
        mgr.add_rollcall.return_value = rc

        with patch("services.rollcalls.manager", mgr), \
             patch("services.rollcalls.log_admin_action"), \
             patch("services.rollcalls.increment_user_stat"):
            from services.rollcalls import start_rollcall
            result = await start_rollcall(100, "", 1, "Admin")

        mgr.add_rollcall.assert_called_once_with(100, "<Empty>")

    async def test_end_rollcall_happy_path(self):
        rc = _make_rc("Weekly")
        rc.id = 99
        rc.inList = [_make_user("Alice", user_id=1)]
        rc.outList = []
        rc.maybeList = []
        rc.waitList = []
        mgr = self._mgr([rc])
        mgr.get_rollcalls.side_effect = [[rc], []]  # before/after removal

        with patch("services.rollcalls.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.rollcalls.log_admin_action"), \
             patch("services.rollcalls.update_streak_on_checkin"), \
             patch("services.rollcalls.reset_user_streak"), \
             patch("services.rollcalls.increment_user_stat"), \
             patch("services.rollcalls.update_proxy_streak_on_checkin"), \
             patch("services.rollcalls.reset_proxy_streak"):
            from services.rollcalls import end_rollcall
            result = await end_rollcall(100, 0, 9, "Admin")

        mgr.remove_rollcall.assert_called_once_with(100, 0)
        self.assertEqual(result["ended"]["title"], "Weekly")
        self.assertEqual(result["rc_number_ended_1based"], 1)

    async def test_end_rollcall_no_rollcall_raises(self):
        from exceptions import rollCallNotStarted
        mgr = self._mgr([])

        with patch("services.rollcalls.manager", mgr):
            from services.rollcalls import end_rollcall
            with self.assertRaises(rollCallNotStarted):
                await end_rollcall(100, 0, 9, "Admin")

    async def test_end_rollcall_out_of_range_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        mgr = self._mgr([rc])

        with patch("services.rollcalls.manager", mgr):
            from services.rollcalls import end_rollcall
            with self.assertRaises(incorrectParameter):
                await end_rollcall(100, 5, 9, "Admin")

    def test_list_rollcalls_empty(self):
        mgr = self._mgr([])
        with patch("services.rollcalls.manager", mgr):
            from services.rollcalls import list_rollcalls
            result = list_rollcalls(100)
        self.assertEqual(result, [])

    def test_list_rollcalls_multiple(self):
        rcs = [_make_rc("RC1"), _make_rc("RC2")]
        mgr = self._mgr(rcs)
        with patch("services.rollcalls.manager", mgr):
            from services.rollcalls import list_rollcalls
            result = list_rollcalls(100)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["title"], "RC1")
        self.assertEqual(result[1]["number"], 2)

    def test_get_rollcall_valid(self):
        rc = _make_rc("Event")
        mgr = self._mgr([rc])
        with patch("services.rollcalls.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.rollcalls import get_rollcall
            result = get_rollcall(100, 0)
        self.assertEqual(result["title"], "Event")

    def test_get_rollcall_not_started_raises(self):
        from exceptions import rollCallNotStarted
        mgr = self._mgr([])
        with patch("services.rollcalls.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.rollcalls import get_rollcall
            with self.assertRaises(rollCallNotStarted):
                get_rollcall(100, 0)


# ---------------------------------------------------------------------------
# services.voting
# ---------------------------------------------------------------------------

class TestVotingService(unittest.IsolatedAsyncioTestCase):

    def _setup(self, rc=None, mgr=None):
        rc = rc or _make_rc()
        mgr = mgr or _make_manager([rc])
        return rc, mgr

    async def test_vote_in_added(self):
        rc = _make_rc()
        rc.addIn.return_value = None  # "added" (not AB/AC/AU)
        mgr = _make_manager([rc])

        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.upsert_chat_member"), \
             patch("services.voting.increment_user_stat"), \
             patch("services.voting.increment_rollcall_stat"), \
             patch("services.voting.get_ghost_count", return_value=0):
            from services.voting import vote_in
            result = await vote_in(100, 1, "Alice", "alice", "hi")

        self.assertEqual(result["action"], "added")
        self.assertIn("rollcall", result)
        self.assertIn("user", result)

    async def test_vote_in_waitlisted_when_at_limit(self):
        rc = _make_rc(limit=1, in_list=[_make_user("Bob", user_id=2)])
        rc.addIn.return_value = "AC"  # waitlisted
        mgr = _make_manager([rc])

        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.upsert_chat_member"), \
             patch("services.voting.increment_user_stat"), \
             patch("services.voting.increment_rollcall_stat"), \
             patch("services.voting.get_ghost_count", return_value=0):
            from services.voting import vote_in
            result = await vote_in(100, 1, "Alice")

        self.assertEqual(result["action"], "waitlisted")

    async def test_vote_in_already_in_raises(self):
        from exceptions import alreadyInList
        rc = _make_rc()
        rc.addIn.return_value = "AB"
        mgr = _make_manager([rc])

        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.upsert_chat_member"), \
             patch("services.voting.increment_user_stat"), \
             patch("services.voting.increment_rollcall_stat"), \
             patch("services.voting.get_ghost_count", return_value=0):
            from services.voting import vote_in
            with self.assertRaises(alreadyInList):
                await vote_in(100, 1, "Alice")

    async def test_vote_out_moved_from_in(self):
        u = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[u])
        rc.addOut.return_value = None  # added (no promotion)
        mgr = _make_manager([rc])

        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.upsert_chat_member"), \
             patch("services.voting.increment_user_stat"), \
             patch("services.voting.increment_rollcall_stat"):
            from services.voting import vote_out
            result = await vote_out(100, 1, "Alice")

        self.assertTrue(result["was_in"])
        self.assertEqual(result["action"], "moved")
        self.assertIsNone(result["promoted"])

    async def test_vote_out_promotes_from_waitlist(self):
        waiter = _make_user("Bob", user_id=2)
        u = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[u], wait_list=[waiter])
        rc.addOut.return_value = waiter  # promoted user returned
        mgr = _make_manager([rc])

        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.upsert_chat_member"), \
             patch("services.voting.increment_user_stat"), \
             patch("services.voting.increment_rollcall_stat"):
            from services.voting import vote_out
            result = await vote_out(100, 1, "Alice")

        self.assertIsNotNone(result["promoted"])
        self.assertEqual(result["promoted"]["name"], "Bob")
        self.assertEqual(result["action"], "moved")

    async def test_vote_maybe_added(self):
        rc = _make_rc()
        rc.addMaybe.return_value = None
        mgr = _make_manager([rc])

        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.upsert_chat_member"), \
             patch("services.voting.increment_user_stat"), \
             patch("services.voting.increment_rollcall_stat"):
            from services.voting import vote_maybe
            result = await vote_maybe(100, 1, "Alice")

        self.assertEqual(result["action"], "added")
        self.assertFalse(result["was_in"])

    def test_check_ghost_reconf_proxy_user_returns_not_needed(self):
        mgr = _make_manager([_make_rc()])
        with patch("services.voting.manager", mgr):
            from services.voting import check_ghost_reconfirmation_needed
            result = check_ghost_reconfirmation_needed(100, "ProxyName")
        self.assertFalse(result["needed"])

    def test_check_ghost_reconf_tracking_off_returns_not_needed(self):
        mgr = _make_manager([_make_rc()], ghost_on=False)
        with patch("services.voting.manager", mgr):
            from services.voting import check_ghost_reconfirmation_needed
            result = check_ghost_reconfirmation_needed(100, 1)
        self.assertFalse(result["needed"])

    def test_check_ghost_reconf_below_limit_not_needed(self):
        rc = _make_rc()
        mgr = _make_manager([rc], ghost_on=True, absent_limit=3)
        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.get_ghost_count", return_value=1):
            from services.voting import check_ghost_reconfirmation_needed
            result = check_ghost_reconfirmation_needed(100, 1)
        self.assertFalse(result["needed"])

    def test_check_ghost_reconf_above_limit_needed(self):
        rc = _make_rc()
        mgr = _make_manager([rc], ghost_on=True, absent_limit=2)
        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.get_ghost_count", return_value=3):
            from services.voting import check_ghost_reconfirmation_needed
            result = check_ghost_reconfirmation_needed(100, 1)
        self.assertTrue(result["needed"])
        self.assertEqual(result["ghost_count"], 3)
        self.assertEqual(result["absent_limit"], 2)

    def test_check_ghost_reconf_already_in_not_needed(self):
        u = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[u])
        mgr = _make_manager([rc], ghost_on=True, absent_limit=1)
        with patch("services.voting.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.voting.get_ghost_count", return_value=5):
            from services.voting import check_ghost_reconfirmation_needed
            result = check_ghost_reconfirmation_needed(100, 1)
        self.assertFalse(result["needed"])
        self.assertTrue(result["already_in"])


# ---------------------------------------------------------------------------
# services.settings
# ---------------------------------------------------------------------------

class TestSettingsService(unittest.TestCase):

    def _mgr(self, rcs=None):
        return _make_manager(rcs or [_make_rc()])

    def test_get_chat_settings(self):
        mgr = self._mgr()
        with patch("services.settings.manager", mgr):
            from services.settings import get_chat_settings
            result = get_chat_settings(100)
        self.assertIn("timezone", result)
        self.assertIn("shh_mode", result)
        self.assertIn("ghost_tracking_enabled", result)

    def test_set_timezone_valid(self):
        mgr = self._mgr()
        mgr.get_rollcalls.return_value = []
        with patch("services.settings.manager", mgr), \
             patch("services.settings.update_chat_settings"), \
             patch("services.settings.log_admin_action"):
            from services.settings import set_timezone
            result = set_timezone(100, "Europe/London", 1, "Admin")
        self.assertEqual(result["timezone"], "Europe/London")

    def test_set_timezone_invalid_raises(self):
        from exceptions import incorrectParameter
        mgr = self._mgr()
        with patch("services.settings.manager", mgr):
            from services.settings import set_timezone
            with self.assertRaises(incorrectParameter):
                set_timezone(100, "Fake/Zone", 1, "Admin")

    def test_set_shh_mode_enable(self):
        mgr = self._mgr()
        with patch("services.settings.manager", mgr), \
             patch("services.settings.log_admin_action"):
            from services.settings import set_shh_mode
            result = set_shh_mode(100, True, 1, "Admin")
        self.assertTrue(result["shh_mode"])
        mgr.set_shh_mode.assert_called_once_with(100, True)

    def test_set_rollcall_limit_removes_limit(self):
        rc = _make_rc()
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.settings.log_admin_action"):
            from services.settings import set_rollcall_limit
            result = set_rollcall_limit(100, 0, 1, "Admin")
        # limit=0 means remove cap
        self.assertIsNone(rc.inListLimit)

    def test_set_rollcall_limit_negative_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.settings import set_rollcall_limit
            with self.assertRaises(incorrectParameter):
                set_rollcall_limit(100, -1, 1, "Admin")

    def test_set_wait_limit_zero_clears_cap_and_promotes_waitlist(self):
        """limit=0 should remove the cap and move all waitlist users to IN."""
        waiter = _make_user("Bob", user_id=2)
        rc = _make_rc(in_list=[], wait_list=[waiter], limit=5)
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.settings.log_admin_action"), \
             patch("services.settings.increment_user_stat"), \
             patch("services.settings.increment_rollcall_stat"):
            from services.settings import set_wait_limit
            result = set_wait_limit(100, 0, 1, "Admin")
        self.assertIsNone(result["new_limit"])
        self.assertEqual(len(result["promoted"]), 1)
        self.assertEqual(result["promoted"][0]["name"], "Bob")
        self.assertIsNone(rc.inListLimit)

    def test_set_wait_limit_promotes_from_waitlist(self):
        waiter = _make_user("Bob", user_id=2)
        rc = _make_rc(in_list=[], wait_list=[waiter], limit=None)
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.settings.log_admin_action"), \
             patch("services.settings.increment_user_stat"), \
             patch("services.settings.increment_rollcall_stat"):
            from services.settings import set_wait_limit
            result = set_wait_limit(100, 2, 1, "Admin")
        self.assertEqual(len(result["promoted"]), 1)
        self.assertEqual(result["promoted"][0]["name"], "Bob")
        self.assertEqual(len(result["demoted"]), 0)

    def test_set_wait_limit_demotes_excess(self):
        u1 = _make_user("Alice", user_id=1)
        u2 = _make_user("Bob", user_id=2)
        u3 = _make_user("Carol", user_id=3)
        rc = _make_rc(in_list=[u1, u2, u3], limit=5)
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.settings.log_admin_action"), \
             patch("services.settings.increment_user_stat"), \
             patch("services.settings.increment_rollcall_stat"):
            from services.settings import set_wait_limit
            result = set_wait_limit(100, 2, 1, "Admin")
        self.assertEqual(len(result["demoted"]), 1)
        self.assertEqual(result["demoted"][0]["name"], "Carol")
        self.assertEqual(len(result["promoted"]), 0)

    def test_set_location(self):
        rc = _make_rc()
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.settings.log_admin_action"):
            from services.settings import set_location
            result = set_location(100, "Gym Hall", 1, "Admin")
        self.assertEqual(rc.location, "Gym Hall")

    def test_set_event_fee(self):
        rc = _make_rc()
        mgr = self._mgr([rc])
        with patch("services.settings.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.settings.log_admin_action"):
            from services.settings import set_event_fee
            result = set_event_fee(100, "500", 1, "Admin")
        self.assertEqual(rc.event_fee, "500")


# ---------------------------------------------------------------------------
# services.ghost
# ---------------------------------------------------------------------------

class TestGhostService(unittest.TestCase):

    def test_get_ghost_settings(self):
        mgr = _make_manager(ghost_on=True, absent_limit=3)
        with patch("services.ghost.manager", mgr):
            from services.ghost import get_ghost_settings
            result = get_ghost_settings(100)
        self.assertTrue(result["ghost_tracking_enabled"])
        self.assertEqual(result["absent_limit"], 3)

    def test_toggle_ghost_tracking_on(self):
        mgr = _make_manager(ghost_on=True)
        with patch("services.ghost.manager", mgr), \
             patch("services.ghost.log_admin_action"):
            from services.ghost import toggle_ghost_tracking
            result = toggle_ghost_tracking(100, True, 1, "Admin")
        mgr.set_ghost_tracking_enabled.assert_called_once_with(100, True)

    def test_toggle_ghost_tracking_off(self):
        mgr = _make_manager(ghost_on=True)
        with patch("services.ghost.manager", mgr), \
             patch("services.ghost.log_admin_action"):
            from services.ghost import toggle_ghost_tracking
            toggle_ghost_tracking(100, False, 1, "Admin")
        mgr.set_ghost_tracking_enabled.assert_called_once_with(100, False)

    def test_set_absent_limit_valid(self):
        mgr = _make_manager()
        with patch("services.ghost.manager", mgr), \
             patch("services.ghost.log_admin_action"):
            from services.ghost import set_absent_limit
            result = set_absent_limit(100, 3, 1, "Admin")
        mgr.set_absent_limit.assert_called_once_with(100, 3)

    def test_set_absent_limit_zero_raises(self):
        from exceptions import incorrectParameter
        mgr = _make_manager()
        with patch("services.ghost.manager", mgr):
            from services.ghost import set_absent_limit
            with self.assertRaises(incorrectParameter):
                set_absent_limit(100, 0, 1, "Admin")

    def test_clear_absent_single_user(self):
        mgr = _make_manager()
        with patch("services.ghost.manager", mgr), \
             patch("services.ghost.reset_ghost_count") as mock_reset, \
             patch("services.ghost.log_admin_action"):
            from services.ghost import clear_absent
            result = clear_absent(100, 1, "Admin", target_user_id=5)
        mock_reset.assert_called_once_with(100, 5)
        self.assertTrue(result["cleared"])

    def test_clear_absent_proxy(self):
        mgr = _make_manager()
        with patch("services.ghost.manager", mgr), \
             patch("services.ghost.reset_ghost_count") as mock_reset, \
             patch("services.ghost.log_admin_action"):
            from services.ghost import clear_absent
            result = clear_absent(100, 1, "Admin", proxy_name="Bob")
        mock_reset.assert_called_once_with(100, -1, proxy_name="Bob")

    def test_clear_absent_all(self):
        mgr = _make_manager()
        leaderboard = [
            {"user_id": 1, "proxy_name": None, "ghost_count": 2},
            {"user_id": None, "proxy_name": "ProxyX", "ghost_count": 1},
        ]
        with patch("services.ghost.manager", mgr), \
             patch("services.ghost.reset_ghost_count") as mock_reset, \
             patch("services.ghost.get_ghost_leaderboard", return_value=leaderboard), \
             patch("services.ghost.log_admin_action"):
            from services.ghost import clear_absent
            result = clear_absent(100, 1, "Admin")
        self.assertEqual(mock_reset.call_count, 2)

    def test_ghost_leaderboard(self):
        board = [{"user_name": "Alice", "proxy_name": None, "user_id": 1, "ghost_count": 3}]
        with patch("services.ghost.get_ghost_leaderboard", return_value=board):
            from services.ghost import ghost_leaderboard
            result = ghost_leaderboard(100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Alice")
        self.assertFalse(result[0]["is_proxy"])
        self.assertEqual(result[0]["ghost_count"], 3)


# ---------------------------------------------------------------------------
# services.proxy
# ---------------------------------------------------------------------------

class TestProxyService(unittest.IsolatedAsyncioTestCase):

    def test_check_proxy_ghost_tracking_off(self):
        mgr = _make_manager(ghost_on=False)
        with patch("services.proxy.manager", mgr):
            from services.proxy import check_proxy_ghost_reconfirmation_needed
            result = check_proxy_ghost_reconfirmation_needed(100, "Bob")
        self.assertFalse(result["needed"])

    def test_check_proxy_ghost_needed(self):
        mgr = _make_manager(ghost_on=True, absent_limit=1)
        with patch("services.proxy.manager", mgr), \
             patch("services.proxy.get_ghost_count_by_proxy_name", return_value=2):
            from services.proxy import check_proxy_ghost_reconfirmation_needed
            result = check_proxy_ghost_reconfirmation_needed(100, "Bob")
        self.assertTrue(result["needed"])
        self.assertEqual(result["ghost_count"], 2)

    def test_check_proxy_ghost_empty_name_raises(self):
        from exceptions import parameterMissing
        mgr = _make_manager()
        with patch("services.proxy.manager", mgr):
            from services.proxy import check_proxy_ghost_reconfirmation_needed
            with self.assertRaises(parameterMissing):
                check_proxy_ghost_reconfirmation_needed(100, "")

    async def test_set_in_for_added(self):
        rc = _make_rc()
        rc.addIn.return_value = None
        mgr = _make_manager([rc])

        with patch("services.proxy.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.proxy.log_admin_action"), \
             patch("services.proxy.increment_user_stat"), \
             patch("services.proxy.increment_rollcall_stat"):
            from services.proxy import set_in_for
            result = await set_in_for(100, 1, "Admin", "ProxyBob")

        self.assertEqual(result["action"], "added")
        self.assertIn("user", result)

    async def test_set_in_for_already_present_raises(self):
        from exceptions import duplicateProxy
        from models import User
        proxy = _make_proxy_user("Bob")
        rc = _make_rc(in_list=[proxy])
        mgr = _make_manager([rc])

        with patch("services.proxy.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.proxy.log_admin_action"):
            from services.proxy import set_in_for
            with self.assertRaises(duplicateProxy):
                await set_in_for(100, 1, "Admin", "Bob")

    async def test_set_in_for_waitlisted(self):
        u = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[u], limit=1)
        rc.addIn.return_value = "AC"
        mgr = _make_manager([rc])

        with patch("services.proxy.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.proxy.log_admin_action"), \
             patch("services.proxy.increment_user_stat"), \
             patch("services.proxy.increment_rollcall_stat"):
            from services.proxy import set_in_for
            result = await set_in_for(100, 1, "Admin", "ProxyBob")

        self.assertEqual(result["action"], "waitlisted")

    async def test_set_out_for_promotes_from_waitlist(self):
        waiter = _make_user("Carol", user_id=3)
        u = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[u], wait_list=[waiter])
        rc.addOut.return_value = waiter
        mgr = _make_manager([rc])

        with patch("services.proxy.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.proxy.log_admin_action"), \
             patch("services.proxy.increment_user_stat"), \
             patch("services.proxy.increment_rollcall_stat"):
            from services.proxy import set_out_for
            result = await set_out_for(100, 1, "Admin", "Alice")

        self.assertIsNotNone(result["promoted"])
        self.assertEqual(result["action"], "moved")

    async def test_set_maybe_for_added(self):
        rc = _make_rc()
        rc.addMaybe.return_value = None
        mgr = _make_manager([rc])

        with patch("services.proxy.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.proxy.log_admin_action"), \
             patch("services.proxy.increment_user_stat"), \
             patch("services.proxy.increment_rollcall_stat"):
            from services.proxy import set_maybe_for
            result = await set_maybe_for(100, 1, "Admin", "ProxyBob")

        self.assertEqual(result["action"], "added")

    async def test_set_in_for_empty_name_raises(self):
        from exceptions import parameterMissing
        mgr = _make_manager([_make_rc()])
        with patch("services.proxy.manager", mgr):
            from services.proxy import set_in_for
            with self.assertRaises(parameterMissing):
                await set_in_for(100, 1, "Admin", "")

    async def test_set_in_for_name_too_long_raises(self):
        from exceptions import parameterMissing
        mgr = _make_manager([_make_rc()])
        with patch("services.proxy.manager", mgr):
            from services.proxy import set_in_for
            with self.assertRaises(parameterMissing):
                await set_in_for(100, 1, "Admin", "A" * 41)


# ---------------------------------------------------------------------------
# services.admin
# ---------------------------------------------------------------------------

class TestAdminService(unittest.TestCase):

    def test_delete_user_from_rollcall_found(self):
        rc = _make_rc()
        rc.delete_user.return_value = True
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.admin.log_admin_action"), \
             patch("services.admin.delete_user_by_id"):
            from services.admin import delete_user_from_rollcall
            result = delete_user_from_rollcall(100, 0, "Alice", 1, "Admin")
        self.assertEqual(result["deleted"], "Alice")
        self.assertEqual(result["rc_number_1based"], 1)

    def test_delete_user_from_rollcall_not_found_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        rc.delete_user.return_value = False
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.admin import delete_user_from_rollcall
            with self.assertRaises(incorrectParameter):
                delete_user_from_rollcall(100, 0, "Ghost", 1, "Admin")

    def test_delete_user_from_rollcall_empty_name_raises(self):
        from exceptions import parameterMissing
        mgr = _make_manager([_make_rc()])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.admin import delete_user_from_rollcall
            with self.assertRaises(parameterMissing):
                delete_user_from_rollcall(100, 0, "", 1, "Admin")

    def test_set_user_status_move(self):
        alice = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[alice])
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr), \
             patch("services.admin.log_admin_action"), \
             patch("services.admin.delete_user_by_id"):
            from services.admin import set_user_status
            result = set_user_status(100, 0, "Alice", "out", 1, "Admin")
        self.assertEqual(result["moved"], "Alice")
        self.assertEqual(result["from_status"], "in")
        self.assertEqual(result["to_status"], "out")

    def test_set_user_status_same_status_raises(self):
        from exceptions import incorrectParameter
        alice = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[alice])
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.admin import set_user_status
            with self.assertRaises(incorrectParameter):
                set_user_status(100, 0, "Alice", "in", 1, "Admin")

    def test_set_user_status_not_found_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.admin import set_user_status
            with self.assertRaises(incorrectParameter):
                set_user_status(100, 0, "Ghost", "out", 1, "Admin")

    def test_set_user_status_invalid_status_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.admin import set_user_status
            with self.assertRaises(incorrectParameter):
                set_user_status(100, 0, "Alice", "flying", 1, "Admin")

    def test_set_user_status_empty_name_raises(self):
        from exceptions import parameterMissing
        rc = _make_rc()
        mgr = _make_manager([rc])
        with patch("services.admin.manager", mgr), \
             patch("rollcall_manager.manager", mgr):
            from services.admin import set_user_status
            with self.assertRaises(parameterMissing):
                set_user_status(100, 0, "", "out", 1, "Admin")


# ---------------------------------------------------------------------------
# services.lists
# ---------------------------------------------------------------------------

class TestListsService(unittest.TestCase):

    def test_no_active_rollcall_returns_candidates_only(self):
        mgr = _make_manager([])
        members = [{"user_id": 1, "first_name": "Alice", "username": "alice"}]
        with patch("services.lists.manager", mgr), \
             patch("services.lists.get_active_members", return_value=members):
            from services.lists import get_non_responders
            result = get_non_responders(100)
        self.assertFalse(result["has_active_rollcall"])
        self.assertEqual(result["candidates"], members)
        self.assertEqual(result["rollcall_titles"], [])

    def test_all_voted_returns_empty(self):
        alice = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[alice])
        mgr = _make_manager([rc])
        members = [{"user_id": 1, "first_name": "Alice", "username": "alice"}]
        with patch("services.lists.manager", mgr), \
             patch("services.lists.get_active_members", return_value=members):
            from services.lists import get_non_responders
            result = get_non_responders(100, rc_number=0)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["has_active_rollcall"])

    def test_one_non_responder_returned(self):
        alice = _make_user("Alice", user_id=1)
        rc = _make_rc(in_list=[alice])
        mgr = _make_manager([rc])
        members = [
            {"user_id": 1, "first_name": "Alice", "username": "alice"},
            {"user_id": 2, "first_name": "Bob", "username": "bob"},
        ]
        with patch("services.lists.manager", mgr), \
             patch("services.lists.get_active_members", return_value=members):
            from services.lists import get_non_responders
            result = get_non_responders(100, rc_number=0)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["first_name"], "Bob")

    def test_rc_number_out_of_range_raises(self):
        from exceptions import incorrectParameter
        rc = _make_rc()
        mgr = _make_manager([rc])
        with patch("services.lists.manager", mgr), \
             patch("services.lists.get_active_members", return_value=[]):
            from services.lists import get_non_responders
            with self.assertRaises(incorrectParameter):
                get_non_responders(100, rc_number=5)

    def test_no_rc_number_unions_all_rollcalls(self):
        u1 = _make_user("Alice", user_id=1)
        u2 = _make_user("Bob", user_id=2)
        rc1 = _make_rc(in_list=[u1])
        rc2 = _make_rc(in_list=[u2])
        mgr = _make_manager([rc1, rc2])
        members = [
            {"user_id": 1, "first_name": "Alice", "username": "alice"},
            {"user_id": 2, "first_name": "Bob", "username": "bob"},
            {"user_id": 3, "first_name": "Carol", "username": "carol"},
        ]
        with patch("services.lists.manager", mgr), \
             patch("services.lists.get_active_members", return_value=members):
            from services.lists import get_non_responders
            result = get_non_responders(100)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["first_name"], "Carol")


# ---------------------------------------------------------------------------
# services.templates
# ---------------------------------------------------------------------------

class TestTemplatesService(unittest.IsolatedAsyncioTestCase):

    def _db_row(self, name="weekly", title="Weekly Game", **kwargs):
        row = {
            "name": name,
            "title": title,
            "inlistlimit": None,
            "location": None,
            "eventfee": None,
            "offsetdays": None,
            "offsethours": None,
            "offsetminutes": None,
            "event_day": "friday",
            "event_time": "19:00",
            "schedule_day": None,
            "schedule_time": None,
            "schedule_enabled": "0",
            "recurrence_type": "weekly",
            "last_scheduled_date": None,
        }
        row.update(kwargs)
        return row

    def test_list_templates_returns_serialized(self):
        rows = [self._db_row("weekly"), self._db_row("monthly")]
        with patch("services.templates.get_templates", return_value=rows):
            from services.templates import list_templates
            result = list_templates(100)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "weekly")
        self.assertFalse(result[0]["schedule_enabled"])

    def test_list_templates_empty(self):
        with patch("services.templates.get_templates", return_value=[]):
            from services.templates import list_templates
            result = list_templates(100)
        self.assertEqual(result, [])

    def test_get_one_template_found(self):
        row = self._db_row("weekly")
        with patch("services.templates.get_template", return_value=row):
            from services.templates import get_one_template
            result = get_one_template(100, "weekly")
        self.assertEqual(result["name"], "weekly")
        self.assertEqual(result["event_day"], "friday")

    def test_get_one_template_not_found_raises(self):
        from exceptions import incorrectParameter
        with patch("services.templates.get_template", return_value=None):
            from services.templates import get_one_template
            with self.assertRaises(incorrectParameter):
                get_one_template(100, "notexist")

    def test_get_one_template_empty_name_raises(self):
        from exceptions import parameterMissing
        from services.templates import get_one_template
        with self.assertRaises(parameterMissing):
            get_one_template(100, "")

    def test_upsert_template_creates(self):
        with patch("services.templates.get_template", return_value=None), \
             patch("services.templates.create_or_update_template", return_value=True), \
             patch("services.templates.log_admin_action"):
            from services.templates import upsert_template
            result = upsert_template(100, "weekly", 1, "Admin", title="Weekly")
        self.assertEqual(result["name"], "weekly")
        self.assertEqual(result["title"], "Weekly")

    def test_upsert_template_partial_update_preserves_existing(self):
        existing = self._db_row("weekly", title="Old Title", inlistlimit=10)
        with patch("services.templates.get_template", return_value=existing), \
             patch("services.templates.create_or_update_template", return_value=True), \
             patch("services.templates.log_admin_action"):
            from services.templates import upsert_template
            result = upsert_template(100, "weekly", 1, "Admin", title="New Title")
        self.assertEqual(result["title"], "New Title")
        self.assertEqual(result["limit"], 10)  # preserved

    def test_upsert_template_invalid_event_day_raises(self):
        from exceptions import incorrectParameter
        with patch("services.templates.get_template", return_value=None):
            from services.templates import upsert_template
            with self.assertRaises(incorrectParameter):
                upsert_template(100, "weekly", 1, "Admin", event_day="Blorgday")

    def test_delete_one_template_success(self):
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row), \
             patch("services.templates.delete_template", return_value=True), \
             patch("services.templates.log_admin_action"):
            from services.templates import delete_one_template
            result = delete_one_template(100, "weekly", 1, "Admin")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["name"], "weekly")

    def test_delete_one_template_not_found_raises(self):
        from exceptions import incorrectParameter
        with patch("services.templates.get_template", return_value=None):
            from services.templates import delete_one_template
            with self.assertRaises(incorrectParameter):
                delete_one_template(100, "ghost", 1, "Admin")

    def test_set_schedule_weekly(self):
        row = self._db_row()
        updated_row = {**row, "schedule_day": "monday", "schedule_time": "09:00",
                       "schedule_enabled": "1"}
        with patch("services.templates.get_template", return_value=row), \
             patch("services.templates.set_template_schedule", return_value=True), \
             patch("services.templates.log_admin_action"), \
             patch("services.templates.get_template", side_effect=[row, updated_row, updated_row]):
            from services.templates import set_schedule
            result = set_schedule(
                100, "weekly", 1, "Admin",
                recurrence_type="weekly",
                schedule_day="monday",
                schedule_time="09:00",
            )
        self.assertIn("schedule_day", result)

    def test_set_schedule_invalid_recurrence_raises(self):
        from exceptions import incorrectParameter
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row):
            from services.templates import set_schedule
            with self.assertRaises(incorrectParameter):
                set_schedule(100, "weekly", 1, "Admin", recurrence_type="daily",
                             schedule_day="monday", schedule_time="09:00")

    def test_set_schedule_template_not_found_raises(self):
        from exceptions import incorrectParameter
        with patch("services.templates.get_template", return_value=None):
            from services.templates import set_schedule
            with self.assertRaises(incorrectParameter):
                set_schedule(100, "ghost", 1, "Admin")

    def test_set_schedule_monthly_missing_day_raises(self):
        from exceptions import parameterMissing
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row):
            from services.templates import set_schedule
            with self.assertRaises(parameterMissing):
                set_schedule(100, "weekly", 1, "Admin", recurrence_type="monthly",
                             schedule_time="09:00")  # no monthly_day

    def test_set_schedule_monthly_invalid_day_raises(self):
        from exceptions import incorrectParameter
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row):
            from services.templates import set_schedule
            with self.assertRaises(incorrectParameter):
                set_schedule(100, "weekly", 1, "Admin", recurrence_type="monthly",
                             monthly_day=32, schedule_time="09:00")

    def test_set_schedule_invalid_time_raises(self):
        from exceptions import incorrectParameter
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row):
            from services.templates import set_schedule
            with self.assertRaises(incorrectParameter):
                set_schedule(100, "weekly", 1, "Admin", recurrence_type="weekly",
                             schedule_day="monday", schedule_time="25:99")

    def test_set_schedule_without_event_day_still_works(self):
        """Schedule must be settable even without event_day/event_time on template."""
        row = self._db_row(event_day=None, event_time=None)
        updated = {**row, "schedule_day": "monday", "schedule_time": "09:00"}
        with patch("services.templates.get_template", side_effect=[row, updated, updated]), \
             patch("services.templates.set_template_schedule", return_value=True), \
             patch("services.templates.log_admin_action"):
            from services.templates import set_schedule
            result = set_schedule(
                100, "weekly", 1, "Admin",
                schedule_day="monday", schedule_time="09:00",
            )
        self.assertIn("schedule_day", result)

    def test_disable_schedule(self):
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row), \
             patch("services.templates.disable_template_schedule", return_value=True), \
             patch("services.templates.log_admin_action"), \
             patch("services.templates.get_one_template", return_value={"name": "weekly",
                    "schedule_day": None, "schedule_time": None,
                    "schedule_enabled": False, "recurrence_type": "weekly",
                    "last_scheduled_date": None}):
            from services.templates import disable_schedule
            result = disable_schedule(100, "weekly", 1, "Admin")
        self.assertFalse(result["schedule_enabled"])

    def test_enable_schedule(self):
        row = self._db_row()
        with patch("services.templates.get_template", return_value=row), \
             patch("services.templates.enable_template_schedule", return_value=True), \
             patch("services.templates.log_admin_action"), \
             patch("services.templates.get_one_template", return_value={"name": "weekly",
                    "schedule_day": "friday", "schedule_time": "19:00",
                    "schedule_enabled": True, "recurrence_type": "weekly",
                    "last_scheduled_date": None}):
            from services.templates import enable_schedule
            result = enable_schedule(100, "weekly", 1, "Admin")
        self.assertTrue(result["schedule_enabled"])

    async def test_start_template_happy_path(self):
        mgr = _make_manager([])
        rc = _make_rc("Weekly Game")
        rc.timezone = "Asia/Kolkata"
        mgr.add_rollcall.return_value = rc
        mgr.get_rollcalls.side_effect = [[], [rc]]  # before/after add
        row = self._db_row()

        with patch("services.templates.get_template", return_value=row), \
             patch("services.templates.manager", mgr), \
             patch("services.templates.log_admin_action"):
            from services.templates import start_template
            result = await start_template(100, "weekly", 1, "Admin")

        self.assertIn("title", result)
        mgr.add_rollcall.assert_called_once()

    async def test_start_template_max_rollcalls_raises(self):
        from exceptions import amountOfRollCallsReached
        rcs = [_make_rc() for _ in range(3)]
        mgr = _make_manager(rcs)
        row = self._db_row()

        with patch("services.templates.get_template", return_value=row), \
             patch("services.templates.manager", mgr):
            from services.templates import start_template
            with self.assertRaises(amountOfRollCallsReached):
                await start_template(100, "weekly", 1, "Admin")

    async def test_start_template_not_found_raises(self):
        from exceptions import incorrectParameter
        mgr = _make_manager([])

        with patch("services.templates.get_template", return_value=None), \
             patch("services.templates.manager", mgr):
            from services.templates import start_template
            with self.assertRaises(incorrectParameter):
                await start_template(100, "ghost", 1, "Admin")

    def test_serialize_template_schedule_enabled_normalization(self):
        """schedule_enabled handles 0, "0", False, None all as False."""
        from services.templates import _serialize_template
        for falsy in [0, "0", False, "False", "None", None, ""]:
            row = {"name": "t", "schedule_enabled": falsy}
            self.assertFalse(_serialize_template(row)["schedule_enabled"],
                             f"Expected False for {falsy!r}")

        for truthy in [1, "1", True, "True"]:
            row = {"name": "t", "schedule_enabled": truthy}
            self.assertTrue(_serialize_template(row)["schedule_enabled"],
                            f"Expected True for {truthy!r}")


# ---------------------------------------------------------------------------
# services.stats
# ---------------------------------------------------------------------------

class TestStatsService(unittest.TestCase):

    def test_personal_stats_returns_dict(self):
        cursor = MagicMock()
        row_data = {
            "total_in": 7, "total_out": 2, "total_maybe": 1,
            "total_rollcalls": 10, "total_waiting_to_in": 0,
            "best_streak": 5, "current_streak": 3,
        }
        cursor.fetchone.return_value = row_data
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch("services.stats.get_chat_ended_rollcall_count", return_value=10), \
             patch("services.stats.get_user_attendance_count", return_value=7), \
             patch("services.stats.get_ghost_count", return_value=1), \
             patch("db.get_connection", return_value=conn), \
             patch("db.db_type", "sqlite"), \
             patch("db.release_connection"):
            from services.stats import personal_stats
            result = personal_stats(100, 1)
        self.assertEqual(result["user_id"], 1)
        self.assertEqual(result["total_rollcalls_in_chat"], 10)
        self.assertEqual(result["sessions_attended"], 7)

    def test_group_stats_returns_dict(self):
        with patch("services.stats.get_group_attendance_totals", return_value={
                "real_attendance_slots": 40, "proxy_attendance_slots": 10,
                "real_participants": 8, "proxy_participants": 2,
                "real_vote_in": 40, "real_vote_out": 5, "real_vote_maybe": 3,
                "proxy_in": 10, "proxy_out": 1, "proxy_maybe": 0,
                "waitlist_promotions": 2,
             }), \
             patch("services.stats.get_chat_ended_rollcall_count", return_value=20), \
             patch("services.stats.get_leaderboard_by_attendance", return_value=[]), \
             patch("services.stats.get_ghost_leaderboard", return_value=[]):
            from services.stats import group_stats
            result = group_stats(100)
        self.assertEqual(result["total_rollcalls"], 20)
        self.assertEqual(result["total_attendance_slots"], 50)
        self.assertEqual(result["real_participants"], 8)

    def test_leaderboard_returns_ranked_list(self):
        rows = [
            {"display_name": "Alice", "first_name": "Alice", "proxy_name": None, "user_id": 1,
             "attended": 10, "total_rollcalls": 12, "username": None, "kind": "real"},
            {"display_name": "Bob-Proxy", "first_name": None, "proxy_name": "Bob-Proxy", "user_id": None,
             "attended": 5, "total_rollcalls": 5, "username": None, "kind": "proxy"},
        ]
        with patch("services.stats.get_chat_ended_rollcall_count", return_value=15), \
             patch("services.stats.get_leaderboard_by_attendance", return_value=rows):
            from services.stats import leaderboard
            result = leaderboard(100, limit=10)
        entries = result["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["rank"], 1)
        self.assertEqual(entries[0]["display_name"], "Alice")
        self.assertEqual(entries[0]["kind"], "real")
        self.assertEqual(entries[1]["rank"], 2)
        self.assertEqual(entries[1]["kind"], "proxy")

    def test_history_returns_list(self):
        rows = [{"id": 1, "title": "Game", "ended_at": "2024-01-01", "in_count": 5,
                 "out_count": 1, "maybe_count": 0}]
        with patch("services.stats.get_rollcall_history", return_value=rows):
            from services.stats import history
            result = history(100, limit=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Game")
        self.assertEqual(result[0]["in_count"], 5)


if __name__ == "__main__":
    unittest.main()
