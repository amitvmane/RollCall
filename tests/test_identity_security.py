"""
Security regression tests for the signed-identity model.

These lock in the fixes for three client-asserted-identity vulnerabilities:

  1. Portal IDOR — reading any user's cross-group attendance by supplying a
     raw tg_user_id. Now requires a signed id_token.
  2. Vote attribution forgery — recording a real-user (non-proxy) vote for an
     arbitrary tg_user_id via the magic link. Now only a valid id_token
     attributes a vote to a real account; otherwise it's a name-only proxy.
  3. Web-admin bypass — starting a rollcall by passing a web admin's raw
     user id. Now requires a signed id_token resolving to that admin.

The identity token is HMAC-signed with a key derived from the bot token, so
tests set TELEGRAM_TOKEN before importing/using the identity module.
"""
import os
import sys
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# identity token primitive
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestIdentityToken(unittest.TestCase):
    def setUp(self):
        from api import identity
        self.identity = identity

    def test_roundtrip(self):
        tok = self.identity.issue_identity_token(4242)
        self.assertEqual(self.identity.verify_identity_token(tok), 4242)

    def test_none_and_garbage_reject(self):
        for bad in (None, "", "not-a-token", "a.b.c", "1.2", "1.2.3.4"):
            self.assertIsNone(self.identity.verify_identity_token(bad))

    def test_tampered_user_id_rejected(self):
        tok = self.identity.issue_identity_token(100)
        _, exp, sig = tok.split(".")
        forged = f"999.{exp}.{sig}"  # swap the user id, keep the signature
        self.assertIsNone(self.identity.verify_identity_token(forged))

    def test_expired_rejected(self):
        tok = self.identity.issue_identity_token(7, ttl_seconds=-1)
        self.assertIsNone(self.identity.verify_identity_token(tok))

    def test_different_bot_token_cannot_forge(self):
        tok = self.identity.issue_identity_token(55)
        orig = os.environ.get("TELEGRAM_TOKEN")
        os.environ["TELEGRAM_TOKEN"] = "999:OTHER_BOT_TOKEN"
        try:
            self.assertIsNone(self.identity.verify_identity_token(tok))
        finally:
            os.environ["TELEGRAM_TOKEN"] = orig


def _good_token(user_id):
    from api import identity
    return identity.issue_identity_token(user_id)


# ---------------------------------------------------------------------------
# Portal IDOR
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestPortalRequiresIdentity(unittest.TestCase):
    def test_raw_user_id_no_longer_accepted(self):
        # Old attack: ?tg_user_id=<victim>. The param is gone; id_token is
        # required, so the request is rejected as malformed (422).
        resp = _client().get("/api/v1/portal/groups?tg_user_id=12345")
        self.assertEqual(resp.status_code, 422)

    def test_invalid_id_token_unauthorized(self):
        resp = _client().get("/api/v1/portal/groups?id_token=forged.0.deadbeef")
        self.assertEqual(resp.status_code, 401)

    def test_valid_id_token_resolves_to_signed_user(self):
        tok = _good_token(777)
        with patch("api.routes.portal._db.get_user_voted_chats", return_value=[]) as m:
            resp = _client().get(f"/api/v1/portal/groups?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        # The user id used for the lookup comes from the signature, not the URL.
        m.assert_called_once_with(777)

    def test_history_requires_identity(self):
        resp = _client().get("/api/v1/portal/groups/-100/history?tg_user_id=12345")
        self.assertEqual(resp.status_code, 422)


# ---------------------------------------------------------------------------
# Vote attribution forgery
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestVoteAttribution(unittest.TestCase):
    def _post(self, body):
        mock_fn = AsyncMock(return_value={
            "rollcall_id": 1, "web_token": "t", "title": "x",
            "finalize_date": None, "limit": None, "location": None,
            "in": [], "out": [], "maybe": [], "waiting": [],
        })
        with patch("services.web.vote_by_token", mock_fn):
            resp = _client().post("/api/v1/web/t/vote", json=body)
        return resp, mock_fn

    def test_no_token_records_proxy_only(self):
        # No id_token → tg_user_id=None → service records a name-only proxy.
        resp, mock_fn = self._post({"name": "Alice", "vote": "in"})
        self.assertEqual(resp.status_code, 200)
        _, kwargs = mock_fn.await_args
        self.assertIsNone(kwargs["tg_user_id"])

    def test_forged_token_not_attributed(self):
        resp, mock_fn = self._post({"name": "Alice", "vote": "in",
                                    "id_token": "1.99999999999.deadbeef"})
        self.assertEqual(resp.status_code, 200)
        _, kwargs = mock_fn.await_args
        self.assertIsNone(kwargs["tg_user_id"])

    def test_valid_token_attributes_to_signed_user(self):
        tok = _good_token(321)
        resp, mock_fn = self._post({"name": "Alice", "vote": "in", "id_token": tok})
        self.assertEqual(resp.status_code, 200)
        _, kwargs = mock_fn.await_args
        self.assertEqual(kwargs["tg_user_id"], 321)


# ---------------------------------------------------------------------------
# Web-admin bypass
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebAdminRequiresIdentity(unittest.TestCase):
    def test_start_rollcall_without_token_unauthorized(self):
        with patch("api.routes.web._db.get_chat_by_group_web_token", return_value={"chat_id": -100}):
            resp = _client().post(
                "/api/v1/web/group/grouptok/start-rollcall",
                json={"id_token": "forged.0.bad", "title": "Game"},
            )
        self.assertEqual(resp.status_code, 401)

    def test_start_rollcall_non_admin_forbidden(self):
        tok = _good_token(500)
        with patch("api.routes.web._db.get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web._db.is_web_admin", return_value=False) as is_admin:
            resp = _client().post(
                "/api/v1/web/group/grouptok/start-rollcall",
                json={"id_token": tok, "title": "Game"},
            )
        self.assertEqual(resp.status_code, 403)
        # The admin check used the signed user id, not anything client-supplied.
        is_admin.assert_called_once_with(-100, 500)

    def test_admin_status_ignores_raw_user_id(self):
        # A bare numeric id can never report is_admin=True.
        with patch("api.routes.web._db.get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web._db.is_web_admin", return_value=True):
            resp = _client().get(
                "/api/v1/web/group/grouptok/admin-status?id_token=12345"
            )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["is_admin"])

    def test_admin_status_true_for_signed_admin(self):
        tok = _good_token(600)
        with patch("api.routes.web._db.get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web._db.is_web_admin", return_value=True):
            resp = _client().get(
                f"/api/v1/web/group/grouptok/admin-status?id_token={tok}"
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["is_admin"])


if __name__ == "__main__":
    unittest.main()
