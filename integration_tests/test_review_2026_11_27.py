"""
Review 2026-11-27 — pre-release pass over email, help, API and logging.
Real DB and services; only the network edges (SMTP, Telegram) are faked.
"""
import asyncio
import os
import ssl
import unittest
from unittest.mock import patch

from helpers import IntegrationBase, ADMIN_USER, CHAT_ID, USERS
from mock_helpers import get_mock_bot, reset_db

EMAIL_ENV = {"TELEGRAM_TOKEN": "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY", "SMTP_HOST": "smtp.test",
             "SMTP_FROM": "RollCall <bot@rollcall.test>", "WEB_BASE_URL": "https://rc.test"}


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.ctx_ssl = host, port, context
        self.starttls_ctx = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.starttls_ctx = context

    def login(self, *_a):
        pass

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


class TestMailTransport(unittest.TestCase):

    def setUp(self):
        _FakeSMTP.instances.clear()
        self.enterContext(patch.dict(os.environ, EMAIL_ENV))

    def _send(self, port):
        from services import email_auth
        with patch.dict(os.environ, {"SMTP_PORT": str(port)}), \
                patch("smtplib.SMTP", _FakeSMTP), patch("smtplib.SMTP_SSL", _FakeSMTP):
            email_auth._send_blocking("asha@example.com", "Hi", "Body")
        return _FakeSMTP.instances[-1]

    def test_starttls_verifies_the_server_certificate(self):
        s = self._send(587)
        self.assertIsInstance(s.starttls_ctx, ssl.SSLContext)
        self.assertEqual(s.starttls_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(s.starttls_ctx.check_hostname)

    def test_implicit_tls_verifies_too(self):
        s = self._send(465)
        self.assertIsInstance(s.ctx_ssl, ssl.SSLContext)
        self.assertEqual(s.ctx_ssl.verify_mode, ssl.CERT_REQUIRED)

    def test_mail_has_date_and_message_id_on_our_domain(self):
        msg = self._send(587).sent[0]
        self.assertTrue(msg["Date"])
        self.assertTrue(msg["Message-ID"].endswith("@rollcall.test>"), msg["Message-ID"])


class TestQueuedMailFollowsTheAccount(unittest.TestCase):

    def setUp(self):
        reset_db()
        self.enterContext(patch.dict(os.environ, EMAIL_ENV))
        self.sent = []

        async def _fake(to, subject, text, headers=None):
            self.sent.append(to)
        self.enterContext(patch("services.email_auth.send_mail", new=_fake))

    def test_mail_queued_before_an_email_change_is_not_sent_to_the_old_address(self):
        from services import email_notify, principals
        pid = principals.for_email("old@example.com", "Asha")
        email_notify.queue_raw(pid, "old@example.com", "🎯 Game", "body", "open")
        principals.unlink(principals.EMAIL, "old@example.com")
        principals.link(pid, principals.EMAIL, "new@example.com")
        email_notify.queue_raw(pid, "old@example.com", "Your RollCall sign-in email was changed",
                               "notice", email_notify.SECURITY)
        asyncio.run(email_notify.deliver_due())
        self.assertEqual(self.sent, ["old@example.com"], "only the security notice goes to the old address")
        import db
        with db._cursor() as c:
            c.execute("SELECT status FROM email_outbox ORDER BY id")
            statuses = [r["status"] if isinstance(r, dict) else r[0] for r in c.fetchall()]
        self.assertEqual(statuses, ["skipped", "sent"])





class TestHandlersNeverGoSilent(IntegrationBase):

    async def test_a_crash_in_an_unguarded_handler_still_answers(self):
        from handlers.templates import list_templates
        get_mock_bot().reset_mock()
        with patch("services.templates.list_templates", side_effect=RuntimeError("db gone")):
            await list_templates(self.msg("/templates", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(texts, "the user got no reply at all")
        self.assertNotIn("db gone", " ".join(texts), "internal errors never reach the chat")

    async def test_start_explains_how_to_begin(self):
        get_mock_bot().reset_mock()
        await self.welcome_and_explanation(self.msg("/start", ADMIN_USER))
        text = " ".join(self.sent_texts())
        self.assertIn("/src", text)
        self.assertIn("/weblink", text)
        self.assertNotIn("im RollCall", text)



class TestLocationAlone(IntegrationBase):

    async def test_anyone_sees_the_place_only_admins_change_it(self):
        await self.start_rc("Sunday Game")
        get_mock_bot().reset_mock()
        await self.set_location(self.msg("/location", USERS[0]))
        self.assertIn("No location set for 'Sunday Game'", " ".join(self.sent_texts()))
        await self.set_location(self.msg("/location Turf 3", ADMIN_USER))
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        get_mock_bot().reset_mock()
        await self.set_location(self.msg("/location", USERS[0]))
        self.assertIn("is at: Turf 3", " ".join(self.sent_texts()))
        await self.set_location(self.msg("/location Somewhere Else", USERS[0]))
        self.assertEqual(self.mgr.get_rollcalls(CHAT_ID)[0].location, "Turf 3",
                         "a member can look, not change it")


if __name__ == "__main__":
    unittest.main()
