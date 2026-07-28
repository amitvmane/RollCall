"""
Unit tests for services/dues.py — core (Task 2 scope).

All DB + manager calls are mocked so tests run offline without any database.
Task 3 additions (mark_late, mark_ditch, mark_paid, waive, etc.) will extend
this file alongside their implementation.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_user(name="Alice", username="alice", user_id=1):
    from models import User
    u = User.__new__(User)
    u.name = name
    u.username = username
    u.user_id = user_id
    u.comment = ""
    u.first_name = name
    return u


def _make_proxy_user(name="Bob Proxy", owner_id=None):
    from models import User
    u = User.__new__(User)
    u.name = name
    u.username = None
    u.user_id = name     # proxy: user_id is the name string
    u.comment = ""
    u.first_name = name
    return u


def _make_rc(
    title="Sunday Game",
    in_list=None,
    event_fee="600",
    rc_id=99,
    proxy_owners=None,
    collector_uid=None,
    collector_name=None,
    collector_paid_ground=0,
    collector_upi=None,
):
    rc = MagicMock()
    rc.title = title
    rc.id = rc_id
    rc.inList = in_list or []
    rc.outList = []
    rc.maybeList = []
    rc.waitList = []
    rc.event_fee = event_fee
    rc.proxy_owners = proxy_owners or {}
    rc.collector_uid = collector_uid
    rc.collector_name = collector_name
    rc.collector_paid_ground = collector_paid_ground
    rc.collector_upi = collector_upi
    rc.absent_marked = False
    rc.save.return_value = None
    return rc


def _make_manager(rollcalls=None):
    m = MagicMock()
    rcs = rollcalls if rollcalls is not None else []
    m.get_rollcalls.return_value = rcs
    m.get_rollcall.return_value = rcs[0] if rcs else None
    m.get_chat.return_value = {"timezone": "Asia/Kolkata"}
    m.get_ghost_tracking_enabled.return_value = False
    m.remove_rollcall.return_value = None
    lock_ctx = MagicMock()
    lock_ctx.__aenter__ = AsyncMock(return_value=None)
    lock_ctx.__aexit__ = AsyncMock(return_value=False)
    m.get_chat_write_lock.return_value = lock_ctx
    return m


# ── compute_shares ───────────────────────────────────────────────────────────

class TestComputeShares(unittest.TestCase):

    def _call(self, ground_cost, subsidy, in_count, step=10):
        from services.dues import compute_shares
        return compute_shares(ground_cost, subsidy, in_count, step)

    def test_canonical_600_7_step10(self):
        per_head, remainder = self._call(600, 0, 7, 10)
        # 600 / 7 = 85.7... → ceil = 86 → round to next 10 = 90
        self.assertEqual(per_head, 90)
        # 90 * 7 - 600 = 630 - 600 = 30
        self.assertEqual(remainder, 30)

    def test_exact_division_no_remainder(self):
        per_head, remainder = self._call(600, 0, 6, 10)
        # 600 / 6 = 100 exactly; already on step boundary
        self.assertEqual(per_head, 100)
        self.assertEqual(remainder, 0)

    def test_step_1_no_rounding(self):
        per_head, remainder = self._call(601, 0, 7, 1)
        # ceil(601/7) = ceil(85.857) = 86
        self.assertEqual(per_head, 86)
        self.assertEqual(remainder, 86 * 7 - 601)

    def test_with_subsidy(self):
        # ground=600, subsidy=60, net=540, 7 players step=10
        # ceil(540/7)=77.14→78, round to 80
        per_head, remainder = self._call(600, 60, 7, 10)
        self.assertEqual(per_head, 80)
        self.assertEqual(remainder, 80 * 7 - 540)

    def test_zero_in_count_raises(self):
        from exceptions import parameterMissing
        with self.assertRaises(parameterMissing):
            self._call(600, 0, 0)

    def test_negative_step_treated_as_one(self):
        per_head, remainder = self._call(600, 0, 7, -1)
        # step ≤ 0 → step=1
        self.assertEqual(per_head, 86)   # ceil(600/7)=86
        self.assertEqual(remainder, 86 * 7 - 600)

    def test_remainder_nonnegative(self):
        # Invariant: remainder ≥ 0 for any valid inputs
        for gc in range(100, 700, 73):
            for n in range(1, 13):
                ph, rem = self._call(gc, 0, n, 10)
                self.assertGreaterEqual(rem, 0)

    def test_subsidy_exceeding_ground_cost_never_produces_negative_per_head(self):
        """Defensive floor — close_game validates subsidy <= ground_cost
        before calling this, but the function itself should never be able
        to return a negative per_head even if a future caller skips that."""
        per_head, _ = self._call(100, 500, 5, 10)
        self.assertGreaterEqual(per_head, 0)
        self.assertEqual(per_head, 0)


# ── _resolve_member ───────────────────────────────────────────────────────────

class TestResolveMember(unittest.TestCase):

    def _call(self, chat_id, token, dues_names=None):
        from services.dues import _resolve_member
        return _resolve_member(chat_id, token, dues_names)

    def _mock_active(self, members):
        return patch("services.dues.db.get_active_members", return_value=members)

    def test_find_by_username(self):
        members = [{"user_id": 1, "first_name": "Alice", "username": "alice"}]
        with self._mock_active(members):
            r = self._call(1, "@alice")
        self.assertEqual(r["user_id"], 1)
        self.assertEqual(r["member_name"], "Alice")

    def test_find_by_first_name(self):
        members = [{"user_id": 2, "first_name": "Ravi", "username": None}]
        with self._mock_active(members):
            r = self._call(1, "Ravi")
        self.assertEqual(r["user_id"], 2)

    def test_case_insensitive_first_name(self):
        members = [{"user_id": 3, "first_name": "Priya", "username": None}]
        with self._mock_active(members):
            r = self._call(1, "priya")
        self.assertEqual(r["user_id"], 3)

    def test_not_found_raises(self):
        from exceptions import incorrectParameter
        with self._mock_active([]):
            with self.assertRaises(incorrectParameter):
                self._call(1, "Nobody")

    def test_ambiguous_raises(self):
        from exceptions import incorrectParameter
        members = [
            {"user_id": 1, "first_name": "Ali", "username": "ali_one"},
            {"user_id": 2, "first_name": "Ali", "username": "ali_two"},
        ]
        with self._mock_active(members):
            with self.assertRaises(incorrectParameter):
                self._call(1, "Ali")

    def test_proxy_fallback(self):
        with self._mock_active([]):
            r = self._call(1, "Walk-in Guest", dues_names=["Walk-in Guest"])
        self.assertIsNone(r["user_id"])
        self.assertEqual(r["member_name"], "Walk-in Guest")

    def test_proxy_case_insensitive(self):
        with self._mock_active([]):
            r = self._call(1, "walk-in guest", dues_names=["Walk-in Guest"])
        self.assertEqual(r["member_name"], "Walk-in Guest")

    def test_departed_real_user_resolved_via_ledger_history(self):
        """A real user no longer in get_active_members (e.g. left the group)
        but with prior dues_entries must still resolve — with their actual
        user_id, not fall through to a proxy-style user_id=None match that
        then can't find their balance."""
        history = [{"user_id": 555, "member_name": "Bob", "balance": 100}]
        with self._mock_active([]), \
             patch("services.dues.db.get_all_dues_balances", return_value=history):
            r = self._call(1, "Bob")
        self.assertEqual(r["user_id"], 555)
        self.assertEqual(r["member_name"], "Bob")

    def test_merged_proxy_canonicalizes_to_real_user(self):
        """A proxy resolved via any tier that's since been merged into a
        real user must attribute to that real user, not the dead alias
        spelling — this is the one deliberate write-path exception (see
        services/identity.py's module docstring): without it, /mark_paid
        <old-alias> after a merge would keep forking the ledger."""
        with self._mock_active([]), \
             patch("services.identity.resolve_canonical",
                   return_value={"kind": "user", "user_id": 777, "proxy_name": None}):
            r = self._call(1, "Rex", dues_names=["Rex"])
        self.assertEqual(r["user_id"], 777)
        self.assertEqual(r["member_name"], "Rex")

    def test_merged_proxy_canonicalizes_to_another_proxy(self):
        with self._mock_active([]), \
             patch("services.identity.resolve_canonical",
                   return_value={"kind": "proxy", "user_id": None, "proxy_name": "Ajay"}):
            r = self._call(1, "Ajya", dues_names=["Ajya"])
        self.assertIsNone(r["user_id"])
        self.assertEqual(r["member_name"], "Ajay")

    def test_unmerged_proxy_resolves_to_itself(self):
        with self._mock_active([]), \
             patch("services.identity.resolve_canonical",
                   return_value={"kind": "proxy", "user_id": None, "proxy_name": "Solo"}):
            r = self._call(1, "Solo", dues_names=["Solo"])
        self.assertIsNone(r["user_id"])
        self.assertEqual(r["member_name"], "Solo")

    def test_real_user_never_canonicalized(self):
        """Real-user matches (tier 1) never even consult identity resolution
        — a real user is always already canonical."""
        members = [{"user_id": 42, "first_name": "Deb", "username": None}]
        with self._mock_active(members), \
             patch("services.identity.resolve_canonical") as mock_resolve:
            r = self._call(1, "Deb")
        self.assertEqual(r["user_id"], 42)
        mock_resolve.assert_not_called()

    def test_ledger_history_ambiguous_raises(self):
        from exceptions import incorrectParameter
        history = [
            {"user_id": 1, "member_name": "Sam", "balance": 10},
            {"user_id": 2, "member_name": "Sam", "balance": 20},
        ]
        with self._mock_active([]), \
             patch("services.dues.db.get_all_dues_balances", return_value=history):
            with self.assertRaises(incorrectParameter):
                self._call(1, "Sam")

    def test_active_real_user_takes_priority_over_ledger_history(self):
        """If someone is both currently active AND has ledger history under
        a different user_id (shouldn't normally happen, but step 1 must win
        so the freshest identity is used)."""
        active = [{"user_id": 9, "first_name": "Nina", "username": None}]
        history = [{"user_id": 9, "member_name": "Nina", "balance": 50}]
        with self._mock_active(active), \
             patch("services.dues.db.get_all_dues_balances", return_value=history):
            r = self._call(1, "Nina")
        self.assertEqual(r["user_id"], 9)


class TestKnownProxyNames(unittest.TestCase):

    def test_collects_proxies_from_active_rollcall_in_list(self):
        from services.dues import _known_proxy_names
        proxy = MagicMock()
        proxy.user_id = "A1"
        proxy.name = "A1"
        rc = MagicMock(inList=[proxy], outList=[], maybeList=[], waitList=[])
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc]
        with patch("services.dues.manager", mgr), \
             patch("services.dues.db.get_unsettled_rollcalls", return_value=[]):
            names = _known_proxy_names(1)
        self.assertIn("A1", names)

    def test_excludes_real_users_from_active_rollcall(self):
        from services.dues import _known_proxy_names
        real = MagicMock()
        real.user_id = 101
        real.name = "Alice"
        rc = MagicMock(inList=[real], outList=[], maybeList=[], waitList=[])
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc]
        with patch("services.dues.manager", mgr), \
             patch("services.dues.db.get_unsettled_rollcalls", return_value=[]):
            names = _known_proxy_names(1)
        self.assertNotIn("Alice", names)

    def test_collects_proxies_from_unsettled_rollcalls(self):
        from services.dues import _known_proxy_names
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = []
        with patch("services.dues.manager", mgr), \
             patch("services.dues.db.get_unsettled_rollcalls", return_value=[{"id": 42}]), \
             patch("services.dues.db.get_rollcall_in_users", return_value=[{"proxy_name": "B5"}]):
            names = _known_proxy_names(1)
        self.assertIn("B5", names)

    def test_swallows_scan_failures_and_returns_empty(self):
        from services.dues import _known_proxy_names
        mgr = MagicMock()
        mgr.get_rollcalls.side_effect = Exception("boom")
        with patch("services.dues.manager", mgr), \
             patch("services.dues.db.get_unsettled_rollcalls", side_effect=Exception("boom too")):
            names = _known_proxy_names(1)   # must not raise
        self.assertEqual(names, [])


# ── settings setters ──────────────────────────────────────────────────────────

class TestDuesSettings(unittest.TestCase):

    def _patch_db(self):
        return patch.multiple(
            "services.dues.db",
            update_chat_settings=MagicMock(),
            log_admin_action=MagicMock(),
            get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "test@upi",
                "dues_round_step": 10,
            }),
        )

    def test_set_upi_valid(self):
        from services.dues import set_upi
        with self._patch_db() as m:
            r = set_upi(1, "amit@upi", 99, "Admin")
        self.assertEqual(r["upi_vpa"], "amit@upi")
        self.assertIn("amit@upi", r["announcement"])

    def test_set_upi_invalid(self):
        from exceptions import incorrectParameter
        from services.dues import set_upi
        with self._patch_db():
            with self.assertRaises(incorrectParameter):
                set_upi(1, "notaupi", 99, "Admin")

    def test_set_upi_invalid_no_at(self):
        from exceptions import incorrectParameter
        from services.dues import set_upi
        with self._patch_db():
            with self.assertRaises(incorrectParameter):
                set_upi(1, "justnodomain", 99, "Admin")

    def test_set_round_step_valid(self):
        from services.dues import set_round_step
        with self._patch_db():
            r = set_round_step(1, 5, 99, "Admin")
        self.assertEqual(r["dues_round_step"], 5)

    def test_set_round_step_zero_raises(self):
        from exceptions import incorrectParameter
        from services.dues import set_round_step
        with self._patch_db():
            with self.assertRaises(incorrectParameter):
                set_round_step(1, 0, 99, "Admin")

    def test_get_dues_settings_defaults(self):
        from services.dues import get_dues_settings
        with self._patch_db():
            s = get_dues_settings(1)
        self.assertEqual(s["dues_round_step"], 10)
        self.assertNotIn("penalty_ditch", s)  # penalty tiers live in penalty_tiers table now


# ── close_game ───────────────────────────────────────────────────────────────

_CLOSE_GAME_DB_DEFAULTS = dict(
    get_or_create_chat=MagicMock(return_value={
        "upi_vpa": None,
        "dues_round_step": 10,
    }),
    get_game_closure=MagicMock(return_value=None),   # not yet closed
    get_fund_balance=MagicMock(return_value=0),
    create_game_closure=MagicMock(return_value=1),
    write_game_closure_batch=MagicMock(return_value=1),
    add_dues_entry=MagicMock(),
    add_fund_transaction=MagicMock(),
    log_admin_action=MagicMock(),
    get_rollcall_in_users=MagicMock(return_value=[]),
    get_latest_closeable_rollcall=MagicMock(return_value=None),
    get_active_members=MagicMock(return_value=[]),
)


def _patch_close(**overrides):
    """Return a patch.multiple context for close_game with overrides."""
    kwargs = dict(_CLOSE_GAME_DB_DEFAULTS)
    kwargs.update(overrides)
    return patch.multiple("services.dues.db", **kwargs)


def _end_result():
    return {
        "ended": {}, "rc_number_ended_1based": 1, "ghost_eligible": False,
        "ghost_rc_db_id": None, "ended_by": {}, "remaining": [], "renumbered": [],
    }


class TestCloseGameActivePath(unittest.IsolatedAsyncioTestCase):
    """close_game when an active rollcall is present."""

    async def test_active_rc_ends_and_writes_shares(self):
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(title="Sunday Game", in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])

        write_batch = MagicMock(return_value=1)
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(write_game_closure_batch=write_batch), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["in_count"], 1)
        self.assertEqual(result["per_head"], 600)   # 600/1 → step 10 → 600
        self.assertEqual(result["remainder"], 0)
        write_batch.assert_called_once()
        dues_entries = write_batch.call_args.args[1]
        self.assertEqual(len(dues_entries), 1)
        self.assertEqual(dues_entries[0]["user_id"], 101)
        self.assertEqual(dues_entries[0]["amount"], 600)

    async def test_result_upi_vpa_prefers_collector_over_group(self):
        """result['upi_vpa'] must resolve the same way as the announcement text
        (collector UPI first, group fallback second) so /settle_dues's QR uses
        the UPI that was actually announced."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(title="Sunday Game", in_list=[alice], event_fee="600", rc_id=77,
                       collector_upi="collector@ybl")
        mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "group@ybl", "dues_round_step": 10,
             })), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["upi_vpa"], "collector@ybl")

    async def test_result_upi_vpa_falls_back_to_group(self):
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(title="Sunday Game", in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "group@ybl", "dues_round_step": 10,
             })), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["upi_vpa"], "group@ybl")

    async def test_active_rc_7_players_step10(self):
        from services.dues import close_game

        users = [_make_user(f"P{i}", user_id=100 + i) for i in range(7)]
        rc = _make_rc(in_list=users, event_fee="600", rc_id=88)
        mgr = _make_manager([rc])

        write_batch = MagicMock(return_value=1)
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(write_game_closure_batch=write_batch), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["per_head"], 90)
        self.assertEqual(result["remainder"], 30)
        write_batch.assert_called_once()
        dues_entries = write_batch.call_args.args[1]
        fund_transactions = write_batch.call_args.args[2]
        self.assertEqual(len(dues_entries), 7)
        self.assertEqual(len(fund_transactions), 1)
        self.assertEqual(fund_transactions[0]["amount"], 30)   # amount = remainder

    async def test_no_event_fee_raises_without_ending_rollcall(self):
        """Validation failure must not side-effect: rollcall must NOT be ended."""
        from exceptions import parameterMissing
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee=None, rc_id=77)
        mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            with self.assertRaises(parameterMissing):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")
        mock_end.assert_not_called()   # rollcall must survive a failed close

    async def test_subsidy_exceeds_fund_raises_without_ending_rollcall(self):
        """Validation failure must not side-effect: rollcall must NOT be ended."""
        from exceptions import incorrectParameter
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(get_fund_balance=MagicMock(return_value=0)), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            with self.assertRaises(incorrectParameter):
                await close_game(1, subsidy=100, admin_uid=1, admin_name="Admin")
        mock_end.assert_not_called()

    async def test_rc_number_out_of_range_raises(self):
        """::N pointing beyond active rollcall count must raise, not silently close ::1."""
        from exceptions import incorrectParameter
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            with self.assertRaises(incorrectParameter):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin", rc_number=2)
        mock_end.assert_not_called()


class TestCloseGameEndedPath(unittest.IsolatedAsyncioTestCase):
    """close_game when no active rollcall — uses latest ended DB rollcall."""

    async def test_uses_latest_closeable(self):
        from services.dues import close_game

        mgr = _make_manager([])   # no active rollcalls
        rc_row = {"id": 55, "title": "Last Game", "event_fee": "600",
                  "collector_uid": None, "collector_name": None,
                  "collector_paid_ground": 0}
        in_users = [{"user_id": 101, "first_name": "Alice", "proxy_name": None}]
        write_batch = MagicMock(return_value=1)

        with _patch_close(
            get_latest_closeable_rollcall=MagicMock(return_value=rc_row),
            get_rollcall_in_users=MagicMock(return_value=in_users),
            write_game_closure_batch=write_batch,
        ), patch("services.dues.manager", mgr):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["rollcall_id"], 55)
        self.assertEqual(result["per_head"], 600)
        write_batch.assert_called_once()
        self.assertEqual(len(write_batch.call_args.args[1]), 1)

    async def test_nothing_to_close_raises(self):
        from exceptions import duesNothingToClose
        from services.dues import close_game

        mgr = _make_manager([])
        with _patch_close(get_latest_closeable_rollcall=MagicMock(return_value=None)), \
             patch("services.dues.manager", mgr):
            with self.assertRaises(duesNothingToClose):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")


class TestCloseGameTargetRollcallId(unittest.IsolatedAsyncioTestCase):
    """close_game(target_rollcall_id=...) — the /settle_dues picker path,
    bypassing the latest-closeable lookup to reach a specific ended game."""

    async def test_target_rollcall_id_uses_specific_row_not_latest(self):
        from services.dues import close_game

        mgr = _make_manager([])   # no active rollcalls
        target_row = {"id": 42, "chat_id": 1, "title": "Older Game", "event_fee": "600",
                       "collector_uid": None, "collector_name": None,
                       "collector_paid_ground": 0, "collector_upi": None}
        in_users = [{"user_id": 101, "first_name": "Alice", "proxy_name": None}]
        write_batch = MagicMock(return_value=1)

        with _patch_close(
            get_rollcall=MagicMock(return_value=target_row),
            get_rollcall_in_users=MagicMock(return_value=in_users),
            write_game_closure_batch=write_batch,
        ), patch("services.dues.manager", mgr):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin",
                                      target_rollcall_id=42)

        self.assertEqual(result["rollcall_id"], 42)
        self.assertEqual(result["title"], "Older Game")
        write_batch.assert_called_once()
        self.assertEqual(len(write_batch.call_args.args[1]), 1)

    async def test_target_rollcall_id_wrong_chat_raises(self):
        from exceptions import duesNothingToClose
        from services.dues import close_game

        mgr = _make_manager([])
        other_chat_row = {"id": 42, "chat_id": 999, "title": "Not Yours"}

        with _patch_close(get_rollcall=MagicMock(return_value=other_chat_row)), \
             patch("services.dues.manager", mgr):
            with self.assertRaises(duesNothingToClose):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin",
                                 target_rollcall_id=42)

    async def test_target_rollcall_id_not_found_raises(self):
        from exceptions import duesNothingToClose
        from services.dues import close_game

        mgr = _make_manager([])
        with _patch_close(get_rollcall=MagicMock(return_value=None)), \
             patch("services.dues.manager", mgr):
            with self.assertRaises(duesNothingToClose):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin",
                                 target_rollcall_id=999)

    async def test_target_rollcall_id_ignores_active_rollcalls(self):
        """An active rollcall existing must not shadow an explicit target — the
        picker always means 'this specific ended game', never the active one."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        active_rc = _make_rc(in_list=[alice], event_fee="999", rc_id=1)
        mgr = _make_manager([active_rc])
        target_row = {"id": 42, "chat_id": 1, "title": "Older Game", "event_fee": "600",
                       "collector_uid": None, "collector_name": None,
                       "collector_paid_ground": 0, "collector_upi": None}
        in_users = [{"user_id": 101, "first_name": "Alice", "proxy_name": None}]

        with _patch_close(
            get_rollcall=MagicMock(return_value=target_row),
            get_rollcall_in_users=MagicMock(return_value=in_users),
        ), patch("services.dues.manager", mgr):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin",
                                      target_rollcall_id=42)

        self.assertEqual(result["rollcall_id"], 42)
        self.assertIsNone(result["end_result"])   # active rc must be untouched
        self.assertIsNotNone(mgr.get_rollcall(1, 0))  # still active


class TestListUnsettledGames(unittest.TestCase):

    def test_wraps_db_call(self):
        from services.dues import list_unsettled_games
        games = [{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]
        with patch("services.dues.db.get_unsettled_rollcalls", return_value=games):
            result = list_unsettled_games(1)
        self.assertEqual(result["games"], games)


class TestCloseGameDoubleClose(unittest.IsolatedAsyncioTestCase):

    async def test_double_close_raises(self):
        from exceptions import duesGameAlreadyClosed
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(
            get_game_closure=MagicMock(return_value={"id": 10, "rollcall_id": 77}),
        ), patch("services.dues.manager", mgr), \
           patch("services.rollcalls.end_rollcall", mock_end):
            with self.assertRaises(duesGameAlreadyClosed):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")


class TestCloseGameProxyAttribution(unittest.IsolatedAsyncioTestCase):

    async def test_owned_proxy_is_name_keyed_with_owner_memo(self):
        """Owned proxy → name-keyed entry (user_id=None), owner referenced in memo."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        proxy_bob = _make_proxy_user("Bob Friend")
        rc = _make_rc(
            in_list=[alice, proxy_bob],
            event_fee="600",
            rc_id=77,
            proxy_owners={"Bob Friend": 202},
        )
        mgr = _make_manager([rc])
        write_batch = MagicMock(return_value=1)
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(write_game_closure_batch=write_batch), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        entries = write_batch.call_args.args[1]
        self.assertEqual(len(entries), 2)
        # Proxy entry: user_id=None (name-keyed), owner reference in memo
        proxy_entry = next(e for e in entries if e["member_name"] == "Bob Friend")
        self.assertIsNone(proxy_entry["user_id"])                 # user_id=None
        memo = proxy_entry["memo"] or ""
        self.assertIn("owner:", memo)                             # owner reference present
        # Real user entry: user_id=101, no memo
        real_entry = next(e for e in entries if e["user_id"] == 101)
        self.assertIsNone(real_entry["memo"])

    async def test_unowned_proxy_is_name_keyed(self):
        """Unowned proxy → user_id=None, name-keyed entry."""
        from services.dues import close_game

        proxy_guest = _make_proxy_user("Walk-in Guest")
        rc = _make_rc(in_list=[proxy_guest], event_fee="600", rc_id=77, proxy_owners={})
        mgr = _make_manager([rc])
        write_batch = MagicMock(return_value=1)
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(write_game_closure_batch=write_batch), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        entries = write_batch.call_args.args[1]
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]["user_id"])
        self.assertEqual(entries[0]["member_name"], "Walk-in Guest")

    async def test_collector_paid_ground_gets_reimbursement(self):
        """When collector_paid_ground, a reimbursement credit is written."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(
            in_list=[alice], event_fee="600", rc_id=77,
            collector_uid=202, collector_name="Ravi", collector_paid_ground=1,
        )
        mgr = _make_manager([rc])
        write_batch = MagicMock(return_value=1)
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(write_game_closure_batch=write_batch), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        entries = write_batch.call_args.args[1]
        self.assertEqual(len(entries), 2)   # share + reimbursement
        reimb = next(e for e in entries if e["entry_type"] == "reimbursement")
        self.assertEqual(reimb["user_id"], 202)
        self.assertEqual(reimb["amount"], -600)

    async def test_subsidy_writes_fund_txn(self):
        """When subsidy > 0, a 'subsidy' fund transaction is written."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])
        write_batch = MagicMock(return_value=1)
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(
            get_fund_balance=MagicMock(return_value=100),
            write_game_closure_batch=write_batch,
        ), patch("services.dues.manager", mgr), \
           patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=100, admin_uid=1, admin_name="Admin")

        fund_transactions = write_batch.call_args.args[2]
        subsidy_txn = next(t for t in fund_transactions if t["txn_type"] == "subsidy")
        self.assertEqual(subsidy_txn["amount"], -100)
        self.assertEqual(result["per_head"], 500)   # (600-100)/1, step=10


# ── read-only services ────────────────────────────────────────────────────────

class TestReadServices(unittest.TestCase):

    def _patch_db(self, **overrides):
        defaults = dict(
            get_dues_balance=MagicMock(return_value=90),
            get_dues_entries=MagicMock(return_value=[{"id": 1}]),
            get_all_dues_balances=MagicMock(return_value=[{"member_name": "Alice", "balance": 90}]),
            get_fund_balance=MagicMock(return_value=55),
            get_fund_transactions=MagicMock(return_value=[{"id": 1}]),
            count_fund_transactions=MagicMock(return_value=3),
        )
        defaults.update(overrides)
        return patch.multiple("services.dues.db", **defaults)

    def test_my_dues(self):
        from services.dues import my_dues
        with self._patch_db():
            r = my_dues(1, user_id=101)
        self.assertEqual(r["balance"], 90)
        self.assertEqual(len(r["entries"]), 1)

    def test_all_dues(self):
        from services.dues import all_dues
        with self._patch_db():
            r = all_dues(1)
        self.assertEqual(len(r["balances"]), 1)

    def test_fund_summary(self):
        from services.dues import fund_summary
        with self._patch_db():
            r = fund_summary(1)
        self.assertEqual(r["fund_balance"], 55)

    def test_fund_history(self):
        from services.dues import fund_history
        with self._patch_db():
            r = fund_history(1, limit=5)
        self.assertEqual(len(r["transactions"]), 1)
        self.assertEqual(r["total"], 3)

    def test_dues_export_csv_no_name_collision_between_same_named_members(self):
        """Regression: last_entries used to be keyed by bare member_name, so
        a real user and a proxy (or two real users) sharing a display name
        would overwrite each other's 'last entry' CSV columns."""
        from services.dues import dues_export_csv

        balances = [
            {"user_id": 101, "member_name": "Sam", "balance": 50},
            {"user_id": None, "member_name": "Sam", "balance": 30},   # proxy, same name
        ]

        def _entries(chat_id, user_id=None, member_name=None, limit=1):
            if user_id == 101:
                return [{"entry_type": "share", "created_at": "2026-01-01", "amount": 50}]
            if user_id is None and member_name == "Sam":
                return [{"entry_type": "penalty", "created_at": "2026-02-02", "amount": 30}]
            return []

        with self._patch_db(
            get_all_dues_balances=MagicMock(return_value=balances),
            get_dues_entries=MagicMock(side_effect=_entries),
        ):
            csv_text = dues_export_csv(1)

        rows = csv_text.strip().splitlines()[1:]  # skip header
        self.assertEqual(len(rows), 2)
        real_row = next(r for r in rows if r.startswith("Sam,101,"))
        proxy_row = next(r for r in rows if r.startswith("Sam,,"))
        self.assertIn("share", real_row)
        self.assertIn("penalty", proxy_row)


# ── Penalties ─────────────────────────────────────────────────────────────────

class TestPenaltyTiers(unittest.TestCase):

    def _patch(self, **kw):
        defaults = dict(
            get_penalty_tiers=MagicMock(return_value=[]),
            get_penalty_tier=MagicMock(return_value=None),
            upsert_penalty_tier=MagicMock(return_value=True),
            delete_penalty_tier=MagicMock(return_value=True),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_add_tier_valid(self):
        from services.dues import add_penalty_tier
        with self._patch() as _:
            r = add_penalty_tier(1, "ditch", 200, "no-show", 99, "Admin")
        self.assertEqual(r["name"], "ditch")
        self.assertEqual(r["amount"], 200)
        self.assertIn("ditch", r["announcement"])
        self.assertIn("₹200", r["announcement"])

    def test_add_tier_zero_amount_raises(self):
        from exceptions import incorrectParameter
        from services.dues import add_penalty_tier
        with self._patch():
            with self.assertRaises(incorrectParameter):
                add_penalty_tier(1, "bad", 0, "", 99, "Admin")

    def test_add_tier_empty_name_raises(self):
        from exceptions import parameterMissing
        from services.dues import add_penalty_tier
        with self._patch():
            with self.assertRaises(parameterMissing):
                add_penalty_tier(1, "  ", 50, "", 99, "Admin")

    def test_remove_tier_existing(self):
        from services.dues import remove_penalty_tier
        with self._patch(delete_penalty_tier=MagicMock(return_value=True)):
            r = remove_penalty_tier(1, "ditch", 99, "Admin")
        self.assertIn("ditch", r["announcement"])

    def test_remove_tier_not_found_raises(self):
        from exceptions import incorrectParameter
        from services.dues import remove_penalty_tier
        with self._patch(delete_penalty_tier=MagicMock(return_value=False)):
            with self.assertRaises(incorrectParameter):
                remove_penalty_tier(1, "nonexistent", 99, "Admin")

    def test_list_tiers_formats_output(self):
        from services.dues import list_penalty_tiers
        tiers = [
            {"name": "ditch", "amount": 200, "description": "no-show"},
            {"name": "late_short", "amount": 50, "description": "under 15 min"},
        ]
        with self._patch(get_penalty_tiers=MagicMock(return_value=tiers)):
            r = list_penalty_tiers(1)
        self.assertIn("ditch", r["announcement"])
        self.assertIn("₹200", r["announcement"])

    def test_seed_defaults_only_if_empty(self):
        from services.dues import seed_default_penalty_tiers
        upsert = MagicMock()
        with self._patch(get_penalty_tiers=MagicMock(return_value=[]), upsert_penalty_tier=upsert):
            seed_default_penalty_tiers(1)
        self.assertEqual(upsert.call_count, 3)  # three defaults

    def test_seed_skips_if_tiers_exist(self):
        from services.dues import seed_default_penalty_tiers
        upsert = MagicMock()
        existing = [{"name": "ditch", "amount": 200, "description": None}]
        with self._patch(get_penalty_tiers=MagicMock(return_value=existing), upsert_penalty_tier=upsert):
            seed_default_penalty_tiers(1)
        upsert.assert_not_called()


class TestMarkPenalty(unittest.TestCase):

    def _patch(self, tier=None, **kw):
        tier = tier or {"name": "ditch", "amount": 200, "description": "no-show"}
        defaults = dict(
            get_penalty_tier=MagicMock(return_value=tier),
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 101, "first_name": "Alice", "username": "alice"}
            ]),
            add_dues_entry=MagicMock(),
            add_fund_transaction=MagicMock(),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_charges_correct_amount(self):
        from services.dues import mark_penalty
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues):
            r = mark_penalty(1, "ditch", "alice", 99, "Admin")
        self.assertEqual(r["amount"], 200)
        self.assertEqual(r["tier_name"], "ditch")

    def test_both_ledgers_written(self):
        from services.dues import mark_penalty
        add_dues = MagicMock()
        add_fund = MagicMock()
        with self._patch(add_dues_entry=add_dues, add_fund_transaction=add_fund):
            mark_penalty(1, "ditch", "alice", 99, "Admin")
        add_dues.assert_called_once()
        add_fund.assert_called_once()
        self.assertEqual(add_dues.call_args.args[4], "penalty")
        self.assertEqual(add_fund.call_args.args[2], "penalty")

    def test_unknown_tier_raises(self):
        from exceptions import incorrectParameter
        from services.dues import mark_penalty
        with self._patch(get_penalty_tier=MagicMock(return_value=None)):
            with self.assertRaises(incorrectParameter):
                mark_penalty(1, "nonexistent", "alice", 99, "Admin")

    def test_first_time_proxy_with_no_ledger_history_raises_without_known_identity(self):
        """Regression guard for the bug this fixes: a proxy with zero prior
        dues_entries (get_all_dues_balances empty) can't be found by
        _resolve_member's name-matching alone."""
        from exceptions import incorrectParameter
        from services.dues import mark_penalty
        with self._patch(get_active_members=MagicMock(return_value=[])):
            with self.assertRaises(incorrectParameter):
                mark_penalty(1, "ditch", "A1", 99, "Admin")

    def test_known_identity_bypasses_resolution_for_first_time_proxy(self):
        """The actual fix: the penalty panel already knows the concrete
        identity from the rollcall's IN list, so it should never need
        _resolve_member's name lookup at all."""
        from services.dues import mark_penalty
        add_dues = MagicMock()
        with self._patch(get_active_members=MagicMock(return_value=[]), add_dues_entry=add_dues):
            r = mark_penalty(1, "ditch", "A1", 99, "Admin", known_identity="A1")
        self.assertEqual(r["member_name"], "A1")
        add_dues.assert_called_once()
        self.assertIsNone(add_dues.call_args.args[2])   # user_id None for a proxy
        self.assertEqual(add_dues.call_args.args[3], "A1")  # member_name

    def test_known_identity_real_user_id_bypasses_resolution(self):
        from services.dues import mark_penalty
        add_dues = MagicMock()
        with self._patch(get_active_members=MagicMock(return_value=[]), add_dues_entry=add_dues):
            r = mark_penalty(1, "ditch", "Bob", 99, "Admin", known_identity=555)
        self.assertEqual(r["user_id"], 555)
        self.assertEqual(r["member_name"], "Bob")


class TestWaive(unittest.TestCase):

    def _patch(self, **kw):
        defaults = dict(
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 101, "first_name": "Alice", "username": "alice"}
            ]),
            add_dues_entry=MagicMock(),
            add_fund_transaction=MagicMock(),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_waive_writes_negative_entries(self):
        from services.dues import waive
        add_dues = MagicMock()
        add_fund = MagicMock()
        with self._patch(add_dues_entry=add_dues, add_fund_transaction=add_fund):
            r = waive(1, "alice", 75, "injury", 99, "Admin")
        self.assertEqual(r["amount"], 75)
        self.assertEqual(add_dues.call_args.args[4], "waiver")
        self.assertEqual(add_dues.call_args.args[5], -75)   # negative = credit
        self.assertEqual(add_fund.call_args.args[3], -75)

    def test_zero_amount_raises(self):
        from exceptions import incorrectParameter
        from services.dues import waive
        with self._patch():
            with self.assertRaises(incorrectParameter):
                waive(1, "alice", 0, "test", 99, "Admin")

    def test_originals_not_touched(self):
        """Waive only adds new entries — no UPDATE/DELETE calls."""
        from services.dues import waive
        update_mock = MagicMock()
        with self._patch(), patch("services.dues.db.update_rollcall", update_mock):
            waive(1, "alice", 50, "reason", 99, "Admin")
        update_mock.assert_not_called()


# ── Payments ──────────────────────────────────────────────────────────────────

class TestMarkPaid(unittest.TestCase):

    def _patch(self, balance=90, closure=None, has_been_collector=False, **kw):
        defaults = dict(
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 101, "first_name": "Alice", "username": "alice"}
            ]),
            get_dues_balance=MagicMock(return_value=balance),
            get_latest_game_closure=MagicMock(return_value=closure),
            has_ever_been_collector=MagicMock(return_value=has_been_collector),
            add_dues_entry=MagicMock(),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_admin_can_mark_paid(self):
        from services.dues import mark_paid
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues):
            r = mark_paid(1, "alice", actor_uid=99, actor_name="Admin",
                          is_admin=True)
        self.assertEqual(r["amount"], 90)
        self.assertEqual(add_dues.call_args.args[4], "payment")
        self.assertEqual(add_dues.call_args.args[5], -90)

    def test_collector_can_mark_paid(self):
        from services.dues import mark_paid
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues, has_been_collector=True):
            r = mark_paid(1, "alice", actor_uid=55, actor_name="Ravi",
                          is_admin=False)
        self.assertEqual(r["amount"], 90)

    def test_past_collector_from_older_closure_can_mark_paid(self):
        """A collector's standing doesn't expire once someone else closes the
        next game — has_ever_been_collector checks all closures, not just
        get_latest_game_closure."""
        from services.dues import mark_paid
        add_dues = MagicMock()
        # latest closure belongs to someone else; actor collected an older game
        with self._patch(add_dues_entry=add_dues,
                         closure={"collector_uid": 999, "per_head": 90},
                         has_been_collector=True):
            r = mark_paid(1, "alice", actor_uid=55, actor_name="Ravi",
                          is_admin=False)
        self.assertEqual(r["amount"], 90)

    def test_known_identity_bypasses_resolution(self):
        """The payment panel already knows the concrete identity from its own
        balances snapshot — this must skip _resolve_member entirely, so it
        works even for a member no active-member/ledger-history lookup would
        find (e.g. a fresh test double with no matching active member)."""
        from services.dues import mark_paid
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues,
                         get_active_members=MagicMock(return_value=[])):
            r = mark_paid(1, "Ghost Name", actor_uid=99, actor_name="Admin",
                          is_admin=True, known_identity=777)
        self.assertEqual(r["user_id"], 777)
        add_dues.assert_called_once()
        self.assertEqual(add_dues.call_args.args[2], 777)

    def test_known_identity_proxy_bypasses_resolution(self):
        from services.dues import mark_paid
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues,
                         get_active_members=MagicMock(return_value=[])):
            r = mark_paid(1, "A1", actor_uid=99, actor_name="Admin",
                          is_admin=True, known_identity="A1")
        self.assertIsNone(r["user_id"])
        self.assertEqual(r["member_name"], "A1")

    def test_non_admin_non_collector_denied(self):
        from exceptions import insufficientPermissions
        from services.dues import mark_paid
        with self._patch(has_been_collector=False):
            with self.assertRaises(insufficientPermissions):
                mark_paid(1, "alice", actor_uid=999, actor_name="Nobody",
                          is_admin=False)

    def test_explicit_amount_used(self):
        from services.dues import mark_paid
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues):
            r = mark_paid(1, "alice", actor_uid=99, actor_name="Admin",
                          amount=50, is_admin=True)
        self.assertEqual(r["amount"], 50)
        self.assertEqual(add_dues.call_args.args[5], -50)

    def test_overpay_allowed(self):
        """Paying more than owed is accepted — results in negative balance (credit)."""
        from services.dues import mark_paid
        add_dues = MagicMock()
        with self._patch(add_dues_entry=add_dues):
            r = mark_paid(1, "alice", actor_uid=99, actor_name="Admin",
                          amount=200, is_admin=True)
        self.assertEqual(r["amount"], 200)

    def test_zero_balance_no_amount_raises(self):
        from exceptions import incorrectParameter
        from services.dues import mark_paid
        with self._patch(balance=0):
            with self.assertRaises(incorrectParameter):
                mark_paid(1, "alice", actor_uid=99, actor_name="Admin",
                          is_admin=True)


# ── Collector ─────────────────────────────────────────────────────────────────

class TestSetCollector(unittest.TestCase):

    def _patch(self, **kw):
        defaults = dict(
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 55, "first_name": "Ravi", "username": "ravi"}
            ]),
            update_rollcall=MagicMock(return_value=True),
            update_game_closure_collector=MagicMock(return_value=True),
            get_latest_game_closure=MagicMock(return_value=None),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_pre_close_updates_rollcall(self):
        from services.dues import set_collector
        rc = _make_rc(rc_id=77)
        mgr = _make_manager([rc])
        update_rc = MagicMock(return_value=True)
        with self._patch(update_rollcall=update_rc), \
             patch("services.dues.manager", mgr):
            r = set_collector(1, "ravi", paid_ground=False, admin_uid=99, admin_name="Admin")
        self.assertEqual(r["collector_uid"], 55)
        update_rc.assert_called_once_with(
            77, collector_uid=55, collector_name="Ravi", collector_paid_ground=0
        )

    def test_post_close_updates_closure(self):
        from services.dues import set_collector
        mgr = _make_manager([])  # no active
        closure = {"rollcall_id": 88, "per_head": 90}
        update_cl = MagicMock(return_value=True)
        with self._patch(
            update_game_closure_collector=update_cl,
            get_latest_game_closure=MagicMock(return_value=closure),
        ), patch("services.dues.manager", mgr):
            r = set_collector(1, "ravi", paid_ground=True, admin_uid=99, admin_name="Admin")
        update_cl.assert_called_once_with(88, 55, "Ravi", collector_paid_ground=1, collector_upi=None)
        self.assertEqual(r["collector_paid_ground"], 1)

    def test_proxy_user_raises(self):
        """Collector must be a real Telegram user."""
        from exceptions import incorrectParameter
        from services.dues import set_collector
        mgr = _make_manager([])
        with self._patch(
            get_active_members=MagicMock(return_value=[]),
            get_all_dues_balances=MagicMock(return_value=[
                {"member_name": "Guest", "balance": 0}
            ]),
            get_latest_game_closure=MagicMock(return_value={"rollcall_id": 88}),
        ), patch("services.dues.manager", mgr):
            with self.assertRaises(incorrectParameter):
                set_collector(1, "Guest", paid_ground=False, admin_uid=99, admin_name="Admin")


class TestSetCollectorRotation(unittest.TestCase):

    def test_enable_writes_flag_1(self):
        from services.dues import set_collector_rotation
        update_mock = MagicMock()
        with patch("services.dues.db.update_chat_settings", update_mock):
            r = set_collector_rotation(1, enabled=True)
        update_mock.assert_called_once_with(1, collector_rotation=1)
        self.assertTrue(r["enabled"])
        self.assertIn("ON", r["announcement"])

    def test_disable_writes_flag_0(self):
        from services.dues import set_collector_rotation
        update_mock = MagicMock()
        with patch("services.dues.db.update_chat_settings", update_mock):
            r = set_collector_rotation(1, enabled=False)
        update_mock.assert_called_once_with(1, collector_rotation=0)
        self.assertFalse(r["enabled"])
        self.assertIn("OFF", r["announcement"])


# ── Add adhoc ─────────────────────────────────────────────────────────────────

class TestAddAdhoc(unittest.TestCase):

    def _patch(self, closure=None, **kw):
        defaults = dict(
            get_latest_game_closure=MagicMock(return_value=closure),
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 101, "first_name": "Alice", "username": "alice"}
            ]),
            add_dues_entry=MagicMock(),
            add_fund_transaction=MagicMock(),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_no_closure_raises(self):
        from exceptions import duesNothingToClose
        from services.dues import add_adhoc
        with self._patch(closure=None):
            with self.assertRaises(duesNothingToClose):
                add_adhoc(1, "alice", 99, "Admin")

    def test_charges_per_head_from_closure(self):
        from services.dues import add_adhoc
        closure = {"rollcall_id": 77, "title": "Sunday Game", "per_head": 90}
        add_dues = MagicMock()
        add_fund = MagicMock()
        with self._patch(closure=closure, add_dues_entry=add_dues, add_fund_transaction=add_fund):
            r = add_adhoc(1, "alice", 99, "Admin")
        self.assertEqual(r["per_head"], 90)
        self.assertEqual(add_dues.call_args.args[4], "adhoc")
        self.assertEqual(add_dues.call_args.args[5], 90)
        add_fund.assert_called_once()
        self.assertEqual(add_fund.call_args.args[2], "adjustment")
        self.assertEqual(add_fund.call_args.args[3], 90)


# ── Cancel game credit ────────────────────────────────────────────────────────

class TestCancelGameCredit(unittest.TestCase):

    def _patch(self, closure, entries, fund_txns=None, **kw):
        defaults = dict(
            get_game_closure=MagicMock(return_value=closure),
            get_dues_entries_for_rollcall=MagicMock(return_value=entries),
            # Targeted per-rollcall query (replaced the chat-wide limit=1000 scan)
            get_fund_transactions_for_rollcall=MagicMock(return_value=fund_txns or []),
            add_dues_entry=MagicMock(),
            add_fund_transaction=MagicMock(),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_no_closure_raises(self):
        from exceptions import incorrectParameter
        from services.dues import cancel_game_credit
        with self._patch(closure=None, entries=[]):
            with self.assertRaises(incorrectParameter):
                cancel_game_credit(1, 99, 99, "Admin")

    def test_reverses_share_and_adhoc_entries(self):
        from services.dues import cancel_game_credit
        closure = {"rollcall_id": 77, "title": "Sunday"}
        entries = [
            {"user_id": 101, "member_name": "Alice", "entry_type": "share", "amount": 90},
            {"user_id": 202, "member_name": "Ravi", "entry_type": "adhoc", "amount": 90},
            {"user_id": 101, "member_name": "Alice", "entry_type": "payment", "amount": -90},
        ]
        add_dues = MagicMock()
        with self._patch(closure=closure, entries=entries, add_dues_entry=add_dues):
            r = cancel_game_credit(1, 77, 99, "Admin")
        # Only share + adhoc reversed; payment stays
        self.assertEqual(r["reversed_count"], 2)
        self.assertEqual(add_dues.call_count, 2)
        for c in add_dues.call_args_list:
            self.assertEqual(c.args[4], "cancel_credit")
            self.assertLess(c.args[5], 0)   # negative = credit

    def test_fund_reversal_written(self):
        from services.dues import cancel_game_credit
        closure = {"rollcall_id": 77, "title": "Sunday"}
        entries = [
            {"user_id": 101, "member_name": "Alice", "entry_type": "share", "amount": 90},
        ]
        # get_fund_transactions_for_rollcall returns only rows for rollcall_id=77
        fund_txns = [
            {"rollcall_id": 77, "txn_type": "rounding", "amount": 30},
            {"rollcall_id": 77, "txn_type": "subsidy", "amount": -60},
        ]
        add_fund = MagicMock()
        with self._patch(
            closure=closure, entries=entries, fund_txns=fund_txns,
            add_fund_transaction=add_fund,
        ):
            r = cancel_game_credit(1, 77, 99, "Admin")
        # Net of rc 77 fund txns = 30 + (-60) = -30 → reversal = +30
        add_fund.assert_called_once()
        self.assertEqual(add_fund.call_args.args[3], 30)
        self.assertEqual(r["fund_reversal"], 30)

    def test_adhoc_adjustment_reversed(self):
        """add_adhoc writes txn_type='adjustment' — must be reversed on cancel."""
        from services.dues import cancel_game_credit
        closure = {"rollcall_id": 77, "title": "Sunday"}
        entries = [
            {"user_id": None, "member_name": "Guest", "entry_type": "adhoc", "amount": 90},
        ]
        fund_txns = [
            {"rollcall_id": 77, "txn_type": "adjustment", "amount": 90},
        ]
        add_fund = MagicMock()
        with self._patch(closure=closure, entries=entries, fund_txns=fund_txns,
                         add_fund_transaction=add_fund):
            r = cancel_game_credit(1, 77, 99, "Admin")
        add_fund.assert_called_once()
        self.assertEqual(add_fund.call_args.args[3], -90)  # reversed

    def test_penalty_fund_txn_not_reversed(self):
        """Penalty fund entries are independent of game cancellation and must stand."""
        from services.dues import cancel_game_credit
        closure = {"rollcall_id": 77, "title": "Sunday"}
        entries = []
        fund_txns = [
            {"rollcall_id": 77, "txn_type": "penalty", "amount": 200},
        ]
        add_fund = MagicMock()
        with self._patch(closure=closure, entries=entries, fund_txns=fund_txns,
                         add_fund_transaction=add_fund):
            cancel_game_credit(1, 77, 99, "Admin")
        add_fund.assert_not_called()

    def test_payments_remain_as_credits(self):
        """Existing payment entries are intentionally NOT reversed."""
        from services.dues import cancel_game_credit
        closure = {"rollcall_id": 77, "title": "Sunday"}
        entries = [
            {"user_id": 101, "member_name": "Alice", "entry_type": "payment", "amount": -90},
        ]
        add_dues = MagicMock()
        with self._patch(closure=closure, entries=entries, add_dues_entry=add_dues):
            r = cancel_game_credit(1, 77, 99, "Admin")
        self.assertEqual(r["reversed_count"], 0)
        add_dues.assert_not_called()


# ── Fund management ───────────────────────────────────────────────────────────

class TestFundManagement(unittest.TestCase):

    def _patch(self, balance=100, **kw):
        defaults = dict(
            add_fund_transaction=MagicMock(),
            get_fund_balance=MagicMock(return_value=balance),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_log_expense_negative_txn(self):
        from services.dues import log_expense
        add_fund = MagicMock()
        with self._patch(add_fund_transaction=add_fund):
            r = log_expense(1, 50, "new balls", 99, "Admin")
        self.assertEqual(r["amount"], 50)
        self.assertEqual(add_fund.call_args.args[3], -50)
        self.assertEqual(add_fund.call_args.args[2], "expense")

    def test_log_expense_zero_raises(self):
        from exceptions import incorrectParameter
        from services.dues import log_expense
        with self._patch():
            with self.assertRaises(incorrectParameter):
                log_expense(1, 0, "free", 99, "Admin")

    def test_fund_topup_positive_txn(self):
        from services.dues import fund_topup
        add_fund = MagicMock()
        with self._patch(add_fund_transaction=add_fund):
            r = fund_topup(1, 200, "donation", 99, "Admin")
        self.assertEqual(r["amount"], 200)
        self.assertEqual(add_fund.call_args.args[3], 200)
        self.assertEqual(add_fund.call_args.args[2], "topup")

    def test_fund_topup_zero_raises(self):
        from exceptions import incorrectParameter
        from services.dues import fund_topup
        with self._patch():
            with self.assertRaises(incorrectParameter):
                fund_topup(1, 0, "nothing", 99, "Admin")


# ── Remind dues ───────────────────────────────────────────────────────────────

class TestRemindDues(unittest.TestCase):

    def _settings(self):
        return {
            "upi_vpa": "collect@upi", "dues_round_step": 10,
            "penalty_late_t1": 50, "penalty_late_t2": 75,
            "penalty_late_t3": 100, "penalty_ditch": 200,
        }

    def test_lists_owed_members(self):
        from services.dues import remind_dues
        balances = [
            {"member_name": "Alice", "balance": 90},
            {"member_name": "Ravi", "balance": 0},
        ]
        with patch.multiple("services.dues.db",
                            get_all_dues_balances=MagicMock(return_value=balances),
                            get_or_create_chat=MagicMock(return_value=self._settings())):
            r = remind_dues(1)
        self.assertEqual(len(r["members_owed"]), 1)
        self.assertEqual(r["members_owed"][0]["member_name"], "Alice")
        self.assertIn("Alice", r["announcement"])
        self.assertIn("collect@upi", r["announcement"])

    def test_all_settled(self):
        from services.dues import remind_dues
        with patch.multiple("services.dues.db",
                            get_all_dues_balances=MagicMock(return_value=[]),
                            get_or_create_chat=MagicMock(return_value=self._settings())):
            r = remind_dues(1)
        self.assertEqual(r["members_owed"], [])
        self.assertIn("settled", r["announcement"])


# ── Treasury UPI & per-collector UPI (new feature) ───────────────────────────

class TestTreasuryUPI(unittest.TestCase):
    """set_treasury_upi service function."""

    def _patch_db(self, **kw):
        defaults = dict(
            update_chat_settings=MagicMock(),
            log_admin_action=MagicMock(),
            get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "group@upi",
                "treasury_upi": None,
                "dues_round_step": 10,
            }),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_set_treasury_upi_valid(self):
        from services.dues import set_treasury_upi
        with self._patch_db() as m:
            r = set_treasury_upi(1, "treasurer@hdfc", 99, "Admin")
        self.assertEqual(r["treasury_upi"], "treasurer@hdfc")
        self.assertIn("treasurer@hdfc", r["announcement"])
        self.assertIn("Treasury", r["announcement"])

    def test_set_treasury_upi_invalid(self):
        from exceptions import incorrectParameter
        from services.dues import set_treasury_upi
        with self._patch_db():
            with self.assertRaises(incorrectParameter):
                set_treasury_upi(1, "notaupi", 99, "Admin")

    def test_set_treasury_upi_no_at(self):
        from exceptions import incorrectParameter
        from services.dues import set_treasury_upi
        with self._patch_db():
            with self.assertRaises(incorrectParameter):
                set_treasury_upi(1, "treasure", 99, "Admin")

    def test_get_dues_settings_includes_treasury_upi(self):
        from services.dues import get_dues_settings
        with patch.multiple("services.dues.db",
            get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "group@upi",
                "treasury_upi": "treasurer@hdfc",
                "dues_round_step": 10,
                "dues_self_paid_mode": "auto",
            }),
        ):
            s = get_dues_settings(1)
        self.assertEqual(s["treasury_upi"], "treasurer@hdfc")
        self.assertEqual(s["upi_vpa"], "group@upi")


class TestCloseGameUPIAnnouncement(unittest.IsolatedAsyncioTestCase):
    """close_game announcement UPI routing: collector UPI > group UPI; treasury shown when distinct."""

    def _patch_close(self, chat_row_overrides=None, **kw):
        chat_row = {
            "upi_vpa": "group@upi",
            "treasury_upi": None,
            "dues_round_step": 10,
            "dues_self_paid_mode": "auto",
            "collector_rotation": False,
        }
        if chat_row_overrides:
            chat_row.update(chat_row_overrides)
        defaults = dict(
            get_or_create_chat=MagicMock(return_value=chat_row),
            get_game_closure=MagicMock(return_value=None),
            get_fund_balance=MagicMock(return_value=0),
            create_game_closure=MagicMock(return_value=1),
            add_dues_entry=MagicMock(),
            add_fund_transaction=MagicMock(),
            log_admin_action=MagicMock(),
            get_rollcall_in_users=MagicMock(return_value=[]),
            get_latest_closeable_rollcall=MagicMock(return_value=None),
            get_active_members=MagicMock(return_value=[]),
            update_chat_settings=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    async def _close(self, rc, mgr=None, patch_kw=None):
        from services.dues import close_game
        if mgr is None:
            mgr = _make_manager([rc])
        mock_end = AsyncMock(return_value={
            "ended": {}, "rc_number_ended_1based": 1, "ghost_eligible": False,
            "ghost_rc_db_id": None, "ended_by": {}, "remaining": [], "renumbered": [],
        })
        with self._patch_close(**(patch_kw or {})), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            return await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

    async def test_collector_upi_used_over_group_upi(self):
        """When collector_upi is set it appears in the announcement instead of group UPI."""
        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77,
                      collector_uid=202, collector_name="Ravi")
        rc.collector_upi = "ravi@ybl"
        result = await self._close(rc)
        ann = result["announcement"]
        self.assertIn("ravi@ybl", ann)
        self.assertNotIn("group@upi", ann)  # fallback not shown when collector UPI set

    async def test_falls_back_to_group_upi_when_no_collector_upi(self):
        """When no collector_upi, group upi_vpa is used."""
        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        rc.collector_upi = None
        result = await self._close(rc)
        self.assertIn("group@upi", result["announcement"])

    async def test_treasury_upi_shown_when_distinct_from_game_upi(self):
        """When treasury_upi != collector_upi both appear in the announcement."""
        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77,
                      collector_uid=202, collector_name="Ravi")
        rc.collector_upi = "ravi@ybl"
        result = await self._close(rc, patch_kw=dict(
            get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "group@upi",
                "treasury_upi": "treasurer@hdfc",
                "dues_round_step": 10,
                "dues_self_paid_mode": "auto",
                "collector_rotation": False,
            }),
        ))
        ann = result["announcement"]
        self.assertIn("ravi@ybl", ann)
        self.assertIn("treasurer@hdfc", ann)

    async def test_no_duplicate_upi_line_when_same(self):
        """When collector UPI == treasury UPI only one payment line appears."""
        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        rc.collector_upi = "shared@upi"
        result = await self._close(rc, patch_kw=dict(
            get_or_create_chat=MagicMock(return_value={
                "upi_vpa": "shared@upi",
                "treasury_upi": "shared@upi",
                "dues_round_step": 10,
                "dues_self_paid_mode": "auto",
                "collector_rotation": False,
            }),
        ))
        ann = result["announcement"]
        self.assertEqual(ann.count("shared@upi"), 1)

    async def test_no_upi_line_when_neither_set(self):
        """When neither upi_vpa nor treasury_upi is set, no UPI line in announcement."""
        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        rc.collector_upi = None
        result = await self._close(rc, patch_kw=dict(
            get_or_create_chat=MagicMock(return_value={
                "upi_vpa": None,
                "treasury_upi": None,
                "dues_round_step": 10,
                "dues_self_paid_mode": "auto",
                "collector_rotation": False,
            }),
        ))
        self.assertNotIn("💳", result["announcement"])
        self.assertNotIn("@", result["announcement"].split("📦")[0])  # no UPI before collector line


class TestSetCollectorUPI(unittest.TestCase):
    """set_collector passes collector_upi through to rollcall/closure."""

    def _patch(self, **kw):
        defaults = dict(
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 55, "first_name": "Ravi", "username": "ravi"}
            ]),
            update_rollcall=MagicMock(return_value=True),
            update_game_closure_collector=MagicMock(return_value=True),
            get_latest_game_closure=MagicMock(return_value=None),
            log_admin_action=MagicMock(),
        )
        defaults.update(kw)
        return patch.multiple("services.dues.db", **defaults)

    def test_pre_close_stores_collector_upi_in_rollcall(self):
        from services.dues import set_collector
        rc = _make_rc(rc_id=77)
        mgr = _make_manager([rc])
        update_rc = MagicMock(return_value=True)
        with self._patch(update_rollcall=update_rc), \
             patch("services.dues.manager", mgr):
            r = set_collector(1, "ravi", paid_ground=False, admin_uid=99,
                              admin_name="Admin", collector_upi="ravi@ybl")
        self.assertEqual(r["collector_upi"], "ravi@ybl")
        update_rc.assert_called_once_with(
            77, collector_uid=55, collector_name="Ravi",
            collector_paid_ground=0, collector_upi="ravi@ybl"
        )

    def test_pre_close_no_upi_kwarg_when_not_provided(self):
        """When collector_upi is None, update_rollcall is called without the kwarg."""
        from services.dues import set_collector
        rc = _make_rc(rc_id=77)
        mgr = _make_manager([rc])
        update_rc = MagicMock(return_value=True)
        with self._patch(update_rollcall=update_rc), \
             patch("services.dues.manager", mgr):
            set_collector(1, "ravi", paid_ground=False, admin_uid=99, admin_name="Admin")
        kwargs = update_rc.call_args.kwargs
        self.assertNotIn("collector_upi", kwargs)

    def test_post_close_stores_collector_upi_in_closure(self):
        from services.dues import set_collector
        mgr = _make_manager([])
        closure = {"rollcall_id": 88, "per_head": 90}
        update_cl = MagicMock(return_value=True)
        with self._patch(
            update_game_closure_collector=update_cl,
            get_latest_game_closure=MagicMock(return_value=closure),
        ), patch("services.dues.manager", mgr):
            r = set_collector(1, "ravi", paid_ground=True, admin_uid=99,
                              admin_name="Admin", collector_upi="ravi@ybl")
        update_cl.assert_called_once_with(
            88, 55, "Ravi", collector_paid_ground=1, collector_upi="ravi@ybl"
        )
        self.assertEqual(r["collector_upi"], "ravi@ybl")

    def test_announcement_includes_upi(self):
        from services.dues import set_collector
        rc = _make_rc(rc_id=77)
        mgr = _make_manager([rc])
        with self._patch(), patch("services.dues.manager", mgr):
            r = set_collector(1, "ravi", paid_ground=False, admin_uid=99,
                              admin_name="Admin", collector_upi="ravi@ybl")
        self.assertIn("ravi@ybl", r["announcement"])

    def test_announcement_no_upi_when_not_set(self):
        from services.dues import set_collector
        rc = _make_rc(rc_id=77)
        mgr = _make_manager([rc])
        with self._patch(), patch("services.dues.manager", mgr):
            r = set_collector(1, "ravi", paid_ground=False, admin_uid=99, admin_name="Admin")
        self.assertNotIn("@", r["announcement"])


class TestMarkPenaltyUPI(unittest.TestCase):
    """mark_penalty announcement includes treasury UPI with correct fallback logic."""

    def _patch_for_penalty(self, upi_vpa=None, treasury_upi=None):
        tier = {"name": "late_long", "amount": 100,
                "description": "15+ min late", "is_ditch": False}
        chat_row = {"upi_vpa": upi_vpa, "treasury_upi": treasury_upi}
        return patch.multiple("services.dues.db",
            get_penalty_tier=MagicMock(return_value=tier),
            get_all_dues_balances=MagicMock(return_value=[]),
            get_active_members=MagicMock(return_value=[
                {"user_id": 10, "first_name": "Amit", "username": "amit"}
            ]),
            add_dues_entry=MagicMock(),
            add_fund_transaction=MagicMock(),
            log_admin_action=MagicMock(),
            get_or_create_chat=MagicMock(return_value=chat_row),
        )

    def test_treasury_upi_in_announcement(self):
        from services.dues import mark_penalty
        with self._patch_for_penalty(upi_vpa="group@upi", treasury_upi="treasurer@hdfc"):
            r = mark_penalty(1, "late_long", "amit", admin_uid=99, admin_name="Admin")
        self.assertIn("treasurer@hdfc", r["announcement"])
        self.assertNotIn("group@upi", r["announcement"])

    def test_falls_back_to_upi_vpa_when_no_treasury_upi(self):
        from services.dues import mark_penalty
        with self._patch_for_penalty(upi_vpa="group@upi", treasury_upi=None):
            r = mark_penalty(1, "late_long", "amit", admin_uid=99, admin_name="Admin")
        self.assertIn("group@upi", r["announcement"])

    def test_no_upi_line_when_neither_set(self):
        from services.dues import mark_penalty
        with self._patch_for_penalty(upi_vpa=None, treasury_upi=None):
            r = mark_penalty(1, "late_long", "amit", admin_uid=99, admin_name="Admin")
        self.assertNotIn("💳", r["announcement"])


class TestNoTierNudge(unittest.TestCase):
    """Flow-audit #6: /mark_late and /mark_ditch must guide the admin when
    tier lookup fails — first-time setup steps if no tiers exist at all,
    the existing tier list if there are tiers but none matches."""

    def test_mark_late_no_tiers_at_all_gives_setup_steps(self):
        from services.dues import mark_late
        from exceptions import incorrectParameter
        with patch("services.dues.db.get_tier_for_minutes", return_value=None), \
             patch("services.dues.db.get_penalty_tiers", return_value=[]):
            with self.assertRaises(incorrectParameter) as ctx:
                mark_late(1, "amit", 20, admin_uid=99, admin_name="Admin")
        msg = str(ctx.exception)
        self.assertIn("no penalty tiers yet", msg)
        self.assertIn("/add_penalty", msg)

    def test_mark_late_with_tiers_lists_them(self):
        from services.dues import mark_late
        from exceptions import incorrectParameter
        tiers = [{"name": "late60", "amount": 100, "late_minutes_threshold": 60,
                  "is_ditch": False}]
        with patch("services.dues.db.get_tier_for_minutes", return_value=None), \
             patch("services.dues.db.get_penalty_tiers", return_value=tiers):
            with self.assertRaises(incorrectParameter) as ctx:
                mark_late(1, "amit", 20, admin_uid=99, admin_name="Admin")
        msg = str(ctx.exception)
        self.assertIn("late60", msg)
        self.assertIn("mins:60", msg)
        self.assertNotIn("no penalty tiers yet", msg)

    def test_mark_ditch_no_tiers_at_all_gives_setup_steps(self):
        from services.dues import mark_ditch
        from exceptions import incorrectParameter
        with patch("services.dues.db.get_ditch_tier", return_value=None), \
             patch("services.dues.db.get_penalty_tiers", return_value=[]):
            with self.assertRaises(incorrectParameter) as ctx:
                mark_ditch(1, "amit", admin_uid=99, admin_name="Admin")
        self.assertIn("no penalty tiers yet", str(ctx.exception))

    def test_mark_ditch_with_tiers_shows_gap(self):
        from services.dues import mark_ditch
        from exceptions import incorrectParameter
        tiers = [{"name": "late15", "amount": 50, "late_minutes_threshold": 15,
                  "is_ditch": False}]
        with patch("services.dues.db.get_ditch_tier", return_value=None), \
             patch("services.dues.db.get_penalty_tiers", return_value=tiers):
            with self.assertRaises(incorrectParameter) as ctx:
                mark_ditch(1, "amit", admin_uid=99, admin_name="Admin")
        msg = str(ctx.exception)
        self.assertIn("late15", msg)
        self.assertIn("ditch", msg)


class TestNewSeason(unittest.TestCase):
    """Season reset: compensating entries only (append-only preserved),
    fund carry/zero choice, epoch stamp, unsettled-games guardrail."""

    def _balances(self):
        return [
            {"user_id": 1, "member_name": "Alice", "balance": 150},
            {"user_id": 2, "member_name": "Bob", "balance": -40},
            {"user_id": None, "member_name": "GuestRavi", "balance": 100},
        ]

    def test_preview_blocked_by_unsettled_games(self):
        from services.dues import new_season_preview
        from exceptions import incorrectParameter
        with patch("services.dues.db.get_unsettled_rollcalls",
                   return_value=[{"id": 9, "title": "Last Sunday"}]):
            with self.assertRaises(incorrectParameter) as ctx:
                new_season_preview(1)
        self.assertIn("Last Sunday", str(ctx.exception))

    def test_preview_totals(self):
        from services.dues import new_season_preview
        with patch("services.dues.db.get_unsettled_rollcalls", return_value=[]), \
             patch("services.dues.db.get_all_dues_balances", return_value=self._balances()), \
             patch("services.dues.fund_summary", return_value={"fund_balance": 320}):
            p = new_season_preview(1)
        self.assertEqual(p["members"], 3)
        self.assertEqual(p["owed_total"], 250)   # 150 + 100
        self.assertEqual(p["credit_total"], 40)
        self.assertEqual(p["fund_balance"], 320)

    def test_reset_writes_compensating_entries_never_deletes(self):
        from services.dues import new_season
        with patch("services.dues.db.get_unsettled_rollcalls", return_value=[]), \
             patch("services.dues.db.get_all_dues_balances", return_value=self._balances()), \
             patch("services.dues.fund_summary", return_value={"fund_balance": 320}), \
             patch("services.dues.db.add_dues_entry") as add_entry, \
             patch("services.dues.db.add_fund_transaction") as add_fund, \
             patch("services.dues.db.update_chat_settings") as upd, \
             patch("services.dues.db.log_admin_action"):
            result = new_season(1, zero_fund=False, admin_uid=99, admin_name="Admin")

        # One adjustment per nonzero balance, exactly cancelling it
        amounts = {c[0][3]: c[0][5] for c in add_entry.call_args_list}  # name → amount
        self.assertEqual(add_entry.call_count, 3)
        self.assertEqual(amounts["Alice"], -150)
        self.assertEqual(amounts["Bob"], 40)
        self.assertEqual(amounts["GuestRavi"], -100)
        for c in add_entry.call_args_list:
            self.assertEqual(c[0][4], "adjustment")
        # Carry-forward: fund untouched
        add_fund.assert_not_called()
        self.assertEqual(result["fund_action"], "carried forward")
        # Epoch stamped
        self.assertIn("dues_epoch", upd.call_args.kwargs)

    def test_reset_zero_fund_writes_fund_adjustment(self):
        from services.dues import new_season
        with patch("services.dues.db.get_unsettled_rollcalls", return_value=[]), \
             patch("services.dues.db.get_all_dues_balances", return_value=[]), \
             patch("services.dues.fund_summary", return_value={"fund_balance": 320}), \
             patch("services.dues.db.add_dues_entry"), \
             patch("services.dues.db.add_fund_transaction") as add_fund, \
             patch("services.dues.db.update_chat_settings"), \
             patch("services.dues.db.log_admin_action"):
            result = new_season(1, zero_fund=True, admin_uid=99, admin_name="Admin")
        self.assertEqual(add_fund.call_args[0][3], -320)
        self.assertEqual(add_fund.call_args[0][2], "adjustment")
        self.assertEqual(result["fund_action"], "zeroed")

    def test_announcement_says_forgiven(self):
        from services.dues import new_season
        with patch("services.dues.db.get_unsettled_rollcalls", return_value=[]), \
             patch("services.dues.db.get_all_dues_balances", return_value=self._balances()), \
             patch("services.dues.fund_summary", return_value={"fund_balance": 0}), \
             patch("services.dues.db.add_dues_entry"), \
             patch("services.dues.db.add_fund_transaction"), \
             patch("services.dues.db.update_chat_settings"), \
             patch("services.dues.db.log_admin_action"):
            result = new_season(1, zero_fund=False, admin_uid=99, admin_name="Admin")
        self.assertIn("₹250", result["announcement"])
        self.assertIn("forgiven", result["announcement"])


if __name__ == "__main__":
    unittest.main()
