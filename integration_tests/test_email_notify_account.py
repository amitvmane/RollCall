"""
Without Telegram, the last two gaps: hearing about things, and owning the account.

  * Notification emails (services/email_notify): an email-only member gets the
    waitlist / not-voted / dues / new-game notices Telegram members get in
    Telegram — by default only when they have no web push on for that group.
    Queued, capped, de-duplicated, retried, one-click unsubscribe.
  * Account settings (services/account): change the sign-in email (confirmed
    from the new address, old one told), sign out everywhere (every earlier
    identity token refused), delete an email-only account.
"""
import asyncio
import os
import re
import time
import unittest
from unittest.mock import patch

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
TG_CHAT = -1001777000222
TG_MEMBER = 7002
EMAIL_ENV = {"TELEGRAM_TOKEN": BOT_TOKEN, "SMTP_HOST": "smtp.test", "SMTP_FROM": "bot@test",
             "WEB_BASE_URL": "https://rc.test"}


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import bot_state  # noqa: F401
        from api.main import app
        from api.identity import issue_identity_token
        import db
        cls.client = TestClient(app)
        cls.issue = staticmethod(issue_identity_token)
        cls.db = db

    def setUp(self):
        reset_db()
        from rollcall_manager import manager
        manager.clear_cache()
        from services.email_auth import reset_send_limits_for_tests
        reset_send_limits_for_tests()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        from services.account import reset_cache_for_tests
        reset_cache_for_tests()
        self.enterContext(patch.dict(os.environ, EMAIL_ENV))
        self.mails = []
        self.fail_sends = False

        async def _fake_send(to, subject, text, headers=None):
            if self.fail_sends:
                raise OSError("smtp down")
            self.mails.append({"to": to, "subject": subject, "text": text, "headers": headers or {}})
        self.enterContext(patch("services.email_auth.send_mail", new=_fake_send))
        self.token = self.db.get_or_create_chat(TG_CHAT)["group_web_token"]

    def h(self, uid_or_token):
        tok = uid_or_token if isinstance(uid_or_token, str) else self.issue(uid_or_token)
        return {"X-Identity-Token": tok}

    def code(self):
        return re.search(r"email_code=([\w-]+)", self.mails[-1]["text"]).group(1)

    def email_login(self, email, name="Asha"):
        r = self.client.post("/api/v1/auth/email/start", json={"email": email, "name": name})
        self.assertEqual(r.status_code, 202, r.text)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def member(self, email="asha@example.com", name="Asha"):
        """An email-only person who joined the Telegram group from its page."""
        uid = self.email_login(email, name)["user_id"]
        r = self.client.post(f"/api/v1/web/group/{self.token}/join", json={}, headers=self.h(uid))
        self.assertEqual(r.status_code, 200, r.text)
        self.mails.clear()
        return uid

    def outbox(self):
        with self.db._cursor() as c:
            c.execute("SELECT email, tag, subject, body, status, attempts FROM email_outbox ORDER BY id")
            return [dict(r) for r in c.fetchall()]

    def deliver(self):
        from services import email_notify
        return asyncio.run(email_notify.deliver_due())

    def notify(self, uids, title="🎉 You're IN — Friday", body="A spot opened up.", tag="promoted"):
        from services import push
        asyncio.run(push.notify_users(TG_CHAT, uids, title, body, tag))


class TestNotificationEmails(_Base):

    def test_a_notice_is_emailed_with_a_one_click_unsubscribe(self):
        asha = self.member()
        self.notify([asha])
        self.assertEqual(self.deliver(), 1)
        m = self.mails[-1]
        self.assertEqual((m["to"], m["subject"]), ("asha@example.com", "🎉 You're IN — Friday"))
        self.assertIn("A spot opened up.", m["text"])
        self.assertIn(f"/web/group/{self.token}", m["text"])
        self.assertIn("List-Unsubscribe=One-Click", m["headers"]["List-Unsubscribe-Post"])
        self.assertIn("/api/v1/auth/email/unsubscribe?t=", m["headers"]["List-Unsubscribe"])
        self.assertEqual(self.outbox()[0]["status"], "sent")
        self.assertEqual(self.deliver(), 0)   # sent once

    def test_auto_skips_someone_with_push_on_here_always_does_not(self):
        asha = self.member()
        self.db.save_push_subscription(self.token, "https://push.example/x", "k", "a", tg_user_id=asha)
        self.notify([asha])
        self.assertEqual(self.outbox(), [])
        r = self.client.post("/api/v1/auth/account/email-notify", json={"pref": "always"}, headers=self.h(asha))
        self.assertEqual(r.status_code, 200, r.text)
        self.notify([asha])
        self.assertEqual(len(self.outbox()), 1)

    def test_telegram_members_are_never_emailed(self):
        # A Telegram member who linked an email still hears in Telegram.
        r = self.client.post("/api/v1/auth/email/link", json={"email": "ravi@example.com"},
                             headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 202, r.text)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 200, r.text)
        self.notify([TG_MEMBER])
        self.assertEqual(self.outbox(), [])

    def test_same_message_once_and_a_daily_cap(self):
        from services import email_notify
        asha = self.member()
        self.notify([asha])
        self.notify([asha])
        self.assertEqual(len(self.outbox()), 1)
        for i in range(email_notify.MAX_PER_DAY + 5):
            self.notify([asha], body=f"reminder {i}")
        self.assertEqual(len(self.outbox()), email_notify.MAX_PER_DAY)

    def test_new_rollcall_reaches_email_only_members(self):
        from services import email_notify
        asha = self.member()
        bea = self.member("bea@example.com", "Bea")
        self.assertEqual(email_notify.queue_rollcall_opened(TG_CHAT, "Friday Football"), 2)
        self.assertEqual(sorted(r["email"] for r in self.outbox()), ["asha@example.com", "bea@example.com"])
        self.assertTrue(all(r["tag"] == "open" for r in self.outbox()))
        self.assertTrue(asha and bea)

    def test_a_scheduled_start_announces_too(self):
        """Template starts (the scheduler, /start_template, web) never sent
        "a game opened" — only /src and the web Start button did."""
        import check_reminders
        from services import templates as tmpl_svc
        self.member()
        tmpl_svc.upsert_template(TG_CHAT, "friday", TG_MEMBER, "Ravi", title="Friday Football")

        async def go():
            before = asyncio.all_tasks()
            ok = await check_reminders._auto_start_from_template(TG_CHAT, self.db.get_template(TG_CHAT, "friday"))
            # The notice is a background task: wait for exactly it, not a guess.
            started = [t for t in asyncio.all_tasks() - before
                       if "_push_rollcall_started" in repr(t.get_coro())]
            await asyncio.gather(*started)
            return ok, len(started)
        with patch("check_reminders.bot"), patch("check_reminders.ensure_reminder_loop"):
            self.assertEqual(asyncio.run(go()), (True, 1))
        self.assertEqual([(r["tag"], r["subject"]) for r in self.outbox()], [("open", "🎯 Friday Football")])

    def test_failed_sends_retry_then_give_up(self):
        from services import email_notify
        asha = self.member()
        self.notify([asha])
        self.fail_sends = True
        for _ in range(email_notify.MAX_ATTEMPTS):
            with self.db._cursor(commit=True) as c:
                c.execute("UPDATE email_outbox SET next_attempt=0")
            self.deliver()
        row = self.outbox()[0]
        self.assertEqual((row["status"], row["attempts"]), ("failed", email_notify.MAX_ATTEMPTS))

    def test_unsubscribe_needs_a_post_and_a_valid_token(self):
        from services import email_notify, principals
        asha = self.member()
        pid = principals.principal_of_local(asha)
        t = email_notify.unsubscribe_token(pid)
        page = self.client.get(f"/api/v1/auth/email/unsubscribe?t={t}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(email_notify.get_pref(pid), "auto")   # a mail scanner's GET changes nothing
        self.assertEqual(self.client.post(f"/api/v1/auth/email/unsubscribe?t={t}").status_code, 200)
        self.assertEqual(email_notify.get_pref(pid), "off")
        self.notify([asha])
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.client.post(f"/api/v1/auth/email/unsubscribe?t={pid}.forged").status_code, 400)
        self.assertEqual(self.client.post(f"/api/v1/auth/email/unsubscribe?t={pid + 1}.{t.split('.')[1]}").status_code,
                         400)

    def test_nothing_is_queued_without_email_configured(self):
        asha = self.member()
        with patch.dict(os.environ, {"SMTP_HOST": ""}):
            self.notify([asha])
        self.assertEqual(self.outbox(), [])


class TestChangeEmail(_Base):

    def test_change_is_confirmed_from_the_new_address_and_the_old_one_is_told(self):
        from services import principals
        asha = self.member()
        r = self.client.post("/api/v1/auth/account/email/change", json={"email": "asha.new@example.com"},
                             headers=self.h(asha))
        self.assertEqual(r.status_code, 202, r.text)
        self.assertEqual(self.mails[-1]["to"], "asha.new@example.com")
        code = self.code()
        # Someone else can't use it — not even signed in as another account.
        other = self.email_login("eve@example.com", "Eve")["user_id"]
        r = self.client.post("/api/v1/auth/email/verify", json={"code": code}, headers=self.h(other))
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/v1/auth/email/verify", json={"code": code}, headers=self.h(asha))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["purpose"], r.json()["user_id"]), ("change", asha))
        pid = principals.principal_of_local(asha)
        self.assertEqual(principals.binding_for(pid, principals.EMAIL), "asha.new@example.com")
        self.assertIsNone(principals.resolve(principals.EMAIL, "asha@example.com"))
        notice = [m for m in self.outbox() if m["tag"] == "security"]
        self.assertEqual([m["email"] for m in notice], ["asha@example.com"])
        # Signing in with the new address is still Asha.
        self.assertEqual(self.email_login("asha.new@example.com")["user_id"], asha)

    def test_an_address_another_account_uses_is_refused(self):
        asha = self.member()
        self.email_login("bea@example.com", "Bea")
        self.client.post("/api/v1/auth/account/email/change", json={"email": "bea@example.com"},
                         headers=self.h(asha))
        r = self.client.post("/api/v1/auth/email/verify", json={"code": self.code()}, headers=self.h(asha))
        self.assertEqual(r.status_code, 422)

    def test_needs_an_email_to_change(self):
        r = self.client.post("/api/v1/auth/account/email/change", json={"email": "x@example.com"},
                             headers=self.h(TG_MEMBER))
        self.assertEqual(r.status_code, 422)


class TestSignOutEverywhere(_Base):

    def test_every_earlier_token_stops_working_but_this_device_stays_in(self):
        asha = self.member()
        old_a, old_b = self.issue(asha), self.issue(asha)
        bea = self.member("bea@example.com", "Bea")
        bea_tok = self.issue(bea)
        r = self.client.post("/api/v1/auth/account/sign-out-everywhere", headers=self.h(old_a))
        self.assertEqual(r.status_code, 200, r.text)
        fresh = r.json()["id_token"]
        for tok in (old_a, old_b):
            self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(tok)).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(fresh)).status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(bea_tok)).status_code, 200)
        # A new sign-in afterwards works as normal.
        time.sleep(1.1)
        self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(self.issue(asha))).status_code, 200)

    def test_works_for_telegram_members_too(self):
        old = self.issue(TG_MEMBER)
        r = self.client.post("/api/v1/auth/account/sign-out-everywhere", headers=self.h(old))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(old)).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/auth/account",
                                         headers=self.h(r.json()["id_token"])).status_code, 200)


class TestDeleteAccount(_Base):

    def _delete(self, uid, confirm="DELETE"):
        return self.client.post("/api/v1/auth/account/delete", json={"confirm": confirm}, headers=self.h(uid))

    def test_delete_removes_the_person_and_keeps_the_groups_records(self):
        from rollcall_manager import manager
        from services import principals, rollcalls as rc_svc
        from services.web import vote_by_token, _ensure_web_token
        asha = self.member()
        tok = self.issue(asha)
        asyncio.run(rc_svc.start_rollcall(TG_CHAT, "Friday", TG_MEMBER, "Ravi"))
        rc = manager.get_rollcall(TG_CHAT, 0)
        asyncio.run(vote_by_token(_ensure_web_token(rc), "Asha", "in", tg_user_id=asha))
        self.db.save_push_subscription(self.token, "https://push.example/a", "k", "a", tg_user_id=asha)

        r = self._delete(asha)
        self.assertEqual(r.status_code, 422)   # IN on an open game
        self.assertIn("vote OUT first", r.json()["detail"])
        asyncio.run(vote_by_token(_ensure_web_token(manager.get_rollcall(TG_CHAT, 0)), "Asha", "out",
                                  tg_user_id=asha))
        self.assertEqual(self._delete(asha, confirm="yes").status_code, 422)
        r = self._delete(asha)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["groups"], 1)

        self.assertIsNone(principals.resolve(principals.EMAIL, "asha@example.com"))
        self.assertFalse(self.db.is_active_chat_member(TG_CHAT, asha))
        self.assertEqual(self.db.get_push_subscriptions(self.token), [])
        self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(tok)).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/auth/account", headers=self.h(asha)).status_code, 401)
        # The game's record still shows her vote.
        rc = manager.get_rollcall(TG_CHAT, 0)
        self.assertEqual([u.user_id for u in rc.outList], [asha])
        # The same address later is a brand-new account.
        self.assertNotEqual(self.email_login("asha@example.com")["user_id"], asha)

    def test_the_only_owner_of_a_group_with_members_must_hand_it_over(self):
        asha = self.email_login("asha@example.com")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf", "your_name": "Asha"},
                             headers=self.h(asha)).json()
        self.assertEqual(self._delete(asha).status_code, 200, "alone in her own group: fine")

        asha = self.email_login("asha2@example.com")["user_id"]
        g = self.client.post("/api/v1/web/groups", json={"name": "Turf 2", "your_name": "Asha"},
                             headers=self.h(asha)).json()
        self.db.upsert_chat_member(g["chat_id"], TG_MEMBER, "Ravi")
        r = self._delete(asha)
        self.assertEqual(r.status_code, 422)
        self.assertIn("only owner", r.json()["detail"])

    def test_telegram_accounts_are_not_deleted_here(self):
        self.assertEqual(self._delete(TG_MEMBER).status_code, 403)


if __name__ == "__main__":
    unittest.main()
