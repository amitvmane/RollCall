"""
Integration tests for Telegram-based admin console sign-in:
  - api/web_admin.check_web_admin_live (shared live-check, cache fallback)
  - GET  /web/group/{token}/admin-status  (refactored to use the shared helper)
  - GET  /auth/admin/groups
  - POST /auth/admin/session

Real services, real DB, FastAPI TestClient.
"""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mock_helpers import mock_bot, reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import rollcall_manager
    from api.main import app
    from api.identity import issue_identity_token
    import db
    return {
        "app": app,
        "manager": rollcall_manager.manager,
        "issue_identity_token": issue_identity_token,
        "db": db,
    }


CHAT_ID = -1001999000701
ALICE_ID = 1001
BOB_ID = 1002


class WebAdminAuthBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.manager = env["manager"]
        cls.issue_identity_token = staticmethod(env["issue_identity_token"])
        cls.db = env["db"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        self.manager.set_admin_rights(CHAT_ID, False)
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        mock_bot.get_chat_member.return_value.status = "administrator"
        mock_bot.get_chat_member.return_value.user.first_name = "Alice"
        self.db.get_or_create_chat(CHAT_ID)
        # Identity tokens are signed from TELEGRAM_TOKEN at both issuance
        # and verification time — keep it patched for the whole test, not
        # just around issuance, since the actual HTTP calls below verify it.
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))

    def _id_token(self, user_id):
        return self.issue_identity_token(user_id)


class TestCheckWebAdminLive(WebAdminAuthBase):
    """The shared helper directly — no HTTP involved."""

    def _call(self, chat_id, user_id):
        import asyncio
        from api.web_admin import check_web_admin_live
        return asyncio.run(check_web_admin_live(chat_id, user_id))

    def test_default_open_group_trusts_cache_no_live_call(self):
        # admin_rights is off (default) — must not call Telegram at all.
        mock_bot.get_chat_member.reset_mock()
        self.assertFalse(self._call(CHAT_ID, ALICE_ID))
        mock_bot.get_chat_member.assert_not_called()

    def test_default_open_group_true_once_cached(self):
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        self.assertTrue(self._call(CHAT_ID, ALICE_ID))

    def test_locked_group_live_checks_and_promotes(self):
        self.manager.set_admin_rights(CHAT_ID, True)
        mock_bot.get_chat_member.return_value.status = "administrator"
        self.assertTrue(self._call(CHAT_ID, ALICE_ID))
        self.assertTrue(self.db.is_web_admin(CHAT_ID, ALICE_ID))

    def test_locked_group_live_checks_and_revokes(self):
        self.manager.set_admin_rights(CHAT_ID, True)
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")  # stale grant
        mock_bot.get_chat_member.return_value.status = "member"
        self.assertFalse(self._call(CHAT_ID, ALICE_ID))
        self.assertFalse(self.db.is_web_admin(CHAT_ID, ALICE_ID))

    def test_locked_group_telegram_failure_falls_back_to_cache(self):
        self.manager.set_admin_rights(CHAT_ID, True)
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        mock_bot.get_chat_member.side_effect = Exception("Telegram unreachable")
        try:
            self.assertTrue(self._call(CHAT_ID, ALICE_ID))
        finally:
            mock_bot.get_chat_member.side_effect = None
            mock_bot.get_chat_member.return_value.status = "administrator"


class TestWebAdminStatusEndpoint(WebAdminAuthBase):
    """Sanity check the refactor didn't change the endpoint's behavior."""

    def test_admin_status_true_when_cached(self):
        chat = self.db.get_or_create_chat(CHAT_ID)
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        token = self._id_token(ALICE_ID)
        r = self.client.get(
            f"/api/v1/web/group/{chat['group_web_token']}/admin-status",
            headers={"X-Identity-Token": token},
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_admin"])

    def test_admin_status_false_when_not_cached(self):
        chat = self.db.get_or_create_chat(CHAT_ID)
        token = self._id_token(BOB_ID)
        r = self.client.get(
            f"/api/v1/web/group/{chat['group_web_token']}/admin-status",
            headers={"X-Identity-Token": token},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["is_admin"])


class TestGroupSettingsExtendedFields(WebAdminAuthBase):
    """admin_rights / ghost_tracking_enabled / absent_limit — same gap the
    group web page's admin-card had that the admin console didn't."""

    def setUp(self):
        super().setUp()
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        self.chat = self.db.get_or_create_chat(CHAT_ID)
        self.token = self._id_token(ALICE_ID)

    def test_group_response_includes_the_three_fields(self):
        r = self.client.get(f"/api/v1/web/group/{self.chat['group_web_token']}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        for key in ("admin_rights", "ghost_tracking_enabled", "absent_limit"):
            self.assertIn(key, body)

    def test_patch_admin_rights(self):
        r = self.client.patch(
            f"/api/v1/web/group/{self.chat['group_web_token']}/settings",
            json={"id_token": self.token, "admin_rights": True},
        )
        self.assertEqual(r.status_code, 204)
        settings = self.db.get_or_create_chat(CHAT_ID)
        self.assertTrue(settings["admin_rights"])

    def test_patch_ghost_tracking(self):
        r = self.client.patch(
            f"/api/v1/web/group/{self.chat['group_web_token']}/settings",
            json={"id_token": self.token, "ghost_tracking_enabled": False},
        )
        self.assertEqual(r.status_code, 204)
        settings = self.db.get_or_create_chat(CHAT_ID)
        self.assertFalse(settings["ghost_tracking_enabled"])

    def test_patch_absent_limit(self):
        r = self.client.patch(
            f"/api/v1/web/group/{self.chat['group_web_token']}/settings",
            json={"id_token": self.token, "absent_limit": 5},
        )
        self.assertEqual(r.status_code, 204)
        settings = self.db.get_or_create_chat(CHAT_ID)
        self.assertEqual(settings["absent_limit"], 5)

    def test_absent_limit_below_one_rejected(self):
        r = self.client.patch(
            f"/api/v1/web/group/{self.chat['group_web_token']}/settings",
            json={"id_token": self.token, "absent_limit": 0},
        )
        self.assertEqual(r.status_code, 422)

    def test_non_admin_cannot_patch_settings(self):
        bob_token = self._id_token(BOB_ID)
        r = self.client.patch(
            f"/api/v1/web/group/{self.chat['group_web_token']}/settings",
            json={"id_token": bob_token, "admin_rights": True},
        )
        self.assertEqual(r.status_code, 403)

    def test_admin_who_lost_telegram_admin_status_is_rejected_on_locked_group(self):
        """The actual gap this fix closes: a stale web_admins cache entry
        must not grant indefinite authority once a group has locked itself
        down with /set_admins and the person is no longer a real Telegram
        admin — checked live, not just trusted from the cache."""
        self.manager.set_admin_rights(CHAT_ID, True)
        mock_bot.get_chat_member.return_value.status = "member"  # demoted
        try:
            r = self.client.patch(
                f"/api/v1/web/group/{self.chat['group_web_token']}/settings",
                json={"id_token": self.token, "admin_rights": True},
            )
            self.assertEqual(r.status_code, 403)
        finally:
            mock_bot.get_chat_member.return_value.status = "administrator"


class TestAdminGroupsEndpoint(WebAdminAuthBase):

    def test_lists_cached_admin_chats(self):
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        token = self._id_token(ALICE_ID)
        r = self.client.get("/api/v1/auth/admin/groups", headers={"X-Identity-Token": token})
        self.assertEqual(r.status_code, 200)
        groups = r.json()["groups"]
        self.assertEqual(len(groups), 1)
        self.assertTrue(groups[0]["group_web_token"])  # needed to link to /web/group/{token}
        self.assertEqual(groups[0]["chat_id"], CHAT_ID)

    def test_empty_for_never_cached_user(self):
        token = self._id_token(BOB_ID)
        r = self.client.get("/api/v1/auth/admin/groups", headers={"X-Identity-Token": token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["groups"], [])

    def test_401_on_bad_identity_token(self):
        r = self.client.get("/api/v1/auth/admin/groups", headers={"X-Identity-Token": "garbage"})
        self.assertEqual(r.status_code, 401)


class TestAdminSessionEndpoint(WebAdminAuthBase):

    def test_mints_working_admin_token_for_verified_admin(self):
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        token = self._id_token(ALICE_ID)
        r = self.client.post(
            "/api/v1/auth/admin/session",
            json={"id_token": token, "chat_id": CHAT_ID},
        )
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["chat_id"], CHAT_ID)
        self.assertTrue(body["token"])

        # The minted token must actually work as a real admin bearer token.
        r2 = self.client.get(
            "/api/v1/admin/groups",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(len(r2.json()), 1)
        self.assertEqual(r2.json()[0]["chat_id"], CHAT_ID)

    def test_403_when_not_an_admin(self):
        self.manager.set_admin_rights(CHAT_ID, True)
        mock_bot.get_chat_member.return_value.status = "member"
        try:
            token = self._id_token(BOB_ID)
            r = self.client.post(
                "/api/v1/auth/admin/session",
                json={"id_token": token, "chat_id": CHAT_ID},
            )
            self.assertEqual(r.status_code, 403)
        finally:
            mock_bot.get_chat_member.return_value.status = "administrator"

    def test_401_on_bad_identity_token(self):
        r = self.client.post(
            "/api/v1/auth/admin/session",
            json={"id_token": "garbage", "chat_id": CHAT_ID},
        )
        self.assertEqual(r.status_code, 401)


class TestDuesAdminLiveReverify(WebAdminAuthBase):
    """Same fix as TestGroupSettingsExtendedFields' revoke test, applied to
    the dues/treasury routes (waive, reimburse, fund_topup, ...) — these
    previously trusted the web_admins cache forever, so someone removed
    from a group kept indefinite financial authority over it via the web
    surface. Now live-reverified through the same check_web_admin_live
    helper every other admin-gated route uses."""

    def setUp(self):
        super().setUp()
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        self.chat = self.db.get_or_create_chat(CHAT_ID)
        self.db.update_chat_settings(CHAT_ID, dues_enabled=True)
        self.token = self._id_token(ALICE_ID)

    def test_cached_admin_can_waive_on_open_group(self):
        r = self.client.post(
            f"/api/v1/web/group/{self.chat['group_web_token']}/dues/waive",
            json={"id_token": self.token, "member_name": "Someone", "amount": 50, "reason": "test"},
        )
        # 200/201 (success) or a dues-domain error (e.g. no such member) —
        # anything but a 403 proves the cached admin grant was honored.
        self.assertNotEqual(r.status_code, 403)

    def test_admin_who_lost_telegram_admin_status_cannot_waive_on_locked_group(self):
        self.manager.set_admin_rights(CHAT_ID, True)
        mock_bot.get_chat_member.return_value.status = "member"  # demoted
        try:
            r = self.client.post(
                f"/api/v1/web/group/{self.chat['group_web_token']}/dues/waive",
                json={"id_token": self.token, "member_name": "Someone", "amount": 50, "reason": "test"},
            )
            self.assertEqual(r.status_code, 403)
        finally:
            mock_bot.get_chat_member.return_value.status = "administrator"


if __name__ == "__main__":
    unittest.main()
