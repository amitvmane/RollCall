"""
Integration: ghost tracking lifecycle — toggle, absent limit, leaderboard,
mark_absent, clear_absent, full ghost flow with erc.
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from conftest import get_mock_bot


class TestGhostToggle(IntegrationBase):

    async def test_toggle_ghost_tracking_enables(self):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        self.assertTrue(self.mgr.get_ghost_tracking_enabled(CHAT_ID))

    async def test_toggle_ghost_tracking_disables(self):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking off", ADMIN_USER))
        self.assertFalse(self.mgr.get_ghost_tracking_enabled(CHAT_ID))

    async def test_toggle_ghost_tracking_flips_state(self):
        initial = self.mgr.get_ghost_tracking_enabled(CHAT_ID)
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking", ADMIN_USER))
        self.assertNotEqual(self.mgr.get_ghost_tracking_enabled(CHAT_ID), initial)

    async def test_toggle_sends_confirmation(self):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("enabled" in t.lower() or "ghost" in t.lower() for t in texts))


class TestAbsentLimit(IntegrationBase):

    async def test_set_absent_limit_stores_value(self):
        await self.set_absent_limit(self.msg("/set_absent_limit 3", ADMIN_USER))
        self.assertEqual(self.mgr.get_absent_limit(CHAT_ID), 3)

    async def test_set_absent_limit_invalid_sends_error(self):
        await self.set_absent_limit(self.msg("/set_absent_limit abc", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("integer" in t.lower() or "positive" in t.lower() for t in texts))

    async def test_set_absent_limit_zero_sends_error(self):
        await self.set_absent_limit(self.msg("/set_absent_limit 0", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("positive" in t.lower() for t in texts))

    async def test_set_absent_limit_no_arg_sends_usage(self):
        await self.set_absent_limit(self.msg("/set_absent_limit", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("usage" in t.lower() for t in texts))


class TestClearAbsent(IntegrationBase):

    async def test_clear_absent_resets_ghost_count(self):
        user = USERS[0]
        db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        # Verify count was recorded
        count = db.get_ghost_count(CHAT_ID, user["id"])
        self.assertGreater(count, 0)
        await self.clear_absent(self.msg(f"/clear_absent {user['first_name']}", ADMIN_USER))
        count_after = db.get_ghost_count(CHAT_ID, user["id"])
        self.assertEqual(count_after, 0)

    async def test_clear_absent_sends_confirmation(self):
        user = USERS[0]
        db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        get_mock_bot().send_message.reset_mock()
        await self.clear_absent(self.msg(f"/clear_absent {user['first_name']}", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("cleared" in t.lower() for t in texts))

    async def test_clear_absent_not_found_sends_error(self):
        await self.clear_absent(self.msg("/clear_absent UnknownPerson", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no ghost" in t.lower() or "not found" in t.lower() for t in texts))

    async def test_clear_absent_no_name_sends_usage(self):
        await self.clear_absent(self.msg("/clear_absent", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("usage" in t.lower() for t in texts))

    async def test_clear_absent_proxy_resets_proxy_ghost_count(self):
        db.increment_ghost_count(CHAT_ID, -1, "Alice", proxy_name="Alice")
        count = db.get_ghost_count_by_proxy_name(CHAT_ID, "Alice")
        self.assertGreater(count, 0)
        await self.clear_absent(self.msg("/clear_absent Alice", ADMIN_USER))
        count_after = db.get_ghost_count_by_proxy_name(CHAT_ID, "Alice")
        self.assertEqual(count_after, 0)


class TestGhostFullFlow(IntegrationBase):
    """End-to-end: ghost events accumulate over multiple rollcalls."""

    async def test_ghost_count_accumulates_over_sessions(self):
        user = USERS[0]
        for _ in range(3):
            db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        count = db.get_ghost_count(CHAT_ID, user["id"])
        self.assertEqual(count, 3)

    async def test_ghost_reconf_required_after_multiple_ghosts(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/set_absent_limit 2", ADMIN_USER))
        user = USERS[0]
        for _ in range(2):
            db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(user)
        # Should NOT be in list yet — reconf dialog
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 0)

    async def test_ghost_reconf_commit_then_out_flow(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/set_absent_limit 1", ADMIN_USER))
        user = USERS[0]
        db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        await self.vote_in(user)
        # Commit IN
        call = self.call(f"reconf_in_0_{user['id']}", user)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 1)
        # Now vote OUT
        await self.vote_out(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.outList), 1)

    async def test_multiple_users_some_ghost_some_clean(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/set_absent_limit 1", ADMIN_USER))
        ghost_user = USERS[0]
        clean_user = USERS[1]
        db.increment_ghost_count(CHAT_ID, ghost_user["id"], ghost_user["first_name"])
        # Clean user votes in normally
        await self.vote_in(clean_user)
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 1)
        # Ghost user triggers reconf
        await self.vote_in(ghost_user)
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 1)
        # Ghost confirms
        call = self.call(f"reconf_in_0_{ghost_user['id']}", ghost_user)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 2)
