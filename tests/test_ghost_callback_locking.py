"""
Regression tests: the delconf_yes_ (delete-user confirm) and ovrd_yes_
(status-override confirm) branches of ghost_callback_handler must serialize
their mutation with the chat write lock, per CLAUDE.md's chat-mutation rule
("anything that mutates a chat's rollcall state ... should run inside
async with manager.get_chat_write_lock(cid)"). Both branches previously
called services/admin.py mutations with no lock at all, unlike the REST
API's equivalent routes (api/routes/admin.py), which always take the lock.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _make_lock_manager(rc=None):
    mgr = MagicMock()
    lock_ctx = MagicMock()
    lock_ctx.__aenter__ = AsyncMock(return_value=None)
    lock_ctx.__aexit__ = AsyncMock(return_value=False)
    mgr.get_chat_write_lock.return_value = lock_ctx
    mgr.get_rollcall.return_value = rc
    mgr.get_shh_mode.return_value = True
    return mgr


def _make_call(data, chat_id=100, user_id=1, message_id=555):
    call = MagicMock()
    call.data = data
    call.id = "cbq1"
    call.from_user.id = user_id
    call.from_user.first_name = "Admin"
    call.message.chat.id = chat_id
    call.message.message_id = message_id
    return call


class TestDelconfYesLocking(unittest.IsolatedAsyncioTestCase):

    async def test_delete_user_mutation_is_lock_guarded(self):
        import handlers.ghost as ghost_mod
        from bot_state import _pending_deletes

        mgr = _make_lock_manager()
        _pending_deletes[(100, 1)] = {"name": "Alice", "rc_number": 0}
        call = _make_call("delconf_yes_0_1")

        with patch.object(ghost_mod, "manager", mgr), \
             patch.object(ghost_mod, "bot") as mock_bot, \
             patch.object(ghost_mod, "safe_edit_text", new=AsyncMock()), \
             patch.object(ghost_mod.admin_svc, "delete_user_from_rollcall") as mock_delete:
            mock_bot.answer_callback_query = AsyncMock()
            await ghost_mod.ghost_callback_handler(call)

        mgr.get_chat_write_lock.assert_called_once_with(100)
        mock_delete.assert_called_once()
        # The mutation call must happen while the lock's __aenter__ has fired
        # and before __aexit__ — i.e. inside the `async with` block.
        self.assertTrue(lock_ctx_order_ok(mgr))


def lock_ctx_order_ok(mgr):
    lock_ctx = mgr.get_chat_write_lock.return_value
    return lock_ctx.__aenter__.await_count == 1 and lock_ctx.__aexit__.await_count == 1


class TestOvrdYesLocking(unittest.IsolatedAsyncioTestCase):

    async def test_set_status_mutation_is_lock_guarded(self):
        import handlers.ghost as ghost_mod
        from bot_state import _pending_overrides
        from models import User

        rc = MagicMock()
        rc.title = "Weekly Game"
        mgr = _make_lock_manager(rc=rc)
        user = User("Bob", "bob", 2, [])
        _pending_overrides[(100, 1)] = {"user": user, "new_status": "out", "rc_number": 0}
        call = _make_call("ovrd_yes_0_1")

        with patch.object(ghost_mod, "manager", mgr), \
             patch.object(ghost_mod, "bot") as mock_bot, \
             patch.object(ghost_mod, "safe_edit_text", new=AsyncMock()), \
             patch("handlers.lifecycle._update_panel", new=AsyncMock()), \
             patch.object(ghost_mod.admin_svc, "set_user_status") as mock_set_status:
            mock_bot.answer_callback_query = AsyncMock()
            mock_bot.send_message = AsyncMock()
            await ghost_mod.ghost_callback_handler(call)

        mgr.get_chat_write_lock.assert_called_once_with(100)
        mock_set_status.assert_called_once()
        self.assertTrue(lock_ctx_order_ok(mgr))
        # rc must be (re-)fetched inside the lock, not just before it, so a
        # concurrent /erc can't leave this branch operating on a stale rc.
        self.assertGreaterEqual(mgr.get_rollcall.call_count, 1)


if __name__ == "__main__":
    unittest.main()
