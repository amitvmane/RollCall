"""
Integration tests for live rollcall voter management on the group web page
(admin console retirement — the biggest gap: view + move/remove voters):
  POST /web/group/{token}/rollcalls/remove-user
  POST /web/group/{token}/rollcalls/move-user

Real services, real DB, real rollcall state, FastAPI TestClient — proves the
actual services.admin wiring works, not just that the route calls a mock.
"""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mock_helpers import mock_bot, reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"

CHAT_ID = -1001999000801
ALICE_ID = 2001  # web admin
BOB_ID = 2002    # real voter


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


class TestWebVoterManagement(unittest.TestCase):

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
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))

        self.chat = self.db.get_or_create_chat(CHAT_ID)
        self.db.set_web_admin(CHAT_ID, ALICE_ID, "Alice")
        self.token = self.issue_identity_token(ALICE_ID)

        import asyncio
        from services.rollcalls import start_rollcall
        from services.voting import vote_in
        asyncio.run(start_rollcall(CHAT_ID, "Sunday Game", ALICE_ID, "Alice"))
        asyncio.run(vote_in(CHAT_ID, BOB_ID, "Bob", "bobreal"))

    def _roster(self):
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        return {
            "in": [u.name for u in rc.inList],
            "out": [u.name for u in rc.outList],
            "maybe": [u.name for u in rc.maybeList],
        }

    def test_bob_starts_in(self):
        self.assertIn("Bob", self._roster()["in"])

    def test_admin_moves_bob_to_out(self):
        r = self.client.post(
            f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/move-user",
            json={"id_token": self.token, "rollcall_num": 1, "name": "Bob", "new_status": "out"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("Bob", [u["name"] for u in body["out"]])
        self.assertNotIn("Bob", [u["name"] for u in body["in"]])
        roster = self._roster()
        self.assertNotIn("Bob", roster["in"])
        self.assertIn("Bob", roster["out"])

    def test_admin_removes_bob(self):
        r = self.client.post(
            f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/remove-user",
            json={"id_token": self.token, "rollcall_num": 1, "name": "Bob"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        all_names = [u["name"] for u in body["in"] + body["out"] + body["maybe"] + body["waiting"]]
        self.assertNotIn("Bob", all_names)
        roster = self._roster()
        self.assertNotIn("Bob", roster["in"])
        self.assertNotIn("Bob", roster["out"])
        self.assertNotIn("Bob", roster["maybe"])

    def test_non_admin_cannot_move_or_remove(self):
        # Bob has to be a genuine non-admin: no cached web_admins grant AND
        # not a Telegram admin. setUp leaves the shared mock reporting
        # "administrator", which in an open group is now itself sufficient
        # for web-admin, so it has to be overridden here.
        bob_token = self.issue_identity_token(BOB_ID)
        mock_bot.get_chat_member.return_value.status = "member"
        try:
            r1 = self.client.post(
                f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/move-user",
                json={"id_token": bob_token, "rollcall_num": 1, "name": "Bob", "new_status": "out"},
            )
            self.assertEqual(r1.status_code, 403)
            r2 = self.client.post(
                f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/remove-user",
                json={"id_token": bob_token, "rollcall_num": 1, "name": "Bob"},
            )
            self.assertEqual(r2.status_code, 403)
        finally:
            mock_bot.get_chat_member.return_value.status = "administrator"
        # Confirm nothing actually changed.
        self.assertIn("Bob", self._roster()["in"])

    def test_removing_unknown_user_returns_422(self):
        r = self.client.post(
            f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/remove-user",
            json={"id_token": self.token, "rollcall_num": 1, "name": "NoSuchPerson"},
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
