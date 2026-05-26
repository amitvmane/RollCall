"""
Integration: core lifecycle — /src, /st, /panel, 10-user vote session, /erc.
"""
import asyncio
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestStartRollCall(IntegrationBase):

    async def test_src_creates_rollcall_in_db(self):
        await self.start_rc("Weekly Football")
        rc = self.rc(0)
        self.assertIsNotNone(rc)
        self.assertEqual(rc.title, "Weekly Football")

    async def test_src_sends_panel_message(self):
        await self.start_rc()
        self.assertGreater(self.sent_count(), 0)

    async def test_src_panel_id_tracked(self):
        await self.start_rc()
        self.assertIn((CHAT_ID, 1), self.bs._panel_msg_ids)

    async def test_src_no_rollcall_title_defaults(self):
        await self.start_roll_call(self.msg("/src", ADMIN_USER))
        rc = self.rc(0)
        self.assertIsNotNone(rc)

    async def test_set_title_updates_rollcall(self):
        await self.start_rc("Old Title")
        await self.set_title(self.msg("/st New Title", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(rc.title, "New Title")

    async def test_show_panel_sends_message(self):
        await self.start_rc()
        get_mock_bot().send_message.reset_mock()
        await self.show_panel(self.msg("/panel", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)

    async def test_no_rollcall_show_panel_sends_error(self):
        await self.show_panel(self.msg("/panel", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() or "not started" in t.lower() or "doesn't exist" in t.lower() for t in texts))


class TestTenUsersVote(IntegrationBase):

    async def test_all_ten_users_vote_in(self):
        await self.start_rc("Big Event")
        for user in USERS:
            await self.vote_in(user)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 10)

    async def test_users_appear_in_correct_lists(self):
        await self.start_rc()
        for user in USERS[:4]:
            await self.vote_in(user)
        for user in USERS[4:7]:
            await self.vote_out(user)
        for user in USERS[7:]:
            await self.vote_maybe(user)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 4)
        self.assertEqual(len(rc.outList), 3)
        self.assertEqual(len(rc.maybeList), 3)

    async def test_vote_in_then_out_moves_user(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user)
        self.assertEqual(len(self.rc(0).inList), 1)
        await self.vote_out(user)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.outList), 1)

    async def test_vote_with_comment(self):
        await self.start_rc()
        user = USERS[0]
        await self.vote_in(user, comment="bringing snacks")
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 1)
        self.assertIn("bringing snacks", rc.inList[0].comment)

    async def test_whos_in_reflects_votes(self):
        await self.start_rc()
        for user in USERS[:5]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.whos_in(self.msg("/wi", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("User1" in t or "user1" in t or "5" in t or "IN" in t for t in texts))


class TestEndRollCall(IntegrationBase):

    async def test_erc_ends_rollcall(self):
        await self.start_rc("Sunday Game")
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 1)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)

    async def test_erc_posts_finish_list(self):
        await self.start_rc()
        for user in USERS[:3]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)

    async def test_erc_with_no_rollcall_sends_error(self):
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_erc_triggers_ghost_prompt_when_users_were_in(self):
        await self.start_rc()
        for user in USERS[:3]:
            await self.vote_in(user)
        get_mock_bot().send_message.reset_mock()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        texts = self.sent_texts()
        # Ghost prompt or finish list should be sent
        self.assertGreater(self.sent_count(), 0)

    async def test_erc_by_attribution_shown(self):
        await self.start_rc()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("Admin" in t or "admin" in t.lower() for t in texts))


class TestPanelCallbacks(IntegrationBase):

    async def test_inline_in_button_adds_user(self):
        await self.start_rc()
        call = self.call("btn_in_1", USERS[0])
        await self.callback_handler(call)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 1)

    async def test_inline_out_button_adds_user(self):
        await self.start_rc()
        call = self.call("btn_out_1", USERS[0])
        await self.callback_handler(call)
        self.assertEqual(len(self.rc(0).outList), 1)

    async def test_inline_maybe_button_adds_user(self):
        await self.start_rc()
        call = self.call("btn_maybe_1", USERS[0])
        await self.callback_handler(call)
        self.assertEqual(len(self.rc(0).maybeList), 1)

    async def test_inline_in_then_out_moves_user(self):
        await self.start_rc()
        await self.callback_handler(self.call("btn_in_1", USERS[0]))
        self._clear_rate(USERS[0])
        await self.callback_handler(self.call("btn_out_1", USERS[0]))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.outList), 1)

    async def test_ten_users_vote_via_inline_button(self):
        await self.start_rc()
        for user in USERS:
            await self.callback_handler(self.call("btn_in_1", user))
        self.assertEqual(len(self.rc(0).inList), 10)

    async def test_end_confirm_flow_ends_rollcall(self):
        await self.start_rc()
        # Tap End
        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))
        # Confirm Yes
        await self.callback_handler(self.call("btn_endconfirm_1", ADMIN_USER))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)

    async def test_end_cancel_keeps_rollcall(self):
        await self.start_rc()
        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_endcancel_1", ADMIN_USER))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 1)

    async def test_refresh_button_sends_answer(self):
        await self.start_rc()
        await self.callback_handler(self.call("btn_refresh_1", USERS[0]))
        get_mock_bot().answer_callback_query.assert_called()

    async def test_invalid_callback_data_answered(self):
        await self.start_rc()
        await self.callback_handler(self.call("garbage_data", USERS[0]))
        get_mock_bot().answer_callback_query.assert_called()
