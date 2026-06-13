"""
Integration regressions for the panel end-rollcall confirmation flow.

Production bugs being prevented:
  1. After pressing the inline ✅ Yes button on the "Are you sure?" prompt,
     the prompt stayed on screen with the buttons still attached. The
     bot sent the finish list (or tried to — see #2) as a NEW message
     but never edited / dismissed the confirmation. UX disaster: user
     saw the prompt forever and could press Yes a second time.
  2. The send_message call for the finish list was wrapped in
     `try/except: pass`. Any failure inside finishList() or the
     send was silently swallowed → user sees no "ended by + list"
     message AT ALL after pressing Yes.
  3. When the rollcall title is empty or "<Empty>" (the /src default),
     the prompt read literally "Are you sure you want to end rollcall ''"
     with empty quotes — confusing and ugly.

Fix lives in rollCall/handlers/lifecycle.py:
  - "end" action: shows "this rollcall" when title is empty/<Empty>.
  - "endconfirm" action: builds the finish list (with a fallback if
    finishList raises), then EDITS the confirmation message in place
    with the final text and removes the inline keyboard. If the edit
    fails (e.g. message too old / >4096 chars), falls back to clearing
    just the markup + sending the finish list as a separate message.
"""
from unittest.mock import patch

from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestEndConfirmDismissesPrompt(IntegrationBase):
    """After pressing ✅ Yes, the confirmation message must be replaced
    in place — the user shouldn't be staring at a stranded prompt."""

    def _edit_args(self):
        """Return the args list of every bot.edit_message_text call so we can
        inspect what the confirmation was replaced with."""
        return [
            (args, kwargs) for args, kwargs in get_mock_bot().edit_message_text.call_args_list
        ]

    async def test_pressing_yes_edits_confirmation_message_in_place(self):
        await self.start_rc()
        bot = get_mock_bot()

        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))
        # The "end" action already does 1 edit (to show the "Are you sure?" prompt).
        # Reset so we can isolate the edit triggered by endconfirm.
        bot.edit_message_text.reset_mock()

        await self.callback_handler(self.call("btn_endconfirm_1", ADMIN_USER))

        # After endconfirm, there MUST be at least one edit_message_text call —
        # this is what dismisses the "Are you sure?" prompt.
        self.assertGreaterEqual(bot.edit_message_text.call_count, 1,
            "endconfirm must edit the confirmation message in place, not leave it stranded")

        # The edit must remove the inline keyboard (reply_markup=None).
        last_edit = self._edit_args()[-1]
        kwargs = last_edit[1]
        self.assertIn("reply_markup", kwargs)
        self.assertIsNone(kwargs["reply_markup"],
            "the Yes/No keyboard must be removed when the prompt is replaced")

    async def test_pressing_yes_replaces_text_with_finish_list_and_ended_by(self):
        await self.start_rc()
        await self.vote_in(USERS[0])
        await self.vote_out(USERS[1])

        bot = get_mock_bot()
        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))
        bot.edit_message_text.reset_mock()

        await self.callback_handler(self.call("btn_endconfirm_1", ADMIN_USER))

        last_edit_args = bot.edit_message_text.call_args_list[-1]
        text_arg = last_edit_args[0][0] if last_edit_args[0] else last_edit_args[1].get("text", "")
        self.assertIn("Ended by", text_arg,
            "the in-place edit must show 'Ended by <name>' so admins know who ended it")

    async def test_rollcall_with_no_title_shows_friendly_prompt(self):
        """rc.title can be empty (auto-started template fallback) or
        the literal "<Empty>" (/src with no args). Neither should leak
        '' into the prompt text."""
        # Manually create a rollcall with the literal "<Empty>" title — what
        # /src with no args produces (see lifecycle.py:187).
        rc = self.mgr.add_rollcall(CHAT_ID, "<Empty>")
        self.assertEqual(rc.title, "<Empty>")

        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))

        edits = get_mock_bot().edit_message_text.call_args_list
        self.assertGreater(len(edits), 0)
        text_arg = edits[-1][0][0]
        self.assertIn("this rollcall", text_arg,
            "<Empty> title should render as 'this rollcall', not '<Empty>' or ''")
        self.assertNotIn("'<Empty>'", text_arg)
        self.assertNotIn("''", text_arg)


class TestFinishListFailureNotSwallowed(IntegrationBase):
    """If finishList() raises, the user must still get SOMETHING — not
    silence. The old code had try/except: pass which gave the user
    zero feedback when anything went wrong."""

    async def test_finishlist_exception_still_shows_fallback_text(self):
        await self.start_rc()
        rc = self.rc(0)
        bot = get_mock_bot()
        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))
        bot.edit_message_text.reset_mock()

        # Patch the bound finishList so it raises during endconfirm.
        with patch.object(type(rc), "finishList", side_effect=RuntimeError("simulated finishList failure")):
            await self.callback_handler(self.call("btn_endconfirm_1", ADMIN_USER))

        # Either edit_message_text or send_message must have been called with
        # SOMETHING — the user can't be left staring at the stale prompt.
        # edit_message_text(text, chat_id, message_id) — text is args[0]
        # send_message(chat_id, text)                  — text is args[1]
        def _texts():
            for args, _kw in bot.edit_message_text.call_args_list:
                if args:
                    yield str(args[0])
            for args, _kw in bot.send_message.call_args_list:
                if len(args) >= 2:
                    yield str(args[1])

        had_some_output = any("ended by" in t.lower() for t in _texts())
        self.assertTrue(had_some_output,
            "finishList failure must NOT swallow the entire user-visible output — "
            "fallback 'Rollcall #N ended by X.' should appear")


class TestEndConfirmCleansUpRollcall(IntegrationBase):
    """Sanity: regardless of the UI fix, the actual rollcall must end —
    removed from manager, audit log written, etc."""

    async def test_rollcall_actually_removed_after_endconfirm(self):
        await self.start_rc()
        await self.callback_handler(self.call("btn_end_1", ADMIN_USER))
        await self.callback_handler(self.call("btn_endconfirm_1", ADMIN_USER))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0,
            "endconfirm must still remove the rollcall from the manager")
