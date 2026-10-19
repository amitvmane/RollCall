"""
Regressions for the October 2026 security review. Real DB, real handlers and
routes; only the Telegram API is mocked.

  1. /weblogin + issue-weblogin: need a REAL Telegram admin (open mode is not
     enough) and the link is DMed, never posted in the group.
  4. Deep-link verify: opening /start v_<code> only asks; the Confirm tap is
     what verifies, so a link someone else minted can't silently hand them
     your identity.
  5. Rate limiter: random bearers no longer earn a fresh bucket each; bucket
     count is bounded.
  6. push-subscribe: https public hosts only, per-group cap.
  7. Admin-scope API tokens stop working when the issuer stops being admin;
     web sign-in tokens last 30 days, not 365.
"""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from mock_helpers import mock_bot, reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
CHAT_ID = -1001999000811
ADMIN_ID = 2001
ALICE_ID = 2002
MALLORY_ID = 2003


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import rollcall_manager
    from api.main import app
    import db
    return app, rollcall_manager.manager, db


def _msg(text, uid, first_name, chat_type="supergroup", chat_id=CHAT_ID):
    m = MagicMock()
    m.text = text
    m.chat.id = chat_id
    m.chat.type = chat_type
    m.from_user.id = uid
    m.from_user.first_name = first_name
    m.from_user.username = first_name.lower()
    return m


def _sent():
    out = []
    for args, kwargs in mock_bot.send_message.call_args_list:
        chat = args[0] if args else kwargs.get("chat_id")
        text = args[1] if len(args) > 1 else kwargs.get("text", "")
        out.append((chat, str(text), kwargs))
    return out


class Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.manager, cls.db = _import()
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        self.manager.set_admin_rights(CHAT_ID, False)  # open mode: the risky default
        from api.rate_limit import reset_buckets_for_tests
        from api.auth import reset_admin_recheck_cache_for_tests
        reset_buckets_for_tests()
        reset_admin_recheck_cache_for_tests()
        self.addCleanup(reset_admin_recheck_cache_for_tests)
        self.db.get_or_create_chat(CHAT_ID)
        self.db.upsert_chat_member(CHAT_ID, ALICE_ID, "Alice", "alice")
        mock_bot.send_message.reset_mock()
        mock_bot.send_message.side_effect = None
        mock_bot.edit_message_text.reset_mock()
        mock_bot.get_chat_member.return_value.status = "administrator"
        self.addCleanup(setattr, mock_bot.get_chat_member.return_value, "status", "administrator")
        self.addCleanup(setattr, mock_bot.send_message, "side_effect", None)
        self.enterContext(patch.dict(os.environ, {
            "TELEGRAM_TOKEN": BOT_TOKEN, "WEB_BASE_URL": "https://rc.example.com",
        }))

    def _member_status(self, status):
        mock_bot.get_chat_member.return_value.status = status


# ── 1. /weblogin ──────────────────────────────────────────────────────────────

class TestWebloginCommand(Base):

    def _run(self, uid, name):
        from handlers.web import weblogin_cmd
        asyncio.run(weblogin_cmd(_msg("/weblogin Alice", uid, name)))

    def test_open_mode_member_cannot_issue(self):
        # admin_rights() says yes to everyone here; /weblogin must not.
        self._member_status("member")
        self._run(MALLORY_ID, "Mallory")
        self.assertFalse(any("/auth/weblogin/" in t for _, t, _ in _sent()))
        self.assertTrue(any("Telegram admin" in t for _, t, _ in _sent()))

    def test_real_admin_gets_link_by_dm_not_in_group(self):
        self._run(ADMIN_ID, "Boss")
        sent = _sent()
        dms = [t for c, t, _ in sent if c == ADMIN_ID]
        group = [t for c, t, _ in sent if c == CHAT_ID]
        self.assertTrue(any("/api/v1/auth/weblogin/" in t for t in dms), sent)
        self.assertFalse(any("/auth/weblogin/" in t for t in group), group)
        self.assertTrue(any("sent to you privately" in t for t in group), group)

    def test_dm_refused_never_falls_back_to_group_and_burns_link(self):
        def _send(chat_id, text, **kw):
            if chat_id == ADMIN_ID:
                raise Exception("Forbidden: bot can't initiate conversation with a user")
            return MagicMock()
        mock_bot.send_message.side_effect = _send
        self._run(ADMIN_ID, "Boss")
        group = [t for c, t, _ in _sent() if c == CHAT_ID]
        self.assertFalse(any("/auth/weblogin/" in t for t in group), group)
        self.assertTrue(any("private chat" in t for t in group), group)
        # The undelivered link was burned, not left live.
        attempted = [t for c, t, _ in _sent() if c == ADMIN_ID][0]
        token = attempted.split("/auth/weblogin/")[1].split("`")[0]
        self.assertIsNone(self.db.peek_web_direct_login_token(token))


class TestIssueWebloginRoute(Base):

    def _post(self, uid):
        from api.identity import issue_identity_token
        return self.client.post(
            f"/api/v1/web/group/{self.db.get_or_create_chat(CHAT_ID)['group_web_token']}/issue-weblogin",
            json={"id_token": issue_identity_token(uid), "member_name": "Alice"},
        )

    def test_cached_open_mode_web_admin_who_is_not_telegram_admin_is_refused(self):
        # Ran /weblink in an open group -> cached grant -> passes
        # check_web_admin_live. Not enough to mint a global identity.
        self.db.set_web_admin(CHAT_ID, MALLORY_ID, "Mallory")
        self._member_status("member")
        r = self._post(MALLORY_ID)
        self.assertEqual(r.status_code, 403, r.text)

    def test_real_telegram_admin_can_issue(self):
        r = self._post(ADMIN_ID)
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("/api/v1/auth/weblogin/", r.json()["login_url"])


# ── 4. Deep-link verify needs an explicit confirm ─────────────────────────────

class TestTgVerifyConfirm(Base):

    def _code(self):
        code = "c0ffee" + os.urandom(13).hex()
        self.db.create_web_verify_token(code, datetime.now(timezone.utc) + timedelta(minutes=10))
        return code

    def _status(self, code):
        return self.client.get(f"/api/v1/auth/tg-verify/status/{code}").json()

    def _tap(self, data, uid, chat_type="private"):
        from handlers.core import handle_tg_verify_confirm
        call = MagicMock()
        call.data = data
        call.id = "cb1"
        call.message.chat.id = uid
        call.message.chat.type = chat_type
        call.message.message_id = 77
        call.from_user.id = uid
        call.from_user.first_name = "Alice"
        call.from_user.username = "alice"
        asyncio.run(handle_tg_verify_confirm(call))

    def test_opening_the_link_does_not_verify(self):
        from handlers.core import handle_tg_verify
        code = self._code()
        asyncio.run(handle_tg_verify(_msg(f"/start v_{code}", ALICE_ID, "Alice",
                                          chat_type="private", chat_id=ALICE_ID)))
        self.assertFalse(self._status(code)["verified"])
        _, text, kwargs = _sent()[-1]
        self.assertIn("asking to sign in", text)
        buttons = kwargs["reply_markup"].keyboard
        self.assertIn(f"tgv_ok:{code}", [b.callback_data for b in buttons])

    def test_confirm_tap_verifies(self):
        code = self._code()
        self._tap(f"tgv_ok:{code}", ALICE_ID)
        st = self._status(code)
        self.assertTrue(st["verified"])
        self.assertEqual(st["user_id"], ALICE_ID)

    def test_cancel_tap_does_not_verify(self):
        code = self._code()
        self._tap("tgv_no", ALICE_ID)
        self.assertFalse(self._status(code)["verified"])

    def test_confirm_outside_private_chat_is_ignored(self):
        code = self._code()
        self._tap(f"tgv_ok:{code}", ALICE_ID, chat_type="supergroup")
        self.assertFalse(self._status(code)["verified"])


# ── 5. Rate limiter ──────────────────────────────────────────────────────────

class TestRateLimiter(Base):

    def test_random_bearers_share_the_ip_bucket(self):
        with patch.dict(os.environ, {"REST_API_RATE_LIMIT_MAX_REQUESTS": "3",
                                     "REST_API_RATE_LIMIT_WINDOW_SECONDS": "60"}):
            codes = [
                self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls",
                                headers={"Authorization": f"Bearer junk{i}"}).status_code
                for i in range(6)
            ]
        self.assertIn(429, codes, codes)

    def test_valid_token_keeps_its_own_bucket(self):
        from db import _hash_token, generate_api_token, insert_api_token
        tok = generate_api_token()
        insert_api_token(_hash_token(tok), CHAT_ID, "read", label="t", issued_by_user_id=ALICE_ID)
        with patch.dict(os.environ, {"REST_API_RATE_LIMIT_MAX_REQUESTS": "3",
                                     "REST_API_RATE_LIMIT_WINDOW_SECONDS": "60"}):
            for _ in range(3):  # exhaust the IP bucket anonymously
                self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls")
            r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls",
                                headers={"Authorization": f"Bearer {tok}"})
        self.assertNotEqual(r.status_code, 429)

    def test_bucket_count_is_bounded(self):
        from collections import deque
        import time
        from api import rate_limit
        now = time.monotonic()
        with patch.object(rate_limit, "_MAX_BUCKETS", 50):
            for i in range(200):
                rate_limit._buckets[f"ip:10.0.{i}"] = deque([now])
            rate_limit._last_sweep[0] = now  # no time-based sweep: the cap alone must hold
            self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls")
            self.assertLessEqual(len(rate_limit._buckets), 50)


# ── 6. push-subscribe ────────────────────────────────────────────────────────

class TestPushSubscribeEndpointValidation(Base):

    def _sub(self, endpoint):
        token = self.db.get_or_create_chat(CHAT_ID)["group_web_token"]
        return self.client.post(
            f"/api/v1/web/group/{token}/push-subscribe",
            json={"endpoint": endpoint, "keys": {"p256dh": "k", "auth": "a"}},
        )

    def test_rejects_internal_and_non_https_targets(self):
        for ep in (
            "http://fcm.googleapis.com/fcm/send/abc",
            "https://127.0.0.1:8081/api/v1/health",
            "https://[::1]/x/y/z/w",
            "https://169.254.169.254/latest/meta-data",
            "https://localhost/some/path",
            "https://db.internal/some/path",
            "https://intranet/some/path",
            "https://user:pw@fcm.googleapis.com/x",
        ):
            self.assertEqual(self._sub(ep).status_code, 400, ep)

    def test_rejects_overlong_endpoint(self):
        self.assertEqual(self._sub("https://fcm.googleapis.com/" + "a" * 1100).status_code, 422)

    def test_accepts_real_push_service(self):
        self.assertEqual(self._sub("https://fcm.googleapis.com/fcm/send/abc123").status_code, 204)

    def test_per_group_cap(self):
        from services import push
        with patch.object(push, "MAX_ACTIVE_SUBSCRIPTIONS_PER_GROUP", 2):
            self.assertEqual(self._sub("https://fcm.googleapis.com/fcm/send/one").status_code, 204)
            self.assertEqual(self._sub("https://fcm.googleapis.com/fcm/send/two").status_code, 204)
            self.assertEqual(self._sub("https://fcm.googleapis.com/fcm/send/three").status_code, 400)
            # An existing subscriber refreshing is never locked out by the cap.
            self.assertEqual(self._sub("https://fcm.googleapis.com/fcm/send/one").status_code, 204)


# ── 7. Admin-scope token re-check ────────────────────────────────────────────

class TestAdminTokenRecheck(Base):

    def _mint(self, scopes):
        from db import _hash_token, generate_api_token, insert_api_token
        tok = generate_api_token()
        insert_api_token(_hash_token(tok), CHAT_ID, scopes, label="t", issued_by_user_id=ADMIN_ID)
        return tok

    def _get(self, tok):
        return self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls",
                               headers={"Authorization": f"Bearer {tok}"})

    def test_admin_token_works_while_issuer_is_admin(self):
        self.assertEqual(self._get(self._mint("admin,read,vote")).status_code, 200)

    def test_admin_token_refused_once_issuer_loses_admin(self):
        self.manager.set_admin_rights(CHAT_ID, True)  # live-checked mode
        tok = self._mint("admin,read,vote")
        self._member_status("left")
        self.assertEqual(self._get(tok).status_code, 403)

    def test_non_admin_scope_token_unaffected(self):
        self.manager.set_admin_rights(CHAT_ID, True)
        tok = self._mint("read")
        self._member_status("left")
        self.assertEqual(self._get(tok).status_code, 200)

    def test_web_signin_token_lifetime_is_30_days(self):
        from api.routes import auth
        self.assertEqual(auth._ADMIN_SESSION_TOKEN_TTL_SECONDS, 30 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()
