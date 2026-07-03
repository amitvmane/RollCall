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
                "penalty_late_t1": 50,
                "penalty_late_t2": 75,
                "penalty_late_t3": 100,
                "penalty_ditch": 200,
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

    def test_set_penalty_tiers_valid(self):
        from services.dues import set_penalty_tiers
        with self._patch_db():
            r = set_penalty_tiers(1, 50, 75, 100, 200, 99, "Admin")
        self.assertEqual(r["penalty_late_t1"], 50)

    def test_set_penalty_tiers_invalid_order(self):
        from exceptions import incorrectParameter
        from services.dues import set_penalty_tiers
        with self._patch_db():
            with self.assertRaises(incorrectParameter):
                set_penalty_tiers(1, 100, 75, 50, 200, 99, "Admin")  # t1 > t2

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
        self.assertEqual(s["penalty_ditch"], 200)


# ── close_game ───────────────────────────────────────────────────────────────

_CLOSE_GAME_DB_DEFAULTS = dict(
    get_or_create_chat=MagicMock(return_value={
        "upi_vpa": None,
        "dues_round_step": 10,
        "penalty_late_t1": 50,
        "penalty_late_t2": 75,
        "penalty_late_t3": 100,
        "penalty_ditch": 200,
    }),
    get_game_closure=MagicMock(return_value=None),   # not yet closed
    get_fund_balance=MagicMock(return_value=0),
    create_game_closure=MagicMock(return_value=1),
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

        add_dues_entry = MagicMock()
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(add_dues_entry=add_dues_entry), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["in_count"], 1)
        self.assertEqual(result["per_head"], 600)   # 600/1 → step 10 → 600
        self.assertEqual(result["remainder"], 0)
        add_dues_entry.assert_called_once()
        call_args = add_dues_entry.call_args
        self.assertEqual(call_args.args[2], 101)    # user_id
        self.assertEqual(call_args.args[5], 600)    # amount

    async def test_active_rc_7_players_step10(self):
        from services.dues import close_game

        users = [_make_user(f"P{i}", user_id=100 + i) for i in range(7)]
        rc = _make_rc(in_list=users, event_fee="600", rc_id=88)
        mgr = _make_manager([rc])

        add_dues_entry = MagicMock()
        add_fund_transaction = MagicMock()
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(add_dues_entry=add_dues_entry, add_fund_transaction=add_fund_transaction), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["per_head"], 90)
        self.assertEqual(result["remainder"], 30)
        self.assertEqual(add_dues_entry.call_count, 7)
        add_fund_transaction.assert_called_once()
        fund_call = add_fund_transaction.call_args
        self.assertEqual(fund_call.args[3], 30)   # amount = remainder

    async def test_no_event_fee_raises(self):
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

    async def test_subsidy_exceeds_fund_raises(self):
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


class TestCloseGameEndedPath(unittest.IsolatedAsyncioTestCase):
    """close_game when no active rollcall — uses latest ended DB rollcall."""

    async def test_uses_latest_closeable(self):
        from services.dues import close_game

        mgr = _make_manager([])   # no active rollcalls
        rc_row = {"id": 55, "title": "Last Game", "event_fee": "600",
                  "collector_uid": None, "collector_name": None,
                  "collector_paid_ground": 0}
        in_users = [{"user_id": 101, "first_name": "Alice", "proxy_name": None}]
        add_dues_entry = MagicMock()

        with _patch_close(
            get_latest_closeable_rollcall=MagicMock(return_value=rc_row),
            get_rollcall_in_users=MagicMock(return_value=in_users),
            add_dues_entry=add_dues_entry,
        ), patch("services.dues.manager", mgr):
            result = await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        self.assertEqual(result["rollcall_id"], 55)
        self.assertEqual(result["per_head"], 600)
        add_dues_entry.assert_called_once()

    async def test_nothing_to_close_raises(self):
        from exceptions import duesNothingToClose
        from services.dues import close_game

        mgr = _make_manager([])
        with _patch_close(get_latest_closeable_rollcall=MagicMock(return_value=None)), \
             patch("services.dues.manager", mgr):
            with self.assertRaises(duesNothingToClose):
                await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")


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

    async def test_owned_proxy_charges_owner(self):
        """Owned proxy → share entry keyed on owner's user_id."""
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
        add_dues_entry = MagicMock()
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(add_dues_entry=add_dues_entry), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        calls = add_dues_entry.call_args_list
        self.assertEqual(len(calls), 2)
        proxy_call = next(c for c in calls if "proxy: Bob Friend" in (c.args[6] or ""))
        self.assertEqual(proxy_call.args[2], 202)   # owner user_id
        real_call = next(c for c in calls if c.args[2] == 101)
        self.assertIsNone(real_call.args[6])          # no memo

    async def test_unowned_proxy_is_name_keyed(self):
        """Unowned proxy → user_id=None, name-keyed entry."""
        from services.dues import close_game

        proxy_guest = _make_proxy_user("Walk-in Guest")
        rc = _make_rc(in_list=[proxy_guest], event_fee="600", rc_id=77, proxy_owners={})
        mgr = _make_manager([rc])
        add_dues_entry = MagicMock()
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(add_dues_entry=add_dues_entry), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        calls = add_dues_entry.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].args[2])
        self.assertEqual(calls[0].args[3], "Walk-in Guest")

    async def test_collector_paid_ground_gets_reimbursement(self):
        """When collector_paid_ground, a reimbursement credit is written."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(
            in_list=[alice], event_fee="600", rc_id=77,
            collector_uid=202, collector_name="Ravi", collector_paid_ground=1,
        )
        mgr = _make_manager([rc])
        add_dues_entry = MagicMock()
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(add_dues_entry=add_dues_entry), \
             patch("services.dues.manager", mgr), \
             patch("services.rollcalls.end_rollcall", mock_end):
            await close_game(1, subsidy=0, admin_uid=1, admin_name="Admin")

        calls = add_dues_entry.call_args_list
        self.assertEqual(len(calls), 2)   # share + reimbursement
        reimb = next(c for c in calls if c.args[4] == "reimbursement")
        self.assertEqual(reimb.args[2], 202)
        self.assertEqual(reimb.args[5], -600)

    async def test_subsidy_writes_fund_txn(self):
        """When subsidy > 0, a 'subsidy' fund transaction is written."""
        from services.dues import close_game

        alice = _make_user("Alice", user_id=101)
        rc = _make_rc(in_list=[alice], event_fee="600", rc_id=77)
        mgr = _make_manager([rc])
        add_fund_transaction = MagicMock()
        mock_end = AsyncMock(return_value=_end_result())

        with _patch_close(
            get_fund_balance=MagicMock(return_value=100),
            add_fund_transaction=add_fund_transaction,
        ), patch("services.dues.manager", mgr), \
           patch("services.rollcalls.end_rollcall", mock_end):
            result = await close_game(1, subsidy=100, admin_uid=1, admin_name="Admin")

        fund_calls = add_fund_transaction.call_args_list
        subsidy_txn = next(c for c in fund_calls if c.args[2] == "subsidy")
        self.assertEqual(subsidy_txn.args[3], -100)
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


if __name__ == "__main__":
    unittest.main()
