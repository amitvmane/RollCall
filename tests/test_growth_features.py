"""
Tests for the growth-feature batch: achievement badges, period summary,
and the monthly digest window math.
"""
import sys
from datetime import datetime
from unittest.mock import patch

import pytest

sys.path.insert(0, "rollCall")


# ── Badges ────────────────────────────────────────────────────────────────────

class TestCollectBadges:
    def _collect(self, in_members, streaks=None, games=None,
                 proxy_streaks=None, proxy_games=None):
        from services import badges as badges_mod
        streaks = streaks or {}
        games = games or {}
        proxy_streaks = proxy_streaks or {}
        proxy_games = proxy_games or {}
        with patch.object(badges_mod, "get_user_streaks",
                          side_effect=lambda c, u: {"current_streak": streaks.get(u, 0), "best_streak": 0}), \
             patch.object(badges_mod, "get_user_attendance_count",
                          side_effect=lambda c, u: games.get(u, 0)), \
             patch.object(badges_mod, "get_proxy_streaks",
                          side_effect=lambda c, n: {"current_streak": proxy_streaks.get(n, 0), "best_streak": 0}), \
             patch.object(badges_mod, "get_proxy_attendance_count",
                          side_effect=lambda c, n: proxy_games.get(n, 0)):
            return badges_mod.collect_badges(-100, in_members)

    def test_streak_milestone_fires_exactly_at_threshold(self):
        lines = self._collect([(1, "Amit")], streaks={1: 5})
        assert any("5-game streak" in l and "Amit" in l for l in lines)

    def test_no_badge_between_milestones(self):
        assert self._collect([(1, "Amit")], streaks={1: 6}, games={1: 11}) == []

    def test_games_milestone(self):
        lines = self._collect([(1, "Ravi")], games={1: 50})
        assert any("game #50" in l and "Ravi" in l for l in lines)

    def test_both_milestones_same_session(self):
        lines = self._collect([(1, "Amit")], streaks={1: 10}, games={1: 25})
        assert len(lines) == 2

    def test_proxy_uses_proxy_tables(self):
        lines = self._collect([(None, "GuestRaj")], proxy_streaks={"GuestRaj": 5})
        assert any("GuestRaj" in l for l in lines)

    def test_zero_stats_no_badges(self):
        assert self._collect([(1, "Amit"), (None, "Guest")]) == []

    def test_helper_exception_does_not_break_others(self):
        from services import badges as badges_mod

        def _boom(c, u):
            raise RuntimeError("db down")

        with patch.object(badges_mod, "get_user_streaks", side_effect=_boom), \
             patch.object(badges_mod, "get_user_attendance_count", side_effect=_boom), \
             patch.object(badges_mod, "get_proxy_streaks",
                          side_effect=lambda c, n: {"current_streak": 5, "best_streak": 0}), \
             patch.object(badges_mod, "get_proxy_attendance_count", side_effect=lambda c, n: 0):
            lines = badges_mod.collect_badges(-100, [(1, "Broken"), (None, "Fine")])
        assert any("Fine" in l for l in lines)
        assert not any("Broken" in l for l in lines)


# ── Monthly digest window ─────────────────────────────────────────────────────

class TestPrevMonthWindow:
    def _window(self, y, m, d):
        import pytz
        from periodic_jobs import _prev_month_window
        tz = pytz.timezone("Asia/Kolkata")
        return _prev_month_window(tz.localize(datetime(y, m, d, 9, 30)))

    def test_january_rolls_back_to_december(self):
        start, end, label = self._window(2026, 1, 1)
        assert label == "December 2025"
        assert start < end

    def test_mid_year(self):
        start, end, label = self._window(2026, 7, 1)
        assert label == "June 2026"
        # Window boundaries are UTC strings covering all of June IST
        assert start.startswith("2026-05-31") or start.startswith("2026-06-01")
        assert end.startswith("2026-06-30") or end.startswith("2026-07-01")

    def test_label_full_month_name(self):
        _, _, label = self._window(2026, 3, 1)
        assert label == "February 2026"


# ── Period summary ────────────────────────────────────────────────────────────

class TestPeriodSummary:
    def test_empty_period(self):
        from services import stats as stats_svc
        import db as db_mod
        with patch.object(db_mod, "get_rollcalls_between", return_value=[], create=True), \
             patch.object(db_mod, "get_attendance_between", return_value=[], create=True):
            data = stats_svc.period_summary(-100, days=7)
        assert data["session_count"] == 0
        assert data["avg_attendance"] == 0.0
        assert data["top_attendees"] == []

    def test_avg_and_top(self):
        from services import stats as stats_svc
        import db as db_mod
        sessions = [
            {"id": 1, "title": "Sat Game", "ended_at": "2026-07-04 12:00:00", "in_count": 8},
            {"id": 2, "title": "Wed Game", "ended_at": "2026-07-01 12:00:00", "in_count": 6},
        ]
        attendance = [{"name": "Amit", "attended": 2}, {"name": "Ravi", "attended": 1}]
        with patch.object(db_mod, "get_rollcalls_between", return_value=sessions, create=True), \
             patch.object(db_mod, "get_attendance_between", return_value=attendance, create=True):
            data = stats_svc.period_summary(-100, days=7)
        assert data["session_count"] == 2
        assert data["avg_attendance"] == 7.0
        assert data["top_attendees"][0] == {"name": "Amit", "attended": 2}
