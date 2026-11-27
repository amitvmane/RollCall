"""
/buzz skips a member an admin already added with /sif — when the name stands
for them: merged into them, their @username, or merged (either side) with
their @username. A proxy vote for them also answers their buzzes (buzz_mute).
Plain names never match a member by spelling alone.

Real SQLite/Postgres + real services; telebot mocked by conftest.
"""
import unittest

from mock_helpers import reset_db

CHAT = -1001999000927
ADMIN = (999, "Admin")
RAVI, PRIYA, SAM = 301, 302, 303


class BuzzSkipsProxied(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        import bot_state  # noqa: F401
        import rollcall_manager
        import db
        from services import proxy, rollcalls, lists, identity, buzz_mute
        cls.manager = rollcall_manager.manager
        cls.db, cls.proxy, cls.rc, cls.lists = db, proxy, rollcalls, lists
        cls.identity, cls.buzz_mute = identity, buzz_mute

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        self.db.get_or_create_chat(CHAT)
        self.db.upsert_chat_member(CHAT, RAVI, "Ravi", "ravi")
        self.db.upsert_chat_member(CHAT, PRIYA, "Priya", "priya")
        self.db.upsert_chat_member(CHAT, SAM, "Sam", None)

    async def _start(self):
        await self.rc.start_rollcall(CHAT, "Match", *ADMIN)

    async def _sif(self, name):
        await self.proxy.set_in_for(CHAT, *ADMIN, name)

    def _pinged(self):
        return {u["user_id"] for u in self.lists.get_non_responders(CHAT)["candidates"]}

    def _link(self, alias, **target):
        self.identity.link_identities(CHAT, alias, admin_user_id=ADMIN[0], admin_name=ADMIN[1], **target)

    async def test_sif_at_username_is_that_member(self):
        await self._start()
        await self._sif("@Ravi")          # case-insensitive
        self.assertEqual(self._pinged(), {PRIYA, SAM})

    async def test_plain_name_matching_a_member_is_still_pinged(self):
        await self._start()
        await self._sif("Ravi")           # could be another Ravi — needs a merge
        self.assertIn(RAVI, self._pinged())

    async def test_name_merged_into_member(self):
        await self._start()
        await self._sif("Sammy")
        self._link("Sammy", canonical_user_id=SAM)
        self.assertNotIn(SAM, self._pinged())

    async def test_merged_with_username_either_side(self):
        await self._start()
        # alias side carries the @username
        await self._sif("Ravi K")
        self._link("@ravi", canonical_proxy_name="Ravi K")
        self.assertNotIn(RAVI, self._pinged())
        # canonical side carries the @username
        await self._sif("Pri")
        self._link("Pri", canonical_proxy_name="@priya")
        self.assertEqual(self._pinged(), {SAM})

    async def test_group_with_two_usernames_is_ambiguous(self):
        await self._start()
        await self._sif("Twin")
        self._link("@ravi", canonical_proxy_name="Twin")
        self._link("@priya", canonical_proxy_name="Twin")
        self.assertEqual(self._pinged(), {RAVI, PRIYA, SAM})

    async def test_unknown_username_matches_nobody(self):
        await self._start()
        await self._sif("@stranger")
        self.assertEqual(self._pinged(), {RAVI, PRIYA, SAM})

    async def test_proxy_vote_answers_their_buzzes(self):
        await self._start()
        rc_id = self.manager.get_rollcall(CHAT, 0).id
        self.buzz_mute.record(CHAT, [rc_id], [RAVI, SAM])
        await self._sif("@ravi")
        await self.proxy.set_out_for(CHAT, *ADMIN, "Stranger")
        await self.rc.end_rollcall(CHAT, 0, *ADMIN)
        left = self.buzz_mute.unanswered(CHAT)
        self.assertNotIn(RAVI, left)
        self.assertEqual(left.get(SAM), 1)


if __name__ == "__main__":
    unittest.main()
