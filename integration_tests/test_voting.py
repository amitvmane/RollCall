"""
Integration: voting details — waitlist, promotion, ghost reconfirmation, rate limit.
"""
import asyncio
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot
import db


class TestWaitlistBehavior(IntegrationBase):

    async def test_sixth_user_goes_to_waitlist_when_limit_five(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 5", ADMIN_USER))
        for user in USERS[:5]:
            await self.vote_in(user)
        self.assertEqual(len(self.rc(0).inList), 5)
        await self.vote_in(USERS[5])
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 5)
        self.assertEqual(len(rc.waitList), 1)

    async def test_out_promotes_top_waiter(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for user in USERS[:3]:
            await self.vote_in(user)
        await self.vote_in(USERS[3])  # goes to waitlist
        self.assertEqual(len(self.rc(0).waitList), 1)
        # User 0 goes OUT → User 3 promoted
        await self.vote_out(USERS[0])
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 3)
        self.assertEqual(len(rc.waitList), 0)
        in_ids = {u.user_id for u in rc.inList}
        self.assertIn(USERS[3]["id"], in_ids)

    async def test_limit_increase_promotes_multiple_waiters(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        for user in USERS[:2]:
            await self.vote_in(user)
        for user in USERS[2:5]:
            await self.vote_in(user)  # 3 go to waitlist
        self.assertEqual(len(self.rc(0).waitList), 3)
        # Increase limit to 4 → top 2 waiters promoted
        await self.wait_limit(self.msg("/sl 4", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 4)
        self.assertEqual(len(rc.waitList), 1)

    async def test_limit_reduce_moves_excess_to_waitlist(self):
        await self.start_rc()
        for user in USERS[:5]:
            await self.vote_in(user)
        self.assertEqual(len(self.rc(0).inList), 5)
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 3)
        self.assertEqual(len(rc.waitList), 2)

    async def test_waitlist_via_inline_button(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        for user in USERS[:2]:
            await self.callback_handler(self.call("btn_in_1", user))
        await self.callback_handler(self.call("btn_in_1", USERS[2]))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 2)
        self.assertEqual(len(rc.waitList), 1)


class TestRateLimiting(IntegrationBase):

    async def test_rapid_votes_ignored_silently(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        # Inject a fresh rate-limit entry AFTER the successful IN vote
        import time
        self.bs._rate_limits[(CHAT_ID, user["id"])] = time.time()
        # Call out_user directly (bypass vote_out helper which clears rate limit)
        await self.out_user(self.msg("/out", user))
        # Rate-limited: user stays IN (out ignored)
        self.assertEqual(len(self.rc(0).inList), 1)

    async def test_inline_button_rate_limited_sends_toast(self):
        await self.start_rc()
        import time
        user = USERS[0]
        self.bs._rate_limits[(CHAT_ID, user["id"])] = time.time()
        await self.callback_handler(self.call("btn_in_1", user))
        get_mock_bot().answer_callback_query.assert_called()
        args = get_mock_bot().answer_callback_query.call_args
        # Should contain rate-limit message in text arg
        all_args = str(args)
        self.assertIn("fast", all_args.lower() if all_args else "fast")


class TestGhostReconfirmation(IntegrationBase):

    def _add_ghost_events(self, user_id, count, first_name="User"):
        """Seed ghost_records so the reconf dialog triggers."""
        for _ in range(count):
            db.increment_ghost_count(CHAT_ID, user_id, first_name)

    async def test_ghost_reconf_dialog_shown_on_in(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._add_ghost_events(user["id"], 1)
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(user)
        texts = self.sent_texts()
        self.assertTrue(any("ghost" in t.lower() or "committing" in t.lower() for t in texts))
        # User should NOT be in inList yet
        self.assertEqual(len(self.rc(0).inList), 0)

    async def test_ghost_reconf_commit_adds_to_in(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._add_ghost_events(user["id"], 1)
        await self.vote_in(user)
        # Confirm commitment
        call = self.call(f"reconf_in_0_{user['id']}", user)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 1)

    async def test_ghost_reconf_back_out_adds_to_out(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._add_ghost_events(user["id"], 1)
        await self.vote_in(user)
        # User declines
        call = self.call(f"reconf_out_0_{user['id']}", user)
        await self.ghost_callback_handler(call)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.outList), 1)

    async def test_ghost_reconf_inline_button_in(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._add_ghost_events(user["id"], 1)
        # Vote via inline button — should trigger reconf
        await self.callback_handler(self.call("btn_in_1", user))
        self.assertEqual(len(self.rc(0).inList), 0)
        # Confirm via ghost callback
        call = self.call(f"reconf_in_0_{user['id']}", user)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 1)
