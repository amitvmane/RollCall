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
        from rollcall_manager import manager
        manager.clear_cache()      # open rollcalls live in memory too
        from services.email_auth import reset_send_limits_for_tests
        reset_send_limits_for_tests()
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
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(TG_MEMBER))
        self.assertEqual((r.status_code, r.json()["purpose"]), (200, "link"), r.text)
        body = self.email_login("ravi@example.com")
        self.assertEqual((body["user_id"], body["name"]), (TG_MEMBER, "Ravi"))
        me = self.client.get("/api/v1/auth/email/me", headers=self.h(TG_MEMBER)).json()
        self.assertEqual((me["email"], me["can_unlink"]), ("ravi@example.com", True))

    def test_an_address_owned_by_another_telegram_member_is_not_taken(self):
        self.db.upsert_chat_member(TG_CHAT, 7002, "Other")
        self._link(7002, "x@example.com")
        self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(7002))
        self._link(TG_MEMBER, "x@example.com")
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 422)
        self.assertIn("different Telegram member", r.json()["detail"])

    def test_a_link_works_only_for_the_account_that_asked(self):
        # Otherwise: a stranger types YOUR email into THEIR account, you click
        # "confirm", and your address now signs in as them.
        self._link(TG_MEMBER, "victim@example.com")
        code = self.code()
        for headers in ({}, self.h(7002)):
            r = self.client.post("/api/v1/auth/email/verify", json={"code": code}, headers=headers)
            self.assertEqual(r.status_code, 403, r.text)
        # Not spent: the right device can still use it.
        r = self.client.post("/api/v1/auth/email/verify", json={"code": code}, headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 200)

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
        self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(TG_MEMBER))
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
        # Web-only groups approve their members: Ben asks, Asha approves.
        self.assertTrue(self.client.post(f"/api/v1/web/group/{gt}/join", json={}, headers=self.h(ben)).json()["pending"])
        self.client.post(f"/api/v1/web/group/{gt}/join-requests/decide", headers=self.h(asha),
                         json={"user_id": ben, "approve": True})
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
        self.assertEqual(r.json(), {"joined": True, "pending": False, "denied": False, "name": "Asha"})
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


class TestQuietLogs(unittest.TestCase):
    """Every send to a web-only group is refused by design; only those
    refusals are kept out of the logs — a real Telegram error still logs."""

    def test_filter_drops_only_local_refusals(self):
        import logging
        import chat_outbox
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())
        h = Capture()
        root = logging.getLogger()
        root.addHandler(h)
        try:
            chat_outbox.install_log_filter()
            local = Exception("refused")
            local._rc_local = True
            try:
                raise local
            except Exception:
                logging.warning("panel send failed", exc_info=True)
            logging.warning("Could not DM user 1: Bad Request: chat not found (%s)",
                            chat_outbox.LOCAL_REFUSAL_MARKER)
            try:
                raise RuntimeError("Bad Request: chat not found")
            except Exception:
                logging.warning("real failure", exc_info=True)
        finally:
            root.removeHandler(h)
        self.assertEqual(records, ["real failure"])


class TestScheduledStartInWebOnlyGroup(_Base):

    def test_auto_start_creates_the_rollcall_with_every_send_refused(self):
        # What production does to a web-only group: every Telegram call is
        # refused (chat_outbox). The scheduled start must still happen.
        import check_reminders
        from rollcall_manager import manager
        from bot_state import bot
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf"}, headers=self.h(asha)).json()
        refused = Exception("chat not found")
        refused._rc_local = True
        refused.error_code = 400
        with patch.object(bot, "send_message", new=AsyncMock(side_effect=refused)):
            ok = asyncio.run(check_reminders._auto_start_from_template(
                g["chat_id"], {"name": "sunday", "title": "Sunday Game"}))
        self.assertTrue(ok)
        self.assertEqual([rc.title for rc in manager.get_rollcalls(g["chat_id"])], ["Sunday Game"])



class TestMergeDuplicateAccount(_Base):
    """An existing member signed in by email before linking it — two accounts.
    Linking the email from the Telegram account offers to merge them."""

    def _setup_duplicate(self):
        from rollcall_manager import manager
        from services.web import vote_by_token, _ensure_web_token
        from services import rollcalls as rc_svc
        self.db.upsert_chat_member(TG_CHAT, TG_MEMBER, "Ravi")
        asyncio.run(rc_svc.start_rollcall(TG_CHAT, "Friday", TG_MEMBER, "Ravi"))
        asyncio.run(rc_svc.start_rollcall(TG_CHAT, "Sunday", TG_MEMBER, "Ravi"))
        fri, sun = manager.get_rollcall(TG_CHAT, 0), manager.get_rollcall(TG_CHAT, 1)
        local = self.email_login("ravi@example.com", name="Ravi E")["user_id"]
        asyncio.run(vote_by_token(_ensure_web_token(fri), "Ravi E", "in", tg_user_id=local))
        asyncio.run(vote_by_token(_ensure_web_token(sun), "Ravi E", "out", tg_user_id=local))
        asyncio.run(vote_by_token(_ensure_web_token(sun), "Ravi", "in", tg_user_id=TG_MEMBER))  # both in Sunday
        self.db.add_dues_entry(TG_CHAT, fri.id, local, "Ravi E", "game_charge", 150, "Friday", TG_MEMBER, "Ravi")
        return local

    def _offer(self):
        self._link(TG_MEMBER, "ravi@example.com")
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(TG_MEMBER))
        self.assertEqual((r.status_code, r.json()["purpose"]), (200, "merge_offer"), r.text)
        return r.json()

    _link = TestLinkEmailToTelegram._link

    def test_merge_moves_votes_dues_and_the_email(self):
        from rollcall_manager import manager
        local = self._setup_duplicate()
        offer = self._offer()
        self.assertEqual((offer["merge_from_user_id"], offer["merge_votes"]), (local, 2))
        r = self.client.post("/api/v1/auth/email/merge", headers=self.h(TG_MEMBER),
                             json={"from_user_id": local, "merge_token": offer["merge_token"]})
        self.assertEqual(r.status_code, 200, r.text)
        fri, sun = manager.get_rollcall(TG_CHAT, 0), manager.get_rollcall(TG_CHAT, 1)
        self.assertEqual([u.user_id for u in fri.inList], [TG_MEMBER])       # re-keyed
        self.assertEqual([u.user_id for u in sun.inList], [TG_MEMBER])       # Telegram vote kept
        self.assertEqual(sun.outList, [])
        self.assertEqual(self.db.get_dues_balance(TG_CHAT, user_id=TG_MEMBER), 150)
        self.assertEqual(self.db.get_dues_balance(TG_CHAT, user_id=local), 0)
        # Ledger stayed append-only: the original charge is still there.
        with self.db._cursor() as c:
            c.execute("SELECT COUNT(*) FROM dues_entries WHERE user_id = ? AND entry_type = 'game_charge'"
                      .replace("?", "%s" if self.db.db_type == "postgresql" else "?"), (local,))
            self.assertEqual(list(dict(c.fetchone()).values())[0] if self.db.db_type == "postgresql"
                             else c.fetchone()[0], 1)
        # The email now signs in as the Telegram member, and an old email
        # token verifies as them too.
        self.assertEqual(self.email_login("ravi@example.com")["user_id"], TG_MEMBER)
        from api.identity import verify_identity_token
        self.assertEqual(verify_identity_token(self.issue(local)), TG_MEMBER)

    def test_merge_token_is_bound_to_both_accounts(self):
        local = self._setup_duplicate()
        offer = self._offer()
        r = self.client.post("/api/v1/auth/email/merge", headers=self.h(7002),
                             json={"from_user_id": local, "merge_token": offer["merge_token"]})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/v1/auth/email/merge", headers=self.h(TG_MEMBER),
                             json={"from_user_id": local + 1, "merge_token": offer["merge_token"]})
        self.assertEqual(r.status_code, 403)

    def test_owner_role_survives_a_merge(self):
        local = self.email_login("ravi@example.com", name="Ravi E")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf"}, headers=self.h(local)).json()
        offer = self._offer()
        self.client.post("/api/v1/auth/email/merge", headers=self.h(TG_MEMBER),
                         json={"from_user_id": local, "merge_token": offer["merge_token"]})
        self.assertEqual(self.db.get_web_admin_role(g["chat_id"], TG_MEMBER), "owner")
        self.assertIsNone(self.db.get_web_admin_role(g["chat_id"], local))


class TestJoinApproval(_Base):

    def test_web_only_groups_approve_their_members(self):
        from services.web import vote_by_token, _ensure_web_token
        from services import rollcalls as rc_svc
        from rollcall_manager import manager
        from exceptions import insufficientPermissions
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        ben = self.email_login("ben@example.com", name="Ben")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf"}, headers=self.h(asha)).json()
        gt, cid = g["group_token"], g["chat_id"]
        r = self.client.post(f"/api/v1/web/group/{gt}/join", json={}, headers=self.h(ben)).json()
        self.assertEqual((r["joined"], r["pending"]), (False, True))
        asyncio.run(rc_svc.start_rollcall(cid, "Game", asha, "Asha"))
        token = _ensure_web_token(manager.get_rollcall(cid, 0))
        with self.assertRaises(insufficientPermissions):
            asyncio.run(vote_by_token(token, "Ben", "in", tg_user_id=ben))
        with self.assertRaises(insufficientPermissions):
            asyncio.run(vote_by_token(token, "Walk-in", "in"))                # guest by name
        reqs = self.client.get(f"/api/v1/web/group/{gt}/join-requests", headers=self.h(asha)).json()["requests"]
        self.assertEqual([q["user_id"] for q in reqs], [ben])
        self.assertEqual(self.client.get(f"/api/v1/web/group/{gt}/join-requests",
                                         headers=self.h(ben)).status_code, 403)
        r = self.client.post(f"/api/v1/web/group/{gt}/join-requests/decide", headers=self.h(asha),
                             json={"user_id": ben, "approve": True})
        self.assertEqual(r.status_code, 200, r.text)
        asyncio.run(vote_by_token(token, "Ben", "in", tg_user_id=ben))
        self.assertEqual([u.user_id for u in manager.get_rollcall(cid, 0).inList], [ben])

    def test_denied_stays_denied_and_telegram_groups_default_open(self):
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        ben = self.email_login("ben@example.com", name="Ben")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf"}, headers=self.h(asha)).json()
        gt = g["group_token"]
        self.client.post(f"/api/v1/web/group/{gt}/join", json={}, headers=self.h(ben))
        self.client.post(f"/api/v1/web/group/{gt}/join-requests/decide", headers=self.h(asha),
                         json={"user_id": ben, "approve": False})
        self.client.post(f"/api/v1/web/group/{gt}/join", json={}, headers=self.h(ben))
        self.assertEqual(self.client.get(f"/api/v1/web/group/{gt}/join-requests",
                                         headers=self.h(asha)).json()["requests"], [])
        token = self.db.get_or_create_chat(TG_CHAT)["group_web_token"]
        self.assertFalse(self.client.get(f"/api/v1/web/group/{token}").json()["join_approval"])
        r = self.client.post(f"/api/v1/web/group/{token}/join", json={}, headers=self.h(ben)).json()
        self.assertTrue(r["joined"])


class TestMergeFromEmailSession(_Base):
    """Signed in by email, then Telegram on the same device: the page holds
    both tokens and can merge without another email."""

    def test_merge_with_both_sign_ins(self):
        from rollcall_manager import manager
        from services.web import vote_by_token, _ensure_web_token
        from services import rollcalls as rc_svc
        self.db.upsert_chat_member(TG_CHAT, TG_MEMBER, "Ravi")
        asyncio.run(rc_svc.start_rollcall(TG_CHAT, "Friday", TG_MEMBER, "Ravi"))
        local = self.email_login("ravi@example.com", name="Ravi E")
        asyncio.run(vote_by_token(_ensure_web_token(manager.get_rollcall(TG_CHAT, 0)), "Ravi E", "in",
                                  tg_user_id=local["user_id"]))
        body = {"local_id_token": local["id_token"]}
        pre = self.client.post("/api/v1/auth/account/merge-local/preview", json=body, headers=self.h(TG_MEMBER))
        self.assertEqual((pre.status_code, pre.json()["votes"]), (200, 1), pre.text)
        r = self.client.post("/api/v1/auth/account/merge-local", json=body, headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([u.user_id for u in manager.get_rollcall(TG_CHAT, 0).inList], [TG_MEMBER])
        self.assertEqual(self.email_login("ravi@example.com")["user_id"], TG_MEMBER)
        # Without a Telegram sign-in — an email account presenting itself — refused.
        other = self.email_login("someone@example.com", name="Someone")
        r = self.client.post("/api/v1/auth/account/merge-local", headers=self.h(other["user_id"]),
                             json={"local_id_token": other["id_token"]})
        self.assertEqual(r.status_code, 403)


class TestOwnerMergeTool(_Base):

    def test_owner_merges_an_account_that_lives_only_in_their_group(self):
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        dup = self.email_login("asha2@example.com", name="Asha 2")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf"}, headers=self.h(asha)).json()
        gt, cid = g["group_token"], g["chat_id"]
        self.db.upsert_chat_member(cid, dup, "Asha 2")
        r = self.client.post(f"/api/v1/web/group/{gt}/accounts/merge", headers=self.h(asha),
                             json={"from_user_id": dup, "into_user_id": asha})
        self.assertEqual(r.status_code, 200, r.text)
        from api.identity import verify_identity_token
        self.assertEqual(verify_identity_token(self.issue(dup)), asha)

    def test_refused_when_the_account_is_in_another_group_or_caller_isnt_owner(self):
        asha = self.email_login("asha@example.com", name="Asha")["user_id"]
        ben = self.email_login("ben@example.com", name="Ben")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf"}, headers=self.h(asha)).json()
        other = self.client.post("/api/v1/web/groups", json={"name": "Elsewhere"}, headers=self.h(ben)).json()
        self.db.upsert_chat_member(g["chat_id"], ben, "Ben")   # Ben is also in his own group
        r = self.client.post(f"/api/v1/web/group/{g['group_token']}/accounts/merge", headers=self.h(asha),
                             json={"from_user_id": ben, "into_user_id": asha})
        self.assertEqual(r.status_code, 403)
        self.assertIn("other groups", r.json()["detail"])
        self.db.set_web_admin(g["chat_id"], ben, "Ben")         # an admin, not an owner
        r = self.client.post(f"/api/v1/web/group/{g['group_token']}/accounts/merge", headers=self.h(ben),
                             json={"from_user_id": ben, "into_user_id": asha})
        self.assertEqual(r.status_code, 403)
        _ = other


class TestEmailSendLimits(_Base):

    def test_per_ip_limit_across_many_addresses(self):
        from services import email_auth
        for i in range(email_auth.MAX_SENDS_PER_IP_HOUR):
            self.assertEqual(self.client.post("/api/v1/auth/email/start",
                                              json={"email": f"u{i}@example.com"}).status_code, 202)
        r = self.client.post("/api/v1/auth/email/start", json={"email": "one-more@example.com"})
        self.assertEqual(r.status_code, 429)
        self.assertIn("from here", r.json()["detail"])

    def test_deployment_wide_ceiling(self):
        from services import email_auth
        with patch.object(email_auth, "MAX_SENDS_PER_IP_HOUR", 1000), \
             patch.object(email_auth, "MAX_SENDS_PER_HOUR", 2):
            for i in range(2):
                self.client.post("/api/v1/auth/email/start", json={"email": f"v{i}@example.com"})
            r = self.client.post("/api/v1/auth/email/start", json={"email": "v9@example.com"})
        self.assertEqual(r.status_code, 429)


class TestTraffic(_Base):

    def test_hits_are_counted_flushed_and_peaked(self):
        from services import traffic
        traffic._counts.clear()
        for _ in range(3):
            self.client.get("/api/v1/auth/email/config")
        from bot_state import note_update
        note_update("message")
        self.assertEqual(traffic.flush(), 2)        # one row per source this hour
        traffic.count("rest", 5)                     # still in memory: included too
        p = traffic.peaks()
        self.assertGreaterEqual(p["rest"]["peak"], 8)
        self.assertGreaterEqual(p["telegram"]["last_24h"], 1)
        self.assertIn("busiest hour", traffic.summary_line())


class TestBuzzMuteWeb(_Base):

    def test_admin_sees_and_unmutes_skipped_members(self):
        from services import buzz_mute, rollcalls as rc_svc
        from rollcall_manager import manager
        token = self.db.get_or_create_chat(TG_CHAT)["group_web_token"]
        self.db.upsert_chat_member(TG_CHAT, TG_MEMBER, "Ravi")
        self.db.upsert_chat_member(TG_CHAT, 7002, "Quiet")
        self.db.set_web_admin(TG_CHAT, TG_MEMBER, "Ravi")
        for g in range(3):
            asyncio.run(rc_svc.start_rollcall(TG_CHAT, f"G{g}", TG_MEMBER, "Ravi"))
            buzz_mute.record(TG_CHAT, [manager.get_rollcall(TG_CHAT, 0).id], [7002])
            asyncio.run(rc_svc.end_rollcall(TG_CHAT, 0, TG_MEMBER, "Ravi"))
        with patch("api.web_admin.check_web_admin_live", new=AsyncMock(return_value=True)):
            d = self.client.get(f"/api/v1/web/group/{token}/buzz-muted", headers=self.h(TG_MEMBER)).json()
            self.assertEqual([m["user_id"] for m in d["muted"]], [7002])
            from services.lists import get_non_responders
            asyncio.run(rc_svc.start_rollcall(TG_CHAT, "Now", TG_MEMBER, "Ravi"))
            svc = get_non_responders(TG_CHAT, 0)
            self.assertNotIn(7002, [c["user_id"] for c in svc["candidates"]])
            self.assertIn(7002, [c["user_id"] for c in svc["muted"]])
            r = self.client.post(f"/api/v1/web/group/{token}/buzz-muted/unmute", headers=self.h(TG_MEMBER),
                                 json={"user_id": 7002})
            self.assertEqual(r.status_code, 204)
        self.assertEqual(buzz_mute.muted_ids(TG_CHAT), set())



class TestBuzzCountsBuzzesNotRollcalls(_Base):

    def test_one_buzz_over_three_rollcalls_is_one_unanswered_buzz(self):
        from services import buzz_mute, rollcalls as rc_svc
        from rollcall_manager import manager
        self.db.get_or_create_chat(TG_CHAT)
        for t in ("A", "B", "C"):
            asyncio.run(rc_svc.start_rollcall(TG_CHAT, t, TG_MEMBER, "Ravi"))
        buzz_mute.record(TG_CHAT, [rc.id for rc in manager.get_rollcalls(TG_CHAT)], [7002])
        for _ in range(3):
            asyncio.run(rc_svc.end_rollcall(TG_CHAT, 0, TG_MEMBER, "Ravi"))
        self.assertEqual(buzz_mute.unanswered(TG_CHAT), {7002: 1})
        self.assertEqual(buzz_mute.muted_ids(TG_CHAT), set())
