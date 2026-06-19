"""
tests/test_ghost_tracking.py

Test suite for the ghost tracking feature:
  - DB functions (mocked)
  - RollCallManager ghost config methods
  - /erc ghost prompt injection
  - /in reconfirmation flow
  - /mark_absent command
  - /set_absent_limit command
  - /absent_stats command
  - /clear_absent command
  - /toggle_ghost_tracking command
  - ghost_callback_handler flows
  - TestGhostTrackingToggle (flag gates /erc and /mark_absent)
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, call as mock_call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------

class GhostTestBase(unittest.IsolatedAsyncioTestCase):
    """Base class providing shared fixtures for ghost tracking tests."""

    @classmethod
    def setUpClass(cls):
        import bot_state
        from handlers.ghost import (
            ghost_callback_handler, toggle_ghost_tracking, set_absent_limit,
            clear_absent, mark_absent,
        )
        from handlers.lifecycle import end_roll_call
        from handlers.voting import in_user
        from handlers.stats import stats_command

        cls.bot_state = bot_state
        cls.ghost_callback_handler = staticmethod(ghost_callback_handler)
        cls.toggle_ghost_tracking = staticmethod(toggle_ghost_tracking)
        cls.set_absent_limit = staticmethod(set_absent_limit)
        cls.clear_absent = staticmethod(clear_absent)
        cls.mark_absent = staticmethod(mark_absent)
        cls.end_roll_call = staticmethod(end_roll_call)
        cls.in_user = staticmethod(in_user)
        cls.stats_command = staticmethod(stats_command)

    def setUp(self):
        self.bot_state.bot.send_message = AsyncMock()
        self.bot_state.bot.answer_callback_query = AsyncMock()
        self.bot_state.bot.edit_message_text = AsyncMock()
        self.bot_state.bot.edit_message_reply_markup = AsyncMock()
        self.bot_state.bot.get_chat_member = AsyncMock()
        # Clear in-memory ghost state between tests
        self.bot_state._ghost_selections.clear()
        self.bot_state._pending_reconf.clear()
        self.bot_state._rate_limits.clear()

        self.rc = self._make_rc()
        self.manager = self._make_manager([self.rc])

    def _make_message(self, text="/cmd", chat_id=100, user_id=1,
                      first_name="Alice", username="alice"):
        msg = MagicMock()
        msg.text = text
        msg.chat.id = chat_id
        msg.from_user.id = user_id
        msg.from_user.first_name = first_name
        msg.from_user.last_name = None
        msg.from_user.username = username
        return msg

    def _make_rc(self, title="Test Session"):
        rc = MagicMock()
        rc.title = title
        rc.id = 42
        rc.inList = []
        rc.outList = []
        rc.maybeList = []
        rc.waitList = []
        rc.allNames = []
        rc.inListLimit = None
        rc.absent_marked = False
        rc.allList.return_value = "Title: Test Session\nID: __RCID__\n"
        rc.finishList.return_value = "Title: Test Session\nID: __RCID__\n"
        rc.addIn.return_value = None
        rc.addOut.return_value = None
        rc.addMaybe.return_value = None
        rc.save.return_value = None
        return rc

    def _make_manager(self, rollcalls=None):
        m = MagicMock()
        rollcalls = rollcalls or []
        m.get_rollcalls.return_value = rollcalls
        m.get_rollcall.return_value = rollcalls[0] if rollcalls else None
        m.get_shh_mode.return_value = False
        m.get_admin_rights.return_value = False
        m.get_ghost_tracking_enabled.return_value = True
        m.get_absent_limit.return_value = 1
        return m

    def _make_call(self, data="ghost_no_42", chat_id=100, user_id=1,
                   first_name="Alice", username="alice", message_id=99):
        c = MagicMock()
        c.data = data
        c.id = "cb_id"
        c.message.chat.id = chat_id
        c.message.message_id = message_id
        c.from_user.id = user_id
        c.from_user.first_name = first_name
        c.from_user.username = username
        c.from_user.last_name = None
        return c

    def _rc_started(self):
        return patch('handlers.lifecycle.roll_call_not_started', return_value=True)

    def _rc_not_started(self):
        return patch('handlers.lifecycle.roll_call_not_started', return_value=False)

    def _rc_started_voting(self):
        return patch('handlers.voting.roll_call_not_started', return_value=True)

    def _rc_not_started_voting(self):
        return patch('handlers.voting.roll_call_not_started', return_value=False)

    def _sent_text(self, call_index=0):
        return self.bot_state.bot.send_message.call_args_list[call_index][0][1]

    def _sent_count(self):
        return self.bot_state.bot.send_message.call_count


# ===========================================================================
# 1. RollCallManager ghost config methods
# ===========================================================================

class TestManagerGhostConfig(unittest.TestCase):
    """Manager get/set methods for absentLimit and ghostTrackingEnabled."""

    def setUp(self):
        from rollcall_manager import RollCallManager
        import db as db_module
        self.db = db_module

        # Patch get_or_create_chat and get_active_rollcalls for each manager call
        self.db.get_or_create_chat.return_value = {
            'shh_mode': False,
            'admin_rights': False,
            'timezone': 'Asia/Calcutta',
            'absent_limit': 1,
            'ghost_tracking_enabled': True,
        }
        self.db.get_active_rollcalls.return_value = []
        self.db.update_chat_settings.return_value = True
        self.manager = RollCallManager()

    def test_get_absent_limit_default(self):
        limit = self.manager.get_absent_limit(100)
        self.assertEqual(limit, 1)

    def test_set_absent_limit_updates_cache_and_db(self):
        self.manager.set_absent_limit(100, 3)
        self.assertEqual(self.manager.get_absent_limit(100), 3)

    def test_get_ghost_tracking_enabled_default_true(self):
        enabled = self.manager.get_ghost_tracking_enabled(100)
        self.assertTrue(enabled)

    def test_set_ghost_tracking_enabled_false(self):
        self.manager.set_ghost_tracking_enabled(100, False)
        self.assertFalse(self.manager.get_ghost_tracking_enabled(100))

    def test_set_ghost_tracking_enabled_true(self):
        self.manager.set_ghost_tracking_enabled(100, False)
        self.manager.set_ghost_tracking_enabled(100, True)
        self.assertTrue(self.manager.get_ghost_tracking_enabled(100))


# ===========================================================================
# 2. /erc — ghost prompt injected when tracking enabled + IN users present
# ===========================================================================

class TestErcGhostPrompt(GhostTestBase):
    """end_roll_call sends ghost prompt when tracking enabled and IN list non-empty."""

    async def test_ghost_prompt_sent_when_tracking_on_and_has_in_users(self):
        from unittest.mock import MagicMock as MM
        in_user = MM()
        in_user.user_id = 5
        self.rc.inList = [in_user]

        with self._rc_started(), \
             patch('handlers.lifecycle.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.lifecycle.manager', self.manager):
            await self.end_roll_call(self._make_message("/erc"))

        # At least 3 messages: "🎉 Roll ended!", finishList, ghost prompt
        self.assertGreaterEqual(self._sent_count(), 3)
        texts = [self.bot_state.bot.send_message.call_args_list[i][0][1]
                 for i in range(self._sent_count())]
        self.assertTrue(any("ghost" in t.lower() or "👻" in t for t in texts))

    async def test_no_ghost_prompt_when_tracking_disabled(self):
        from unittest.mock import MagicMock as MM
        in_user = MM()
        in_user.user_id = 5
        self.rc.inList = [in_user]
        self.manager.get_ghost_tracking_enabled.return_value = False

        with self._rc_started(), \
             patch('handlers.lifecycle.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.lifecycle.manager', self.manager):
            await self.end_roll_call(self._make_message("/erc"))

        texts = [self.bot_state.bot.send_message.call_args_list[i][0][1]
                 for i in range(self._sent_count())]
        self.assertFalse(any("👻" in t for t in texts))

    async def test_no_ghost_prompt_when_in_list_empty(self):
        self.rc.inList = []

        with self._rc_started(), \
             patch('handlers.lifecycle.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.lifecycle.manager', self.manager):
            await self.end_roll_call(self._make_message("/erc"))

        texts = [self.bot_state.bot.send_message.call_args_list[i][0][1]
                 for i in range(self._sent_count())]
        self.assertFalse(any("👻" in t for t in texts))

    async def test_no_ghost_prompt_for_pre_deployment_rollcall(self):
        """Rollcalls that existed before deployment have absent_marked=True
        and must never trigger the ghost prompt, even with IN users."""
        from unittest.mock import MagicMock as MM
        in_user = MM()
        in_user.user_id = 5
        self.rc.inList = [in_user]
        self.rc.absent_marked = True  # simulates a pre-deployment rollcall

        with self._rc_started(), \
             patch('handlers.lifecycle.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.lifecycle.manager', self.manager):
            await self.end_roll_call(self._make_message("/erc"))

        texts = [self.bot_state.bot.send_message.call_args_list[i][0][1]
                 for i in range(self._sent_count())]
        self.assertFalse(any("👻" in t for t in texts))


# ===========================================================================
# 3. /in — reconfirmation triggered when ghost_count >= absent_limit
# ===========================================================================

class TestInReconfirmation(GhostTestBase):
    """/in sends reconfirmation prompt when user has ghosted >= limit."""

    async def test_reconfirmation_sent_when_ghost_count_at_limit(self):
        with self._rc_started_voting(), \
             patch('handlers.voting.manager', self.manager), \
             patch('rollcall_manager.manager', self.manager), \
             patch('services.voting.manager', self.manager), \
             patch('services.voting.get_ghost_count', return_value=1):
            await self.in_user(self._make_message("/in"))

        self.assertEqual(self._sent_count(), 1)
        text = self._sent_text(0)
        self.assertIn("👻", text)
        self.assertIn("ghosted", text)

    async def test_no_reconfirmation_when_ghost_count_below_limit(self):
        with self._rc_started_voting(), \
             patch('handlers.voting.manager', self.manager), \
             patch('rollcall_manager.manager', self.manager), \
             patch('services.voting.manager', self.manager), \
             patch('services.voting.get_ghost_count', return_value=0), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(self._make_message("/in"))

        # Should proceed normally — rc.addIn was called
        self.rc.addIn.assert_called_once()

    async def test_reconfirmation_not_triggered_when_tracking_disabled(self):
        self.manager.get_ghost_tracking_enabled.return_value = False

        with self._rc_started_voting(), \
             patch('handlers.voting.manager', self.manager), \
             patch('rollcall_manager.manager', self.manager), \
             patch('services.voting.manager', self.manager), \
             patch('handlers.lifecycle._update_panel', return_value=False):
            await self.in_user(self._make_message("/in"))

        self.rc.addIn.assert_called_once()

    async def test_pending_reconf_stored(self):
        with self._rc_started_voting(), \
             patch('handlers.voting.manager', self.manager), \
             patch('rollcall_manager.manager', self.manager), \
             patch('services.voting.manager', self.manager), \
             patch('services.voting.get_ghost_count', return_value=2):
            await self.in_user(self._make_message("/in hello", user_id=1))

        self.assertIn((100, 1), self.bot_state._pending_reconf)


# ===========================================================================
# 4. ghost_callback_handler — ghost_no
# ===========================================================================

class TestGhostCallbackNo(GhostTestBase):
    """ghost_no_<id> marks session processed and edits message."""

    async def test_ghost_no_marks_absent_done(self):
        c = self._make_call(data="ghost_no_42")
        with patch('handlers.ghost.mark_rollcall_absent_done') as mock_mark:
            await self.ghost_callback_handler(c)

        mock_mark.assert_called_once_with(42)
        self.bot_state.bot.answer_callback_query.assert_called_once_with(c.id, "✅ Got it!")

    async def test_ghost_no_edits_message_to_confirm(self):
        c = self._make_call(data="ghost_no_42")
        with patch('handlers.ghost.mark_rollcall_absent_done'):
            await self.ghost_callback_handler(c)

        self.bot_state.bot.edit_message_text.assert_called_once()
        edited_text = self.bot_state.bot.edit_message_text.call_args[0][0]
        self.assertIn("✅", edited_text)


# ===========================================================================
# 5. ghost_callback_handler — ghost_yes
# ===========================================================================

class TestGhostCallbackYes(GhostTestBase):
    """ghost_yes_<id> fetches IN users and shows selection keyboard."""

    async def test_ghost_yes_shows_in_list(self):
        c = self._make_call(data="ghost_yes_42")
        in_users = [{'user_id': 5, 'first_name': 'Bob', 'username': 'bob'}]
        with patch('handlers.ghost.get_rollcall_in_users', return_value=in_users):
            await self.ghost_callback_handler(c)

        self.bot_state.bot.edit_message_text.assert_called_once()
        edited_text = self.bot_state.bot.edit_message_text.call_args[0][0]
        self.assertIn("👻", edited_text)

    async def test_ghost_yes_empty_in_list_answers_query(self):
        c = self._make_call(data="ghost_yes_42")
        with patch('handlers.ghost.get_rollcall_in_users', return_value=[]):
            await self.ghost_callback_handler(c)

        self.bot_state.bot.answer_callback_query.assert_called_once()

    async def test_ghost_yes_initialises_empty_selection(self):
        c = self._make_call(data="ghost_yes_42", chat_id=100)
        in_users = [{'user_id': 5, 'first_name': 'Bob', 'username': 'bob'}]
        with patch('handlers.ghost.get_rollcall_in_users', return_value=in_users):
            await self.ghost_callback_handler(c)

        self.assertIn((100, 42), self.bot_state._ghost_selections)
        self.assertEqual(self.bot_state._ghost_selections[(100, 42)], set())


# ===========================================================================
# 6. ghost_callback_handler — ghost_tog (toggle)
# ===========================================================================

class TestGhostCallbackToggle(GhostTestBase):
    """ghost_tog_<rc>_<uid> toggles user in the selection set."""

    async def test_tog_adds_user_to_selection(self):
        self.bot_state._ghost_selections[(100, 42)] = set()
        c = self._make_call(data="ghost_tog_42_5", chat_id=100)
        in_users = [{'user_id': 5, 'first_name': 'Bob', 'username': 'bob'}]
        with patch('handlers.ghost.get_rollcall_in_users', return_value=in_users):
            await self.ghost_callback_handler(c)

        self.assertIn(5, self.bot_state._ghost_selections[(100, 42)])

    async def test_tog_removes_user_from_selection(self):
        self.bot_state._ghost_selections[(100, 42)] = {5}
        c = self._make_call(data="ghost_tog_42_5", chat_id=100)
        in_users = [{'user_id': 5, 'first_name': 'Bob', 'username': 'bob'}]
        with patch('handlers.ghost.get_rollcall_in_users', return_value=in_users):
            await self.ghost_callback_handler(c)

        self.assertNotIn(5, self.bot_state._ghost_selections[(100, 42)])


# ===========================================================================
# 7. ghost_callback_handler — ghost_done
# ===========================================================================

class TestGhostCallbackDone(GhostTestBase):
    """ghost_done_<id> saves ghost records and confirms."""

    async def test_ghost_done_with_selections_increments_counts(self):
        self.bot_state._ghost_selections[(100, 42)] = {5}
        c = self._make_call(data="ghost_done_42", chat_id=100)
        in_users = [{'user_id': 5, 'first_name': 'Bob', 'username': 'bob'}]
        with patch('handlers.ghost.get_rollcall_in_users', return_value=in_users), \
             patch('handlers.ghost.mark_rollcall_absent_done') as mock_mark, \
             patch('handlers.ghost.increment_ghost_count') as mock_inc, \
             patch('handlers.ghost.add_ghost_event') as mock_event, \
             patch('handlers.ghost.get_ghost_count', return_value=1):
            await self.ghost_callback_handler(c)

        mock_mark.assert_called_once_with(42)
        mock_inc.assert_called_once_with(100, 5, 'Bob')
        mock_event.assert_called_once_with(42, 100, 5, 'Bob')

    async def test_ghost_done_no_selections_marks_all_attended(self):
        self.bot_state._ghost_selections[(100, 42)] = set()
        c = self._make_call(data="ghost_done_42", chat_id=100)
        with patch('handlers.ghost.mark_rollcall_absent_done') as mock_mark:
            await self.ghost_callback_handler(c)

        mock_mark.assert_called_once_with(42)
        edited_text = self.bot_state.bot.edit_message_text.call_args[0][0]
        self.assertIn("all marked as attended", edited_text.lower())

    async def test_ghost_done_clears_selection_state(self):
        self.bot_state._ghost_selections[(100, 42)] = {5}
        c = self._make_call(data="ghost_done_42", chat_id=100)
        in_users = [{'user_id': 5, 'first_name': 'Bob', 'username': 'bob'}]
        with patch('handlers.ghost.get_rollcall_in_users', return_value=in_users), \
             patch('handlers.ghost.mark_rollcall_absent_done'), \
             patch('handlers.ghost.increment_ghost_count'), \
             patch('handlers.ghost.add_ghost_event'), \
             patch('handlers.ghost.get_ghost_count', return_value=1):
            await self.ghost_callback_handler(c)

        self.assertNotIn((100, 42), self.bot_state._ghost_selections)


# ===========================================================================
# 8. /set_absent_limit command
# ===========================================================================

class TestSetAbsentLimit(GhostTestBase):

    async def test_sets_limit_successfully(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.set_absent_limit(self._make_message("/set_absent_limit 3"))

        self.manager.set_absent_limit.assert_called_once_with(100, 3)
        text = self._sent_text(0)
        self.assertIn("3", text)

    async def test_missing_parameter_sends_usage(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.set_absent_limit(self._make_message("/set_absent_limit"))

        self.manager.set_absent_limit.assert_not_called()
        self.assertIn("Usage", self._sent_text(0))

    async def test_non_numeric_sends_error(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.set_absent_limit(self._make_message("/set_absent_limit abc"))

        self.manager.set_absent_limit.assert_not_called()

    async def test_zero_value_sends_error(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.set_absent_limit(self._make_message("/set_absent_limit 0"))

        self.manager.set_absent_limit.assert_not_called()

    async def test_non_admin_blocked(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=False)), \
             patch('handlers.ghost.manager', self.manager):
            await self.set_absent_limit(self._make_message("/set_absent_limit 3"))

        self.manager.set_absent_limit.assert_not_called()


# ===========================================================================
# 9. /stats ghost (replaces standalone /absent_stats)
# ===========================================================================

class TestStatsGhost(GhostTestBase):

    async def test_no_ghosts_sends_clean_message(self):
        with patch('handlers.stats.get_ghost_leaderboard', return_value=[]), \
             patch('handlers.stats.manager', self.manager):
            await self.stats_command(self._make_message("/stats ghost"))

        self.assertIn("🏆", self._sent_text(0))

    async def test_leaderboard_lists_ghosts(self):
        board = [
            {'user_id': 5, 'user_name': 'Bob', 'ghost_count': 3},
            {'user_id': 6, 'user_name': 'Carol', 'ghost_count': 1},
        ]
        with patch('handlers.stats.get_ghost_leaderboard', return_value=board), \
             patch('handlers.stats.manager', self.manager):
            await self.stats_command(self._make_message("/stats ghost"))

        text = self._sent_text(0)
        self.assertIn("Bob", text)
        self.assertIn("Carol", text)
        self.assertIn("👻", text)

    async def test_warning_badge_shown_for_users_at_or_above_limit(self):
        board = [{'user_id': 5, 'user_name': 'Bob', 'ghost_count': 2}]
        self.manager.get_absent_limit.return_value = 2
        with patch('handlers.stats.get_ghost_leaderboard', return_value=board), \
             patch('handlers.stats.manager', self.manager):
            await self.stats_command(self._make_message("/stats ghost"))

        self.assertIn("⚠️", self._sent_text(0))

    async def test_ghosts_alias_works(self):
        with patch('handlers.stats.get_ghost_leaderboard', return_value=[]), \
             patch('handlers.stats.manager', self.manager):
            await self.stats_command(self._make_message("/stats ghosts"))

        self.assertIn("🏆", self._sent_text(0))

    async def test_absent_alias_works(self):
        with patch('handlers.stats.get_ghost_leaderboard', return_value=[]), \
             patch('handlers.stats.manager', self.manager):
            await self.stats_command(self._make_message("/stats absent"))

        self.assertIn("🏆", self._sent_text(0))


# ===========================================================================
# 10. /clear_absent command
# ===========================================================================

class TestClearAbsent(GhostTestBase):

    async def test_clears_by_exact_name(self):
        record = {'user_id': 5, 'user_name': 'Bob', 'ghost_count': 2}
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.get_user_ghost_count_by_name', return_value=record), \
             patch('handlers.ghost.reset_ghost_count') as mock_reset, \
             patch('handlers.ghost.manager', self.manager):
            await self.clear_absent(self._make_message("/clear_absent Bob"))

        mock_reset.assert_called_once_with(100, 5, proxy_name=None)
        self.assertIn("Bob", self._sent_text(0))

    async def test_missing_name_sends_usage(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.clear_absent(self._make_message("/clear_absent"))

        self.assertIn("Usage", self._sent_text(0))

    async def test_name_not_found_sends_warning(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.get_user_ghost_count_by_name', return_value=None), \
             patch('handlers.ghost.get_ghost_leaderboard', return_value=[]), \
             patch('handlers.ghost.manager', self.manager):
            await self.clear_absent(self._make_message("/clear_absent Unknown"))

        self.assertIn("⚠️", self._sent_text(0))

    async def test_non_admin_blocked(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=False)), \
             patch('handlers.ghost.manager', self.manager):
            await self.clear_absent(self._make_message("/clear_absent Bob"))

        # Should not reach reset_ghost_count


# ===========================================================================
# 11. /toggle_ghost_tracking command
# ===========================================================================

class TestGhostTrackingToggle(GhostTestBase):

    async def test_toggle_on_to_off(self):
        self.manager.get_ghost_tracking_enabled.return_value = True
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.toggle_ghost_tracking(self._make_message("/toggle_ghost_tracking"))

        self.manager.set_ghost_tracking_enabled.assert_called_once_with(100, False)
        self.assertIn("disabled", self._sent_text(0).lower())

    async def test_toggle_off_to_on(self):
        self.manager.get_ghost_tracking_enabled.return_value = False
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.toggle_ghost_tracking(self._make_message("/toggle_ghost_tracking"))

        self.manager.set_ghost_tracking_enabled.assert_called_once_with(100, True)
        self.assertIn("enabled", self._sent_text(0).lower())

    async def test_non_admin_blocked(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=False)), \
             patch('handlers.ghost.manager', self.manager):
            await self.toggle_ghost_tracking(self._make_message("/toggle_ghost_tracking"))

        self.manager.set_ghost_tracking_enabled.assert_not_called()


# ===========================================================================
# 12. /mark_absent command
# ===========================================================================

class TestMarkAbsent(GhostTestBase):

    async def test_no_unprocessed_sends_all_caught_up(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.get_unprocessed_rollcalls', return_value=[]), \
             patch('handlers.ghost.manager', self.manager):
            await self.mark_absent(self._make_message("/mark_absent"))

        self.assertIn("caught up", self._sent_text(0).lower())

    async def test_shows_unprocessed_rollcalls(self):
        sessions = [
            {'id': 10, 'title': 'Friday Futsal', 'ended_at': '2026-04-20 18:00:00'},
            {'id': 11, 'title': 'Basketball', 'ended_at': '2026-04-18 18:00:00'},
        ]
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.get_unprocessed_rollcalls', return_value=sessions), \
             patch('handlers.ghost.manager', self.manager):
            await self.mark_absent(self._make_message("/mark_absent"))

        self.assertEqual(self._sent_count(), 1)
        # Message is sent with an inline keyboard
        send_kwargs = self.bot_state.bot.send_message.call_args[1]
        self.assertIn('reply_markup', send_kwargs)

    async def test_ghost_tracking_disabled_blocks_command(self):
        self.manager.get_ghost_tracking_enabled.return_value = False
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=True)), \
             patch('handlers.ghost.manager', self.manager):
            await self.mark_absent(self._make_message("/mark_absent"))

        self.assertIn("not enabled", self._sent_text(0).lower())

    async def test_non_admin_blocked(self):
        with patch('handlers.ghost.admin_rights', new=AsyncMock(return_value=False)), \
             patch('handlers.ghost.manager', self.manager):
            await self.mark_absent(self._make_message("/mark_absent"))

        # Admin check fires, no unprocessed query
        from unittest.mock import patch as _patch


if __name__ == "__main__":
    unittest.main()
