"""
Unit tests for _weekly_dues_report() in periodic_jobs.py.
Uses the same patch approach as other periodic job tests.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


CHAT = -9001


def _run(coro):
    return asyncio.run(coro)


def _mk_snapshot():
    return {"text": "📊 Dues Snapshot\n\nAmit owes ₹90\n🏦 Fund balance: ₹500"}


class TestWeeklyDuesReport:

    def _invoke(self, chat_ids, weekday=6, hour=21, week="2026-W28",
                stamp=None, snapshot=None):
        """Run _weekly_dues_report with controllable time and DB state."""
        from periodic_jobs import _weekly_dues_report

        now_mock = MagicMock()
        now_mock.weekday.return_value = weekday
        now_mock.hour = hour
        now_mock.strftime.return_value = week

        if snapshot is None:
            snapshot = _mk_snapshot()

        with patch("db.get_all_chat_ids_with_dues_report", return_value=chat_ids), \
             patch("db.get_system_config", return_value=stamp), \
             patch("db.set_system_config") as mock_set, \
             patch("services.dues.dues_snapshot", return_value=snapshot) as mock_snap, \
             patch("bot_state.send_md_fallback", new_callable=AsyncMock) as mock_send, \
             patch("periodic_jobs._tz_for", return_value=__import__("pytz").utc), \
             patch("periodic_jobs.datetime") as mock_dt:

            mock_dt.now.return_value = now_mock
            _run(_weekly_dues_report())
            return mock_send, mock_snap, mock_set

    def test_posts_when_sunday_evening_and_not_stamped(self):
        mock_send, mock_snap, _ = self._invoke([CHAT], weekday=6, hour=20, stamp=None)
        mock_snap.assert_called_once_with(CHAT)
        mock_send.assert_called_once()

    def test_includes_snapshot_text_in_post(self):
        mock_send, _, _ = self._invoke([CHAT], weekday=6, hour=21, stamp=None)
        text = mock_send.call_args[0][1]
        assert "Dues Snapshot" in text or "Amit" in text

    def test_no_post_when_already_stamped_this_week(self):
        mock_send, _, _ = self._invoke([CHAT], weekday=6, hour=21,
                                       week="2026-W28", stamp="2026-W28")
        mock_send.assert_not_called()

    def test_no_post_on_non_sunday(self):
        mock_send, _, _ = self._invoke([CHAT], weekday=5, hour=21, stamp=None)  # Saturday
        mock_send.assert_not_called()

    def test_no_post_before_2000(self):
        mock_send, _, _ = self._invoke([CHAT], weekday=6, hour=19, stamp=None)
        mock_send.assert_not_called()

    def test_stamps_after_posting(self):
        _, _, mock_set = self._invoke([CHAT], weekday=6, hour=20, stamp=None, week="2026-W29")
        mock_set.assert_called_once_with(f"dues_report:{CHAT}", "2026-W29")

    def test_no_chats_no_crash(self):
        mock_send, _, _ = self._invoke([], weekday=6, hour=21, stamp=None)
        mock_send.assert_not_called()

    def test_multiple_chats_each_posted(self):
        chat_a, chat_b = -9001, -9002
        from periodic_jobs import _weekly_dues_report

        now_mock = MagicMock()
        now_mock.weekday.return_value = 6
        now_mock.hour = 21
        now_mock.strftime.return_value = "2026-W28"

        with patch("db.get_all_chat_ids_with_dues_report", return_value=[chat_a, chat_b]), \
             patch("db.get_system_config", return_value=None), \
             patch("db.set_system_config"), \
             patch("services.dues.dues_snapshot", return_value=_mk_snapshot()), \
             patch("bot_state.send_md_fallback", new_callable=AsyncMock) as mock_send, \
             patch("periodic_jobs._tz_for", return_value=__import__("pytz").utc), \
             patch("periodic_jobs.datetime") as mock_dt:

            mock_dt.now.return_value = now_mock
            _run(_weekly_dues_report())

        assert mock_send.call_count == 2
        posted_chats = {c[0][0] for c in mock_send.call_args_list}
        assert chat_a in posted_chats and chat_b in posted_chats

    def test_snapshot_exception_does_not_crash_sweep(self):
        """One chat failing should not prevent others from posting."""
        from periodic_jobs import _weekly_dues_report

        now_mock = MagicMock()
        now_mock.weekday.return_value = 6
        now_mock.hour = 21
        now_mock.strftime.return_value = "2026-W28"

        with patch("db.get_all_chat_ids_with_dues_report", return_value=[CHAT]), \
             patch("db.get_system_config", return_value=None), \
             patch("db.set_system_config"), \
             patch("services.dues.dues_snapshot", side_effect=RuntimeError("db down")), \
             patch("bot_state.send_md_fallback", new_callable=AsyncMock) as mock_send, \
             patch("periodic_jobs._tz_for", return_value=__import__("pytz").utc), \
             patch("periodic_jobs.datetime") as mock_dt:

            mock_dt.now.return_value = now_mock
            _run(_weekly_dues_report())  # must not raise

        mock_send.assert_not_called()
