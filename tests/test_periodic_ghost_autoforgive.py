"""
Unit tests for _ghost_auto_forgive() in periodic_jobs.py.

The behaviour under test: a session nobody ever answered the post-/erc
"any ghosts?" prompt for is, after a grace window, treated as "everyone who
was IN attended" — which forgives one absence each.

The property that makes this safe is asserted explicitly below: the sweep can
only ever DECREMENT. A session that was never marked never added a ghost to
anyone, so there is no input to this job that makes it punish someone.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

CHAT = -9101


def _run(coro):
    return asyncio.run(coro)


def _rc(rc_id, days_ago, title="Sunday Game"):
    ended = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"id": rc_id, "title": title, "ended_at": ended.strftime("%Y-%m-%d %H:%M:%S")}


class _Harness:
    """Drives _ghost_auto_forgive with controllable DB state."""

    def __init__(self, unprocessed, tracking=True, stamp=None, in_users=None):
        self.unprocessed = unprocessed
        self.tracking = tracking
        self.stamp = stamp
        self.in_users = in_users if in_users is not None else [
            {"user_id": 1, "first_name": "Alex", "proxy_name": None},
            {"user_id": None, "first_name": None, "proxy_name": "Guest"},
        ]
        self.decremented = []
        self.marked_done = []

    def run(self):
        from periodic_jobs import _ghost_auto_forgive

        def _dec(cid, in_users, selected):
            self.decremented.append((cid, list(in_users), set(selected)))

        with patch("db.get_all_chat_ids", return_value=[CHAT]), \
             patch("db.get_or_create_chat",
                   return_value={"ghost_tracking_enabled": self.tracking}), \
             patch("db.get_unprocessed_rollcalls", return_value=self.unprocessed), \
             patch("db.get_rollcall_in_users", return_value=self.in_users), \
             patch("db.mark_rollcall_absent_done",
                   side_effect=lambda rc: self.marked_done.append(rc)), \
             patch("db.get_system_config", return_value=self.stamp), \
             patch("db.set_system_config"), \
             patch("handlers.ghost._decrement_attended", side_effect=_dec):
            _run(_ghost_auto_forgive())
        return self


class TestGhostAutoForgive:

    def test_forgives_a_session_left_unmarked_past_the_window(self):
        h = _Harness([_rc(41, days_ago=10)]).run()
        assert h.marked_done == [41]
        assert len(h.decremented) == 1
        cid, in_users, selected = h.decremented[0]
        assert cid == CHAT
        assert len(in_users) == 2

    def test_selected_is_always_empty_so_the_sweep_can_only_forgive(self):
        """The core safety property. `selected` is what marks people as
        ghosts; passing the empty set means 'nobody ghosted', so every IN
        member is forgiven and nobody can be punished by this job."""
        h = _Harness([_rc(41, days_ago=10), _rc(42, days_ago=99)]).run()
        # Guard against a vacuous pass: if nothing was swept the loop below
        # asserts nothing at all.
        assert len(h.decremented) == 2
        for _cid, _in_users, selected in h.decremented:
            assert selected == set(), "auto-forgive must never mark anyone a ghost"

    def test_leaves_recent_sessions_alone(self):
        """Admins keep their grace window — a session ended yesterday is
        still theirs to mark."""
        h = _Harness([_rc(41, days_ago=1)]).run()
        assert h.marked_done == []
        assert h.decremented == []

    def test_boundary_just_inside_the_window_is_untouched(self):
        from periodic_jobs import GHOST_AUTOFORGIVE_DAYS
        h = _Harness([_rc(41, days_ago=GHOST_AUTOFORGIVE_DAYS - 1)]).run()
        assert h.marked_done == []

    def test_skips_chats_with_ghost_tracking_disabled(self):
        h = _Harness([_rc(41, days_ago=30)], tracking=False).run()
        assert h.marked_done == []
        assert h.decremented == []

    def test_runs_once_per_day(self):
        """Stamped already today → no work, so a restart loop can't sweep
        the same sessions repeatedly."""
        today = datetime.now().strftime("%Y-%m-%d")
        h = _Harness([_rc(41, days_ago=30)], stamp=today).run()
        assert h.marked_done == []

    def test_marks_processed_so_it_cannot_double_forgive(self):
        h = _Harness([_rc(41, days_ago=10)]).run()
        assert h.marked_done == [41], "session must be marked done or it forgives again tomorrow"

    def test_disabled_when_window_is_zero(self):
        h = _Harness([_rc(41, days_ago=99)])
        with patch("periodic_jobs.GHOST_AUTOFORGIVE_DAYS", 0):
            h.run()
        assert h.marked_done == []

    def test_one_bad_chat_does_not_abort_the_sweep(self):
        """Per-chat try/except — a single broken chat must not stop the rest."""
        from periodic_jobs import _ghost_auto_forgive
        calls = []

        def _chat(cid):
            if cid == -1:
                raise RuntimeError("boom")
            return {"ghost_tracking_enabled": True}

        with patch("db.get_all_chat_ids", return_value=[-1, CHAT]), \
             patch("db.get_or_create_chat", side_effect=_chat), \
             patch("db.get_unprocessed_rollcalls", return_value=[_rc(41, days_ago=10)]), \
             patch("db.get_rollcall_in_users", return_value=[]), \
             patch("db.mark_rollcall_absent_done", side_effect=lambda rc: calls.append(rc)), \
             patch("db.get_system_config", return_value=None), \
             patch("db.set_system_config"), \
             patch("handlers.ghost._decrement_attended"):
            _run(_ghost_auto_forgive())

        assert calls == [41], "healthy chat should still be swept after a broken one"


class TestEndedAtParsing:
    """ended_at is a datetime on Postgres and a string on SQLite."""

    def test_parses_both_backend_shapes_and_bad_input(self):
        from periodic_jobs import _parse_ended_at
        aware = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)

        assert _parse_ended_at(aware) == aware
        # naive datetime (Postgres TIMESTAMP WITHOUT TIME ZONE) → assumed UTC
        assert _parse_ended_at(datetime(2026, 8, 1, 10, 0)).tzinfo is not None
        # SQLite text forms
        assert _parse_ended_at("2026-08-01 10:00:00") is not None
        assert _parse_ended_at("2026-08-01T10:00:00Z") is not None
        # unparseable input must not raise — it just skips that session
        assert _parse_ended_at("not-a-date") is None
        assert _parse_ended_at(None) is None
