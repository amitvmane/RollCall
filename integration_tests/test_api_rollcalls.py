"""
Integration tests for REST API rollcall lifecycle routes.

Uses FastAPI's TestClient against the real `api.main:app`, which means
real services + real DB. Telebot is mocked by conftest.py since none of
the API routes touch it.

This is the cross-validation guarantee: HTTP requests hit the same DB
the bot would mutate, so a bot `/src` followed by an API GET sees the
same rollcall.
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db


def _import():
    """Lazy import — conftest must have set up the mocks first."""
    import bot_state  # noqa: F401  warm conftest's mock graph
    import rollcall_manager
    from api.main import app
    from db import _hash_token, generate_api_token, insert_api_token
    return {
        "app": app,
        "manager": rollcall_manager.manager,
        "generate_api_token": generate_api_token,
        "insert_api_token": insert_api_token,
        "_hash_token": _hash_token,
    }


CHAT_ID = -1001999000701
ALICE = {"id": 100, "name": "Alice", "username": "alice"}


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
        # Reset rate-limit buckets so test order doesn't pollute counts.
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        # Issue a full-scope token for CHAT_ID and set it as the
        # client's default header so every existing test request
        # auto-authenticates. Tests that want to override (or omit)
        # auth set/clear `self.client.headers["Authorization"]`.
        from db import _hash_token, generate_api_token, insert_api_token
        token = generate_api_token()
        insert_api_token(_hash_token(token), CHAT_ID, "read,vote,admin",
                         label="test", issued_by_user_id=ALICE["id"])
        self.client.headers["Authorization"] = f"Bearer {token}"


class TestHealthRoute(APIBase):

    def test_health_returns_200(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["api_version"], "v1")
        self.assertIn("telegram_ok", body)


class TestRollcallLifecycle(APIBase):

    def _start_body(self, title="Match"):
        return {
            "title": title,
            "started_by_user_id": ALICE["id"],
            "started_by_name": ALICE["name"],
            "started_by_username": ALICE["username"],
        }

    def test_post_rollcall_creates_rollcall(self):
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("Friday Football"),
        )
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["title"], "Friday Football")
        self.assertEqual(body["number"], 1)
        self.assertEqual(body["in_count"], 0)

    def test_post_rollcall_with_none_title_uses_placeholder(self):
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={
                "title": None,
                "started_by_user_id": ALICE["id"],
                "started_by_name": ALICE["name"],
            },
        )
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["title"], "<Empty>")

    def test_post_fourth_rollcall_returns_409(self):
        for i in range(3):
            self.client.post(
                f"/api/v1/chats/{CHAT_ID}/rollcalls",
                json=self._start_body(f"RC{i}"),
            )
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("RC4"),
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "amountOfRollCallsReached")

    def test_get_rollcalls_lists_active(self):
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("A"),
        )
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("B"),
        )
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 2)
        self.assertEqual([rc["title"] for rc in body], ["A", "B"])
        self.assertEqual([rc["number"] for rc in body], [1, 2])

    def test_get_rollcalls_empty_returns_empty_list(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_get_single_rollcall(self):
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("OnlyOne"),
        )
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "OnlyOne")

    def test_get_rollcall_not_found_returns_404(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/1")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "rollCallNotStarted")

    def test_get_rollcall_out_of_range_returns_422(self):
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("Solo"),
        )
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/9")
        self.assertEqual(r.status_code, 422)

    def test_get_rollcall_invalid_number_returns_pydantic_422(self):
        # rc_number is ge=1 — 0 should fail validation
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/0")
        self.assertEqual(r.status_code, 422)

    def test_delete_rollcall_ends_it(self):
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("ToEnd"),
        )
        r = self.client.request(
            "DELETE",
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1",
            json={
                "ended_by_user_id": ALICE["id"],
                "ended_by_name": ALICE["name"],
                "ended_by_username": ALICE["username"],
            },
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["ended"]["title"], "ToEnd")
        self.assertEqual(body["rc_number_ended_1based"], 1)
        self.assertEqual(body["remaining"], [])
        # Now GET should return empty
        r2 = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls")
        self.assertEqual(r2.json(), [])

    def test_delete_renumbers_remaining(self):
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("First"),
        )
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json=self._start_body("Second"),
        )
        r = self.client.request(
            "DELETE",
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1",
            json={
                "ended_by_user_id": ALICE["id"],
                "ended_by_name": ALICE["name"],
            },
        )
        body = r.json()
        self.assertEqual(body["remaining"][0]["title"], "Second")
        self.assertEqual(body["remaining"][0]["number"], 1)
        self.assertEqual(
            body["renumbered"], [{"old": 2, "new": 1, "title": "Second"}]
        )

    def test_delete_with_no_active_returns_404(self):
        r = self.client.request(
            "DELETE",
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1",
            json={
                "ended_by_user_id": ALICE["id"],
                "ended_by_name": ALICE["name"],
            },
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "rollCallNotStarted")


class TestRequestValidation(APIBase):

    def test_post_missing_required_field_returns_pydantic_422(self):
        # missing started_by_name
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={"title": "X", "started_by_user_id": 100},
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
