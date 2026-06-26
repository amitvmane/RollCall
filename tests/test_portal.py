"""
Functional tests for the member portal API.

Tests cover:
  - GET /portal/groups — returns groups the user has voted in, with stats
  - GET /portal/upcoming — returns upcoming scheduled rollcalls across user's groups
  - GET /portal/groups/{chat_id}/history — returns session history for one group
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

os.environ.setdefault("TELEGRAM_TOKEN", "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY")

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def _app():
    from api.main import create_app
    return create_app()


def _client():
    return TestClient(_app(), raise_server_exceptions=False)


def _good_token(user_id):
    from api import identity
    return identity.issue_identity_token(user_id)


def _reset_rate_limit():
    from api.rate_limit import reset_buckets_for_tests
    reset_buckets_for_tests()


# ── /portal/groups ────────────────────────────────────────────────────────────

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestPortalGroups(unittest.TestCase):
    def setUp(self):
        _reset_rate_limit()

    def test_missing_id_token_returns_422(self):
        resp = _client().get("/api/v1/portal/groups")
        self.assertEqual(resp.status_code, 422)

    def test_invalid_id_token_returns_401(self):
        resp = _client().get("/api/v1/portal/groups?id_token=bad.token.here")
        self.assertEqual(resp.status_code, 401)

    def test_valid_token_empty_groups(self):
        tok = _good_token(42)
        with patch("api.routes.portal._db.get_user_voted_chats", return_value=[]), \
             patch("api.routes.portal._db.get_user_rank_in_chat", return_value=None):
            resp = _client().get(f"/api/v1/portal/groups?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tg_user_id"], 42)
        self.assertEqual(body["groups"], [])

    def test_groups_populated_with_computed_rates(self):
        tok = _good_token(99)
        fake_rows = [{
            "chat_id": -100111,
            "group_name": "Test Group",
            "timezone": "Asia/Kolkata",
            "group_web_token": "abc123",
            "current_streak": 3,
            "best_streak": 7,
            "total_voted": 8,
            "sessions_attended": 6,
            "total_sessions": 10,
            "ghost_count": 1,
        }]
        with patch("api.routes.portal._db.get_user_voted_chats", return_value=fake_rows), \
             patch("api.routes.portal._db.get_user_rank_in_chat", return_value=2):
            resp = _client().get(f"/api/v1/portal/groups?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        g = resp.json()["groups"][0]
        self.assertEqual(g["chat_id"], -100111)
        self.assertEqual(g["group_name"], "Test Group")
        self.assertEqual(g["sessions_attended"], 6)
        self.assertEqual(g["total_sessions"], 10)
        self.assertEqual(g["total_voted"], 8)
        # attendance_rate = 6/10 * 100 = 60.0
        self.assertAlmostEqual(g["attendance_rate"], 60.0)
        # voting_rate = 8/10 * 100 = 80.0
        self.assertAlmostEqual(g["voting_rate"], 80.0)
        self.assertEqual(g["current_streak"], 3)
        self.assertEqual(g["best_streak"], 7)
        self.assertEqual(g["ghost_count"], 1)
        self.assertEqual(g["rank"], 2)

    def test_zero_total_sessions_no_rate(self):
        tok = _good_token(55)
        fake_rows = [{
            "chat_id": -200,
            "group_name": "Empty Group",
            "timezone": "UTC",
            "group_web_token": None,
            "current_streak": 0,
            "best_streak": 0,
            "total_voted": 0,
            "sessions_attended": 0,
            "total_sessions": 0,
            "ghost_count": 0,
        }]
        with patch("api.routes.portal._db.get_user_voted_chats", return_value=fake_rows), \
             patch("api.routes.portal._db.get_user_rank_in_chat", return_value=None):
            resp = _client().get(f"/api/v1/portal/groups?id_token={tok}")
        g = resp.json()["groups"][0]
        self.assertIsNone(g["attendance_rate"])
        self.assertIsNone(g["voting_rate"])


# ── /portal/upcoming ─────────────────────────────────────────────────────────

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestPortalUpcoming(unittest.TestCase):
    def setUp(self):
        _reset_rate_limit()

    def test_missing_token_returns_422(self):
        resp = _client().get("/api/v1/portal/upcoming")
        self.assertEqual(resp.status_code, 422)

    def test_invalid_token_returns_401(self):
        resp = _client().get("/api/v1/portal/upcoming?id_token=x.y.z")
        self.assertEqual(resp.status_code, 401)

    def test_empty_upcoming(self):
        tok = _good_token(10)
        with patch("api.routes.portal._db.get_user_upcoming_scheduled_rollcalls", return_value=[]):
            resp = _client().get(f"/api/v1/portal/upcoming?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])

    def test_upcoming_items_returned(self):
        tok = _good_token(10)
        fake = [{
            "id": 7,
            "chat_id": -300,
            "group_name": "Cricket Group",
            "group_web_token": "tok99",
            "title": "Sunday Match",
            "scheduled_at": "2026-07-06T07:00:00Z",
        }]
        with patch("api.routes.portal._db.get_user_upcoming_scheduled_rollcalls", return_value=fake):
            resp = _client().get(f"/api/v1/portal/upcoming?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Sunday Match")
        self.assertEqual(items[0]["group_name"], "Cricket Group")
        self.assertEqual(items[0]["scheduled_at"], "2026-07-06T07:00:00Z")


# ── /portal/groups/{chat_id}/history ─────────────────────────────────────────

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestPortalHistory(unittest.TestCase):
    def setUp(self):
        _reset_rate_limit()

    def test_missing_token_returns_422(self):
        resp = _client().get("/api/v1/portal/groups/-100/history")
        self.assertEqual(resp.status_code, 422)

    def test_invalid_token_returns_401(self):
        resp = _client().get("/api/v1/portal/groups/-100/history?id_token=bad")
        self.assertEqual(resp.status_code, 401)

    def test_history_returned(self):
        tok = _good_token(20)
        fake_sessions = [
            {"id": 1, "title": "Session A", "ended_at": "2026-06-01 10:00:00", "status": "in"},
            {"id": 2, "title": "Session B", "ended_at": "2026-06-08 10:00:00", "status": "miss"},
        ]
        with patch("api.routes.portal._db.get_user_session_history", return_value=fake_sessions):
            resp = _client().get(f"/api/v1/portal/groups/-100/history?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["chat_id"], -100)
        self.assertEqual(body["tg_user_id"], 20)
        self.assertEqual(len(body["sessions"]), 2)
        self.assertEqual(body["sessions"][0]["status"], "in")
        self.assertEqual(body["sessions"][1]["status"], "miss")

    def test_limit_param_accepted(self):
        tok = _good_token(20)
        with patch("api.routes.portal._db.get_user_session_history", return_value=[]) as m:
            _client().get(f"/api/v1/portal/groups/-100/history?id_token={tok}&limit=5")
        m.assert_called_once_with(-100, 20, limit=5)

    def test_limit_above_max_rejected(self):
        tok = _good_token(20)
        resp = _client().get(f"/api/v1/portal/groups/-100/history?id_token={tok}&limit=100")
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
