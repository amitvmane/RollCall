"""
Integration: list query commands — /wi, /wo, /wm, /ww, /history, /buzz.
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestWhosIn(IntegrationBase):

    async def test_wi_no_rollcall_sends_error(self):
        await self.whos_in(self.msg("/wi", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() or "active" in t.lower() for t in texts))

    async def test_wi_shows_in_list(self):
        await self.start_rc()
        for user in USERS[:3]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.whos_in(self.msg("/wi", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)
        full = " ".join(texts)
        self.assertTrue(any(f"User{u['id']}" in full for u in USERS[:3]))

    async def test_wi_empty_list_still_responds(self):
        await self.start_rc()
        get_mock_bot().send_message.reset_mock()
        await self.whos_in(self.msg("/wi", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestWhosOut(IntegrationBase):

    async def test_wo_no_rollcall_sends_error(self):
        await self.whos_out(self.msg("/wo", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_wo_shows_out_list(self):
        await self.start_rc()
        for user in USERS[:2]:
            await self.vote_out(user)
        get_mock_bot().send_message.reset_mock()
        await self.whos_out(self.msg("/wo", ADMIN_USER))
        full = " ".join(self.sent_texts())
        self.assertTrue(any(f"User{u['id']}" in full for u in USERS[:2]))


class TestWhosMaybe(IntegrationBase):

    async def test_wm_no_rollcall_sends_error(self):
        await self.whos_maybe(self.msg("/wm", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_wm_shows_maybe_list(self):
        await self.start_rc()
        await self.vote_maybe(USERS[0])
        get_mock_bot().send_message.reset_mock()
        await self.whos_maybe(self.msg("/wm", ADMIN_USER))
        full = " ".join(self.sent_texts())
        self.assertIn("User1", full)


class TestWhosWaiting(IntegrationBase):

    async def test_ww_no_rollcall_sends_error(self):
        await self.whos_waiting(self.msg("/ww", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_ww_shows_waitlist(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        for user in USERS[:3]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.whos_waiting(self.msg("/ww", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)

    async def test_ww_empty_still_responds(self):
        await self.start_rc()
        get_mock_bot().send_message.reset_mock()
        await self.whos_waiting(self.msg("/ww", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestHistory(IntegrationBase):

    async def test_history_no_ended_rollcalls(self):
        await self.history_command(self.msg("/history", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no ended" in t.lower() or "not found" in t.lower() or "no rollcall" in t.lower() for t in texts))

    async def test_history_shows_ended_rollcall(self):
        await self.start_rc("Past Game")
        for user in USERS[:3]:
            await self.vote_in(user)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.history_command(self.msg("/history", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)
        full = " ".join(texts)
        self.assertIn("Past Game", full)

    async def test_history_pagination_limit(self):
        # End 3 rollcalls, then request page 2 with limit 2
        for i in range(3):
            await self.start_rc(f"Game {i+1}")
            for user in USERS[:2]:
                await self.vote_in(user)
            await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.history_command(self.msg("/history 2 2", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_history_page_beyond_end(self):
        await self.history_command(self.msg("/history 10 99", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no" in t.lower() or "page" in t.lower() for t in texts))


class TestBuzz(IntegrationBase):

    async def test_buzz_no_members_sends_notice(self):
        await self.start_rc()
        await self.buzz_command(self.msg("/buzz", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no known" in t.lower() or "member" in t.lower() for t in texts))

    async def test_buzz_everyone_voted_sends_notice(self):
        await self.start_rc()
        for user in USERS[:3]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.buzz_command(self.msg("/buzz", ADMIN_USER))
        texts = self.sent_texts()
        # Everyone has voted, so either "everyone voted" or no unvoted to ping
        self.assertGreater(len(texts), 0)

    async def test_buzz_rate_limited(self):
        import time
        await self.start_rc()
        for user in USERS[:2]:
            await self.vote_in(user)
        # Seed rate-limit entry
        self.bs._buzz_cooldowns[CHAT_ID] = time.time()
        get_mock_bot().send_message.reset_mock()
        await self.buzz_command(self.msg("/buzz", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("wait" in t.lower() or "recently" in t.lower() for t in texts))

    async def test_buzz_non_admin_rejected(self):
        await self.start_rc()
        await self.set_admins(self.msg("/set_admins", ADMIN_USER))
        try:
            get_mock_bot().get_chat_member.return_value.status = "member"
            await self.buzz_command(self.msg("/buzz", USERS[0]))
            texts = self.sent_texts()
            self.assertTrue(any("permission" in t.lower() for t in texts))
        finally:
            get_mock_bot().get_chat_member.return_value.status = "administrator"

    async def test_buzz_without_rollcall_pings_members(self):
        # Vote in a rollcall first to register members, then end it
        await self.start_rc()
        for user in USERS[:3]:
            await self.vote_in(user)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        # Now buzz without any active rollcall
        await self.buzz_command(self.msg("/buzz Heads up!", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)
