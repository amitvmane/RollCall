"""
Unit tests for rollCall/services/proxy.py — set_in_for, set_out_for,
set_maybe_for, check_proxy_ghost_reconfirmation_needed.

Real SQLite + real RollCallManager. Telebot mocked by conftest.
"""
import unittest

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import rollcall_manager
    from services import proxy as proxy_svc
    from services import rollcalls as rc_svc
    from services import voting as vote_svc
    from exceptions import (
        duplicateProxy,
        incorrectParameter,
        parameterMissing,
        repeatlyName,
        rollCallNotStarted,
    )
    return {
        "proxy_svc": proxy_svc,
        "rc_svc": rc_svc,
        "vote_svc": vote_svc,
        "manager": rollcall_manager.manager,
        "duplicateProxy": duplicateProxy,
        "incorrectParameter": incorrectParameter,
        "parameterMissing": parameterMissing,
        "repeatlyName": repeatlyName,
        "rollCallNotStarted": rollCallNotStarted,
    }


CHAT_ID = -1001999000401
ADMIN = {"id": 999, "name": "Admin"}
BOB = {"id": 200, "name": "Bob", "username": "bob"}


class ProxyBase(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.proxy = env["proxy_svc"]
        cls.rc = env["rc_svc"]
        cls.vote = env["vote_svc"]
        cls.manager = env["manager"]
        cls.duplicateProxy = env["duplicateProxy"]
        cls.incorrectParameter = env["incorrectParameter"]
        cls.parameterMissing = env["parameterMissing"]
        cls.repeatlyName = env["repeatlyName"]
        cls.rollCallNotStarted = env["rollCallNotStarted"]

    def setUp(self):
        reset_db()
        self.manager.clear_cache()

    async def _start(self, title="Match"):
        return await self.rc.start_rollcall(
            CHAT_ID, title, ADMIN["id"], ADMIN["name"]
        )


class TestSetInFor(ProxyBase):

    async def test_set_in_for_adds_proxy(self):
        await self._start()
        result = await self.proxy.set_in_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
        )
        self.assertEqual(result["action"], "added")
        self.assertEqual(result["user"]["name"], "Alex")
        self.assertTrue(result["user"]["is_proxy"])
        self.assertEqual(result["proxy_owner_id"], ADMIN["id"])
        in_names = [u["name"] for u in result["rollcall"]["in_list"]]
        self.assertIn("Alex", in_names)

    async def test_set_in_for_with_comment(self):
        await self._start()
        result = await self.proxy.set_in_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex", comment="bringing snacks"
        )
        self.assertEqual(result["user"]["comment"], "bringing snacks")

    async def test_set_in_for_duplicate_raises(self):
        await self._start()
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex")
        with self.assertRaises(self.duplicateProxy):
            await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex")

    async def test_set_in_for_past_cap_waitlists(self):
        await self._start()
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "First")
        result = await self.proxy.set_in_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Second"
        )
        self.assertEqual(result["action"], "waitlisted")
        wait_names = [u["name"] for u in result["rollcall"]["wait_list"]]
        self.assertIn("Second", wait_names)

    async def test_set_in_for_empty_name_raises(self):
        await self._start()
        with self.assertRaises(self.parameterMissing):
            await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "  ")

    async def test_set_in_for_long_name_raises(self):
        await self._start()
        with self.assertRaises(self.parameterMissing):
            await self.proxy.set_in_for(
                CHAT_ID, ADMIN["id"], ADMIN["name"], "X" * 60
            )

    async def test_set_in_for_with_no_active_rollcall(self):
        with self.assertRaises(self.rollCallNotStarted):
            await self.proxy.set_in_for(
                CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
            )


class TestSetOutFor(ProxyBase):

    async def test_set_out_for_fresh_adds_to_out(self):
        await self._start()
        result = await self.proxy.set_out_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
        )
        self.assertEqual(result["action"], "added")
        self.assertFalse(result["was_in"])
        self.assertIsNone(result["promoted"])
        out_names = [u["name"] for u in result["rollcall"]["out_list"]]
        self.assertIn("Alex", out_names)

    async def test_set_out_for_moves_in_proxy(self):
        await self._start()
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex")
        result = await self.proxy.set_out_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
        )
        self.assertEqual(result["action"], "moved")
        self.assertTrue(result["was_in"])

    async def test_set_out_for_promotes_waitlister(self):
        await self._start()
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex")
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Brian")
        # Brian was waitlisted; now Alex /out should promote Brian
        result = await self.proxy.set_out_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
        )
        self.assertEqual(result["action"], "moved")
        self.assertIsNotNone(result["promoted"])
        self.assertEqual(result["promoted"]["name"], "Brian")
        in_names = [u["name"] for u in result["rollcall"]["in_list"]]
        self.assertIn("Brian", in_names)


class TestSetMaybeFor(ProxyBase):

    async def test_set_maybe_for_adds_proxy(self):
        await self._start()
        result = await self.proxy.set_maybe_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
        )
        self.assertEqual(result["action"], "added")
        maybe_names = [u["name"] for u in result["rollcall"]["maybe_list"]]
        self.assertIn("Alex", maybe_names)

    async def test_set_maybe_for_promotes_waitlister_after_in(self):
        await self._start()
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex")
        await self.proxy.set_in_for(CHAT_ID, ADMIN["id"], ADMIN["name"], "Brian")
        result = await self.proxy.set_maybe_for(
            CHAT_ID, ADMIN["id"], ADMIN["name"], "Alex"
        )
        self.assertEqual(result["promoted"]["name"], "Brian")


class TestProxyGhostCheck(ProxyBase):

    async def test_check_returns_not_needed_when_tracking_off(self):
        await self._start()
        self.manager.set_ghost_tracking_enabled(CHAT_ID, False)
        result = self.proxy.check_proxy_ghost_reconfirmation_needed(
            CHAT_ID, "Alex"
        )
        self.assertFalse(result["needed"])

    async def test_check_returns_not_needed_for_new_proxy(self):
        await self._start()
        result = self.proxy.check_proxy_ghost_reconfirmation_needed(
            CHAT_ID, "FreshProxy"
        )
        self.assertFalse(result["needed"])

    async def test_check_returns_needed_at_limit(self):
        await self._start()
        from db import increment_ghost_count
        # increment_ghost_count accepts a proxy_name kwarg; user_id is
        # ignored when proxy_name is set (db routes the increment to the
        # proxy ghost-record).
        increment_ghost_count(CHAT_ID, 0, "RepeatGhost", proxy_name="RepeatGhost")
        result = self.proxy.check_proxy_ghost_reconfirmation_needed(
            CHAT_ID, "RepeatGhost"
        )
        self.assertTrue(result["needed"])
        self.assertEqual(result["ghost_count"], 1)


if __name__ == "__main__":
    unittest.main()
