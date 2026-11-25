"""
People and groups with no Telegram behind them.

The blocker: every table keys people on a Telegram user id and groups on a
Telegram chat id. Rather than re-key a live database, this app issues its own
ids in a range Telegram never uses (identity_space), so the existing tables
work unchanged — and a guard in the one function every Telegram request goes
through refuses those ids.

On top of that:
  * web-only groups — created on the web, admins from this app's own list;
  * email sign-in — a person with no Telegram, acting under a local id;
  * linking an email to an existing Telegram member, so email sign-in IS that
    member, history and all.
"""
import asyncio
import os
import re
import time
import unittest
from unittest.mock import AsyncMock, patch

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
TG_CHAT = -1001777000111
TG_MEMBER = 7001
EMAIL_ENV = {"TELEGRAM_TOKEN": BOT_TOKEN, "SMTP_HOST": "smtp.test", "SMTP_FROM": "bot@test",
             "WEB_BASE_URL": "https://rc.test"}


class TestIdSpace(unittest.TestCase):

    def test_telegram_ids_are_never_local(self):
        from identity_space import is_local_id
        for tid in (1, 168415137, 2 ** 52 - 1, -1, -1001234567890, -(2 ** 52 - 1)):
            self.assertFalse(is_local_id(tid), tid)

    def test_local_ids_are_js_safe_and_round_trip(self):
        from identity_space import (is_local_chat, is_local_user, local_chat_id, local_user_id,
                                    principal_of_local_user)
        uid = local_user_id(42)
        self.assertTrue(is_local_user(uid) and uid > 0 and uid < 2 ** 53)
        self.assertEqual(principal_of_local_user(uid), 42)
        cid = local_chat_id(3)
        self.assertTrue(is_local_chat(cid) and cid < 0 and cid > -(2 ** 53))
        self.assertFalse(is_local_user(cid) or is_local_chat(uid))


class TestTelegramGuard(unittest.TestCase):
    """No request naming a local chat or person may reach Telegram."""

    def _helper(self):
        class ApiTelegramException(Exception):
            def __init__(self, fn, result, result_json):
                super().__init__(result_json["description"])
                self.error_code = result_json["error_code"]

        class Helper:
            pass
        h = Helper()
        h.ApiTelegramException = ApiTelegramException
        h.sent = []

        async def _process_request(token, url, method="get", params=None, files=None, **kw):
            h.sent.append((url, params))
            return {"ok": True}
        h._process_request = _process_request
        return h

    def test_local_ids_are_refused_before_sending(self):
        import chat_outbox
        from identity_space import local_chat_id, local_user_id
        h = self._helper()
        self.assertTrue(chat_outbox.install_request_pacing(h))
        for params in ({"chat_id": local_chat_id(1), "text": "hi"},
                       {"chat_id": TG_CHAT, "user_id": local_user_id(1)}):
            with self.assertRaises(h.ApiTelegramException) as ctx:
                asyncio.run(h._process_request("t", "sendMessage", "post", params))
            self.assertEqual(ctx.exception.error_code, 400)
        self.assertEqual(h.sent, [])
        asyncio.run(h._process_request("t", "sendMessage", "post", {"chat_id": TG_CHAT, "text": "hi"}))
        self.assertEqual(len(h.sent), 1)


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import bot_state  # noqa: F401
        from api.main import app
        from api.identity import issue_identity_token, issue_chat_bound_token
        import db
        cls.client = TestClient(app)
        cls.issue = staticmethod(issue_identity_token)
        cls.issue_bound = staticmethod(issue_chat_bound_token)
        cls.db = db

    def setUp(self):
        reset_db()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.enterContext(patch.dict(os.environ, EMAIL_ENV))
        self.mails = []

        async def _fake_send(to, subject, text):
            self.mails.append((to, subject, text))
        self.enterContext(patch("services.email_auth.send_mail", new=_fake_send))

    def h(self, uid):
        return {"X-Identity-Token": self.issue(uid)}

    def code(self):
        return re.search(r"email_code=([\w-]+)", self.mails[-1][2]).group(1)

    def email_login(self, email, name=None, group_token=None):
        r = self.client.post("/api/v1/auth/email/start",
                             json={"email": email, "name": name, "group_token": group_token})
        self.assertEqual(r.status_code, 202, r.text)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestEmailSignIn(_Base):

    def test_new_address_gets_a_local_identity(self):
        from identity_space import is_local_user
        body = self.email_login("Asha@Example.com", name="Asha")
        self.assertTrue(is_local_user(body["user_id"]))
        self.assertEqual((body["name"], body["email"]), ("Asha", "asha@example.com"))
        from api.identity import verify_identity_token
        self.assertEqual(verify_identity_token(body["id_token"]), body["user_id"])

    def test_same_address_is_the_same_person(self):
        a = self.email_login("asha@example.com", name="Asha")
        b = self.email_login(" ASHA@example.com ")
        self.assertEqual(a["user_id"], b["user_id"])
        self.assertEqual(b["name"], "Asha")

    def test_code_is_single_use_and_expires(self):
        self.client.post("/api/v1/auth/email/start", json={"email": "a@example.com"})
        code = self.code()
        self.assertEqual(self.client.post("/api/v1/auth/email/verify", json={"code": code}).status_code, 200)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": code})
        self.assertEqual(r.status_code, 422)
        self.assertIn("already used", r.json()["detail"])
        self.client.post("/api/v1/auth/email/start", json={"email": "b@example.com"})
        with patch("services.email_auth.time.time", return_value=time.time() + 16 * 60):
            r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        self.assertIn("expired", r.json()["detail"])

    def test_link_returns_to_the_group_page(self):
        token = self.db.get_or_create_chat(TG_CHAT)["group_web_token"]
        self.client.post("/api/v1/auth/email/start", json={"email": "a@example.com", "group_token": token})
        self.assertIn(f"https://rc.test/web/group/{token}?email_code=", self.mails[-1][2])
        self.client.post("/api/v1/auth/email/start", json={"email": "c@example.com", "group_token": "nope"})
        self.assertIn("https://rc.test/web/?email_code=", self.mails[-1][2])

    def test_too_many_emails_for_one_address(self):
        for _ in range(3):
            self.assertEqual(self.client.post("/api/v1/auth/email/start",
                                              json={"email": "a@example.com"}).status_code, 202)
        r = self.client.post("/api/v1/auth/email/start", json={"email": "a@example.com"})
        self.assertEqual(r.status_code, 429)

    def test_bad_address_and_unconfigured_server(self):
        r = self.client.post("/api/v1/auth/email/start", json={"email": "not-an-email"})
        self.assertEqual(r.status_code, 422)
        with patch.dict(os.environ, {"SMTP_HOST": ""}):
            self.assertFalse(self.client.get("/api/v1/auth/email/config").json()["enabled"])
            r = self.client.post("/api/v1/auth/email/start", json={"email": "a@example.com"})
            self.assertEqual(r.status_code, 503)


class TestLinkEmailToTelegram(_Base):

    def _link(self, uid, email, headers=None):
        r = self.client.post("/api/v1/auth/email/link", json={"email": email},
                             headers=headers or self.h(uid))
        return r

    def test_linked_email_signs_in_as_the_telegram_member(self):
        self.db.upsert_chat_member(TG_CHAT, TG_MEMBER, "Ravi")
        self.assertEqual(self._link(TG_MEMBER, "ravi@example.com").status_code, 202)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        self.assertEqual((r.status_code, r.json()["purpose"]), (200, "link"), r.text)
        body = self.email_login("ravi@example.com")
        self.assertEqual((body["user_id"], body["name"]), (TG_MEMBER, "Ravi"))
        me = self.client.get("/api/v1/auth/email/me", headers=self.h(TG_MEMBER)).json()
        self.assertEqual((me["email"], me["can_unlink"]), ("ravi@example.com", True))

    def test_an_address_owned_by_someone_else_is_not_taken(self):
        self.email_login("asha@example.com", name="Asha")          # email-only account
        self._link(TG_MEMBER, "asha@example.com")
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        self.assertEqual(r.status_code, 422)
        self.assertIn("different RollCall account", r.json()["detail"])

    def test_a_group_bound_login_cannot_add_an_email(self):
        # /weblogin tokens prove identity in ONE group; an email login works
        # everywhere, so it must not be minted from one.
        bound = {"X-Identity-Token": self.issue_bound(TG_MEMBER, TG_CHAT)}
        self.assertEqual(self._link(TG_MEMBER, "x@example.com", bound).status_code, 401)

    def test_an_email_only_account_cannot_link_another_email(self):
        body = self.email_login("asha@example.com", name="Asha")
        self.assertEqual(self._link(body["user_id"], "other@example.com").status_code, 403)

    def test_unlink_only_when_telegram_remains(self):
        body = self.email_login("asha@example.com", name="Asha")
        r = self.client.post("/api/v1/auth/email/unlink", headers=self.h(body["user_id"]))
        self.assertEqual(r.status_code, 403)
        self._link(TG_MEMBER, "ravi@example.com")
        self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        r = self.client.post("/api/v1/auth/email/unlink", headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 204)
        self.assertIsNone(self.client.get("/api/v1/auth/email/me", headers=self.h(TG_MEMBER)).json()["email"])


class TestWebOnlyGroups(_Base):

    def _create(self, uid, name="Sunday Turf"):
        return self.client.post("/api/v1/web/groups", json={"name": name, "your_name": "Asha"},
                                headers=self.h(uid))

    def test_create_makes_an_owned_local_group(self):
        from identity_space import is_local_chat
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        r = self._create(asha)
        self.assertEqual(r.status_code, 201, r.text)
        g = r.json()
        self.assertTrue(is_local_chat(g["chat_id"]))
        self.assertEqual(self.db.get_web_admin_role(g["chat_id"], asha), "owner")
        from services.admin import get_admin_source
        self.assertEqual(get_admin_source(g["chat_id"]), "local")
        page = self.client.get(f"/api/v1/web/group/{g['group_token']}")
        self.assertEqual((page.status_code, page.json()["group_name"]), (200, "Sunday Turf"))
        second = self._create(asha, "Wednesday Nets").json()
        self.assertNotEqual(second["chat_id"], g["chat_id"])

    def test_admin_check_never_asks_telegram(self):
        from api.web_admin import check_web_admin_live
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        g = self._create(asha).json()
        from bot_state import bot
        with patch.object(bot, "get_chat_member", new=AsyncMock(side_effect=AssertionError("asked Telegram"))):
            self.assertTrue(asyncio.run(check_web_admin_live(g["chat_id"], asha)))
            self.assertFalse(asyncio.run(check_web_admin_live(g["chat_id"], TG_MEMBER)))

    def test_owner_manages_admins_and_the_last_owner_stays(self):
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        ben = self.email_login("ben@example.com", name="Ben")["user_id"]
        g = self._create(asha).json()
        gt = g["group_token"]
        # Ben isn't a member yet, then joins.
        r = self.client.post(f"/api/v1/web/group/{gt}/admins/grant", json={"user_id": ben}, headers=self.h(asha))
        self.assertEqual(r.status_code, 422)
        self.assertTrue(self.client.post(f"/api/v1/web/group/{gt}/join", json={}, headers=self.h(ben)).json()["joined"])
        r = self.client.post(f"/api/v1/web/group/{gt}/admins/grant", json={"user_id": ben}, headers=self.h(asha))
        self.assertEqual((r.status_code, r.json()["role"]), (200, "admin"), r.text)
        # Ben (admin, not owner) can't remove Asha; Asha can't remove herself as last owner.
        r = self.client.post(f"/api/v1/web/group/{gt}/admins/remove", json={"user_id": asha}, headers=self.h(ben))
        self.assertEqual(r.status_code, 403)
        r = self.client.post(f"/api/v1/web/group/{gt}/admins/remove", json={"user_id": asha}, headers=self.h(asha))
        self.assertEqual(r.status_code, 403)
        from services.admin import set_admin_source
        from exceptions import incorrectParameter
        with self.assertRaises(incorrectParameter):
            set_admin_source(g["chat_id"], "platform", actor_user_id=asha, actor_name="Asha")

    def test_telegram_group_admins_stay_on_telegram(self):
        token = self.db.get_or_create_chat(TG_CHAT)["group_web_token"]
        self.db.set_web_admin(TG_CHAT, TG_MEMBER, "Ravi")
        self.db.set_web_admin_role(TG_CHAT, TG_MEMBER, "owner")
        r = self.client.post(f"/api/v1/web/group/{token}/admins/grant", json={"user_id": 1},
                             headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 422)
        self.assertIn("Telegram", r.json()["detail"])

    def test_create_needs_sign_in_and_is_capped(self):
        self.assertEqual(self.client.post("/api/v1/web/groups", json={"name": "X"}).status_code, 401)
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        from services import local_groups
        for i in range(local_groups.MAX_OWNED_GROUPS):
            self.assertEqual(self._create(asha, f"G{i}").status_code, 201)
        self.assertEqual(self._create(asha, "One too many").status_code, 403)


class TestNoTelegramMemberInTelegramGroup(_Base):
    """An email-only person in an existing Telegram group is a real member,
    not a typed guest name."""

    def test_join_and_vote_as_yourself(self):
        from identity_space import is_local_user
        token = self.db.get_or_create_chat(TG_CHAT)["group_web_token"]
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        r = self.client.post(f"/api/v1/web/group/{token}/join", json={}, headers=self.h(asha))
        self.assertEqual(r.json(), {"joined": True, "name": "Asha"})
        # A Telegram member is the bot's to track, not the web page's.
        r = self.client.post(f"/api/v1/web/group/{token}/join", json={"name": "Fake"}, headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 422)
        self.assertTrue(is_local_user(asha))

    def test_web_vote_without_joining_makes_them_a_member(self):
        from rollcall_manager import manager
        from services.web import vote_by_token, _ensure_web_token
        from services import rollcalls as rc_svc
        self.db.get_or_create_chat(TG_CHAT)
        asyncio.run(rc_svc.start_rollcall(TG_CHAT, "Friday", TG_MEMBER, "Ravi"))
        rc = manager.get_rollcall(TG_CHAT, 0)
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        asyncio.run(vote_by_token(_ensure_web_token(rc), "Asha", "in", tg_user_id=asha))
        rc = manager.get_rollcall(TG_CHAT, 0)
        self.assertEqual([(u.user_id, u.name) for u in rc.inList], [(asha, "Asha")])
        self.assertEqual(self.db.get_member_display_info(TG_CHAT, asha)["first_name"], "Asha")

    def test_mentions_are_plain_text(self):
        from bot_state import format_mention_with_name_md, format_mention
        from models import User
        from identity_space import local_user_id
        u = User("Asha", None, local_user_id(5), [])
        self.assertEqual(format_mention(u), "Asha")
        self.assertNotIn("tg://", format_mention_with_name_md(u))
