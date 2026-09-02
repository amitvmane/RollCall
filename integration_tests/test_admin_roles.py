"""
Integration tests for web-admin roles (owner / admin) and admin_source.

Groundwork for running a group without asking Telegram who its admins are.
Today every grant is equal and `web_admins` does double duty: a cache of a
Telegram fact in locked-down groups, the source of truth in open ones. The
owner concept has to exist BEFORE a group can be switched to its own list —
afterwards there is nobody with standing to grant anything.

The rules exist to stop one specific unrecoverable state: a group with no
owner that has also stopped asking Telegram. Nobody can administer it, and
there is no path back short of editing the database by hand.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"

CHAT_ID = -1001999000988
OWNER_ID = 6001      # first grant → becomes owner by backfill
ADMIN_ID = 6002
OTHER_ID = 6003
STRANGER_ID = 6004   # no grant at all


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import db
    from services import admin as admin_svc
    return db, admin_svc


class TestAdminRoles(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db, cls.svc = _import()

    def setUp(self):
        reset_db()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        self.db.get_or_create_chat(CHAT_ID)
        self.db.set_web_admin(CHAT_ID, OWNER_ID, "Owner")
        self.db.set_web_admin(CHAT_ID, ADMIN_ID, "Admin")
        # The backfill runs at schema-reconcile time, i.e. before these rows
        # existed. Promote explicitly so each test starts from "one owner".
        self.db.set_web_admin_role(CHAT_ID, OWNER_ID, "owner")

    # ── Defaults: adding roles must change nothing until asked ───────────

    def test_new_grants_are_plain_admins(self):
        self.db.set_web_admin(CHAT_ID, OTHER_ID, "Other")
        self.assertEqual(self.db.get_web_admin_role(CHAT_ID, OTHER_ID), "admin")

    def test_chats_still_ask_telegram_by_default(self):
        """admin_source defaults to 'platform' — adding the column must not
        quietly change how any existing group is administered."""
        self.assertEqual(self.svc.get_admin_source(CHAT_ID), "platform")

    def test_no_grant_has_no_role(self):
        self.assertIsNone(self.db.get_web_admin_role(CHAT_ID, STRANGER_ID))

    # ── The /weblink trap ────────────────────────────────────────────────

    def test_reweblinking_does_not_reset_a_role(self):
        """set_web_admin used INSERT OR REPLACE on SQLite, which deletes the
        row and inserts a fresh one — silently resetting role to its default
        every time an admin ran /weblink. An owner would have quietly become
        an admin again, and only on SQLite."""
        self.db.set_web_admin(CHAT_ID, OWNER_ID, "Owner Renamed")
        self.assertEqual(self.db.get_web_admin_role(CHAT_ID, OWNER_ID), "owner")
        names = {a["tg_user_id"]: a["tg_name"] for a in self.db.list_web_admins(CHAT_ID)}
        self.assertEqual(names[OWNER_ID], "Owner Renamed", "the name should still update")

    # ── Who may change what ──────────────────────────────────────────────

    def test_owner_can_promote(self):
        self.svc.promote_to_owner(CHAT_ID, ADMIN_ID,
                                  actor_user_id=OWNER_ID, actor_name="Owner")
        self.assertEqual(self.db.get_web_admin_role(CHAT_ID, ADMIN_ID), "owner")

    def test_plain_admin_cannot_promote(self):
        from exceptions import insufficientPermissions
        with self.assertRaises(insufficientPermissions):
            self.svc.promote_to_owner(CHAT_ID, OTHER_ID,
                                      actor_user_id=ADMIN_ID, actor_name="Admin")

    def test_stranger_cannot_promote(self):
        from exceptions import insufficientPermissions
        with self.assertRaises(insufficientPermissions):
            self.svc.promote_to_owner(CHAT_ID, ADMIN_ID,
                                      actor_user_id=STRANGER_ID, actor_name="Nobody")

    def test_cannot_promote_someone_with_no_grant(self):
        from exceptions import incorrectParameter
        with self.assertRaises(incorrectParameter):
            self.svc.promote_to_owner(CHAT_ID, STRANGER_ID,
                                      actor_user_id=OWNER_ID, actor_name="Owner")

    # ── The unrecoverable state ──────────────────────────────────────────

    def test_last_owner_cannot_be_demoted(self):
        """A group with no owner is one nobody can ever administer again."""
        from exceptions import insufficientPermissions
        with self.assertRaises(insufficientPermissions):
            self.svc.demote_to_admin(CHAT_ID, OWNER_ID,
                                     actor_user_id=OWNER_ID, actor_name="Owner")
        self.assertEqual(self.db.count_web_admin_owners(CHAT_ID), 1)

    def test_demotion_works_once_a_second_owner_exists(self):
        self.svc.promote_to_owner(CHAT_ID, ADMIN_ID,
                                  actor_user_id=OWNER_ID, actor_name="Owner")
        self.svc.demote_to_admin(CHAT_ID, OWNER_ID,
                                 actor_user_id=ADMIN_ID, actor_name="Admin")
        self.assertEqual(self.db.get_web_admin_role(CHAT_ID, OWNER_ID), "admin")
        self.assertEqual(self.db.count_web_admin_owners(CHAT_ID), 1)

    def test_cannot_demote_a_plain_admin(self):
        from exceptions import incorrectParameter
        with self.assertRaises(incorrectParameter):
            self.svc.demote_to_admin(CHAT_ID, ADMIN_ID,
                                     actor_user_id=OWNER_ID, actor_name="Owner")

    # ── Switching a group off Telegram's admin list ──────────────────────

    def test_owner_can_switch_to_local(self):
        self.svc.set_admin_source(CHAT_ID, "local",
                                  actor_user_id=OWNER_ID, actor_name="Owner")
        self.assertEqual(self.svc.get_admin_source(CHAT_ID), "local")

    def test_plain_admin_cannot_switch(self):
        from exceptions import insufficientPermissions
        with self.assertRaises(insufficientPermissions):
            self.svc.set_admin_source(CHAT_ID, "local",
                                      actor_user_id=ADMIN_ID, actor_name="Admin")
        self.assertEqual(self.svc.get_admin_source(CHAT_ID), "platform")

    def test_unknown_source_refused(self):
        from exceptions import incorrectParameter
        with self.assertRaises(incorrectParameter):
            self.svc.set_admin_source(CHAT_ID, "whatever",
                                      actor_user_id=OWNER_ID, actor_name="Owner")

    def test_listing_puts_owners_first(self):
        rows = self.svc.list_admins(CHAT_ID)
        self.assertEqual(rows[0]["tg_user_id"], OWNER_ID)
        self.assertEqual(rows[0]["role"], "owner")



class TestAdminRolesApi(unittest.TestCase):
    """The REST surface the web panel uses.

    Both failure modes have to be answers, not 500s: "you aren't an owner"
    and "that's the last owner" are things the UI needs to show, and the
    second is the one that protects an unrecoverable state.
    """

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
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        chat = self.db.get_or_create_chat(CHAT_ID)
        self.token = chat["group_web_token"]
        self.db.set_web_admin(CHAT_ID, OWNER_ID, "Owner")
        self.db.set_web_admin(CHAT_ID, ADMIN_ID, "Admin")
        self.db.set_web_admin_role(CHAT_ID, OWNER_ID, "owner")

    def _get(self, actor):
        with patch("api.routes.web.check_web_admin_live", return_value=True):
            return self.client.get(f"/api/v1/web/group/{self.token}/admins",
                                   headers={"X-Identity-Token": self.issue(actor)})

    def _set(self, actor, target, role):
        with patch("api.routes.web.check_web_admin_live", return_value=True):
            return self.client.post(
                f"/api/v1/web/group/{self.token}/admins/role",
                json={"id_token": self.issue(actor), "tg_user_id": target, "role": role})

    def test_lists_roles_and_flags_the_caller(self):
        r = self._get(OWNER_ID)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["you_are_owner"])
        self.assertEqual(body["admin_source"], "platform")
        me = next(a for a in body["admins"] if a["tg_user_id"] == OWNER_ID)
        self.assertEqual(me["role"], "owner")
        self.assertTrue(me["is_you"])

    def test_a_plain_admin_sees_the_list_but_is_not_an_owner(self):
        body = self._get(ADMIN_ID).json()
        self.assertFalse(body["you_are_owner"])
        self.assertEqual(len(body["admins"]), 2)

    def test_owner_promotes(self):
        r = self._set(OWNER_ID, ADMIN_ID, "owner")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.db.get_web_admin_role(CHAT_ID, ADMIN_ID), "owner")

    def test_non_owner_promotion_is_403_not_500(self):
        r = self._set(ADMIN_ID, ADMIN_ID, "owner")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self.db.get_web_admin_role(CHAT_ID, ADMIN_ID), "admin")

    def test_demoting_the_last_owner_is_403_not_500(self):
        r = self._set(OWNER_ID, OWNER_ID, "admin")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("only owner", r.json()["detail"].lower())
        self.assertEqual(self.db.count_web_admin_owners(CHAT_ID), 1)

    def test_unknown_role_is_rejected(self):
        self.assertEqual(self._set(OWNER_ID, ADMIN_ID, "superuser").status_code, 422)

    def test_requires_identity(self):
        r = self.client.get(f"/api/v1/web/group/{self.token}/admins")
        self.assertEqual(r.status_code, 401)

if __name__ == "__main__":
    unittest.main()
