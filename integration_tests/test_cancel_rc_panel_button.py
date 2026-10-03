"""
Integration tests for the panel's 🗑 Cancel RollCall button.

Feature: cancelling a rollcall (/xrc) was typed-command only. Admins asked
for the same thing as a panel button and on the group web page, with an
optional reason prompt either way — mirrors /xrc's own optional free-text
reason, just reached by tapping instead of typing.

Flow under test (lifecycle.py):
  btn_cancelrc_N    -> confirmation prompt (Yes/No)
  btn_cancelyes_N   -> asks for an optional reason (Skip button + free-text reply)
  btn_canceldismiss_N -> back to the panel, nothing cancelled
  btn_cancelskip_N  -> cancels now with no reason
  free-text reply   -> cancels now with that reason

cancel_rollcall() itself (is_cancelled semantics, no stats, no ghost prompt)
is already covered by tests/test_handlers.py's TestCancelRollCall and
services unit tests — these tests are specifically about the NEW button/
reason-prompt wiring and its one easy-to-get-wrong part: the pending reason
is keyed by rc_db_id, not the rc_number captured when the prompt was shown,
so a renumber while the admin is still typing must not cancel the wrong
rollcall (or crash).
"""
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot
import db


class TestCancelButtonConfirmation(IntegrationBase):

    async def test_cancel_button_shows_confirmation(self):
        await self.start_rc("Friday Futsal")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))

        edits = get_mock_bot().edit_message_text.call_args_list
        self.assertGreater(len(edits), 0)
        text_arg = edits[-1][0][0]
        self.assertIn("Cancel", text_arg)
        self.assertIn("No stats will be recorded", text_arg)
        # Still open — only the confirmation prompt was shown.
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 1)

    async def test_no_title_shows_friendly_prompt(self):
        rc = self.mgr.add_rollcall(CHAT_ID, "<Empty>")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        edits = get_mock_bot().edit_message_text.call_args_list
        text_arg = edits[-1][0][0]
        self.assertIn("this rollcall", text_arg)
        self.assertNotIn("'<Empty>'", text_arg)

    async def test_dismiss_returns_to_panel_without_cancelling(self):
        await self.start_rc("Friday Futsal")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        get_mock_bot().edit_message_text.reset_mock()

        await self.callback_handler(self.call("btn_canceldismiss_1", ADMIN_USER))

        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 1,
            "dismissing the confirmation must not cancel the rollcall")
        edits = get_mock_bot().edit_message_text.call_args_list
        self.assertGreater(len(edits), 0)
        # Back to the panel: the keyboard must be restored (not None).
        kwargs = edits[-1][1]
        self.assertIsNotNone(kwargs.get("reply_markup"))


class TestCancelButtonReasonPrompt(IntegrationBase):

    async def test_yes_asks_for_optional_reason_with_skip_button(self):
        await self.start_rc("Friday Futsal")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        get_mock_bot().edit_message_text.reset_mock()

        await self.callback_handler(self.call("btn_cancelyes_1", ADMIN_USER))

        edits = get_mock_bot().edit_message_text.call_args_list
        self.assertGreater(len(edits), 0)
        text_arg = edits[-1][0][0]
        self.assertIn("optional", text_arg.lower())
        # Rollcall must still be open — reason hasn't been supplied yet.
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 1)
        self.assertIn((CHAT_ID, ADMIN_USER["id"]), self.bs._pending_cancel_reason)

    async def test_typed_reason_cancels_with_that_reason(self):
        await self.start_rc("Friday Futsal")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_cancelyes_1", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        get_mock_bot().edit_message_text.reset_mock()

        await self.cancel_reason_reply(self.msg("rain", ADMIN_USER))

        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)
        self.assertNotIn((CHAT_ID, ADMIN_USER["id"]), self.bs._pending_cancel_reason)
        edits = get_mock_bot().edit_message_text.call_args_list
        self.assertGreater(len(edits), 0)
        final_text = edits[-1][0][0]
        self.assertIn("cancelled", final_text.lower())
        self.assertIn("rain", final_text.lower())
        self.assertIn("Admin", final_text)
        self.assertIsNone(edits[-1][1].get("reply_markup"))

    async def test_skip_button_cancels_with_no_reason(self):
        await self.start_rc("Friday Futsal")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_cancelyes_1", ADMIN_USER))
        get_mock_bot().edit_message_text.reset_mock()

        await self.callback_handler(self.call("btn_cancelskip_1", ADMIN_USER))

        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)
        self.assertNotIn((CHAT_ID, ADMIN_USER["id"]), self.bs._pending_cancel_reason)
        edits = get_mock_bot().edit_message_text.call_args_list
        final_text = edits[-1][0][0]
        self.assertIn("cancelled", final_text.lower())
        self.assertNotIn(" — ", final_text, "no reason was given, so none should appear")

    async def test_cancel_records_is_cancelled_and_no_ghost_prompt(self):
        await self.start_rc("Friday Futsal")
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.vote_in(USERS[0])
        rc_db_id = getattr(self.rc(0), "db_id", None) or getattr(self.rc(0), "id", None)
        get_mock_bot().send_message.reset_mock()

        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_cancelyes_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_cancelskip_1", ADMIN_USER))

        row = db.get_rollcall(rc_db_id)
        self.assertTrue(bool(row.get("is_cancelled")))
        texts = self.sent_texts()
        self.assertFalse(any("ghost" in t.lower() for t in texts),
            "a cancelled session must never trigger the ghost-mark prompt")


class TestCancelReasonRaceResilience(IntegrationBase):
    """The pending reason is keyed by rc_db_id specifically so that a
    renumber between 'Yes, cancel' and the reply doesn't cancel the wrong
    rollcall or crash."""

    async def test_rollcall_already_gone_by_the_time_reason_arrives(self):
        await self.start_rc("Friday Futsal")
        await self.callback_handler(self.call("btn_cancelrc_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_cancelyes_1", ADMIN_USER))
        self.assertIn((CHAT_ID, ADMIN_USER["id"]), self.bs._pending_cancel_reason)

        # Simulate another admin ending/cancelling it via /xrc in the meantime.
        await self.cancel_roll_call(self.msg("/xrc already handled", ADMIN_USER))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)

        get_mock_bot().edit_message_text.reset_mock()
        # The pending entry from btn_cancelyes_1 was never consumed by /xrc —
        # it's a different mechanism — so it's still sitting there.
        await self.cancel_reason_reply(self.msg("rain", ADMIN_USER))

        edits = get_mock_bot().edit_message_text.call_args_list
        self.assertGreater(len(edits), 0)
        self.assertIn("no longer open", edits[-1][0][0].lower())
        self.assertNotIn((CHAT_ID, ADMIN_USER["id"]), self.bs._pending_cancel_reason)


class TestXrcStillWorksAfterRefactor(IntegrationBase):
    """/xrc's own cleanup now goes through the same _post_cancel_cleanup
    helper as the panel button — sanity check nothing drifted."""

    async def test_xrc_cancels_without_stats(self):
        await self.start_rc("Friday Futsal")
        rc_db_id = getattr(self.rc(0), "db_id", None) or getattr(self.rc(0), "id", None)

        await self.cancel_roll_call(self.msg("/xrc low count", ADMIN_USER))

        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)
        row = db.get_rollcall(rc_db_id)
        self.assertTrue(bool(row.get("is_cancelled")))
        texts = self.sent_texts()
        self.assertTrue(any("low count" in t.lower() for t in texts), texts)
        self.assertFalse(any("ghost" in t.lower() for t in texts), texts)
