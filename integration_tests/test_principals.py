"""
App-local principals — an identity that isn't a Telegram account.

Every table in this app keys on a Telegram user id, and identity tokens carry
one as the subject, so the app cannot authenticate anyone Telegram doesn't
know and has no way to say two accounts are the same person.

A principal is the person; `principal_bindings` maps them to platform
accounts. What is deliberately NOT here: the 11 tables keyed on tg_user_id
are untouched. Re-keying them is a large migration against a live database
and speculative until a second login method exists — this is the stable id to
migrate TO, later.

The invariant worth protecting is one Telegram account never pointing at two
principals. That is what "same person" means here, and the way to break it is
a race between two concurrent first-logins, so it is enforced by a UNIQUE
constraint rather than by a check in the service.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"

TG_A = 7001
TG_B = 7002
CHAT_ID = -1001999000999


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import db
    from services import principals
    return db, principals


class TestPrincipals(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db, cls.p = _import()

    def setUp(self):
        reset_db()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))

    # ── Identity ─────────────────────────────────────────────────────────

    def test_creates_a_principal_for_a_telegram_account(self):
        pid = self.p.for_telegram(TG_A, "Amit")
        self.assertIsNotNone(pid)
        self.assertEqual(self.p.resolve("telegram", TG_A), pid)

    def test_is_idempotent(self):
        """Signing in twice is the same person, not two."""
        first = self.p.for_telegram(TG_A)
        second = self.p.for_telegram(TG_A)
        self.assertEqual(first, second)

    def test_different_accounts_are_different_people(self):
        self.assertNotEqual(self.p.for_telegram(TG_A), self.p.for_telegram(TG_B))

    def test_unknown_account_resolves_to_nothing(self):
        self.assertIsNone(self.p.resolve("telegram", 999999))

    # ── The point of the exercise: linking ───────────────────────────────

    def test_a_second_platform_can_join_the_same_person(self):
        pid = self.p.for_telegram(TG_A)
        self.assertTrue(self.p.link(pid, "discord", "d-1"))
        self.assertEqual(self.p.resolve("discord", "d-1"), pid,
                         "both accounts should resolve to one person")
        self.assertEqual({b["platform"] for b in self.p.bindings(pid)},
                         {"telegram", "discord"})

    def test_a_principal_can_exist_with_no_platform_at_all(self):
        """What an email login would create — the case that is impossible
        today, and the reason this table exists."""
        pid = self.p.get_or_create("email", "someone@example.com")
        self.assertIsNotNone(pid)
        self.assertEqual([b["platform"] for b in self.p.bindings(pid)], ["email"])

    def test_an_account_cannot_be_claimed_by_a_second_person(self):
        """Silently re-pointing a binding would merge two people's history on
        a typo, so it has to be refused rather than overwritten."""
        first = self.p.for_telegram(TG_A)
        second = self.p.for_telegram(TG_B)
        self.assertFalse(self.p.link(second, "telegram", TG_A))
        self.assertEqual(self.p.resolve("telegram", TG_A), first)

    def test_unlink_detaches_without_destroying_the_person(self):
        pid = self.p.for_telegram(TG_A)
        self.p.link(pid, "discord", "d-2")
        self.assertTrue(self.p.unlink("discord", "d-2"))
        self.assertIsNone(self.p.resolve("discord", "d-2"))
        self.assertEqual(self.p.resolve("telegram", TG_A), pid,
                         "unlinking one account must not delete the principal")

    def test_unlinking_something_absent_is_not_an_error(self):
        self.assertFalse(self.p.unlink("discord", "never-existed"))

    # ── Existing users ───────────────────────────────────────────────────

    def test_backfill_covers_people_who_predate_the_feature(self):
        """Otherwise 'the same person' would mean different things either side
        of the deploy — principals only for accounts that signed in after."""
        self.db.get_or_create_chat(CHAT_ID)
        self.db.upsert_chat_member(CHAT_ID, TG_A, "Amit", "amit")
        self.db.set_web_admin(CHAT_ID, TG_B, "Admin")

        created = self.p.backfill_from_telegram()
        self.assertGreaterEqual(created, 2)
        self.assertIsNotNone(self.p.resolve("telegram", TG_A))
        self.assertIsNotNone(self.p.resolve("telegram", TG_B))

    def test_backfill_is_idempotent(self):
        self.db.get_or_create_chat(CHAT_ID)
        self.db.upsert_chat_member(CHAT_ID, TG_A, "Amit", "amit")
        self.p.backfill_from_telegram()
        before = self.p.resolve("telegram", TG_A)
        self.assertEqual(self.p.backfill_from_telegram(), 0,
                         "a second run should create nobody")
        self.assertEqual(self.p.resolve("telegram", TG_A), before)

    def test_backfill_ignores_proxy_sentinels(self):
        """Guest rows use -1 as their user_id. A guest name is not an account
        and must never become a principal."""
        self.db.get_or_create_chat(CHAT_ID)
        self.db.increment_ghost_count(CHAT_ID, -1, "Guest", proxy_name="Guest")
        self.p.backfill_from_telegram()
        self.assertIsNone(self.p.resolve("telegram", -1))


if __name__ == "__main__":
    unittest.main()
