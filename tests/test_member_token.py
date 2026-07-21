"""
Persistent member login token (/mytoken) — Telegram-independent self-serve
web login.

Unit scope: the POST /auth/member-token redeem endpoint (db mocked per
conftest) and the /mytoken bot handler (DM-only delivery, reissue, revoke).
Real-db round trips live in integration_tests/test_member_token_db.py.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


class TestHashLoginToken(unittest.TestCase):

    def test_hash_is_sha256_hex_not_plaintext(self):
        import hashlib
        from services.web import hash_login_token
        h = hash_login_token("some-code")
        self.assertEqual(h, hashlib.sha256(b"some-code").hexdigest())
        self.assertNotIn("some-code", h)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestMemberTokenEndpoint(unittest.TestCase):

    def setUp(self):
        from api.routes.tg_verify import _verify_buckets
        _verify_buckets.clear()

    def _client(self):
        from api.main import create_app
        return TestClient(create_app(), raise_server_exceptions=False)

    def _post(self, token, row):
        import api.routes.tg_verify as mod
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}), \
             patch.object(mod._db, "get_member_login_token_by_hash", return_value=row) as get, \
             patch.object(mod._db, "touch_member_login_token") as touch:
            res = self._client().post("/api/v1/auth/member-token", json={"token": token})
        return res, get, touch

    def test_valid_code_returns_identity_token(self):
        row = {"user_id": 9100, "first_name": "Dana", "username": "dana"}
        res, get, touch = self._post("unit-test-code", row)
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["verified"])
        self.assertEqual(body["user_id"], 9100)
        self.assertEqual(body["name"], "Dana")
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            from api.identity import verify_identity_token
            self.assertEqual(verify_identity_token(body["id_token"]), 9100)
        touch.assert_called_once_with(9100)
        # The endpoint must look up by hash, never by the raw code
        from services.web import hash_login_token
        get.assert_called_once_with(hash_login_token("unit-test-code"))

    def test_unknown_code_401(self):
        res, _, touch = self._post("no-such-code", None)
        self.assertEqual(res.status_code, 401)
        touch.assert_not_called()

    def test_blank_code_401(self):
        res, get, _ = self._post("   ", None)
        self.assertEqual(res.status_code, 401)
        get.assert_not_called()  # rejected before any db lookup

    def test_oversized_code_401(self):
        res, get, _ = self._post("x" * 200, None)
        self.assertEqual(res.status_code, 401)
        get.assert_not_called()

    def test_missing_token_field_422(self):
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            res = self._client().post("/api/v1/auth/member-token", json={})
        self.assertEqual(res.status_code, 422)

    def test_rate_limited_after_burst(self):
        import api.routes.tg_verify as mod
        client = self._client()
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}), \
             patch.object(mod._db, "get_member_login_token_by_hash", return_value=None):
            codes = [client.post("/api/v1/auth/member-token",
                                 json={"token": f"guess-{i}"}).status_code
                     for i in range(8)]
        self.assertIn(429, codes)


class TestMyTokenHandler(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()

    def _msg(self, text="/mytoken", chat_id=-100, chat_type="supergroup",
             user_id=501, first_name="Eve", username="eve"):
        m = MagicMock()
        m.text = text
        m.chat.id = chat_id
        m.chat.type = chat_type
        m.from_user.id = user_id
        m.from_user.first_name = first_name
        m.from_user.username = username
        return m

    def _texts(self):
        return [c[0] for c in self.bot_state.bot.send_message.call_args_list]

    async def test_dms_code_and_confirms_in_group(self):
        from handlers.web import mytoken_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web._db.upsert_member_login_token", return_value=True) as up:
            await mytoken_cmd(self._msg())
        calls = self._texts()
        # Code goes out ALONE on its own message — nothing else on the line
        # to interfere with tap/long-press-to-copy.
        self.assertEqual(calls[0][0], 501)
        self.assertTrue(calls[0][1].startswith("`") and calls[0][1].endswith("`"))
        # Instructions + portal button follow as a separate DM
        self.assertEqual(calls[1][0], 501)
        self.assertIn("login code", calls[1][1])
        markup = self.bot_state.bot.send_message.call_args_list[1].kwargs.get("reply_markup")
        self.assertIsNotNone(markup, "portal login button missing from instructions DM")
        # Group gets only the pointer, not the code or instructions
        self.assertEqual(calls[2][0], -100)
        self.assertIn("DM", calls[2][1])
        # Stored value is the hash — it must not appear in either DM
        stored_hash = up.call_args[0][1]
        self.assertNotIn(stored_hash, calls[0][1])
        self.assertNotIn(stored_hash, calls[1][1])

    async def test_portal_button_links_to_portal_login(self):
        import telebot.types as tt
        from handlers.web import mytoken_cmd
        tt.InlineKeyboardButton.reset_mock()
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web._db.upsert_member_login_token", return_value=True):
            await mytoken_cmd(self._msg())
        self.assertEqual(
            tt.InlineKeyboardButton.call_args.kwargs.get("url"), "https://rc.example/portal/")

    async def test_dm_failure_points_user_at_private_chat(self):
        from handlers.web import mytoken_cmd

        async def _send(chat_id, *a, **k):
            if chat_id == 501:
                raise Exception("Forbidden: bot can't initiate conversation")
            return MagicMock()

        self.bot_state.bot.send_message = AsyncMock(side_effect=_send)
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web._db.upsert_member_login_token", return_value=True):
            await mytoken_cmd(self._msg())
        group_texts = [c[0][1] for c in self.bot_state.bot.send_message.call_args_list
                       if c[0][0] == -100]
        self.assertTrue(any("private chat" in t for t in group_texts))

    async def test_private_chat_gets_no_group_confirmation(self):
        from handlers.web import mytoken_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web._db.upsert_member_login_token", return_value=True):
            await mytoken_cmd(self._msg(chat_id=501, chat_type="private"))
        calls = self._texts()
        self.assertEqual(len(calls), 2)                    # code + instructions, no group ping
        self.assertTrue(all(c[0] == 501 for c in calls))

    async def test_off_revokes(self):
        from handlers.web import mytoken_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web._db.delete_member_login_token", return_value=True) as rm:
            await mytoken_cmd(self._msg(text="/mytoken off"))
        rm.assert_called_once_with(501)
        self.assertIn("revoked", self._texts()[0][1])

    async def test_unconfigured_web_base_url_warns(self):
        from handlers.web import mytoken_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": ""}):
            await mytoken_cmd(self._msg())
        self.assertIn("not configured", self._texts()[0][1])


if __name__ == "__main__":
    unittest.main()
