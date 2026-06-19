"""
Tests for services/stats.py, services/ghost.py, and services/settings.py.
"""
import unittest

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa
    import rollcall_manager
    from services import stats as stats_svc
    from services import ghost as ghost_svc
    from services import settings as settings_svc
    from services import rollcalls as rc_svc
    from services import voting as vote_svc
    from exceptions import incorrectParameter
    return {
        "stats": stats_svc,
        "ghost": ghost_svc,
        "settings": settings_svc,
        "rc": rc_svc,
        "vote": vote_svc,
        "manager": rollcall_manager.manager,
        "incorrectParameter": incorrectParameter,
    }


CHAT_ID = -1001999000099
ADMIN = {"id": 999, "name": "Admin"}
BOB = {"id": 200, "name": "Bob", "username": "bob"}


class ServiceBase(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        for k, v in env.items():
            setattr(cls, k, v)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()


class TestStatsService(ServiceBase):

    async def test_personal_stats_empty(self):
        result = self.stats.personal_stats(CHAT_ID, BOB["id"])
        self.assertEqual(result["sessions_attended"], 0)
        self.assertEqual(result["total_rollcalls_in_chat"], 0)
        self.assertIsNone(result["attendance_rate"])

    async def test_group_stats_empty(self):
        result = self.stats.group_stats(CHAT_ID)
        self.assertEqual(result["total_rollcalls"], 0)
        self.assertEqual(result["top_attendees"], [])

    async def test_leaderboard_empty(self):
        self.assertEqual(self.stats.leaderboard(CHAT_ID), [])

    async def test_history_empty(self):
        self.assertEqual(self.stats.history(CHAT_ID), [])

    async def test_history_after_rollcall_ended(self):
        await self.rc.start_rollcall(CHAT_ID, "Match", ADMIN["id"], ADMIN["name"])
        await self.vote.vote_in(CHAT_ID, BOB["id"], BOB["name"])
        await self.rc.end_rollcall(CHAT_ID, 0, ADMIN["id"], ADMIN["name"])
        h = self.stats.history(CHAT_ID, limit=5)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["title"], "Match")
        self.assertEqual(h[0]["in_count"], 1)


class TestGhostService(ServiceBase):

    def test_get_settings_defaults(self):
        s = self.ghost.get_ghost_settings(CHAT_ID)
        self.assertTrue(s["ghost_tracking_enabled"])  # default on
        self.assertEqual(s["absent_limit"], 1)

    def test_toggle_off(self):
        s = self.ghost.toggle_ghost_tracking(CHAT_ID, False, ADMIN["id"], ADMIN["name"])
        self.assertFalse(s["ghost_tracking_enabled"])

    def test_set_absent_limit(self):
        s = self.ghost.set_absent_limit(CHAT_ID, 3, ADMIN["id"], ADMIN["name"])
        self.assertEqual(s["absent_limit"], 3)

    def test_set_absent_limit_zero_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.ghost.set_absent_limit(CHAT_ID, 0, ADMIN["id"], ADMIN["name"])

    def test_clear_absent_all(self):
        result = self.ghost.clear_absent(CHAT_ID, ADMIN["id"], ADMIN["name"])
        self.assertTrue(result["cleared"])

    def test_ghost_leaderboard_empty(self):
        self.assertEqual(self.ghost.ghost_leaderboard(CHAT_ID), [])


class TestSettingsService(ServiceBase):

    def test_get_chat_settings_defaults(self):
        s = self.settings.get_chat_settings(CHAT_ID)
        self.assertIn("timezone", s)
        self.assertIn("shh_mode", s)

    def test_set_timezone_valid(self):
        s = self.settings.set_timezone(CHAT_ID, "America/New_York",
                                       ADMIN["id"], ADMIN["name"])
        self.assertEqual(s["timezone"], "America/New_York")

    def test_set_timezone_invalid_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.settings.set_timezone(CHAT_ID, "Not/Real",
                                       ADMIN["id"], ADMIN["name"])

    def test_set_shh_mode(self):
        r = self.settings.set_shh_mode(CHAT_ID, True, ADMIN["id"], ADMIN["name"])
        self.assertTrue(r["shh_mode"])
        r2 = self.settings.set_shh_mode(CHAT_ID, False, ADMIN["id"], ADMIN["name"])
        self.assertFalse(r2["shh_mode"])

    async def test_set_rollcall_limit(self):
        await self.rc.start_rollcall(CHAT_ID, "M", ADMIN["id"], ADMIN["name"])
        rc = self.settings.set_rollcall_limit(CHAT_ID, 5, ADMIN["id"], ADMIN["name"])
        self.assertEqual(rc["limit"], 5)

    async def test_set_rollcall_limit_zero_removes_cap(self):
        await self.rc.start_rollcall(CHAT_ID, "M", ADMIN["id"], ADMIN["name"])
        self.settings.set_rollcall_limit(CHAT_ID, 5, ADMIN["id"], ADMIN["name"])
        rc = self.settings.set_rollcall_limit(CHAT_ID, 0, ADMIN["id"], ADMIN["name"])
        self.assertIsNone(rc["limit"])

    async def test_set_location(self):
        await self.rc.start_rollcall(CHAT_ID, "M", ADMIN["id"], ADMIN["name"])
        rc = self.settings.set_location(CHAT_ID, "Stadium", ADMIN["id"], ADMIN["name"])
        self.assertEqual(rc["location"], "Stadium")

    async def test_set_event_fee(self):
        await self.rc.start_rollcall(CHAT_ID, "M", ADMIN["id"], ADMIN["name"])
        rc = self.settings.set_event_fee(CHAT_ID, "500", ADMIN["id"], ADMIN["name"])
        self.assertEqual(rc["event_fee"], "500")


if __name__ == "__main__":
    unittest.main()
