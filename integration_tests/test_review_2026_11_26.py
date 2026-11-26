"""
Review 2026-11-26 — partner API key lifecycle, gateway conventions, and the
membership / account gaps found for people without Telegram.

Real DB and services through FastAPI's TestClient; only mail sending, the
Telegram admin check and push are replaced.
"""
import asyncio
import os
import re
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
CHAT = -1001999000926
OWNER, ADMIN, MEMBER = 9101, 9102, 9103
EMAIL_ENV = {"TELEGRAM_TOKEN": BOT_TOKEN, "SMTP_HOST": "smtp.test", "SMTP_FROM": "bot@test",
             "WEB_BASE_URL": "https://rc.test"}
PARTNER_IP = ("203.0.113.7", 40000)


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import bot_state  # noqa: F401
        import rollcall_manager
        from api.main import app
        from api.identity import issue_identity_token
        import db
        cls.app = app
        cls.manager = rollcall_manager.manager
        cls.client = TestClient(app)
        cls.partner = TestClient(app, client=PARTNER_IP)
        cls.issue = staticmethod(issue_identity_token)
        cls.db = db

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        from api.auth import reset_admin_recheck_cache_for_tests
        reset_admin_recheck_cache_for_tests()
        from services.account import reset_cache_for_tests
        reset_cache_for_tests()
        from services.email_auth import reset_send_limits_for_tests
        reset_send_limits_for_tests()
        self.enterContext(patch.dict(os.environ, EMAIL_ENV))
        self.mails = []

        async def _fake_send(to, subject, text, headers=None):
            self.mails.append({"to": to, "subject": subject, "text": text, "headers": headers or {}})
        self.enterContext(patch("services.email_auth.send_mail", new=_fake_send))
        chat = self.db.get_or_create_chat(CHAT)
        self.gt = chat["group_web_token"]
        for uid, name in ((OWNER, "Olga"), (ADMIN, "Adam"), (MEMBER, "Mia")):
            self.db.upsert_chat_member(CHAT, uid, name)
        self.db.set_web_admin(CHAT, OWNER, "Olga")
        self.db.set_web_admin_role(CHAT, OWNER, "owner")
        self.db.set_web_admin(CHAT, ADMIN, "Adam")
        self.admins = {OWNER, ADMIN}
        self.enterContext(patch("api.web_admin.check_web_admin_live",
                                new=AsyncMock(side_effect=lambda c, u: u in self.admins)))

    def h(self, uid_or_token):
        tok = uid_or_token if isinstance(uid_or_token, str) else self.issue(uid_or_token)
        return {"X-Identity-Token": tok}

    def url(self, tail, gt=None):
        return f"/api/v1/web/group/{gt or self.gt}/{tail}"

    def key(self, who=OWNER, **body):
        body.setdefault("label", "Club site")
        body.setdefault("scopes", ["read", "vote"])
        r = self.client.post(self.url("api-keys"), headers=self.h(who), json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def bearer(self, key):
        return {"Authorization": f"Bearer {key['token']}"}

    def start(self, title="Game"):
        from services import rollcalls as rc_svc
        asyncio.run(rc_svc.start_rollcall(CHAT, title, OWNER, "Olga"))
        return self.manager.get_rollcalls(CHAT)[-1].id

    def code(self):
        return re.search(r"email_code=([\w-]+)", self.mails[-1]["text"]).group(1)

    def email_login(self, email, name="Asha"):
        r = self.client.post("/api/v1/auth/email/start", json={"email": email, "name": name})
        self.assertEqual(r.status_code, 202, r.text)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


# ── Partner keys: agreement, allowlist, issuer standing, rotation ────────────

class TestKeyLifecycle(_Base):

    def test_key_records_the_agreement_it_was_issued_under(self):
        k = self.key(partner="Playfield Ltd", contact="ops@playfield.example",
                     agreement_ref="AGR-2026-014", allowed_ips=["203.0.113.0/24"])
        self.assertEqual(k["allowed_ips"], ["203.0.113.0/24"])
        listed = self.client.get(self.url("integrations"), headers=self.h(OWNER)).json()["keys"][0]
        self.assertEqual((listed["partner"], listed["agreement_ref"], listed["status"], listed["kind"]),
                         ("Playfield Ltd", "AGR-2026-014", "active", "app"))
        for bad in (["not-an-ip"], ["0.0.0.0/0"]):
            r = self.client.post(self.url("api-keys"), headers=self.h(OWNER),
                                 json={"label": "x", "scopes": ["read"], "allowed_ips": bad})
            self.assertEqual(r.status_code, 422, bad)

    def test_allowlisted_key_is_refused_from_other_addresses(self):
        k = self.key(allowed_ips=["203.0.113.0/24"])
        self.assertEqual(self.partner.get(f"/api/v1/chats/{CHAT}/rollcalls", headers=self.bearer(k)).status_code, 200)
        r = self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers=self.bearer(k))   # "testclient"
        self.assertEqual(r.status_code, 403)
        listed = self.client.get(self.url("integrations"), headers=self.h(OWNER)).json()["keys"][0]
        self.assertEqual(listed["last_used_ip"], "203.0.113.7")

    def test_every_app_key_dies_with_its_issuers_admin_standing(self):
        k = self.key(who=ADMIN, scopes=["read"])
        self.assertEqual(self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers=self.bearer(k)).status_code, 200)
        self.admins.discard(ADMIN)
        from api.auth import reset_admin_recheck_cache_for_tests
        reset_admin_recheck_cache_for_tests()
        r = self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers=self.bearer(k))
        self.assertEqual(r.status_code, 403)
        self.assertIn("no longer an admin", r.json()["detail"])

    def test_rotation_keeps_the_old_key_for_a_grace_period(self):
        old = self.key(partner="Playfield Ltd", agreement_ref="AGR-1", days=365)
        r = self.client.post(self.url(f"api-keys/{old['key_id']}/rotate"), headers=self.h(OWNER), json={})
        self.assertEqual(r.status_code, 200, r.text)
        new = r.json()
        self.assertNotEqual(new["token"], old["token"])
        self.assertEqual((new["partner"], new["agreement_ref"]), ("Playfield Ltd", "AGR-1"))
        for k in (old, new):
            self.assertEqual(self.client.get(f"/api/v1/chats/{CHAT}/rollcalls",
                                             headers=self.bearer(k)).status_code, 200)
        keys = {k["key_id"]: k for k in self.client.get(self.url("integrations"), headers=self.h(OWNER)).json()["keys"]}
        self.assertEqual(keys[old["key_id"]]["rotated_to"], new["key_id"])
        grace_end = datetime.fromisoformat(keys[old["key_id"]]["expires_at"].split(".")[0])
        self.assertLess(grace_end, datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=7, minutes=1))
        r = self.client.post(self.url(f"api-keys/{old['key_id']}/rotate"), headers=self.h(OWNER), json={})
        self.assertEqual(r.status_code, 422)

    def test_admin_cannot_rotate_an_owners_admin_key(self):
        k = self.key(scopes=["admin"])
        r = self.client.post(self.url(f"api-keys/{k['key_id']}/rotate"), headers=self.h(ADMIN), json={})
        self.assertEqual(r.status_code, 403)

    def test_gentoken_tokens_are_listed_and_revocable(self):
        import db
        tok = db.generate_api_token()
        db.insert_api_token(db._hash_token(tok), CHAT, "admin,read,vote", label="Grp — /gentoken by Olga",
                            issued_by_user_id=OWNER,
                            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30))
        keys = self.client.get(self.url("integrations"), headers=self.h(OWNER)).json()["keys"]
        personal = [k for k in keys if k["kind"] == "personal"]
        self.assertEqual(len(personal), 1)
        self.client.post(self.url(f"api-keys/{personal[0]['key_id']}/revoke"), headers=self.h(OWNER))
        r = self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 401)

    def test_expiry_notices_go_once_per_threshold(self):
        k = self.key(days=10)
        from services import partner_api
        with patch("services.push.notify_users", new=AsyncMock(return_value=1)) as notify:
            self.assertEqual(asyncio.run(partner_api.notify_expiring()), 1)
            self.assertEqual(asyncio.run(partner_api.notify_expiring()), 0)
        who = notify.call_args.args[1]
        self.assertIn(OWNER, who)
        self.assertIn("Club site", notify.call_args.args[3])
        # Inside 3 days the second notice goes.
        with self.db._cursor(commit=True) as c:
            ph = "%s" if self.db.db_type == "postgresql" else "?"
            c.execute(f"UPDATE api_tokens SET expires_at={ph} WHERE app_key=1",
                      (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2),))
        with patch("services.push.notify_users", new=AsyncMock(return_value=1)):
            self.assertEqual(asyncio.run(partner_api.notify_expiring()), 1)
        self.assertTrue(k)


# ── Gateway: request ids, error shape, rate-limit + expiry headers, idempotency ─

class TestGateway(_Base):

    def test_errors_have_one_shape_with_a_request_id(self):
        r = self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers={"Authorization": "Bearer nope"})
        self.assertEqual(r.status_code, 401)
        body = r.json()
        self.assertEqual(body["error"], "unauthorized")
        self.assertEqual(body["request_id"], r.headers["X-Request-Id"])
        r = self.client.get("/api/v1/web/version", headers={"X-Request-Id": "partner-abc.123"})
        self.assertEqual(r.headers["X-Request-Id"], "partner-abc.123")
        r = self.client.get("/api/v1/web/version", headers={"X-Request-Id": "bad id with spaces"})
        self.assertNotEqual(r.headers["X-Request-Id"], "bad id with spaces")

    def test_rate_limit_and_key_expiry_headers(self):
        k = self.key(days=30)
        r = self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers=self.bearer(k))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-RateLimit-Limit"], "60")
        self.assertEqual(r.headers["X-RateLimit-Remaining"], "59")
        self.assertTrue(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", r.headers["X-RollCall-Key-Expires"]))

    def test_replays_are_rate_limited_and_429_has_a_request_id(self):
        k = self.key(scopes=["admin"])
        hdr = {**self.bearer(k), "Idempotency-Key": "same"}
        body = {"title": "Friday", "started_by_user_id": OWNER, "started_by_name": "Olga"}
        with patch.dict(os.environ, {"REST_API_RATE_LIMIT_MAX_REQUESTS": "2"}):
            codes = [self.client.post(f"/api/v1/chats/{CHAT}/rollcalls", headers=hdr, json=body)
                     for _ in range(3)]
        self.assertEqual(codes[2].status_code, 429)
        self.assertEqual(codes[2].json()["request_id"], codes[2].headers["X-Request-Id"])

    def test_idempotency_key_replays_instead_of_repeating(self):
        k = self.key(who=OWNER, scopes=["admin"])
        hdr = {**self.bearer(k), "Idempotency-Key": "start-fri-001"}
        body = {"title": "Friday", "started_by_user_id": OWNER, "started_by_name": "Olga"}
        r1 = self.client.post(f"/api/v1/chats/{CHAT}/rollcalls", headers=hdr, json=body)
        self.assertIn(r1.status_code, (200, 201), r1.text)
        r2 = self.client.post(f"/api/v1/chats/{CHAT}/rollcalls", headers=hdr, json=body)
        self.assertEqual((r2.status_code, r2.json()), (r1.status_code, r1.json()))
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(len(self.manager.get_rollcalls(CHAT)), 1)
        r3 = self.client.post(f"/api/v1/chats/{CHAT}/rollcalls", headers=hdr, json={**body, "title": "Other"})
        self.assertEqual(r3.status_code, 422)
        self.assertEqual(r3.json()["error"], "idempotencyKeyReused")
        from api import partner_gateway
        self.assertEqual(partner_gateway.prune(now=10**10), 1)


# ── Votes by app key ──────────────────────────────────────────────────────────

class TestAppKeyVotes(_Base):

    def test_only_current_members_and_under_their_own_name(self):
        k = self.key()
        rc_id = self.start()
        url = f"/api/v1/chats/{CHAT}/rollcalls/by-id/{rc_id}/votes"
        r = self.client.post(url, headers=self.bearer(k), json={"vote": "in", "user_id": MEMBER, "first_name": "Typo"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["user"]["name"], "Mia")
        self.db.mark_member_inactive(CHAT, MEMBER)
        r = self.client.post(url, headers=self.bearer(k), json={"vote": "out", "user_id": MEMBER, "first_name": "Mia"})
        self.assertEqual(r.status_code, 403)


class TestWebhookReenable(_Base):

    def test_only_an_owner_switches_a_webhook_back_on(self):
        hook = self.client.post(self.url("webhooks"), headers=self.h(OWNER),
                                json={"url": "https://partner.example.com/h"}).json()
        off = self.client.post(self.url(f"webhooks/{hook['id']}/active"), headers=self.h(ADMIN), json={"active": False})
        self.assertEqual(off.status_code, 204)
        on = self.client.post(self.url(f"webhooks/{hook['id']}/active"), headers=self.h(ADMIN), json={"active": True})
        self.assertEqual(on.status_code, 403)
        on = self.client.post(self.url(f"webhooks/{hook['id']}/active"), headers=self.h(OWNER), json={"active": True})
        self.assertEqual(on.status_code, 204)


# ── People without Telegram: membership, leaving, removal ────────────────────

class TestMembership(_Base):

    def setUp(self):
        super().setUp()
        self.enterContext(patch("services.human_check.mode", new=lambda: "off"))
        owner = self.email_login("owner@example.com", "Ola")
        self.owner_uid, self.owner_tok = owner["user_id"], owner["id_token"]
        r = self.client.post("/api/v1/web/groups", headers=self.h(self.owner_tok), json={"name": "Park FC"})
        self.assertEqual(r.status_code, 201, r.text)
        self.wgt = r.json()["group_token"]
        self.wchat = r.json()["chat_id"]
        self.admins.add(self.owner_uid)
        guest = self.email_login("ravi@example.com", "Ravi")
        self.ravi_uid, self.ravi_tok = guest["user_id"], guest["id_token"]

    def ms(self, tok):
        return self.client.get(self.url("membership", self.wgt), headers=self.h(tok)).json()

    def join(self, tok):
        return self.client.post(self.url("join", self.wgt), headers=self.h(tok), json={}).json()

    def approve(self, uid, yes=True):
        return self.client.post(self.url("join-requests/decide", self.wgt), headers=self.h(self.owner_tok),
                                json={"user_id": uid, "approve": yes})

    def test_ask_approve_leave_and_ask_again(self):
        with patch("services.push.notify_users", new=AsyncMock(return_value=0)) as notify:
            self.assertTrue(self.join(self.ravi_tok)["pending"])
        self.assertIn(self.owner_uid, set(notify.call_args.args[1]))   # the owner hears about it
        self.assertEqual(self.ms(self.ravi_tok)["pending"], True)
        self.assertEqual(self.approve(self.ravi_uid).status_code, 200)
        m = self.ms(self.ravi_tok)
        self.assertEqual((m["member"], m["can_leave"]), (True, True))
        r = self.client.post(self.url("leave", self.wgt), headers=self.h(self.ravi_tok))
        self.assertEqual(r.status_code, 204, r.text)
        self.assertFalse(self.ms(self.ravi_tok)["member"])
        # Gone means gone: an approval-gated group doesn't take their vote...
        from services import local_groups
        from exceptions import insufficientPermissions
        info = self.db.get_member_display_info(self.wchat, self.ravi_uid)
        with self.assertRaises(insufficientPermissions):
            local_groups.gate_web_vote(self.wchat, self.ravi_uid, info, "Ravi")
        # ...and coming back is a new request, not an automatic way in.
        self.assertTrue(self.join(self.ravi_tok)["pending"])

    def test_denied_stays_denied(self):
        self.join(self.ravi_tok)
        self.approve(self.ravi_uid, yes=False)
        j = self.join(self.ravi_tok)
        self.assertEqual((j["joined"], j["pending"], j["denied"]), (False, False, True))
        self.assertTrue(self.ms(self.ravi_tok)["denied"])

    def test_admin_removes_a_member_but_not_the_only_owner(self):
        self.join(self.ravi_tok)
        self.approve(self.ravi_uid)
        listed = self.client.get(self.url("members/removable", self.wgt), headers=self.h(self.owner_tok)).json()
        self.assertEqual({m["user_id"] for m in listed["members"]}, {self.owner_uid, self.ravi_uid})
        r = self.client.post(self.url("members/remove", self.wgt), headers=self.h(self.owner_tok),
                             json={"user_id": self.ravi_uid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.db.is_active_chat_member(self.wchat, self.ravi_uid))
        r = self.client.post(self.url("members/remove", self.wgt), headers=self.h(self.owner_tok),
                             json={"user_id": self.owner_uid})
        self.assertEqual(r.status_code, 422)     # yourself: use Leave
        r = self.client.post(self.url("leave", self.wgt), headers=self.h(self.owner_tok))
        self.assertEqual(r.status_code, 403)     # the only owner can't leave
        # Non-admins can't remove anyone.
        r = self.client.post(self.url("members/remove", self.wgt), headers=self.h(self.ravi_tok),
                             json={"user_id": self.owner_uid})
        self.assertEqual(r.status_code, 403)

    def test_telegram_members_of_a_telegram_group_are_removed_in_telegram(self):
        r = self.client.post(self.url("members/remove"), headers=self.h(OWNER), json={"user_id": MEMBER})
        self.assertEqual(r.status_code, 422)


# ── Account: merge after sign-out, delete revokes keys ──────────────────────

class TestAccountEdges(_Base):

    def setUp(self):
        super().setUp()
        self.enterContext(patch("services.human_check.mode", new=lambda: "off"))

    def test_signed_out_email_session_cannot_be_merged(self):
        old = self.email_login("asha@example.com")
        r = self.client.post("/api/v1/auth/account/sign-out-everywhere", headers=self.h(old["id_token"]))
        self.assertEqual(r.status_code, 200)
        body = {"local_id_token": old["id_token"]}
        r = self.client.post("/api/v1/auth/account/merge-local/preview", headers=self.h(MEMBER), json=body)
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/v1/auth/account/merge-local", headers=self.h(MEMBER), json=body)
        self.assertEqual(r.status_code, 403)

    def test_deleting_an_account_revokes_the_keys_it_issued(self):
        me = self.email_login("ola@example.com", "Ola")
        r = self.client.post("/api/v1/web/groups", headers=self.h(me["id_token"]), json={"name": "Solo"})
        gt = r.json()["group_token"]
        chat_id = r.json()["chat_id"]
        self.admins.add(me["user_id"])
        k = self.client.post(f"/api/v1/web/group/{gt}/api-keys", headers=self.h(me["id_token"]),
                             json={"label": "x", "scopes": ["read"]}).json()
        self.assertEqual(self.client.get(f"/api/v1/chats/{chat_id}/rollcalls",
                                         headers=self.bearer(k)).status_code, 200)
        r = self.client.post("/api/v1/auth/account/delete", headers=self.h(me["id_token"]), json={"confirm": "DELETE"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get(f"/api/v1/chats/{chat_id}/rollcalls",
                                         headers=self.bearer(k)).status_code, 401)


class TestEmailEdges(_Base):

    def test_multiline_subject_is_one_line_and_unsubscribe_after_queue_is_honoured(self):
        from services import email_notify, principals
        pid = principals.for_email("asha@example.com", "Asha")
        self.assertTrue(email_notify.queue_raw(pid, "asha@example.com", "Friday\nFootball", "body", "open"))
        with self.db._cursor() as c:
            c.execute("SELECT subject FROM email_outbox")
            row = c.fetchone()
        self.assertEqual(row["subject"] if isinstance(row, dict) else row[0], "Friday Football")
        email_notify.set_pref(pid, email_notify.OFF)
        self.assertEqual(asyncio.run(email_notify.deliver_due()), 0)
        self.assertEqual(self.mails, [])


if __name__ == "__main__":
    unittest.main()
