"""
Web stats lookup (GET /web/group/{token}/stats/member): a signed-in member
of the group can open anyone's stats in it, the web parity for /stats @user
(stats are open to every member, 2026-11-22). A bare group link still only
ever reaches guest stats.
"""
import os
from unittest.mock import patch

from fastapi.testclient import TestClient

import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


class TestWebMemberStats(IntegrationBase):

    def setUp(self):
        super().setUp()
        from api.main import app
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.client = TestClient(app)
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        self.token = db.get_or_create_chat(CHAT_ID).get("group_web_token")
        if not self.token:
            from services.web import get_group_web_token
            self.token = get_group_web_token(CHAT_ID)

    def _get(self, params, uid=None, token=None):
        from api.identity import issue_identity_token
        hdr = {"X-Identity-Token": token or issue_identity_token(uid)} if (uid or token) else {}
        return self.client.get(f"/api/v1/web/group/{self.token}/stats/member", params=params, headers=hdr)

    async def _played(self):
        await self.start_rc("Game")
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.set_in_for(self.msg("/sif Ann", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    async def test_member_sees_another_members_stats(self):
        await self._played()
        r = self._get({"user_id": USERS[1]["id"]}, uid=USERS[0]["id"])
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["display_name"], body["kind"]), ("User2", "real"))
        self.assertEqual(body["personal"]["sessions_attended"], 1)
        self.assertIsNotNone(body["personal"]["rank"])

    async def test_member_sees_a_guests_stats(self):
        await self._played()
        r = self._get({"name": "Ann"}, uid=USERS[0]["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["kind"], "proxy")

    async def test_not_signed_in_or_not_a_member_is_refused(self):
        await self._played()
        self.assertEqual(self._get({"user_id": USERS[1]["id"]}).status_code, 401)
        stranger = 987654321    # a valid sign-in, never seen in this group
        self.assertEqual(self._get({"user_id": USERS[1]["id"]}, uid=stranger).status_code, 403)
        self.assertEqual(self._get({"user_id": USERS[1]["id"]}, token="1.2.forged").status_code, 401)

    async def test_no_record_and_bad_requests(self):
        await self._played()
        self.assertEqual(self._get({"user_id": USERS[8]["id"]}, uid=USERS[0]["id"]).status_code, 404)
        self.assertEqual(self._get({"name": "Nobody"}, uid=USERS[0]["id"]).status_code, 404)
        # A member's name never resolves here: members are addressed by id.
        self.assertEqual(self._get({"name": "User2"}, uid=USERS[0]["id"]).status_code, 404)
        self.assertEqual(self._get({}, uid=USERS[0]["id"]).status_code, 422)

    async def test_group_stats_rows_carry_who_they_are(self):
        await self._played()
        db.increment_ghost_count(CHAT_ID, USERS[2]["id"], "User3")
        db.increment_ghost_count(CHAT_ID, None, "Ann", proxy_name="Ann")
        body = self.client.get(f"/api/v1/web/group/{self.token}/stats").json()
        self.assertTrue(all("user_id" in e for e in body["leaderboard"]))
        self.assertTrue(body["ghost_leaderboard"])
        for g in body["ghost_leaderboard"]:
            self.assertTrue(g["user_id"] or g["proxy_name"], g)
        # The bare link still never hands out a real member's record.
        r = self.client.get(f"/api/v1/web/group/{self.token}/stats", params={"name": "User2"})
        self.assertIsNone(r.json()["personal"])
