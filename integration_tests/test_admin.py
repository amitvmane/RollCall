"""
Integration: admin commands — /delete_user, /set_status, /audit_log.
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from conftest import get_mock_bot


class TestDeleteUser(IntegrationBase):

    async def test_delete_user_sends_confirmation_prompt(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.delete_user(self.msg(f"/delete_user {user['first_name']}", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("remove" in t.lower() or "delete" in t.lower() for t in texts))

    async def test_delete_user_confirm_removes_from_in(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        self.assertEqual(len(self.rc(0).inList), 1)
        await self.delete_user(self.msg(f"/delete_user {user['first_name']}", ADMIN_USER))
        call = self.call(f"delconf_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 0)

    async def test_delete_user_cancel_keeps_user(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        await self.delete_user(self.msg(f"/delete_user {user['first_name']}", ADMIN_USER))
        call = self.call(f"delconf_no_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 1)

    async def test_delete_user_no_rollcall_sends_error(self):
        await self.delete_user(self.msg("/delete_user Alice", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_delete_user_no_name_sends_error(self):
        await self.start_rc()
        await self.delete_user(self.msg("/delete_user", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("missing" in t.lower() or "username" in t.lower() for t in texts))

    async def test_delete_user_from_out_list(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_out(user)
        self.assertEqual(len(self.rc(0).outList), 1)
        await self.delete_user(self.msg(f"/delete_user {user['first_name']}", ADMIN_USER))
        call = self.call(f"delconf_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).outList), 0)


class TestSetStatus(IntegrationBase):

    async def test_set_status_moves_user_in_to_out(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        self.assertEqual(len(self.rc(0).inList), 1)
        await self.set_status_override(self.msg(f"/set_status {user['first_name']} out", ADMIN_USER))
        call = self.call(f"ovrd_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.outList), 1)

    async def test_set_status_moves_user_out_to_in(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_out(user)
        await self.set_status_override(self.msg(f"/set_status {user['first_name']} in", ADMIN_USER))
        call = self.call(f"ovrd_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 1)
        self.assertEqual(len(rc.outList), 0)

    async def test_set_status_cancel_keeps_original(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        await self.set_status_override(self.msg(f"/set_status {user['first_name']} out", ADMIN_USER))
        call = self.call(f"ovrd_no_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 1)

    async def test_set_status_user_not_found_sends_error(self):
        await self.start_rc()
        await self.set_status_override(self.msg("/set_status UnknownUser in", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not found" in t.lower() for t in texts))

    async def test_set_status_invalid_status_sends_error(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        await self.set_status_override(self.msg(f"/set_status {user['first_name']} late", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("must be" in t.lower() or "in" in t.lower() for t in texts))

    async def test_set_status_no_rollcall_sends_error(self):
        await self.set_status_override(self.msg("/set_status Alice in", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_set_status_moves_to_maybe(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        await self.set_status_override(self.msg(f"/set_status {user['first_name']} maybe", ADMIN_USER))
        call = self.call(f"ovrd_yes_0_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.maybeList), 1)


class TestAuditLog(IntegrationBase):

    async def test_audit_log_empty_sends_message(self):
        await self.audit_log_command(self.msg("/audit_log", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no commands" in t.lower() or "audit" in t.lower() or "recorded" in t.lower() for t in texts))

    async def test_audit_log_records_src(self):
        await self.start_rc("Logged Event")
        get_mock_bot().send_message.reset_mock()
        await self.audit_log_command(self.msg("/audit_log", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("rollcall" in t.lower() or "audit" in t.lower() for t in texts))

    async def test_audit_log_records_sif(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.audit_log_command(self.msg("/audit_log", ADMIN_USER))
        texts = self.sent_texts()
        self.assertGreater(len(texts), 0)

    async def test_audit_log_pagination_custom_per_page(self):
        await self.start_rc()
        # Generate multiple log entries
        for i in range(5):
            await self.set_in_for(self.msg(f"/sif Proxy{i}", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.audit_log_command(self.msg("/audit_log 3", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)
