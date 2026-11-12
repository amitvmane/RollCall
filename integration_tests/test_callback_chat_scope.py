"""
Integration: a button's callback data is not proof of anything.

Telegram relays whatever data a client sends with a button press — it is not
checked against the buttons the bot actually posted — and rollcall ids are
sequential. So an admin of their OWN group (anyone is, of a group they made)
could press a button there carrying another group's rollcall id, or pair
their own user id with another group's chat id.

Every case here presses a button in OTHER_CHAT that names a session of
CHAT_ID, and asserts nothing about CHAT_ID's session is read or changed.
Also covers the once-only review and the admin gates added alongside.
"""
from unittest.mock import AsyncMock, patch

import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID, make_call
from mock_helpers import get_mock_bot

OTHER_CHAT = -100_000_777
STRANGER = {"id": 4242, "first_name": "Mallory", "last_name": None, "username": "mallory"}


def _answers():
    """Texts the bot answered button presses with."""
    out = []
    for args, kwargs in get_mock_bot().answer_callback_query.call_args_list:
        out.append(str(args[1] if len(args) > 1 else kwargs.get("text", "")))
    return out


class _EndedSession(IntegrationBase):

    async def _victim_session(self, n_users=3):
        """An ended, unreviewed session in CHAT_ID. Returns its db id."""
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.start_rc("Victim Game")
        for user in USERS[:n_users]:
            await self.vote_in(user)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        rc_id = db.get_unprocessed_rollcalls(CHAT_ID, days=30)[0]["id"]
        db.get_or_create_chat(OTHER_CHAT)
        bot = get_mock_bot()
        bot.send_message.reset_mock()
        bot.edit_message_text.reset_mock()
        bot.edit_message_reply_markup.reset_mock()
        bot.answer_callback_query.reset_mock()
        return rc_id

    def _foreign(self, data):
        return make_call(data, STRANGER, chat_id=OTHER_CHAT)

    def _still_pending(self, rc_id):
        return rc_id in {r["id"] for r in db.get_unprocessed_rollcalls(CHAT_ID, days=30)}


class TestRollcallInChat(_EndedSession):

    async def test_helper_only_returns_the_owning_chats_rollcall(self):
        rc_id = await self._victim_session()
        self.assertEqual(db.get_rollcall_in_chat(rc_id, CHAT_ID)["id"], rc_id)
        self.assertIsNone(db.get_rollcall_in_chat(rc_id, OTHER_CHAT))
        self.assertIsNone(db.get_rollcall_in_chat(rc_id + 9999, CHAT_ID))
        self.assertIsNone(db.get_rollcall_in_chat("not-a-number", CHAT_ID))


class TestGhostCallbacksAreChatScoped(_EndedSession):

    async def test_ghost_no_cannot_close_another_chats_review(self):
        rc_id = await self._victim_session()
        await self.ghost_callback_handler(self._foreign(f"ghost_no_{rc_id}"))
        self.assertTrue(self._still_pending(rc_id), "another chat closed this session's review")
        self.assertTrue(any("isn't part of this group" in a for a in _answers()))

    async def test_ghost_done_cannot_record_ghosts_on_another_chats_session(self):
        rc_id = await self._victim_session()
        self.bs._ghost_selections[(OTHER_CHAT, rc_id)] = {USERS[0]["id"]}
        await self.ghost_callback_handler(self._foreign(f"ghost_done_{rc_id}"))
        self.assertTrue(self._still_pending(rc_id))
        self.assertEqual(db.get_ghost_count(CHAT_ID, USERS[0]["id"]), 0)
        self.assertEqual(db.get_ghost_count(OTHER_CHAT, USERS[0]["id"]), 0)

    async def test_player_list_is_not_shown_to_another_chat(self):
        rc_id = await self._victim_session()
        for data in (f"ghost_yes_{rc_id}", f"mabs_sel_{rc_id}", f"ghost_moreout_{rc_id}"):
            await self.ghost_callback_handler(self._foreign(data))
        bot = get_mock_bot()
        bot.edit_message_text.assert_not_called()
        bot.edit_message_reply_markup.assert_not_called()

    async def test_toggle_cannot_touch_another_chats_selection(self):
        rc_id = await self._victim_session()
        await self.ghost_callback_handler(self._foreign(f"ghost_tog_{rc_id}_{USERS[0]['id']}"))
        self.assertNotIn((OTHER_CHAT, rc_id), self.bs._ghost_selections)
        self.assertFalse(db.load_ghost_selections(OTHER_CHAT, rc_id))

    async def test_malformed_review_button_is_answered_not_raised(self):
        await self._victim_session()
        await self.ghost_callback_handler(self._foreign("ghost_no_"))
        self.assertTrue(any("Unknown" in a for a in _answers()))

    async def test_own_chat_still_works(self):
        rc_id = await self._victim_session()
        await self.ghost_callback_handler(self.call(f"ghost_no_{rc_id}", ADMIN_USER))
        self.assertFalse(self._still_pending(rc_id))


class TestReviewHappensOnce(_EndedSession):

    async def test_second_answer_does_not_count_twice(self):
        # Two prompts for one game can be on screen at once (the end-of-game
        # prompt plus /mark_absent). Answering both counted every no-show —
        # and every forgiveness — twice.
        rc_id = await self._victim_session(n_users=2)
        ghost = USERS[0]["id"]
        self.bs._ghost_selections[(CHAT_ID, rc_id)] = {ghost}
        await self.ghost_callback_handler(self.call(f"ghost_done_{rc_id}", ADMIN_USER))
        self.assertEqual(db.get_ghost_count(CHAT_ID, ghost), 1)

        self.bs._ghost_selections[(CHAT_ID, rc_id)] = {ghost}
        await self.ghost_callback_handler(self.call(f"ghost_done_{rc_id}", ADMIN_USER, message_id=501))
        self.assertEqual(db.get_ghost_count(CHAT_ID, ghost), 1, "ghost counted twice")
        self.assertTrue(any("already reviewed" in a for a in _answers()))

    async def test_all_showed_up_after_review_forgives_nobody(self):
        rc_id = await self._victim_session(n_users=2)
        ghost = USERS[0]["id"]
        self.bs._ghost_selections[(CHAT_ID, rc_id)] = {ghost}
        await self.ghost_callback_handler(self.call(f"ghost_done_{rc_id}", ADMIN_USER))
        await self.ghost_callback_handler(self.call(f"ghost_no_{rc_id}", ADMIN_USER, message_id=501))
        self.assertEqual(db.get_ghost_count(CHAT_ID, ghost), 1, "a stale prompt forgave the no-show")


class TestSessionPickerIsAdminGated(_EndedSession):

    async def test_non_admin_cannot_open_picker_in_locked_group(self):
        rc_id = await self._victim_session()
        self.mgr.set_admin_rights(CHAT_ID, True)
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"
        try:
            await self.ghost_callback_handler(self.call(f"mabs_sel_{rc_id}", USERS[5]))
        finally:
            bot.get_chat_member.return_value.status = "administrator"
        bot.edit_message_text.assert_not_called()
        self.assertTrue(any("Only admins" in a for a in _answers()))


class TestSettleCallbacksAreChatScoped(_EndedSession):

    async def _foreign_dues_chat(self):
        rc_id = await self._victim_session()
        db.update_chat_settings(OTHER_CHAT, dues_enabled=1)
        return rc_id

    async def test_settle_buttons_do_not_open_another_chats_game(self):
        from handlers.dues import settle_now_callback
        rc_id = await self._foreign_dues_chat()
        with patch("handlers.dues._begin_settlement", new=AsyncMock()) as begin:
            await settle_now_callback(self._foreign(f"settle_now:{rc_id}"))
            await self.dues_settle_pick_callback(self._foreign(f"settle_pick:{rc_id}"))
        begin.assert_not_awaited()
        self.assertTrue(all("isn't part of this group" in a for a in _answers()))
        self.assertEqual(len(_answers()), 2)

    async def test_custom_subsidy_does_not_arm_on_another_chats_game(self):
        rc_id = await self._foreign_dues_chat()
        await self.dues_settle_custom_callback(self._foreign(f"settle_custom:{rc_id}"))
        self.assertNotIn((OTHER_CHAT, STRANGER["id"]), self.bs._pending_subsidy_input)
        self.assertFalse(any("Victim Game" in t for t in self.edited_texts()))

    async def test_begin_settlement_refuses_another_chats_game(self):
        from handlers.dues import _begin_settlement
        rc_id = await self._foreign_dues_chat()
        await _begin_settlement(OTHER_CHAT, rc_id, "x")
        self.assertEqual(self.sent_count(), 0)
        self.assertFalse(self.penalty_panel_sessions)
        self.assertTrue(self._still_pending(rc_id))

    async def test_penalty_panel_refuses_another_chats_game(self):
        from handlers.penalty_panel import send_penalty_panel
        rc_id = await self._foreign_dues_chat()
        db.upsert_penalty_tier(OTHER_CHAT, "ditch", 100, "No-show", is_ditch=True)
        opened = await send_penalty_panel(OTHER_CHAT, rc_id, "x", ghost_eligible=True)
        self.assertFalse(opened)
        self.assertEqual(self.sent_count(), 0)
        self.assertTrue(self._still_pending(rc_id))


class TestDismissButtonsAreAdminGated(_EndedSession):

    async def test_non_admin_cannot_dismiss_settle_card_in_locked_group(self):
        rc_id = await self._victim_session()
        self.mgr.set_admin_rights(CHAT_ID, True)
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"
        try:
            await self.dues_settle_skip_callback(self.call(f"settle_skip:{rc_id}", USERS[5]))
            await self.dues_settle_cancel_callback(self.call(f"settle_cancel:{rc_id}", USERS[5]))
        finally:
            bot.get_chat_member.return_value.status = "administrator"
        bot.edit_message_text.assert_not_called()


class TestIdleStartButton(IntegrationBase):
    """idle_start_<chat_id>_<uid>: both numbers come from the button."""

    def _call(self, chat_id=CHAT_ID):
        # Pressed in the stranger's own DM with the bot, naming a group.
        return make_call(f"idle_start_{chat_id}_{STRANGER['id']}", STRANGER, chat_id=STRANGER["id"])

    async def _with_template(self):
        await self.set_template(self.msg('/set_template weekly "Weekly Game"', ADMIN_USER))
        self.assertTrue(db.get_templates(CHAT_ID), "fixture: template was not created")

    async def test_stranger_cannot_start_a_rollcall_in_a_group(self):
        from handlers.templates import idle_start_callback
        await self._with_template()
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "left"
        try:
            with patch("check_reminders._auto_start_from_template", new=AsyncMock()) as start:
                await idle_start_callback(self._call())
        finally:
            bot.get_chat_member.return_value.status = "administrator"
        start.assert_not_awaited()
        self.assertFalse(self.mgr.get_rollcalls(CHAT_ID))

    async def test_fails_closed_when_telegram_cannot_be_asked(self):
        from handlers.templates import idle_start_callback
        await self._with_template()
        bot = get_mock_bot()
        bot.get_chat_member.side_effect = Exception("Telegram unreachable")
        try:
            with patch("check_reminders._auto_start_from_template", new=AsyncMock()) as start:
                await idle_start_callback(self._call())
        finally:
            bot.get_chat_member.side_effect = None
        start.assert_not_awaited()

    async def test_plain_member_refused_once_group_is_locked(self):
        from handlers.templates import idle_start_callback
        await self._with_template()
        self.mgr.set_admin_rights(CHAT_ID, True)
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"
        try:
            with patch("check_reminders._auto_start_from_template", new=AsyncMock()) as start:
                await idle_start_callback(self._call())
        finally:
            bot.get_chat_member.return_value.status = "administrator"
        start.assert_not_awaited()

    async def test_real_admin_can_still_start(self):
        from handlers.templates import idle_start_callback
        await self._with_template()
        with patch("check_reminders._auto_start_from_template", new=AsyncMock()) as start:
            await idle_start_callback(self._call())
        start.assert_awaited_once()


class TestAuditPagingButtons(IntegrationBase):

    async def test_non_admin_cannot_page_in_locked_group(self):
        self.mgr.set_admin_rights(CHAT_ID, True)
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"
        try:
            with patch("handlers.admin._send_audit_page", new=AsyncMock()) as page:
                await self.audit_pagination_callback(self.call("audit_pg_2_15", USERS[5]))
        finally:
            bot.get_chat_member.return_value.status = "administrator"
        page.assert_not_awaited()

    async def test_page_size_from_the_button_is_bounded(self):
        with patch("handlers.admin._send_audit_page", new=AsyncMock()) as page:
            await self.audit_pagination_callback(self.call("audit_pg_-3_100000", ADMIN_USER))
            await self.audit_pagination_callback(self.call("audit_pg_1_0", ADMIN_USER))
        first, second = page.await_args_list
        self.assertEqual((first.kwargs["page"], first.kwargs["per_page"]), (1, 50))
        self.assertEqual(second.kwargs["per_page"], 5)
