"""
Regression: freeing an IN slot must always drain the waitlist.

The bug: removing a member (admin delete) skipped the promotion step that
`addOut`/`addMaybe` and `set_limit` all perform, so a capped IN list sat
under its cap and the first waitlister stayed WAITING until some unrelated
vote happened to free another slot.

Covered for every entry point that can free a slot:
  • Telegram  /delete_user  → delconf_yes callback
  • REST      DELETE /chats/{id}/rollcalls/{n}/users/{name}
  • Web       POST  /web/group/{token}/rollcalls/remove-user
plus the "promotion is announced, not silent" half for the web move-user
path, which promoted correctly but told nobody.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import mock_bot, reset_db


class TestDeletePromotesWaitlistTelegram(IntegrationBase):
    """/delete_user frees a slot — the first waitlister must move up."""

    async def _capped_rc(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 2", ADMIN_USER))
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        await self.vote_in(USERS[2])
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User1", "User2"])
        self.assertEqual([u.name for u in rc.waitList], ["User3"])

    async def _confirm_delete(self, name):
        await self.delete_user(self.msg(f"/delete_user {name}", ADMIN_USER))
        await self.ghost_callback_handler(
            self.call(f"delconf_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        )

    async def test_delete_from_in_promotes_first_waitlister(self):
        await self._capped_rc()
        await self._confirm_delete("User1")
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User2", "User3"])
        self.assertEqual(rc.waitList, [])

    async def test_promotion_is_announced_in_chat(self):
        await self._capped_rc()
        mock_bot.send_message.reset_mock()
        await self._confirm_delete("User1")
        texts = " | ".join(self.sent_texts())
        self.assertIn("User3", texts)
        self.assertIn("WAITING", texts.upper())

    async def test_delete_from_waitlist_promotes_nobody(self):
        await self._capped_rc()
        await self._confirm_delete("User3")
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User1", "User2"])
        self.assertEqual(rc.waitList, [])

    async def test_delete_from_out_does_not_disturb_in_list(self):
        await self._capped_rc()
        await self.vote_out(USERS[3])
        await self._confirm_delete("User4")
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User1", "User2"])
        self.assertEqual([u.name for u in rc.waitList], ["User3"])

    async def test_uncapped_rollcall_unaffected(self):
        await self.start_rc()
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        await self._confirm_delete("User1")
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User2"])
        self.assertEqual(rc.waitList, [])

    async def test_two_deletes_drain_two_waitlisters(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 2", ADMIN_USER))
        for u in USERS[:4]:
            await self.vote_in(u)
        self.assertEqual([u.name for u in self.rc(0).waitList], ["User3", "User4"])
        await self._confirm_delete("User1")
        await self._confirm_delete("User2")
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User3", "User4"])
        self.assertEqual(rc.waitList, [])

    async def test_move_in_to_out_promotes_and_announces(self):
        """set_status (the /ovrd confirm path) already promoted — but silently."""
        await self._capped_rc()
        mock_bot.send_message.reset_mock()
        await self.set_status_override(
            self.msg("/set_status User1 out", ADMIN_USER)
        )
        await self.ghost_callback_handler(
            self.call(f"ovrd_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        )
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User2", "User3"])
        texts = " | ".join(self.sent_texts())
        self.assertIn("User3", texts)


# ── REST + Web entry points ──────────────────────────────────────────────────

API_CHAT_ID = -1001999000901
ADMIN_ID = 3001
BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


class TestDeletePromotesWaitlistApi(unittest.TestCase):
    """Same invariant through the REST admin API and the group web page."""

    @classmethod
    def setUpClass(cls):
        import bot_state  # noqa: F401  warm conftest mocks
        import rollcall_manager
        import db
        from api.main import app
        from api.identity import issue_identity_token
        cls.app = app
        cls.manager = rollcall_manager.manager
        cls.db = db
        cls.issue_identity_token = staticmethod(issue_identity_token)
        cls.client = TestClient(app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        self.manager.set_admin_rights(API_CHAT_ID, False)
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        mock_bot.get_chat_member.return_value.status = "administrator"
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))

        self.chat = self.db.get_or_create_chat(API_CHAT_ID)
        self.db.set_web_admin(API_CHAT_ID, ADMIN_ID, "Admin")
        self.id_token = self.issue_identity_token(ADMIN_ID)

        from services.rollcalls import start_rollcall
        from services.settings import set_wait_limit
        from services.voting import vote_in
        asyncio.run(start_rollcall(API_CHAT_ID, "Sunday Game", ADMIN_ID, "Admin"))
        set_wait_limit(API_CHAT_ID, 2, ADMIN_ID, "Admin", rc_number=0)
        for uid, nm in ((4001, "Ann"), (4002, "Ben"), (4003, "Cal")):
            asyncio.run(vote_in(API_CHAT_ID, uid, nm, nm.lower()))
        rc = self.manager.get_rollcall(API_CHAT_ID, 0)
        assert [u.name for u in rc.inList] == ["Ann", "Ben"]
        assert [u.name for u in rc.waitList] == ["Cal"]

    def _roster(self):
        rc = self.manager.get_rollcall(API_CHAT_ID, 0)
        return [u.name for u in rc.inList], [u.name for u in rc.waitList]

    def test_web_remove_user_promotes_waitlister(self):
        r = self.client.post(
            f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/remove-user",
            json={"id_token": self.id_token, "rollcall_num": 1, "name": "Ann"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual([u["name"] for u in body["in"]], ["Ben", "Cal"])
        self.assertEqual(body["waiting"], [])
        self.assertEqual(self._roster(), (["Ben", "Cal"], []))

    def test_web_move_user_out_promotes_waitlister(self):
        r = self.client.post(
            f"/api/v1/web/group/{self.chat['group_web_token']}/rollcalls/move-user",
            json={"id_token": self.id_token, "rollcall_num": 1,
                  "name": "Ann", "new_status": "out"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._roster(), (["Ben", "Cal"], []))

    def test_rest_delete_user_promotes_and_reports_promotion(self):
        from db import _hash_token, generate_api_token, insert_api_token
        api_token = generate_api_token()
        insert_api_token(_hash_token(api_token), API_CHAT_ID, "read,vote,admin",
                         label="test", issued_by_user_id=ADMIN_ID)
        headers = {"Authorization": f"Bearer {api_token}"}
        r = self.client.request(
            "DELETE",
            f"/api/v1/chats/{API_CHAT_ID}/rollcalls/1/users/Ann",
            json={"admin_user_id": ADMIN_ID, "admin_name": "Admin"},
            headers=headers,
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual([u["name"] for u in body["promoted"]], ["Cal"])
        self.assertEqual(self._roster(), (["Ben", "Cal"], []))
