"""
Regression: the group page's proxy-vote / remove-user / move-user act on the
rollcall the page was showing (rollcall_id) and on the exact member
(user_id / is_proxy), not on a tab position or a display label.

Before: body.rollcall_num - 1 was resolved after the fact, so a page loaded
before an earlier rollcall closed acted on the next one; and a remove sent the
disambiguated label ("Ravi (@ravi)"), which the name matcher could not find.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mock_helpers import mock_bot, reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"
CHAT_ID = -1001999000802
ALICE_ID = 3001  # web admin


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import rollcall_manager
    from api.main import app
    from api.identity import issue_identity_token
    import db
    return rollcall_manager.manager, app, issue_identity_token, db


class TestWebActionsByRollcallId(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.manager, cls.app, issue, cls.db = _import()
        cls.issue = staticmethod(issue)
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
        self.token = self.issue(ALICE_ID)
        from services.rollcalls import start_rollcall
        for t in ("Alpha", "Bravo"):
            asyncio.run(start_rollcall(CHAT_ID, t, ALICE_ID, "Alice"))
        self.alpha_id = self.manager.get_rollcall(CHAT_ID, 0).id
        self.bravo_id = self.manager.get_rollcall(CHAT_ID, 1).id

    def _url(self, path):
        return f"/api/v1/web/group/{self.chat['group_web_token']}/{path}"

    def _end_alpha(self):
        from services.rollcalls import end_rollcall
        asyncio.run(end_rollcall(CHAT_ID, 0, ALICE_ID, "Alice"))

    def test_proxy_vote_from_stale_tab_lands_in_its_own_rollcall(self):
        self._end_alpha()
        # Page still shows Bravo as tab #2.
        r = self.client.post(self._url("proxy-vote"), json={
            "id_token": self.token, "rollcall_num": 2, "rollcall_id": self.bravo_id,
            "proxy_name": "Guest", "vote": "in"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual([u.name for u in self.manager.get_rollcall(CHAT_ID, 0).inList], ["Guest"])

    def test_proxy_vote_on_closed_rollcall_is_409_and_changes_nothing(self):
        self._end_alpha()
        r = self.client.post(self._url("proxy-vote"), json={
            "id_token": self.token, "rollcall_num": 1, "rollcall_id": self.alpha_id,
            "proxy_name": "Guest", "vote": "in"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.manager.get_rollcall(CHAT_ID, 0).inList, [])

    def test_remove_and_move_on_closed_rollcall_are_409(self):
        from services.voting import vote_in
        asyncio.run(vote_in(CHAT_ID, 4001, "Bob", "bob", rc_number=1))
        self._end_alpha()
        for path, extra in (("rollcalls/remove-user", {}),
                            ("rollcalls/move-user", {"new_status": "out"})):
            r = self.client.post(self._url(path), json={
                "id_token": self.token, "rollcall_num": 1, "rollcall_id": self.alpha_id,
                "name": "Bob", **extra})
            self.assertEqual(r.status_code, 409, r.text)
        # Bravo (now #1) untouched.
        self.assertEqual([u.name for u in self.manager.get_rollcall(CHAT_ID, 0).inList], ["Bob"])

    def test_remove_by_user_id_handles_disambiguated_label(self):
        from services.proxy import set_in_for
        from services.voting import vote_in
        asyncio.run(set_in_for(CHAT_ID, ALICE_ID, "Alice", "Ravi"))
        asyncio.run(vote_in(CHAT_ID, 4002, "Ravi", "ravi"))
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        real = next(u for u in rc.inList if u.user_id == 4002)
        self.assertEqual(real.name, "Ravi (@ravi)")
        page = self.client.get(f"/api/v1/web/group/{self.chat['group_web_token']}").json()
        row = next(u for u in page["rollcalls"][0]["in"] if u["name"] == "Ravi (@ravi)")
        self.assertEqual(row["user_id"], 4002)
        r = self.client.post(self._url("rollcalls/remove-user"), json={
            "id_token": self.token, "rollcall_num": 1, "rollcall_id": self.alpha_id,
            "name": real.name, "user_id": 4002})
        self.assertEqual(r.status_code, 200, r.text)
        left = self.manager.get_rollcall(CHAT_ID, 0).inList
        self.assertEqual([(u.name, u.user_id) for u in left], [("Ravi", "Ravi")])

    def test_move_namesake_without_username_by_user_id(self):
        from services.voting import vote_in
        asyncio.run(vote_in(CHAT_ID, 4003, "Rahul", None))
        asyncio.run(vote_in(CHAT_ID, 4004, "Rahul", None))
        r = self.client.post(self._url("rollcalls/move-user"), json={
            "id_token": self.token, "rollcall_num": 1, "rollcall_id": self.alpha_id,
            "name": "Rahul", "user_id": 4004, "new_status": "out"})
        self.assertEqual(r.status_code, 200, r.text)
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        self.assertEqual([u.user_id for u in rc.inList], [4003])
        self.assertEqual([u.user_id for u in rc.outList], [4004])

    def test_remove_proxy_by_exact_name(self):
        from services.proxy import set_in_for
        asyncio.run(set_in_for(CHAT_ID, ALICE_ID, "Alice", "Guest"))
        r = self.client.post(self._url("rollcalls/remove-user"), json={
            "id_token": self.token, "rollcall_num": 1, "rollcall_id": self.alpha_id,
            "name": "Guest", "is_proxy": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.manager.get_rollcall(CHAT_ID, 0).inList, [])


if __name__ == "__main__":
    unittest.main()
