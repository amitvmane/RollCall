"""
Unit tests for the reminder-DM settings commands (handlers/lists.py) and
the stats CSV export (services/stats.py).
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class TestReminderCommands(unittest.IsolatedAsyncioTestCase):

    def _msg(self, text, uid=1, first_name="Admin"):
        m = MagicMock()
        m.text = text
        m.chat.id = -100
        m.from_user.id = uid
        m.from_user.first_name = first_name
        return m

    async def test_before_close_no_args_shows_off_status(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch("db.get_or_create_chat", return_value={"reminder_before_close_hours": 0}), \
             patch.object(lc.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await lc.remind_before_close_command(self._msg("/remind_before_close"))
        text = mock_send.call_args.args[1]
        self.assertIn("OFF", text)

    async def test_before_close_no_args_shows_on_status(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch("db.get_or_create_chat", return_value={"reminder_before_close_hours": 3}), \
             patch.object(lc.bot, "send_message", new_callable=AsyncMock) as mock_send:
            await lc.remind_before_close_command(self._msg("/remind_before_close"))
        text = mock_send.call_args.args[1]
        self.assertIn("ON", text)
        self.assertIn("3h", text)

    async def test_before_close_valid_hours_updates_setting(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch("db.update_chat_settings") as mock_update, \
             patch("db.log_admin_action"), \
             patch.object(lc.bot, "send_message", new_callable=AsyncMock):
            await lc.remind_before_close_command(self._msg("/remind_before_close 5"))
        mock_update.assert_called_once_with(-100, reminder_before_close_hours=5)

    async def test_before_close_off_clears_setting(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch("db.update_chat_settings") as mock_update, \
             patch.object(lc.bot, "send_message", new_callable=AsyncMock):
            await lc.remind_before_close_command(self._msg("/remind_before_close off"))
        mock_update.assert_called_once_with(-100, reminder_before_close_hours=0)

    async def test_after_open_valid_hours_updates_the_right_field(self):
        """Regression: before-close and after-open must never cross-write
        each other's column."""
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch("db.update_chat_settings") as mock_update, \
             patch("db.log_admin_action"), \
             patch.object(lc.bot, "send_message", new_callable=AsyncMock):
            await lc.remind_after_open_command(self._msg("/remind_after_open 6"))
        mock_update.assert_called_once_with(-100, reminder_after_open_hours=6)

    async def test_non_numeric_hours_rejected(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch.object(lc.bot, "send_message", new_callable=AsyncMock) as mock_send, \
             patch("handlers.lists.reply_error", new_callable=AsyncMock) as mock_reply_error:
            await lc.remind_before_close_command(self._msg("/remind_before_close abc"))
        mock_reply_error.assert_called_once()

    async def test_hours_out_of_range_rejected(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=True)), \
             patch("handlers.lists.reply_error", new_callable=AsyncMock) as mock_reply_error:
            await lc.remind_before_close_command(self._msg("/remind_before_close 200"))
        mock_reply_error.assert_called_once()

    async def test_non_admin_rejected(self):
        import handlers.lists as lc
        with patch("handlers.lists.admin_rights", new=AsyncMock(return_value=False)), \
             patch("handlers.lists.reply_error", new_callable=AsyncMock) as mock_reply_error:
            await lc.remind_before_close_command(self._msg("/remind_before_close 3"))
        mock_reply_error.assert_called_once()


class TestExportStatsCsv(unittest.TestCase):

    def _leaderboard(self):
        return {
            "total_rollcalls_in_chat": 4,
            "entries": [
                {
                    "rank": 1, "display_name": "Alice", "username": "alice",
                    "user_id": 111, "kind": "real",
                    "sessions_attended": 4, "total_sessions_voted": 4,
                    "attendance_rate": 100.0, "voting_rate": 100.0,
                },
                {
                    "rank": 2, "display_name": "GhostProxy", "username": None,
                    "user_id": None, "kind": "proxy",
                    "sessions_attended": 2, "total_sessions_voted": 3,
                    "attendance_rate": 50.0, "voting_rate": 75.0,
                },
            ],
        }

    def test_export_includes_real_and_proxy_rows_with_ghost_and_streaks(self):
        import services.stats as stats_svc
        with patch.object(stats_svc, "leaderboard", return_value=self._leaderboard()), \
             patch.object(stats_svc, "get_ghost_count", return_value=1) as mock_gc, \
             patch.object(stats_svc, "get_ghost_count_by_proxy_name", return_value=2) as mock_gcp, \
             patch.object(stats_svc, "get_user_streaks", return_value={"current_streak": 3, "best_streak": 4}), \
             patch.object(stats_svc, "get_proxy_streaks", return_value={"current_streak": 0, "best_streak": 1}):
            csv_str = stats_svc.export_stats_csv(-100)

        lines = csv_str.strip().splitlines()
        self.assertEqual(
            lines[0],
            "rank,name,username,user_id,kind,sessions_attended,total_sessions_voted,"
            "attendance_rate,voting_rate,ghost_count,current_streak,best_streak",
        )
        self.assertIn("Alice", lines[1])
        self.assertIn("111", lines[1])
        self.assertIn("real", lines[1])
        self.assertIn("3,4", lines[1])  # current_streak,best_streak

        self.assertIn("GhostProxy", lines[2])
        self.assertIn("proxy", lines[2])
        self.assertIn("0,1", lines[2])

        mock_gc.assert_called_once_with(-100, 111)
        mock_gcp.assert_called_once_with(-100, "GhostProxy")

    def test_export_empty_group_produces_header_only(self):
        import services.stats as stats_svc
        with patch.object(stats_svc, "leaderboard", return_value={"total_rollcalls_in_chat": 0, "entries": []}):
            csv_str = stats_svc.export_stats_csv(-100)
        lines = csv_str.strip().splitlines()
        self.assertEqual(len(lines), 1)  # header only


if __name__ == "__main__":
    unittest.main()
