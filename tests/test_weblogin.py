"""
Functional tests for the admin-issued weblogin flow.

GET /auth/weblogin/{token} — a real browser navigation (can't send a custom
header) — used to mint the final 30-day id_token and put it directly in the
redirect URL, leaking it via server access logs. It now only PEEKS the
token (never consumes it) to resolve which group to redirect to, and hands
the still-unconsumed, single-use code to the frontend via ?weblogin_code=.

POST /auth/weblogin/redeem — new — does the actual one-time consumption and
mints the real id_token, returned in a JSON body (mirrors member_token_login
in tg_verify.py). This is what the frontend immediately calls after landing
on the redirect target.

Zero prior test coverage of this flow existed before this session.
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

PAYLOAD = {"chat_id": -100, "tg_user_id": 42, "tg_name": "Ravi"}
CHAT = {"chat_id": -100, "group_web_token": "grp123"}


def _app():
    from api.main import create_app
    return create_app()


def _client():
    return TestClient(_app(), raise_server_exceptions=False, follow_redirects=False)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWeblogInRedirectGet(unittest.TestCase):
    """GET /auth/weblogin/{token} — peeks only, never consumes."""

    def test_valid_token_redirects_with_weblogin_code_not_id_token(self):
        with patch("db.peek_web_direct_login_token", return_value=PAYLOAD) as peek, \
             patch("db.get_or_create_chat", return_value=CHAT), \
             patch("db.consume_web_direct_login_token") as consume:
            r = _client().get("/api/v1/auth/weblogin/rawcode123")
        self.assertEqual(r.status_code, 302)
        location = r.headers["location"]
        self.assertIn("weblogin_code=rawcode123", location)
        self.assertNotIn("login_token=", location)
        self.assertNotIn("id_token=", location)
        peek.assert_called_once_with("rawcode123")
        consume.assert_not_called()  # the whole point — GET must never consume

    def test_expired_or_used_token_returns_410(self):
        with patch("db.peek_web_direct_login_token", return_value=None):
            r = _client().get("/api/v1/auth/weblogin/stale")
        self.assertEqual(r.status_code, 410)

    def test_unresolvable_group_returns_404(self):
        with patch("db.peek_web_direct_login_token", return_value=PAYLOAD), \
             patch("db.get_or_create_chat", return_value={"chat_id": -100, "group_web_token": ""}):
            r = _client().get("/api/v1/auth/weblogin/rawcode123")
        self.assertEqual(r.status_code, 404)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWeblogInRedeemPost(unittest.TestCase):
    """POST /auth/weblogin/redeem — the actual single-use consumption + id_token mint."""

    def test_valid_code_consumes_and_returns_id_token(self):
        with patch("db.consume_web_direct_login_token", return_value=PAYLOAD) as consume:
            r = _client().post("/api/v1/auth/weblogin/redeem", json={"token": "rawcode123"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["chat_id"], -100)
        self.assertTrue(body["id_token"])
        # The token verifies to the same user encoded in the payload.
        from api.identity import verify_identity_token
        self.assertEqual(verify_identity_token(body["id_token"]), 42)
        consume.assert_called_once_with("rawcode123")

    def test_already_used_code_returns_410(self):
        with patch("db.consume_web_direct_login_token", return_value=None):
            r = _client().post("/api/v1/auth/weblogin/redeem", json={"token": "rawcode123"})
        self.assertEqual(r.status_code, 410)

    def test_reusing_the_same_code_twice_only_succeeds_once(self):
        # First call consumes it for real (mock simulates that by returning
        # payload once then None), proving the endpoint can't be replayed.
        with patch("db.consume_web_direct_login_token", side_effect=[PAYLOAD, None]):
            r1 = _client().post("/api/v1/auth/weblogin/redeem", json={"token": "rawcode123"})
            r2 = _client().post("/api/v1/auth/weblogin/redeem", json={"token": "rawcode123"})
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 410)

    def test_missing_token_field_returns_422(self):
        r = _client().post("/api/v1/auth/weblogin/redeem", json={})
        self.assertEqual(r.status_code, 422)
