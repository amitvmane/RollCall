"""
Unit tests for rollCall/services/rollcalls.py — start, end, list, get.

Service tests use the real SQLite DB + real RollCallManager + real models.
Telebot is mocked by conftest.py since services don't touch it.
"""
import unittest

from mock_helpers import reset_db


def _import():
    """Lazy import — modules become importable only after conftest mocks."""
    import bot_state  # noqa: F401  (warms up the mocked telebot graph)
    import rollcall_manager
    from services import rollcalls as rc_svc
    from exceptions import (
        amountOfRollCallsReached,
        incorrectParameter,
        rollCallNotStarted,
    )
    return {
        "rc_svc": rc_svc,
        "manager": rollcall_manager.manager,
        "amountOfRollCallsReached": amountOfRollCallsReached,
        "incorrectParameter": incorrectParameter,
        "rollCallNotStarted": rollCallNotStarted,
    }


CHAT_ID = -1001999000901
ALICE = {"id": 100, "name": "Alice", "username": "alice"}


class RollcallServiceBase(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.rc_svc = env["rc_svc"]
        cls.manager = env["manager"]
        cls.amountOfRollCallsReached = env["amountOfRollCallsReached"]
        cls.incorrectParameter = env["incorrectParameter"]
        cls.rollCallNotStarted = env["rollCallNotStarted"]

    def setUp(self):
        reset_db()
        self.manager.clear_cache()


class TestStartRollcall(RollcallServiceBase):

    async def test_start_creates_active_rollcall(self):
        result = await self.rc_svc.start_rollcall(
            CHAT_ID, "Friday Football", ALICE["id"], ALICE["name"], ALICE["username"]
        )
        self.assertEqual(result["title"], "Friday Football")
        self.assertEqual(result["number"], 1)
        self.assertEqual(result["rc_index"], 0)
        self.assertEqual(result["in_count"], 0)
        active = self.manager.get_rollcalls(CHAT_ID)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].title, "Friday Football")

    async def test_start_with_empty_title_falls_back_to_placeholder(self):
        result = await self.rc_svc.start_rollcall(
            CHAT_ID, "   ", ALICE["id"], ALICE["name"], ALICE["username"]
        )
        self.assertEqual(result["title"], "<Empty>")

    async def test_start_with_none_title_falls_back_to_placeholder(self):
        result = await self.rc_svc.start_rollcall(
            CHAT_ID, None, ALICE["id"], ALICE["name"], ALICE["username"]
        )
        self.assertEqual(result["title"], "<Empty>")

    async def test_start_assigns_sequential_rc_index(self):
        a = await self.rc_svc.start_rollcall(CHAT_ID, "Morning", ALICE["id"], ALICE["name"])
        b = await self.rc_svc.start_rollcall(CHAT_ID, "Evening", ALICE["id"], ALICE["name"])
        self.assertEqual(a["rc_index"], 0)
        self.assertEqual(a["number"], 1)
        self.assertEqual(b["rc_index"], 1)
        self.assertEqual(b["number"], 2)

    async def test_start_raises_when_three_rollcalls_active(self):
        for i in range(3):
            await self.rc_svc.start_rollcall(CHAT_ID, f"RC{i}", ALICE["id"], ALICE["name"])
        with self.assertRaises(self.amountOfRollCallsReached):
            await self.rc_svc.start_rollcall(CHAT_ID, "RC4", ALICE["id"], ALICE["name"])


class TestEndRollcall(RollcallServiceBase):

    async def asyncSetUp(self):
        await self.rc_svc.start_rollcall(CHAT_ID, "MatchA", ALICE["id"], ALICE["name"])
        await self.rc_svc.start_rollcall(CHAT_ID, "MatchB", ALICE["id"], ALICE["name"])

    async def test_end_first_rollcall_returns_snapshot(self):
        result = await self.rc_svc.end_rollcall(
            CHAT_ID, 0, ALICE["id"], ALICE["name"], ALICE["username"]
        )
        self.assertEqual(result["ended"]["title"], "MatchA")
        self.assertEqual(result["rc_number_ended_1based"], 1)
        self.assertEqual(result["ended_by"]["name"], "Alice")
        # MatchB should remain, renumbered from #2 to #1
        self.assertEqual(len(result["remaining"]), 1)
        self.assertEqual(result["remaining"][0]["title"], "MatchB")
        self.assertEqual(result["remaining"][0]["number"], 1)
        self.assertEqual(result["renumbered"],
                         [{"old": 2, "new": 1, "title": "MatchB"}])

    async def test_end_second_rollcall_leaves_first_intact(self):
        result = await self.rc_svc.end_rollcall(
            CHAT_ID, 1, ALICE["id"], ALICE["name"]
        )
        self.assertEqual(result["ended"]["title"], "MatchB")
        # MatchA still #1 — no renumbering
        self.assertEqual(result["remaining"][0]["title"], "MatchA")
        self.assertEqual(result["renumbered"], [])

    async def test_end_with_no_active_rollcall_raises(self):
        # End both first
        await self.rc_svc.end_rollcall(CHAT_ID, 0, ALICE["id"], ALICE["name"])
        await self.rc_svc.end_rollcall(CHAT_ID, 0, ALICE["id"], ALICE["name"])
        with self.assertRaises(self.rollCallNotStarted):
            await self.rc_svc.end_rollcall(CHAT_ID, 0, ALICE["id"], ALICE["name"])

    async def test_end_out_of_range_raises(self):
        with self.assertRaises(self.incorrectParameter):
            await self.rc_svc.end_rollcall(CHAT_ID, 99, ALICE["id"], ALICE["name"])

    async def test_end_removes_from_manager(self):
        await self.rc_svc.end_rollcall(CHAT_ID, 0, ALICE["id"], ALICE["name"])
        remaining = self.manager.get_rollcalls(CHAT_ID)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].title, "MatchB")

    async def test_end_with_ghost_tracking_off_returns_ghost_eligible_false(self):
        # Default: ghost tracking is ON. Disable it.
        self.manager.set_ghost_tracking_enabled(CHAT_ID, False)
        result = await self.rc_svc.end_rollcall(
            CHAT_ID, 0, ALICE["id"], ALICE["name"]
        )
        self.assertFalse(result["ghost_eligible"])


class TestListAndGet(RollcallServiceBase):

    async def test_list_empty(self):
        self.assertEqual(self.rc_svc.list_rollcalls(CHAT_ID), [])

    async def test_list_returns_in_order_with_number_index(self):
        await self.rc_svc.start_rollcall(CHAT_ID, "First", ALICE["id"], ALICE["name"])
        await self.rc_svc.start_rollcall(CHAT_ID, "Second", ALICE["id"], ALICE["name"])
        listed = self.rc_svc.list_rollcalls(CHAT_ID)
        self.assertEqual(len(listed), 2)
        self.assertEqual(listed[0]["title"], "First")
        self.assertEqual(listed[0]["number"], 1)
        self.assertEqual(listed[1]["title"], "Second")
        self.assertEqual(listed[1]["number"], 2)

    async def test_get_default_first(self):
        await self.rc_svc.start_rollcall(CHAT_ID, "Solo", ALICE["id"], ALICE["name"])
        result = self.rc_svc.get_rollcall(CHAT_ID)
        self.assertEqual(result["title"], "Solo")
        self.assertEqual(result["number"], 1)

    async def test_get_with_explicit_rc_number(self):
        await self.rc_svc.start_rollcall(CHAT_ID, "First", ALICE["id"], ALICE["name"])
        await self.rc_svc.start_rollcall(CHAT_ID, "Second", ALICE["id"], ALICE["name"])
        result = self.rc_svc.get_rollcall(CHAT_ID, rc_number=1)
        self.assertEqual(result["title"], "Second")

    async def test_get_with_no_active_raises(self):
        with self.assertRaises(self.rollCallNotStarted):
            self.rc_svc.get_rollcall(CHAT_ID)

    async def test_get_out_of_range_raises(self):
        await self.rc_svc.start_rollcall(CHAT_ID, "Only", ALICE["id"], ALICE["name"])
        with self.assertRaises(self.incorrectParameter):
            self.rc_svc.get_rollcall(CHAT_ID, rc_number=5)


if __name__ == "__main__":
    unittest.main()
