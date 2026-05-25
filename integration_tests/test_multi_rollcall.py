"""
Integration: multiple simultaneous rollcalls — ::N routing, isolation, panel renumbering.
"""
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from conftest import get_mock_bot


class TestMultiRollcallRouting(IntegrationBase):

    async def test_two_rollcalls_exist_simultaneously(self):
        await self.start_rc("Event One")
        await self.start_rc("Event Two")
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 2)

    async def test_vote_default_goes_to_first(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.vote_in(USERS[0])
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 1)
        self.assertEqual(len(rc2.inList), 0)

    async def test_vote_with_suffix_routes_to_second(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.vote_in(USERS[0], rc_suffix="::2")
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 0)
        self.assertEqual(len(rc2.inList), 1)

    async def test_vote_out_with_suffix_routes_to_second(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.vote_out(USERS[0], rc_suffix="::2")
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc2.outList), 1)

    async def test_vote_maybe_with_suffix_routes_correctly(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.vote_maybe(USERS[0], rc_suffix="::2")
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc2.maybeList), 1)

    async def test_users_split_between_rollcalls(self):
        await self.start_rc("Alpha")
        await self.start_rc("Beta")
        for user in USERS[:5]:
            await self.vote_in(user)           # default → rc1
        for user in USERS[5:]:
            await self.vote_in(user, rc_suffix="::2")   # rc2
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 5)
        self.assertEqual(len(rc2.inList), 5)

    async def test_end_first_renumbers_second(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 2)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        rcs = self.mgr.get_rollcalls(CHAT_ID)
        self.assertEqual(len(rcs), 1)
        self.assertEqual(rcs[0].title, "Second")

    async def test_three_rollcalls_each_gets_own_votes(self):
        for i in range(3):
            await self.start_rc(f"Event {i+1}")
        await self.vote_in(USERS[0])               # rc1
        await self.vote_in(USERS[1], rc_suffix="::2")  # rc2
        await self.vote_in(USERS[2], rc_suffix="::3")  # rc3
        for i in range(3):
            self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, i).inList), 1)

    async def test_inline_button_routes_to_correct_rc(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        # btn_in_2 routes to rollcall #2 (index 1)
        await self.callback_handler(self.call("btn_in_2", USERS[0]))
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 0)
        self.assertEqual(len(rc2.inList), 1)

    async def test_erc_with_no_active_rollcall_sends_error(self):
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_sif_routes_to_second_rollcall(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        names1 = [u.name for u in rc1.inList]
        names2 = [u.name for u in rc2.inList]
        self.assertNotIn("Alice", names1)
        self.assertIn("Alice", names2)


class TestRollcallIsolation(IntegrationBase):

    async def test_waitlist_limit_per_rollcall(self):
        await self.start_rc("Capped")
        await self.start_rc("Uncapped")
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))  # limits rc1
        for user in USERS[:3]:
            await self.vote_in(user)          # rc1: 2 in, 1 wait
        # rc2 has no limit
        for user in USERS[3:6]:
            await self.vote_in(user, rc_suffix="::2")
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 2)
        self.assertEqual(len(rc1.waitList), 1)
        self.assertEqual(len(rc2.inList), 3)
        self.assertEqual(len(rc2.waitList), 0)

    async def test_title_update_targets_first_rollcall(self):
        await self.start_rc("Original")
        await self.start_rc("Other")
        await self.set_title(self.msg("/st Updated Title", ADMIN_USER))
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(rc1.title, "Updated Title")
        self.assertEqual(rc2.title, "Other")
