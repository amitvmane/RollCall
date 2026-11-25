"""
The human check: guest votes and sign-in emails need a pass from a solved
proof-of-work (default) or Cloudflare Turnstile; signed-in people never do.
Real routes, real DB — the check switched ON here (the suite default is off).
"""
import asyncio
import hashlib
import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mock_helpers import reset_db

CHAT = -1001999000901
ENV = {"HUMAN_CHECK": "pow", "TELEGRAM_TOKEN": "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY",
       "SMTP_HOST": "smtp.test", "SMTP_FROM": "bot@test", "WEB_BASE_URL": "https://rc.test"}


def solve(challenge, bits):
    from services.human_check import _leading_zero_bits
    n = 0
    while _leading_zero_bits(hashlib.sha256(f"{challenge}:{n}".encode()).digest()) < bits:
        n += 1
    return str(n)


class TestHumanCheck(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import bot_state  # noqa: F401
        import rollcall_manager
        from api.main import app
        cls.client = TestClient(app)
        cls.manager = rollcall_manager.manager

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        from services import human_check, email_auth
        human_check.reset_for_tests()
        email_auth.reset_send_limits_for_tests()
        self.enterContext(patch.dict(os.environ, ENV))
        self.enterContext(patch("services.email_auth.send_mail", new=AsyncMock()))

    def get_pass(self):
        ch = self.client.get("/api/v1/human/challenge").json()
        r = self.client.post("/api/v1/human/verify",
                             json={"challenge": ch["challenge"], "nonce": solve(ch["challenge"], ch["bits"])})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["human_pass"]

    def guest_vote(self, headers=None):
        from services import rollcalls as rc_svc
        from services.web import _ensure_web_token
        import db
        db.get_or_create_chat(CHAT)
        if not self.manager.get_rollcalls(CHAT):
            asyncio.run(rc_svc.start_rollcall(CHAT, "Game", 1, "Admin"))
        token = _ensure_web_token(self.manager.get_rollcall(CHAT, 0))
        return self.client.post(f"/api/v1/web/{token}/vote", headers=headers or {},
                                json={"name": "Walk-in", "vote": "in"})

    def test_guest_vote_needs_a_pass(self):
        r = self.guest_vote()
        self.assertEqual(r.status_code, 428)
        self.assertEqual(r.headers["X-Human-Check"], "pow")
        self.assertEqual(self.guest_vote({"X-Human-Pass": self.get_pass()}).status_code, 200)

    def test_one_pass_covers_a_limited_number_of_actions(self):
        from services import human_check
        p = self.get_pass()
        with patch.object(human_check, "MAX_USES_PER_PASS", 2):
            for _ in range(2):
                self.assertTrue(human_check.use_pass(p))
            self.assertFalse(human_check.use_pass(p))
            self.assertEqual(self.guest_vote({"X-Human-Pass": p}).status_code, 428)

    def test_signed_in_votes_skip_the_check(self):
        from api.identity import issue_identity_token
        from services import rollcalls as rc_svc
        from services.web import _ensure_web_token
        import db
        db.get_or_create_chat(CHAT)
        db.upsert_chat_member(CHAT, 4242, "Member")
        asyncio.run(rc_svc.start_rollcall(CHAT, "Game", 1, "Admin"))
        token = _ensure_web_token(self.manager.get_rollcall(CHAT, 0))
        r = self.client.post(f"/api/v1/web/{token}/vote",
                             json={"name": "Member", "vote": "in", "id_token": issue_identity_token(4242)})
        self.assertEqual(r.status_code, 200, r.text)

    def test_email_start_needs_a_pass(self):
        self.assertEqual(self.client.post("/api/v1/auth/email/start", json={"email": "a@example.com"}).status_code, 428)
        r = self.client.post("/api/v1/auth/email/start", json={"email": "a@example.com"},
                             headers={"X-Human-Pass": self.get_pass()})
        self.assertEqual(r.status_code, 202)

    def test_a_challenge_works_once_and_wrong_answers_fail(self):
        ch = self.client.get("/api/v1/human/challenge").json()
        nonce = solve(ch["challenge"], ch["bits"])
        ok = self.client.post("/api/v1/human/verify", json={"challenge": ch["challenge"], "nonce": nonce})
        self.assertEqual(ok.status_code, 200)
        again = self.client.post("/api/v1/human/verify", json={"challenge": ch["challenge"], "nonce": nonce})
        self.assertEqual(again.status_code, 403)
        ch2 = self.client.get("/api/v1/human/challenge").json()
        bad = self.client.post("/api/v1/human/verify", json={"challenge": ch2["challenge"], "nonce": "1"})
        if solve(ch2["challenge"], ch2["bits"]) != "1":
            self.assertEqual(bad.status_code, 403)
        forged = ch2["challenge"].rsplit(".", 1)[0] + ".deadbeef"
        self.assertEqual(self.client.post("/api/v1/human/verify",
                                          json={"challenge": forged, "nonce": "0"}).status_code, 403)

    def test_expired_challenge_and_pass_from_another_scope_fail(self):
        from services import human_check
        from api.identity import issue_identity_token
        ch = human_check.new_challenge()
        nonce = solve(ch["challenge"], ch["bits"])
        with patch("services.human_check.time.time", return_value=10 ** 10):
            self.assertFalse(human_check._verify_pow(ch["challenge"], nonce))
        # An ordinary identity token is not a human pass.
        self.assertEqual(self.guest_vote({"X-Human-Pass": issue_identity_token(1)}).status_code, 428)

    def test_turnstile_mode_and_off(self):
        from services import human_check
        with patch.dict(os.environ, {"HUMAN_CHECK": "turnstile", "TURNSTILE_SITE_KEY": "site",
                                     "TURNSTILE_SECRET_KEY": "secret"}):
            self.assertEqual(self.client.get("/api/v1/human/config").json(), {"mode": "turnstile", "bits": None,
                                                                               "site_key": "site"})
            with patch.object(human_check, "_verify_turnstile", new=AsyncMock(return_value=True)):
                r = self.client.post("/api/v1/human/verify", json={"turnstile_token": "t"})
                self.assertEqual(r.status_code, 200)
            with patch.object(human_check, "_verify_turnstile", new=AsyncMock(return_value=False)):
                self.assertEqual(self.client.post("/api/v1/human/verify",
                                                  json={"turnstile_token": "t"}).status_code, 403)
            csp = self.client.get("/web/").headers.get("Content-Security-Policy", "")
            self.assertIn("challenges.cloudflare.com", csp)
        with patch.dict(os.environ, {"HUMAN_CHECK": "turnstile", "TURNSTILE_SITE_KEY": ""}):
            self.assertEqual(human_check.mode(), "pow")     # no keys: never silently off
        with patch.dict(os.environ, {"HUMAN_CHECK": "off"}):
            self.assertEqual(self.guest_vote().status_code, 200)


class TestApiGuidePage(unittest.TestCase):

    def test_help_api_serves_the_guide(self):
        from api.main import app
        r = TestClient(app).get("/help/api")
        self.assertEqual(r.status_code, 200)
        self.assertIn("RollCall partner API", r.text)
        self.assertIn("<table>", r.text)
        self.assertIn("X-RollCall-Signature", r.text)
        self.assertNotIn("<script>alert", r.text)

    def test_renderer_escapes_everything(self):
        from api.api_guide import render
        out = render("# T <b>\n\nHi <script>alert(1)</script> [x](javascript:alert(1)) [ok](https://a.b)\n\n```\n<x>\n```")
        self.assertNotIn("<script>", out)
        self.assertNotIn("javascript:", out)
        self.assertIn('<a href="https://a.b">ok</a>', out)
        self.assertIn("&lt;x&gt;", out)


class TestPartnerReference(unittest.TestCase):
    """The Swagger reference lists the partner API only — never the web-page,
    auth or admin-console internals that /api/docs (off by default) exposes."""

    def test_spec_is_partner_routes_only(self):
        from api.main import app
        c = TestClient(app)
        spec = c.get("/api/v1/partner/openapi.json").json()
        paths = spec["paths"]
        self.assertIn("/api/v1/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/votes", paths)
        self.assertTrue(all(p.startswith("/api/v1/chats/") for p in paths))
        self.assertFalse(any("{rc_number}" in p for p in paths))      # deprecated paths left out
        self.assertEqual(spec["security"], [{"appKey": []}])
        self.assertFalse(any(k.startswith("Web") for k in spec["components"]["schemas"]))
        page = c.get("/help/api/reference")
        self.assertEqual(page.status_code, 200)
        self.assertIn("/help/swagger/swagger-ui-bundle.js", page.text)
        self.assertNotIn("cdn.jsdelivr", page.text)
        self.assertEqual(c.get("/help/swagger/swagger-ui-bundle.js").status_code, 200)
