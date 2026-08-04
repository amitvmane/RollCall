"""
Tests for the Telegram Login Widget auth flow.

Covers the pure HMAC verification (_verify_login_widget) and the
POST /auth/tg-login + GET /auth/tg-login/config endpoints.

Login Widget crypto differs from Mini App initData:
  secret_key = SHA256(bot_token)          (plain digest)
vs
  secret_key = HMAC("WebAppData", token)  (Mini App)
"""
import hashlib
import hmac
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


def _make_login_payload(user_id=111, first_name="Alice", username="alice",
                        last_name=None, photo_url=None, age_seconds=60,
                        bot_token=BOT_TOKEN):
    """Build a valid-HMAC Login Widget payload."""
    fields = {
        "id": user_id,
        "first_name": first_name,
        "auth_date": int(time.time()) - age_seconds,
    }
    if username is not None:
        fields["username"] = username
    if last_name is not None:
        fields["last_name"] = last_name
    if photo_url is not None:
        fields["photo_url"] = photo_url

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    fields["hash"] = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    return fields


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestVerifyLoginWidget(unittest.TestCase):
    """Pure-function verification without HTTP or DB."""

    def _call(self, payload, bot_token=BOT_TOKEN, **kw):
        from api.routes.tg_verify import _verify_login_widget
        return _verify_login_widget(payload, bot_token, **kw)

    def test_valid_payload_passes(self):
        result = self._call(_make_login_payload())
        self.assertEqual(result["id"], 111)
        self.assertEqual(result["first_name"], "Alice")

    def test_valid_payload_with_all_optional_fields(self):
        p = _make_login_payload(last_name="Smith", photo_url="https://t.me/p.jpg")
        result = self._call(p)
        self.assertEqual(result["last_name"], "Smith")

    def test_tampered_hash_rejected(self):
        p = _make_login_payload()
        p["hash"] = "0" * 64
        with self.assertRaises(ValueError):
            self._call(p)

    def test_tampered_user_id_rejected(self):
        p = _make_login_payload(user_id=111)
        p["id"] = 999  # impersonation attempt after signing
        with self.assertRaises(ValueError):
            self._call(p)

    def test_wrong_bot_token_rejected(self):
        p = _make_login_payload(bot_token="999:OTHER_BOT")
        with self.assertRaises(ValueError):
            self._call(p, bot_token=BOT_TOKEN)

    def test_missing_hash_rejected(self):
        p = _make_login_payload()
        del p["hash"]
        with self.assertRaises(ValueError):
            self._call(p)

    def test_stale_auth_date_rejected(self):
        p = _make_login_payload(age_seconds=7200)  # 2h old, max 1h
        with self.assertRaises(ValueError):
            self._call(p)

    def test_none_fields_excluded_from_check_string(self):
        # A payload signed without last_name must verify even when the request
        # model carries last_name=None (model_dump includes it as None).
        p = _make_login_payload()
        p["last_name"] = None
        p["photo_url"] = None
        result = self._call(p)
        self.assertEqual(result["id"], 111)

    def test_miniapp_style_key_rejected(self):
        # A payload signed with the Mini App HMAC("WebAppData") derivation must
        # NOT verify against the Login Widget's SHA256(token) derivation.
        fields = {"id": 111, "first_name": "Alice",
                  "auth_date": int(time.time()) - 60}
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        wrong_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(wrong_key, data_check.encode(), hashlib.sha256).hexdigest()
        with self.assertRaises(ValueError):
            self._call(fields)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestTgLoginEndpoints(unittest.TestCase):

    def _client(self):
        from api.main import create_app
        return TestClient(create_app(), raise_server_exceptions=False)

    def test_login_valid_payload_returns_id_token(self):
        p = _make_login_payload(user_id=222, first_name="Bob", username="bob")
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            res = self._client().post("/api/v1/auth/tg-login", json=p)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["verified"])
        self.assertEqual(body["user_id"], 222)
        self.assertEqual(body["name"], "Bob")
        self.assertTrue(body["id_token"])
        # id_token must verify back to the same user
        from api.identity import verify_identity_token
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            self.assertEqual(verify_identity_token(body["id_token"]), 222)

    def test_login_name_includes_last_name(self):
        p = _make_login_payload(first_name="Bob", last_name="Jones")
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            res = self._client().post("/api/v1/auth/tg-login", json=p)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "Bob Jones")

    def test_login_forged_hash_401(self):
        p = _make_login_payload()
        p["hash"] = "f" * 64
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            res = self._client().post("/api/v1/auth/tg-login", json=p)
        self.assertEqual(res.status_code, 401)

    def test_login_stale_payload_401(self):
        p = _make_login_payload(age_seconds=7200)
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            res = self._client().post("/api/v1/auth/tg-login", json=p)
        self.assertEqual(res.status_code, 401)

    def test_login_missing_required_field_422(self):
        p = _make_login_payload()
        del p["first_name"]
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            res = self._client().post("/api/v1/auth/tg-login", json=p)
        self.assertEqual(res.status_code, 422)

    def test_config_returns_bot_username(self):
        with patch("api.routes.tg_verify._bot_username", return_value="rollcall_bot"):
            res = self._client().get("/api/v1/auth/tg-login/config")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["bot_username"], "rollcall_bot")

    def test_config_503_when_bot_not_connected(self):
        with patch("api.routes.tg_verify._bot_username", return_value=""):
            res = self._client().get("/api/v1/auth/tg-login/config")
        self.assertEqual(res.status_code, 503)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestBotUsernameFallback(unittest.TestCase):
    """_bot_username() must survive a restart-during-outage: if the
    in-memory Telegram status was never populated (get_me() never ran),
    it should fall back to the persisted system_config value instead of
    permanently blocking sign-in until the bot reconnects."""

    def setUp(self):
        from api.routes.tg_verify import _telegram_status
        self._status = _telegram_status
        self._orig = dict(_telegram_status)

    def tearDown(self):
        self._status.clear()
        self._status.update(self._orig)

    def test_uses_live_status_when_present(self):
        self._status["bot_username"] = "@live_bot"
        from api.routes.tg_verify import _bot_username
        with patch("api.routes.tg_verify._db.get_system_config") as mock_get:
            self.assertEqual(_bot_username(), "live_bot")
            mock_get.assert_not_called()

    def test_falls_back_to_persisted_config_when_live_status_empty(self):
        self._status["bot_username"] = None
        from api.routes.tg_verify import _bot_username
        with patch("api.routes.tg_verify._db.get_system_config", return_value="persisted_bot") as mock_get:
            self.assertEqual(_bot_username(), "persisted_bot")
            mock_get.assert_called_once_with("bot_username")

    def test_empty_when_neither_source_has_it(self):
        self._status["bot_username"] = None
        from api.routes.tg_verify import _bot_username
        with patch("api.routes.tg_verify._db.get_system_config", return_value=None):
            self.assertEqual(_bot_username(), "")


if __name__ == "__main__":
    unittest.main()
