"""
Integration tests for the Mini App cross-group picker flow:
  GET  /portal/groups                      (existing, reused)
  POST /auth/telegram/miniapp/group         (new)

Real DB, real services, FastAPI TestClient. Exercises the actual scenario
this feature exists for: the Mini App's only working entry point today is
Telegram's private-chat menu button, which never carries real group
context — so the app authenticates with chat_is_group=False, lists the
user's groups via /portal/groups, and this endpoint mints a chat-scoped
session for whichever one they tap.
"""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"

CHAT_ID = -1001999000950
ADMIN_ID = 3001   # web admin, never personally voted
VOTER_ID = 3002   # real voting history, not a web admin


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    from api.main import app
    from api.identity import issue_identity_token
    import db
    return {"app": app, "issue_identity_token": issue_identity_token, "db": db}


class TestMiniAppGroupPicker(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.issue_identity_token = staticmethod(env["issue_identity_token"])
        cls.db = env["db"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))

        self.chat = self.db.get_or_create_chat(CHAT_ID)
        self.db.set_web_admin(CHAT_ID, ADMIN_ID, "Admin")

        # Give VOTER_ID real voting history: start a rollcall, vote in,
        # end it — this is what actually populates user_stats, which
        # get_user_voted_chats (and therefore /portal/groups) reads from.
        import asyncio
        import rollcall_manager
        from services.rollcalls import start_rollcall, end_rollcall
        from services.voting import vote_in
        rollcall_manager.manager.clear_cache()
        asyncio.run(start_rollcall(CHAT_ID, "Sunday Game", ADMIN_ID, "Admin"))
        asyncio.run(vote_in(CHAT_ID, VOTER_ID, "Voter", "votertg"))
        asyncio.run(end_rollcall(CHAT_ID, 0, ADMIN_ID, "Admin"))

    def test_portal_groups_lists_the_chat_for_the_voter(self):
        tok = self.issue_identity_token(VOTER_ID)
        resp = self.client.get(f"/api/v1/portal/groups?id_token={tok}")
        self.assertEqual(resp.status_code, 200)
        chat_ids = [g["chat_id"] for g in resp.json()["groups"]]
        self.assertIn(CHAT_ID, chat_ids)

    def test_switch_session_succeeds_for_voter_with_history(self):
        tok = self.issue_identity_token(VOTER_ID)
        resp = self.client.post(
            "/api/v1/auth/telegram/miniapp/group",
            json={"id_token": tok, "chat_id": CHAT_ID},
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["chat_id"], CHAT_ID)
        self.assertEqual(body["user_id"], VOTER_ID)
        self.assertTrue(body["chat_is_group"])
        self.assertIsNotNone(body["token"])

    def test_switch_session_succeeds_for_web_admin_without_voting(self):
        """ADMIN_ID never voted (only started/ended), so it has no
        user_stats row — must still get in via the is_web_admin check."""
        tok = self.issue_identity_token(ADMIN_ID)
        resp = self.client.post(
            "/api/v1/auth/telegram/miniapp/group",
            json={"id_token": tok, "chat_id": CHAT_ID},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()["is_web_admin"])

    def test_switch_session_rejected_for_unrelated_chat(self):
        tok = self.issue_identity_token(VOTER_ID)
        resp = self.client.post(
            "/api/v1/auth/telegram/miniapp/group",
            json={"id_token": tok, "chat_id": -999999999},
        )
        self.assertEqual(resp.status_code, 403)

    def test_switch_session_token_can_actually_fetch_rollcalls(self):
        """The whole point: the minted token must be a real, working
        chat-scoped bearer token — not just a 201 with no substance."""
        tok = self.issue_identity_token(VOTER_ID)
        session = self.client.post(
            "/api/v1/auth/telegram/miniapp/group",
            json={"id_token": tok, "chat_id": CHAT_ID},
        ).json()

        resp = self.client.get(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            headers={"Authorization": f"Bearer {session['token']}"},
        )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
