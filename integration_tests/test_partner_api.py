"""
Partner API: app keys made on the web page, and webhooks.

Real DB, real services, FastAPI TestClient; the HTTP send is replaced so no
request leaves the machine (services.webhooks._post).
"""
import asyncio
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
CHAT = -1001999000801
OWNER, ADMIN, MEMBER = 9001, 9002, 9003


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import bot_state  # noqa: F401
        import rollcall_manager
        from api.main import app
        from api.identity import issue_identity_token
        import db
        cls.manager = rollcall_manager.manager
        cls.client = TestClient(app)
        cls.issue = staticmethod(issue_identity_token)
        cls.db = db

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        from services import webhooks
        webhooks._invalidate()
        webhooks._rc_chat.clear()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        chat = self.db.get_or_create_chat(CHAT)
        self.gt = chat["group_web_token"]
        for uid, name in ((OWNER, "Olga"), (ADMIN, "Adam"), (MEMBER, "Mia")):
            self.db.upsert_chat_member(CHAT, uid, name)
        self.db.set_web_admin(CHAT, OWNER, "Olga")
        self.db.set_web_admin_role(CHAT, OWNER, "owner")
        self.db.set_web_admin(CHAT, ADMIN, "Adam")
        self.enterContext(patch("api.web_admin.check_web_admin_live",
                                new=AsyncMock(side_effect=lambda c, u: u in (OWNER, ADMIN))))
        self.enterContext(patch("api.auth._issuer_still_admin", new=AsyncMock(return_value=True)))

    def h(self, uid):
        return {"X-Identity-Token": self.issue(uid)}

    def url(self, tail):
        return f"/api/v1/web/group/{self.gt}/{tail}"


class TestAppKeys(_Base):

    def test_owner_makes_keys_admins_only_read_vote(self):
        r = self.client.post(self.url("api-keys"), headers=self.h(ADMIN),
                             json={"label": "Club site", "scopes": ["read", "admin"]})
        self.assertEqual(r.status_code, 403)
        r = self.client.post(self.url("api-keys"), headers=self.h(ADMIN),
                             json={"label": "Club site", "scopes": ["read", "vote"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["token"].startswith("rc_"))
        r = self.client.post(self.url("api-keys"), headers=self.h(OWNER),
                             json={"label": "Ops", "scopes": ["admin"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.post(self.url("api-keys"), headers=self.h(MEMBER),
                                          json={"label": "x", "scopes": ["read"]}).status_code, 403)
        keys = self.client.get(self.url("integrations"), headers=self.h(OWNER)).json()["keys"]
        self.assertEqual(sorted(k["label"] for k in keys), ["Club site", "Ops"])
        self.assertNotIn("token", keys[0])

    def test_an_app_key_votes_for_members_and_can_be_revoked(self):
        key = self.client.post(self.url("api-keys"), headers=self.h(OWNER),
                               json={"label": "Club site", "scopes": ["read", "vote"]}).json()
        auth = {"Authorization": f"Bearer {key['token']}"}
        from services import rollcalls as rc_svc
        asyncio.run(rc_svc.start_rollcall(CHAT, "Game", OWNER, "Olga"))
        rc_id = self.manager.get_rollcall(CHAT, 0).id
        r = self.client.post(f"/api/v1/chats/{CHAT}/rollcalls/by-id/{rc_id}/votes", headers=auth,
                             json={"vote": "in", "user_id": MEMBER, "first_name": "Mia"})
        self.assertEqual(r.status_code, 201, r.text)
        self.client.post(self.url(f"api-keys/{key['key_id']}/revoke"), headers=self.h(OWNER))
        r = self.client.get(f"/api/v1/chats/{CHAT}/rollcalls", headers=auth)
        self.assertEqual(r.status_code, 401)


class TestWebhooks(_Base):

    def _hook(self, url="https://partner.example.com/hooks", events=None):
        r = self.client.post(self.url("webhooks"), headers=self.h(OWNER),
                             json={"url": url, **({"events": events} if events else {})})
        return r

    def _pending(self):
        with self.db._cursor() as c:
            c.execute("SELECT event, payload, status, attempts FROM webhook_deliveries ORDER BY id")
            return [dict(r) if isinstance(r, dict) else dict(zip(("event", "payload", "status", "attempts"), r))
                    for r in c.fetchall()]

    def test_urls_must_be_public_https(self):
        for bad in ("http://partner.example.com/h", "https://127.0.0.1/h", "https://10.1.2.3/h",
                    "https://localhost/h", "https://printer.local/h"):
            self.assertEqual(self._hook(bad).status_code, 422, bad)
        self.assertEqual(self._hook().status_code, 200)

    def test_every_surface_queues_events_only_for_hooked_groups(self):
        from services import rollcalls as rc_svc, voting
        asyncio.run(rc_svc.start_rollcall(CHAT, "Before", OWNER, "Olga"))   # no webhook yet
        self.assertEqual(self._pending(), [])
        ep = self._hook().json()
        self.assertTrue(ep["secret"].startswith("whsec_"))
        asyncio.run(rc_svc.start_rollcall(CHAT, "Game", OWNER, "Olga"))
        rc = self.manager.get_rollcall(CHAT, 1)
        asyncio.run(voting.vote_in(CHAT, MEMBER, "Mia", rollcall_id=rc.id))
        self.db.add_dues_entry(CHAT, rc.id, MEMBER, "Mia", "game_charge", 150, "Game", OWNER, "Olga")
        asyncio.run(rc_svc.end_rollcall(CHAT, 1, OWNER, "Olga"))
        events = [d["event"] for d in self._pending()]
        self.assertEqual(events[0], "rollcall.opened")
        for e in ("vote.changed", "dues.posted", "rollcall.closed"):
            self.assertIn(e, events)
        vote = next(json.loads(d["payload"]) for d in self._pending() if d["event"] == "vote.changed")
        self.assertEqual((vote["data"]["member"]["user_id"], vote["data"]["status"], vote["data"]["rollcall_id"]),
                         (MEMBER, "in", rc.id))

    def test_a_rolled_back_write_sends_nothing(self):
        self._hook()
        with self.assertRaises(RuntimeError):
            with self.db._cursor(commit=True):
                self.db._webhook("vote.changed", chat_id=CHAT, data={})
                raise RuntimeError("boom")
        self.assertEqual(self._pending(), [])

    def test_delivery_is_signed_retried_then_switched_off(self):
        from services import webhooks
        ep = self._hook(events=["dues.posted"]).json()
        sent = []

        async def ok(url, secret, delivery_id, event, body):
            sent.append((secret, body))
            return None
        self.db.add_dues_entry(CHAT, None, MEMBER, "Mia", "adjustment", 50, "x", OWNER, "Olga")
        with patch.object(webhooks, "_post", new=ok):
            asyncio.run(webhooks.deliver_due())
        self.assertEqual(len(sent), 1)
        self.assertEqual(json.loads(sent[0][1])["type"], "dues.posted")
        # The signature a receiver checks: HMAC-SHA256(secret, "<t>.<body>").
        sig = webhooks.sign(ep["secret"], 1700000000, sent[0][1])
        expected = hmac.new(ep["secret"].encode(), b"1700000000." + sent[0][1], hashlib.sha256).hexdigest()
        self.assertEqual(sig, f"t=1700000000,v1={expected}")
        self.assertEqual(self._pending()[0]["status"], "delivered")

        # A failing receiver: retried with backoff, given up after MAX_ATTEMPTS,
        # and the endpoint switched off after DISABLE_AFTER_FAILURES give-ups.
        with patch.object(webhooks, "DISABLE_AFTER_FAILURES", 1), \
             patch.object(webhooks, "_post", new=AsyncMock(return_value="HTTP 500")):
            self.db.add_dues_entry(CHAT, None, MEMBER, "Mia", "adjustment", 60, "y", OWNER, "Olga")
            for _ in range(webhooks.MAX_ATTEMPTS):
                with self.db._cursor(commit=True) as c:
                    c.execute("UPDATE webhook_deliveries SET next_attempt = 0 WHERE status = 'pending'")
                asyncio.run(webhooks.deliver_due())
        last = self._pending()[-1]
        self.assertEqual((last["status"], last["attempts"]), ("failed", webhooks.MAX_ATTEMPTS))
        hooks = self.client.get(self.url("integrations"), headers=self.h(OWNER)).json()["webhooks"]
        self.assertFalse(hooks[0]["active"])
        self.assertEqual(hooks[0]["last_status"], "HTTP 500")

    def test_ping_and_delete(self):
        ep = self._hook(events=["vote.changed"]).json()
        r = self.client.post(self.url(f"webhooks/{ep['id']}/test"), headers=self.h(OWNER))
        self.assertEqual(r.json()["queued"], 1)
        self.assertEqual(self._pending()[-1]["event"], "ping")
        self.assertEqual(self.client.post(self.url(f"webhooks/{ep['id']}/delete"),
                                          headers=self.h(OWNER)).status_code, 204)
        self.assertEqual(self._pending(), [])
        self.assertEqual(self.client.post(self.url("webhooks/999/test"), headers=self.h(OWNER)).status_code, 422)


class TestPublicOnlyResolver(unittest.TestCase):

    def test_private_answers_are_dropped_at_connect_time(self):
        from types import SimpleNamespace
        from services.webhooks import _PublicOnlyResolver
        fake = [{"host": "10.0.0.5", "port": 443}, {"host": "93.184.216.34", "port": 443}]
        r = _PublicOnlyResolver(inner=SimpleNamespace(resolve=AsyncMock(return_value=fake)))
        self.assertEqual([h["host"] for h in asyncio.run(r.resolve("x.example"))], ["93.184.216.34"])
        r = _PublicOnlyResolver(inner=SimpleNamespace(resolve=AsyncMock(return_value=fake[:1])))
        with self.assertRaises(OSError):
            asyncio.run(r.resolve("x.example"))
