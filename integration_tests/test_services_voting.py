"""
Unit tests for rollCall/services/voting.py — vote_in, vote_out, vote_maybe,
and check_ghost_reconfirmation_needed.

Real SQLite + real RollCallManager + real handlers logic via service layer.
Telebot is mocked by conftest.py.
"""
import unittest

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import rollcall_manager
    from services import rollcalls as rc_svc
    from services import voting as vote_svc
    from exceptions import (
        alreadyInList,
        incorrectParameter,
        rollCallNotStarted,
    )
    return {
        "rc_svc": rc_svc,
        "vote_svc": vote_svc,
        "manager": rollcall_manager.manager,
        "alreadyInList": alreadyInList,
        "incorrectParameter": incorrectParameter,
        "rollCallNotStarted": rollCallNotStarted,
    }


CHAT_ID = -1001999000801
ALICE = {"id": 100, "name": "Alice", "username": "alice"}
BOB = {"id": 200, "name": "Bob", "username": "bob"}
CAROL = {"id": 300, "name": "Carol", "username": "carol"}
DAVE = {"id": 400, "name": "Dave", "username": "dave"}


class VotingServiceBase(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.rc_svc = env["rc_svc"]
        cls.vote_svc = env["vote_svc"]
        cls.manager = env["manager"]
        cls.alreadyInList = env["alreadyInList"]
        cls.incorrectParameter = env["incorrectParameter"]
        cls.rollCallNotStarted = env["rollCallNotStarted"]

    def setUp(self):
        reset_db()
        self.manager.clear_cache()

    async def _start(self, title="Match"):
        return await self.rc_svc.start_rollcall(
            CHAT_ID, title, ALICE["id"], ALICE["name"], ALICE["username"]
        )


class TestVoteIn(VotingServiceBase):

    async def test_vote_in_adds_user(self):
        await self._start()
        result = await self.vote_svc.vote_in(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
        )
        self.assertEqual(result["action"], "added")
        self.assertEqual(result["user"]["name"], "Bob")
        self.assertEqual(result["rc_number_1based"], 1)
        self.assertEqual(result["rollcall"]["in_count"], 1)
        self.assertIn("Bob", [u["name"] for u in result["rollcall"]["in_list"]])

    async def test_vote_in_with_comment(self):
        await self._start()
        result = await self.vote_svc.vote_in(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"], comment="running late"
        )
        self.assertEqual(result["user"]["comment"], "running late")
        self.assertEqual(result["rollcall"]["in_list"][0]["comment"], "running late")

    async def test_vote_in_twice_raises_already_in(self):
        await self._start()
        await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        with self.assertRaises(self.alreadyInList):
            await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])

    async def test_vote_in_with_no_active_rollcall_raises(self):
        with self.assertRaises(self.rollCallNotStarted):
            await self.vote_svc.vote_in(
                CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
            )

    async def test_vote_in_out_of_range_rc_raises(self):
        await self._start()
        with self.assertRaises(self.incorrectParameter):
            await self.vote_svc.vote_in(
                CHAT_ID, BOB["id"], BOB["name"], BOB["username"], rc_number=99
            )

    async def test_vote_in_past_cap_waitlists(self):
        await self._start()
        # Set cap to 2 via manager
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 2
        rc.save()
        await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        await self.vote_svc.vote_in(CHAT_ID, CAROL["id"], CAROL["name"], CAROL["username"])
        # Third user should waitlist
        result = await self.vote_svc.vote_in(
            CHAT_ID, DAVE["id"], DAVE["name"], DAVE["username"]
        )
        self.assertEqual(result["action"], "waitlisted")
        self.assertEqual(result["rollcall"]["wait_count"], 1)
        self.assertIn("Dave", [u["name"] for u in result["rollcall"]["wait_list"]])

    async def test_vote_in_with_explicit_rc_number(self):
        await self._start("First")
        await self.rc_svc.start_rollcall(CHAT_ID, "Second", ALICE["id"], ALICE["name"])
        result = await self.vote_svc.vote_in(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"], rc_number=1
        )
        self.assertEqual(result["rc_number_1based"], 2)
        self.assertEqual(result["rollcall"]["title"], "Second")


class TestVoteOut(VotingServiceBase):

    async def test_vote_out_adds_user(self):
        await self._start()
        result = await self.vote_svc.vote_out(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
        )
        self.assertEqual(result["action"], "added")
        self.assertFalse(result["was_in"])
        self.assertIsNone(result["promoted"])
        self.assertIn("Bob", [u["name"] for u in result["rollcall"]["out_list"]])

    async def test_vote_out_after_in_moves_user(self):
        await self._start()
        await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        result = await self.vote_svc.vote_out(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
        )
        self.assertEqual(result["action"], "moved")
        self.assertTrue(result["was_in"])
        self.assertNotIn("Bob", [u["name"] for u in result["rollcall"]["in_list"]])
        self.assertIn("Bob", [u["name"] for u in result["rollcall"]["out_list"]])

    async def test_vote_out_promotes_waitlister(self):
        await self._start()
        # Cap=1 so Carol waitlists; Bob's /out promotes her
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        await self.vote_svc.vote_in(CHAT_ID, CAROL["id"], CAROL["name"], CAROL["username"])
        result = await self.vote_svc.vote_out(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
        )
        self.assertEqual(result["action"], "moved")
        self.assertIsNotNone(result["promoted"])
        self.assertEqual(result["promoted"]["name"], "Carol")
        self.assertIn("Carol", [u["name"] for u in result["rollcall"]["in_list"]])
        self.assertEqual(result["rollcall"]["wait_count"], 0)

    async def test_vote_out_twice_raises_already_out(self):
        await self._start()
        await self.vote_svc.vote_out(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        with self.assertRaises(self.alreadyInList):
            await self.vote_svc.vote_out(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])


class TestVoteMaybe(VotingServiceBase):

    async def test_vote_maybe_adds_user(self):
        await self._start()
        result = await self.vote_svc.vote_maybe(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
        )
        self.assertEqual(result["action"], "added")
        self.assertFalse(result["was_in"])
        self.assertIn("Bob", [u["name"] for u in result["rollcall"]["maybe_list"]])

    async def test_vote_maybe_after_in_moves_and_promotes(self):
        await self._start()
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        await self.vote_svc.vote_in(CHAT_ID, CAROL["id"], CAROL["name"], CAROL["username"])
        result = await self.vote_svc.vote_maybe(
            CHAT_ID, BOB["id"], BOB["name"], BOB["username"]
        )
        self.assertEqual(result["action"], "moved")
        self.assertTrue(result["was_in"])
        self.assertEqual(result["promoted"]["name"], "Carol")
        self.assertIn("Bob", [u["name"] for u in result["rollcall"]["maybe_list"]])
        self.assertIn("Carol", [u["name"] for u in result["rollcall"]["in_list"]])

    async def test_vote_maybe_twice_raises_already_maybe(self):
        await self._start()
        await self.vote_svc.vote_maybe(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        with self.assertRaises(self.alreadyInList):
            await self.vote_svc.vote_maybe(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])


class TestGhostReconfCheck(VotingServiceBase):

    async def test_check_returns_not_needed_for_proxy_user(self):
        await self._start()
        result = self.vote_svc.check_ghost_reconfirmation_needed(
            CHAT_ID, "ProxyAlice"
        )
        self.assertFalse(result["needed"])

    async def test_check_returns_not_needed_when_ghost_tracking_off(self):
        await self._start()
        self.manager.set_ghost_tracking_enabled(CHAT_ID, False)
        result = self.vote_svc.check_ghost_reconfirmation_needed(
            CHAT_ID, BOB["id"]
        )
        self.assertFalse(result["needed"])

    async def test_check_returns_not_needed_under_limit(self):
        await self._start()
        # Bob has 0 ghost count, default limit 1 — not needed
        result = self.vote_svc.check_ghost_reconfirmation_needed(
            CHAT_ID, BOB["id"]
        )
        self.assertFalse(result["needed"])
        self.assertEqual(result["ghost_count"], 0)

    async def test_check_returns_needed_at_limit_for_new_voter(self):
        await self._start()
        # Simulate ghost_count by inserting into DB
        from db import increment_ghost_count
        increment_ghost_count(CHAT_ID, BOB["id"], BOB["name"])
        result = self.vote_svc.check_ghost_reconfirmation_needed(
            CHAT_ID, BOB["id"]
        )
        self.assertTrue(result["needed"])
        self.assertEqual(result["ghost_count"], 1)
        self.assertEqual(result["absent_limit"], 1)

    async def test_check_skips_prompt_when_user_already_in(self):
        await self._start()
        from db import increment_ghost_count
        increment_ghost_count(CHAT_ID, BOB["id"], BOB["name"])
        # User is already in IN list
        await self.vote_svc.vote_in(CHAT_ID, BOB["id"], BOB["name"], BOB["username"])
        result = self.vote_svc.check_ghost_reconfirmation_needed(
            CHAT_ID, BOB["id"]
        )
        self.assertFalse(result["needed"])
        self.assertTrue(result["already_in"])


if __name__ == "__main__":
    unittest.main()
