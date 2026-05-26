"""
Integration: /stats command — personal, group, leaderboard, ghost, bot-wide.
"""
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestStatsPersonal(IntegrationBase):

    async def test_stats_no_data_returns_message(self):
        await self.stats_command(self.msg("/stats", USERS[0]))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_stats_after_voting_shows_data(self):
        await self.start_rc()
        for _ in range(3):
            await self.vote_in(USERS[0])
            await self.vote_out(USERS[0])
            await self.vote_in(USERS[0])
        get_mock_bot().send_message.reset_mock()
        await self.stats_command(self.msg("/stats", USERS[0]))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_stats_me_sends_response(self):
        await self.stats_command(self.msg("/stats", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestStatsGroup(IntegrationBase):

    async def test_stats_group_no_data(self):
        await self.stats_command(self.msg("/stats group", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_stats_group_after_votes(self):
        await self.start_rc()
        for user in USERS[:4]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.stats_command(self.msg("/stats group", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestStatsLeaderboard(IntegrationBase):

    async def test_stats_top_no_data(self):
        await self.stats_command(self.msg("/stats top", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_stats_top_after_votes(self):
        await self.start_rc()
        for user in USERS[:5]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.stats_command(self.msg("/stats top", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestStatsGhost(IntegrationBase):

    async def test_stats_ghost_no_ghosts(self):
        await self.stats_command(self.msg("/stats ghost", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no ghost" in t.lower() or "everyone" in t.lower() for t in texts))

    async def test_stats_ghost_shows_leaderboard(self):
        import db
        db.increment_ghost_count(CHAT_ID, USERS[0]["id"], USERS[0]["first_name"])
        db.increment_ghost_count(CHAT_ID, USERS[0]["id"], USERS[0]["first_name"])
        get_mock_bot().send_message.reset_mock()
        await self.stats_command(self.msg("/stats ghost", ADMIN_USER))
        texts = self.sent_texts()
        full = " ".join(texts)
        self.assertIn("User1", full)


class TestStatsBotWide(IntegrationBase):

    async def test_stats_bot_blocked_for_non_super_admin(self):
        await self.stats_command(self.msg("/stats bot", USERS[0]))
        texts = self.sent_texts()
        self.assertTrue(any("restricted" in t.lower() or "admin" in t.lower() for t in texts))

    async def test_stats_bot_allowed_for_super_admin(self):
        # ADMIN_USER has id=999 which is in config.ADMINS=[999]
        await self.stats_command(self.msg("/stats bot", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_stats_unknown_user_sends_not_found(self):
        await self.stats_command(self.msg("/stats NoSuchPersonXYZ", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not found" in t.lower() or "could not" in t.lower() for t in texts))
