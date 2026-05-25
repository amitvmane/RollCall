"""
Integration: core commands — /start, /help, /version, /rollcalls, /r,
/set_admins, /unset_admins, /timezone, /broadcast.
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestWelcome(IntegrationBase):

    async def test_start_sends_welcome(self):
        await self.welcome_and_explanation(self.msg("/start", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("rollcall" in t.lower() or "hi" in t.lower() for t in texts))

    async def test_start_non_admin_sends_error(self):
        await self.set_admins(self.msg("/set_admins", ADMIN_USER))
        try:
            get_mock_bot().get_chat_member.return_value.status = "member"
            await self.welcome_and_explanation(self.msg("/start", USERS[0]))
            texts = self.sent_texts()
            self.assertTrue(any("permission" in t.lower() for t in texts))
        finally:
            get_mock_bot().get_chat_member.return_value.status = "administrator"


class TestHelp(IntegrationBase):

    async def test_help_sends_command_list(self):
        await self.help_commands(self.msg("/help", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("/in" in t or "/src" in t or "rollcall" in t.lower() for t in texts))

    async def test_help_mentions_key_commands(self):
        await self.help_commands(self.msg("/help", ADMIN_USER))
        full = " ".join(self.sent_texts())
        for cmd in ["/in", "/out", "/src", "/erc"]:
            self.assertIn(cmd, full)


class TestVersion(IntegrationBase):

    async def test_version_sends_a_message(self):
        await self.version_command(self.msg("/version", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)

    async def test_version_short_alias(self):
        await self.version_command(self.msg("/v", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestRollcallsList(IntegrationBase):

    async def test_rollcalls_empty_sends_message(self):
        await self.show_reminders(self.msg("/rollcalls", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("empty" in t.lower() or "no" in t.lower() for t in texts))

    async def test_rollcalls_lists_active(self):
        await self.start_rc("Friday Pickup")
        get_mock_bot().send_message.reset_mock()
        await self.show_reminders(self.msg("/rollcalls", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("Friday Pickup" in t for t in texts))

    async def test_rollcalls_r_alias(self):
        await self.start_rc()
        get_mock_bot().send_message.reset_mock()
        await self.show_reminders(self.msg("/r", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)

    async def test_rollcalls_shows_all_active(self):
        await self.start_rc("Game 1")
        await self.start_rc("Game 2")
        get_mock_bot().send_message.reset_mock()
        await self.show_reminders(self.msg("/rollcalls", ADMIN_USER))
        full = " ".join(self.sent_texts())
        self.assertIn("Game 1", full)
        self.assertIn("Game 2", full)


class TestAdminMode(IntegrationBase):

    async def test_set_admins_activates(self):
        await self.set_admins(self.msg("/set_admins", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("admin" in t.lower() for t in texts))

    async def test_unset_admins_deactivates(self):
        await self.set_admins(self.msg("/set_admins", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.unset_admins(self.msg("/unset_admins", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("disabled" in t.lower() or "admin" in t.lower() for t in texts))

    async def test_set_admins_non_telegram_admin_rejected(self):
        get_mock_bot().get_chat_member.return_value.status = "member"
        await self.set_admins(self.msg("/set_admins", USERS[0]))
        texts = self.sent_texts()
        self.assertTrue(any("permission" in t.lower() for t in texts))
        get_mock_bot().get_chat_member.return_value.status = "administrator"


class TestTimezone(IntegrationBase):

    async def test_timezone_sets_valid_tz(self):
        await self.start_rc()
        await self.config_timezone(self.msg("/tz Asia/Kolkata", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("asia/kolkata" in t.lower() or "timezone" in t.lower() for t in texts))

    async def test_timezone_invalid_format_sends_error(self):
        await self.config_timezone(self.msg("/tz badformat", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("format" in t.lower() or "invalid" in t.lower() for t in texts))

    async def test_timezone_no_arg_sends_error(self):
        await self.config_timezone(self.msg("/tz", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("format" in t.lower() for t in texts))


class TestBroadcast(IntegrationBase):

    async def test_broadcast_no_text_sends_error(self):
        await self.broadcast(self.msg("/broadcast", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("missing" in t.lower() for t in texts))

    async def test_broadcast_no_chats_sends_notice(self):
        # DB is fresh — no chats seeded via get_all_chat_ids
        await self.broadcast(self.msg("/broadcast Hello everyone", ADMIN_USER))
        texts = self.sent_texts()
        # Either "no chats" or a broadcast complete message
        self.assertGreater(len(texts), 0)

    async def test_broadcast_sends_to_registered_chats(self):
        # Seed a chat so broadcast has a target
        self.mgr.get_chat(CHAT_ID)
        get_mock_bot().send_message.reset_mock()
        await self.broadcast(self.msg("/broadcast Test blast", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("broadcast" in t.lower() or "sent" in t.lower() or "complete" in t.lower() for t in texts))
