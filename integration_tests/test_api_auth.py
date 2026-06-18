"""
Integration tests for REST API authentication, scopes, and rate limiting.

Drives the real FastAPI app via TestClient. Mints / revokes tokens via
db helpers. Verifies:
  - Missing header → 401
  - Bad / unknown token → 401
  - Revoked token → 401
  - Expired token → 401
  - Token with wrong scope → 403
  - Token bound to chat A used against chat B → 403
  - Health is public (no auth required)
  - Rate-limit fires at the configured threshold → 429 with Retry-After
"""
import os
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa: F401
    import rollcall_manager
    from api.main import app
    from api.rate_limit import reset_buckets_for_tests
    from db import (
        _hash_token,
        generate_api_token,
        insert_api_token,
        revoke_api_token,
    )
    return {
        "app": app,
        "manager": rollcall_manager.manager,
        "reset_buckets_for_tests": reset_buckets_for_tests,
        "_hash_token": _hash_token,
        "generate_api_token": generate_api_token,
        "insert_api_token": insert_api_token,
        "revoke_api_token": revoke_api_token,
    }


CHAT_ID = -1001999000501
OTHER_CHAT_ID = -1001999000502
ALICE = {"id": 100, "name": "Alice", "username": "alice"}


def _mint_token(chat_id: int, scopes: str, label: str = "test", expires_at=None) -> str:
    """Issue a fresh token, return the plaintext."""
    env = _import()
    token = env["generate_api_token"]()
    env["insert_api_token"](
        env["_hash_token"](token),
        chat_id,
        scopes,
        label=label,
        issued_by_user_id=ALICE["id"],
        expires_at=expires_at,
    )
    return token


class AuthBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.env = _import()
        cls.app = cls.env["app"]
        cls.manager = cls.env["manager"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        self.env["reset_buckets_for_tests"]()
        # Clear any leftover default header from other test classes
        self.client.headers.pop("Authorization", None)


class TestNoAuthHeader(AuthBase):

    def test_health_is_public(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.status_code, 200)

    def test_protected_route_without_header_returns_401(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls")
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)


class TestBadTokens(AuthBase):

    def test_unknown_token_returns_401(self):
        r = self.client.get(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            headers={"Authorization": "Bearer rc_nonexistent_token_aaaaaaaa"},
        )
        self.assertEqual(r.status_code, 401)

    def test_non_bearer_scheme_returns_401(self):
        r = self.client.get(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
        self.assertEqual(r.status_code, 401)

    def test_revoked_token_returns_401(self):
        token = _mint_token(CHAT_ID, "read,vote,admin", label="will_revoke")
        self.env["revoke_api_token"](self.env["_hash_token"](token))
        r = self.client.get(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)

    def test_expired_token_returns_401(self):
        expired_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        token = _mint_token(CHAT_ID, "read", label="expired", expires_at=expired_at)
        r = self.client.get(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 401)


class TestScopes(AuthBase):

    def test_read_token_can_get_but_not_post(self):
        token = _mint_token(CHAT_ID, "read", label="readonly")
        h = {"Authorization": f"Bearer {token}"}
        # GET is allowed
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls", headers=h)
        self.assertEqual(r.status_code, 200)
        # POST is forbidden
        r2 = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={
                "title": "X",
                "started_by_user_id": ALICE["id"],
                "started_by_name": ALICE["name"],
            },
            headers=h,
        )
        self.assertEqual(r2.status_code, 403)

    def test_vote_token_can_post_but_not_delete(self):
        token = _mint_token(CHAT_ID, "read,vote", label="voter")
        h = {"Authorization": f"Bearer {token}"}
        # Need an active rollcall first to attempt DELETE
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={
                "title": "X",
                "started_by_user_id": ALICE["id"],
                "started_by_name": ALICE["name"],
            },
            headers=h,
        )
        # DELETE requires 'admin'
        r = self.client.request(
            "DELETE",
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1",
            json={"ended_by_user_id": ALICE["id"], "ended_by_name": ALICE["name"]},
            headers=h,
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_token_implies_lower_scopes(self):
        token = _mint_token(CHAT_ID, "admin", label="god")
        h = {"Authorization": f"Bearer {token}"}
        # admin can GET (would normally need 'read')
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls", headers=h)
        self.assertEqual(r.status_code, 200)
        # admin can POST (would normally need 'vote')
        r2 = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={
                "title": "X",
                "started_by_user_id": ALICE["id"],
                "started_by_name": ALICE["name"],
            },
            headers=h,
        )
        self.assertEqual(r2.status_code, 201)


class TestCrossChatIsolation(AuthBase):

    def test_token_for_chat_A_cannot_operate_on_chat_B(self):
        token = _mint_token(CHAT_ID, "admin", label="A-only")
        h = {"Authorization": f"Bearer {token}"}
        r = self.client.get(f"/api/v1/chats/{OTHER_CHAT_ID}/rollcalls", headers=h)
        self.assertEqual(r.status_code, 403)


class TestRateLimit(AuthBase):

    def setUp(self):
        super().setUp()
        # Force a low limit for these tests; lift after each test
        self._old_max = os.environ.get("REST_API_RATE_LIMIT_MAX_REQUESTS")
        self._old_window = os.environ.get("REST_API_RATE_LIMIT_WINDOW_SECONDS")
        os.environ["REST_API_RATE_LIMIT_MAX_REQUESTS"] = "3"
        os.environ["REST_API_RATE_LIMIT_WINDOW_SECONDS"] = "60"

    def tearDown(self):
        for k, v in (
            ("REST_API_RATE_LIMIT_MAX_REQUESTS", self._old_max),
            ("REST_API_RATE_LIMIT_WINDOW_SECONDS", self._old_window),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_rate_limit_returns_429_after_threshold(self):
        token = _mint_token(CHAT_ID, "read", label="ratelim")
        h = {"Authorization": f"Bearer {token}"}
        # Within limit (3)
        for _ in range(3):
            r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls", headers=h)
            self.assertEqual(r.status_code, 200)
        # 4th request hits the cap
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls", headers=h)
        self.assertEqual(r.status_code, 429)
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.json()["error"], "rateLimitExceeded")

    def test_rate_limit_skips_health_endpoint(self):
        # Hammer /health well past the limit — should never 429
        for _ in range(10):
            r = self.client.get("/api/v1/health")
            self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
