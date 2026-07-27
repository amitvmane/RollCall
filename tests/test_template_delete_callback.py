"""
Tests for handlers/templates.py::template_delete_callback — the inline
"Delete" button flow added to /templates (previously the only way to
delete a template was the raw /delete_template <name> command; there was
no button in Telegram or either web surface).
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _make_call(data, chat_id=100, user_id=1, message_id=7):
    call = MagicMock()
    call.data = data
    call.id = "cbq1"
    call.from_user.id = user_id
    call.from_user.first_name = "Admin"
    call.message.chat.id = chat_id
    call.message.message_id = message_id
    return call


class TestTemplateDeleteCallback(unittest.IsolatedAsyncioTestCase):

    async def test_ask_shows_confirm_keyboard(self):
        import handlers.templates as tmpl_mod
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = False
        with patch.object(tmpl_mod, "manager", mgr), \
             patch.object(tmpl_mod, "bot") as mock_bot, \
             patch.object(tmpl_mod, "InlineKeyboardButton") as mock_btn:
            mock_bot.answer_callback_query = AsyncMock()
            mock_bot.send_message = AsyncMock()
            await tmpl_mod.template_delete_callback(_make_call("tmpldel_ask_sunday"))
        mock_bot.send_message.assert_awaited_once()
        text = mock_bot.send_message.await_args.args[1]
        self.assertIn("sunday", text)
        callback_datas = [c.kwargs.get("callback_data") for c in mock_btn.call_args_list]
        self.assertIn("tmpldel_yes_sunday", callback_datas)
        self.assertIn("tmpldel_no_sunday", callback_datas)

    async def test_no_cancels(self):
        import handlers.templates as tmpl_mod
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = False
        with patch.object(tmpl_mod, "manager", mgr), \
             patch.object(tmpl_mod, "bot") as mock_bot, \
             patch.object(tmpl_mod, "safe_edit_text", new=AsyncMock()) as edit:
            mock_bot.answer_callback_query = AsyncMock()
            await tmpl_mod.template_delete_callback(_make_call("tmpldel_no_sunday"))
        edit.assert_awaited_once()
        self.assertIn("cancel", edit.await_args.args[2].lower())

    async def test_yes_deletes_and_confirms(self):
        import handlers.templates as tmpl_mod
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = False
        with patch.object(tmpl_mod, "manager", mgr), \
             patch.object(tmpl_mod, "bot") as mock_bot, \
             patch.object(tmpl_mod, "safe_edit_text", new=AsyncMock()) as edit, \
             patch.object(tmpl_mod.templates_svc, "delete_one_template") as mock_delete:
            mock_bot.answer_callback_query = AsyncMock()
            await tmpl_mod.template_delete_callback(_make_call("tmpldel_yes_sunday"))
        mock_delete.assert_called_once_with(100, "sunday", 1, "Admin")
        edit.assert_awaited_once()
        self.assertIn("deleted", edit.await_args.args[2].lower())

    async def test_yes_template_not_found_shows_alert(self):
        import handlers.templates as tmpl_mod
        from exceptions import incorrectParameter
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = False
        with patch.object(tmpl_mod, "manager", mgr), \
             patch.object(tmpl_mod, "bot") as mock_bot, \
             patch.object(tmpl_mod, "safe_edit_text", new=AsyncMock()) as edit, \
             patch.object(tmpl_mod.templates_svc, "delete_one_template",
                          side_effect=incorrectParameter("Template 'ghost' not found.")):
            mock_bot.answer_callback_query = AsyncMock()
            await tmpl_mod.template_delete_callback(_make_call("tmpldel_yes_ghost"))
        edit.assert_not_awaited()
        mock_bot.answer_callback_query.assert_awaited_once()
        self.assertTrue(mock_bot.answer_callback_query.await_args.kwargs.get("show_alert"))

    async def test_restricted_admin_mode_blocks_non_admin(self):
        import handlers.templates as tmpl_mod
        mgr = MagicMock()
        mgr.get_admin_rights.return_value = True
        member = MagicMock()
        member.status = "member"
        with patch.object(tmpl_mod, "manager", mgr), \
             patch.object(tmpl_mod, "bot") as mock_bot, \
             patch.object(tmpl_mod.templates_svc, "delete_one_template") as mock_delete:
            mock_bot.get_chat_member = AsyncMock(return_value=member)
            mock_bot.answer_callback_query = AsyncMock()
            await tmpl_mod.template_delete_callback(_make_call("tmpldel_yes_sunday"))
        mock_delete.assert_not_called()
        mock_bot.answer_callback_query.assert_awaited_once()
        self.assertTrue(mock_bot.answer_callback_query.await_args.kwargs.get("show_alert"))


if __name__ == "__main__":
    unittest.main()
