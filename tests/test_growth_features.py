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
    """collect_badges reads the folded per-person numbers (identity_stats)
    and remembers games milestones so each is announced once."""

    def _collect(self, in_members, streaks=None, games=None, announced=None, stats_fn=None):
        from services import badges as badges_mod
        streaks = streaks or {}
        games = games or {}
        store = dict(announced or {})

        def _stats(chat_id, user_id=None, proxy_name=None):
            k = user_id if user_id is not None else proxy_name
            return {"current_streak": streaks.get(k, 0), "sessions_attended": games.get(k, 0)}

        def _canon(chat_id, user_id=None, proxy_name=None):
            return ({"kind": "user", "user_id": user_id, "proxy_name": None} if user_id is not None
                    else {"kind": "proxy", "user_id": None, "proxy_name": proxy_name})

        with patch("services.identity.identity_stats", side_effect=stats_fn or _stats), \
             patch("services.identity.resolve_canonical", side_effect=_canon), \
             patch.object(badges_mod._db, "get_system_config", side_effect=store.get), \
             patch.object(badges_mod._db, "set_system_config", side_effect=store.__setitem__):
            lines = badges_mod.collect_badges(-100, in_members)
        self.store = store
        return lines

    def test_streak_milestone_fires_exactly_at_threshold(self):
        lines = self._collect([(1, "Amit")], streaks={1: 5})
        assert any("5-game streak" in l and "Amit" in l for l in lines)

    def test_no_badge_between_milestones(self):
        assert self._collect([(1, "Amit")], streaks={1: 6}, games={1: 11}) == []

    def test_games_milestone(self):
        lines = self._collect([(1, "Ravi")], games={1: 50})
        assert any("game #50" in l and "Ravi" in l for l in lines)

    def test_games_milestone_announced_only_once(self):
        """A no-show steps the count back; reaching 50 again must not repeat it."""
        self._collect([(1, "Ravi")], games={1: 50})
        assert self._collect([(1, "Ravi")], games={1: 50}, announced=self.store) == []

    def test_streak_milestone_can_repeat(self):
        self._collect([(1, "Amit")], streaks={1: 5})
        assert self._collect([(1, "Amit")], streaks={1: 5}, announced=self.store)

    def test_both_milestones_same_session(self):
        lines = self._collect([(1, "Amit")], streaks={1: 10}, games={1: 25})
        assert len(lines) == 2

    def test_proxy_member(self):
        lines = self._collect([(None, "GuestRaj")], streaks={"GuestRaj": 5})
        assert any("GuestRaj" in l for l in lines)

    def test_member_and_merged_guest_both_in_count_once(self):
        def _canon_same(chat_id, user_id=None, proxy_name=None):
            return {"kind": "user", "user_id": 1, "proxy_name": None}
        from services import badges as badges_mod
        with patch("services.identity.resolve_canonical", side_effect=_canon_same), \
             patch("services.identity.identity_stats",
                   return_value={"current_streak": 5, "sessions_attended": 0}), \
             patch.object(badges_mod._db, "get_system_config", return_value=None), \
             patch.object(badges_mod._db, "set_system_config"):
            lines = badges_mod.collect_badges(-100, [(1, "Amit"), (None, "Amit guest")])
        assert len(lines) == 1

    def test_zero_stats_no_badges(self):
        assert self._collect([(1, "Amit"), (None, "Guest")]) == []

    def test_helper_exception_does_not_break_others(self):
        def _stats(chat_id, user_id=None, proxy_name=None):
            if user_id is not None:
                raise RuntimeError("db down")
            return {"current_streak": 5, "sessions_attended": 0}
        lines = self._collect([(1, "Broken"), (None, "Fine")], stats_fn=_stats)
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
        assert data["top_attendees"][0] == {"name": "Amit", "attended": 2, "kind": "real"}


# ── Collector rotation ────────────────────────────────────────────────────────

class TestNextRotationCollector:
    def _members(self, *pairs):
        return [{"user_id": uid, "member_name": name} for uid, name in pairs]

    def test_first_pick_no_history(self):
        from services.dues import _next_rotation_collector
        m = self._members((30, "C"), (10, "A"), (20, "B"))
        assert _next_rotation_collector(m, None) == (10, "A")

    def test_advances_past_last(self):
        from services.dues import _next_rotation_collector
        m = self._members((10, "A"), (20, "B"), (30, "C"))
        assert _next_rotation_collector(m, 10) == (20, "B")
        assert _next_rotation_collector(m, 20) == (30, "C")

    def test_wraps_around(self):
        from services.dues import _next_rotation_collector
        m = self._members((10, "A"), (20, "B"))
        assert _next_rotation_collector(m, 20) == (10, "A")
        assert _next_rotation_collector(m, 999) == (10, "A")

    def test_skips_proxies(self):
        from services.dues import _next_rotation_collector
        m = [{"user_id": None, "member_name": "Guest"},
             {"user_id": 20, "member_name": "B"}]
        assert _next_rotation_collector(m, None) == (20, "B")

    def test_all_proxies_returns_none(self):
        from services.dues import _next_rotation_collector
        m = [{"user_id": None, "member_name": "Guest1"},
             {"user_id": None, "member_name": "Guest2"}]
        assert _next_rotation_collector(m, None) is None

    def test_last_collector_absent_from_in_list(self):
        # Last collector didn't play this game — rotation still advances
        from services.dues import _next_rotation_collector
        m = self._members((10, "A"), (30, "C"))
        assert _next_rotation_collector(m, 20) == (30, "C")
