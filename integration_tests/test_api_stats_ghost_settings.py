"""
API route tests for stats, ghost, and settings endpoints.
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa
    import rollcall_manager
    from api.main import app
    return {"app": app, "manager": rollcall_manager.manager}


CHAT_ID = -1001999000088
ADMIN = {"id": 999, "name": "Admin"}


class APIBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.manager = env["manager"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        from db import _hash_token, generate_api_token, insert_api_token
        token = generate_api_token()
        insert_api_token(_hash_token(token), CHAT_ID, "read,vote,admin",
                         label="test", issued_by_user_id=ADMIN["id"])
        self.client.headers["Authorization"] = f"Bearer {token}"


class TestStatsRoutes(APIBase):

    def test_personal_stats_returns_200(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/stats/users/200")
        self.assertEqual(r.status_code, 200)
        self.assertIn("sessions_attended", r.json())

    def test_group_stats_returns_200(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/stats/group")
        self.assertEqual(r.status_code, 200)
        self.assertIn("total_rollcalls", r.json())

    def test_leaderboard_returns_list(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/stats/leaderboard")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_history_returns_list(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/history")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_history_limit_and_offset(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/history?limit=5&offset=0")
        self.assertEqual(r.status_code, 200)


class TestGhostRoutes(APIBase):

    def test_get_ghost_settings(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/ghost/settings")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("ghost_tracking_enabled", body)
        self.assertIn("absent_limit", body)

    def test_toggle_ghost_off(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/ghost/settings/tracking",
            json={"enabled": False, "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["ghost_tracking_enabled"])

    def test_set_absent_limit(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/ghost/settings/limit",
            json={"limit": 3, "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["absent_limit"], 3)

    def test_set_absent_limit_zero_rejected(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/ghost/settings/limit",
            json={"limit": 0, "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 422)

    def test_clear_absent(self):
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/ghost/clear",
            json={"admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["cleared"])

    def test_ghost_leaderboard(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/ghost/leaderboard")
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)


class TestSettingsRoutes(APIBase):

    def test_get_chat_settings(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/settings")
        self.assertEqual(r.status_code, 200)
        self.assertIn("timezone", r.json())

    def test_set_timezone(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/settings/timezone",
            json={"timezone": "America/New_York",
                  "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["timezone"], "America/New_York")

    def test_set_timezone_invalid_422(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/settings/timezone",
            json={"timezone": "Not/Real",
                  "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 422)

    def test_set_shh_mode(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/settings/shh",
            json={"enabled": True, "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["shh_mode"])

    def _start_rollcall(self):
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={"title": "M", "started_by_user_id": ADMIN["id"],
                  "started_by_name": ADMIN["name"]}
        )

    def test_set_limit(self):
        self._start_rollcall()
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1/settings/limit",
            json={"limit": 5, "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 5)

    def test_set_location(self):
        self._start_rollcall()
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1/settings/location",
            json={"location": "Field A",
                  "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["location"], "Field A")

    def test_set_fee(self):
        self._start_rollcall()
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1/settings/fee",
            json={"fee": "250", "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["event_fee"], "250")

    def test_settings_require_admin_scope(self):
        from db import _hash_token, generate_api_token, insert_api_token
        ro = generate_api_token()
        insert_api_token(_hash_token(ro), CHAT_ID, "read",
                         label="readonly", issued_by_user_id=ADMIN["id"])
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/settings/timezone",
            json={"timezone": "Asia/Kolkata",
                  "admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]},
            headers={"Authorization": f"Bearer {ro}"}
        )
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
