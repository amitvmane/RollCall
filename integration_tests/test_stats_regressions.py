"""
Integration regression tests for the /stats calculation bugs fixed on 2026-06-12.

Bugs covered:
- BUG #1: Attendance rate used total_in (vote count) instead of actual
  final-IN count. A user who voted IN then changed to OUT in every rollcall
  showed 100% attendance. The min(100, ...) clamp hid the >100% symptom but
  not the false-positive one.
- BUG #2: Leaderboard ranked by total_in DESC, so users who flip-flopped a
  lot ranked higher than steady attenders.
- BUG #3: current_streak only reset when an admin marked a user as ghost.
  Voting OUT or MAYBE at /erc never reset the streak, so streaks inflated
  between ghost-marks and didn't mean "in a row attended."

Fix lives in:
- rollCall/db.py: new get_user_attendance_count + get_leaderboard_by_attendance
  helpers; reset_streak_on_ghost renamed to reset_user_streak.
- rollCall/handlers/stats.py: build_user_stats_text + build_leaderboard_text
  now use the real attendance helpers.
- rollCall/handlers/lifecycle.py + check_reminders.py: streak reset also
  fires for participants who ended OUT/MAYBE at rollcall end.
"""
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot

import db


class TestAttendanceRateUsesRealAttendance(IntegrationBase):
    """BUG #1: /stats attendance rate must reflect final IN, not vote count."""

    async def test_user_who_flipped_to_out_shows_voted_100_attended_0(self):
        u = USERS[0]
        await self.start_rc()
        # Vote IN then change mind to OUT — total_in bumps but final state is OUT.
        await self.vote_in(u)
        await self.vote_out(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        # total_in is 1 in user_stats, but actual attendance is 0 because the
        # user's final status in the users table is 'out'.
        attended = db.get_user_attendance_count(CHAT_ID, u["id"])
        self.assertEqual(attended, 0,
                         "user ended OUT — must NOT be counted as attended")

        from handlers.stats import build_user_stats_text
        text = await build_user_stats_text(CHAT_ID, u["id"], u["first_name"])
        # User engaged (voted) → 100% voting. User ended OUT → 0% attendance.
        # Two separate metrics, both displayed.
        self.assertIn("Voted in: 1 of 1", text)
        self.assertIn("Attended: 0 of 1", text)
        self.assertIn("(0%)", text)

    async def test_user_who_flipped_in_out_in_shows_100_percent_attended(self):
        u = USERS[0]
        await self.start_rc()
        # IN → OUT → IN — total_in=2 but only one rollcall, final IN once.
        await self.vote_in(u)
        await self.vote_out(u)
        await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        attended = db.get_user_attendance_count(CHAT_ID, u["id"])
        self.assertEqual(attended, 1, "ended IN in 1 rollcall = 1 attended")

        from handlers.stats import build_user_stats_text
        text = await build_user_stats_text(CHAT_ID, u["id"], u["first_name"])
        # Both voting and attendance are 100% — engaged AND attended.
        self.assertIn("Voted in: 1 of 1", text)
        self.assertIn("Attended: 1 of 1", text)
        self.assertIn("(100%)", text)

    async def test_no_show_shows_voted_50_attended_50(self):
        """Engagement and attendance diverge when user skips some sessions."""
        u = USERS[0]
        # 2 rollcalls. User votes IN in only one of them; misses the other entirely.
        await self.start_rc("RC1")
        await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        await self.start_rc("RC2")
        # User does not vote in RC2 — no-show.
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        from handlers.stats import build_user_stats_text
        text = await build_user_stats_text(CHAT_ID, u["id"], u["first_name"])
        # Voted in 1/2, attended 1/2 — both metrics 50%.
        self.assertIn("Voted in: 1 of 2", text)
        self.assertIn("Attended: 1 of 2", text)
        self.assertIn("(50%)", text)


class TestLeaderboardRanksByActualAttendance(IntegrationBase):
    """BUG #2: leaderboard must rank by attended, not by total_in vote count."""

    async def test_steady_attender_ranks_above_flip_flopper(self):
        steady = USERS[0]
        flipper = USERS[1]

        # One rollcall: steady votes IN, flipper votes IN then OUT.
        await self.start_rc()
        await self.vote_in(steady)
        await self.vote_in(flipper)
        await self.vote_out(flipper)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        # By total_in: flipper=1, steady=1 — same. By real attendance:
        # steady=1, flipper=0. New ranking puts steady first.
        rows = db.get_leaderboard_by_attendance(CHAT_ID, limit=10)
        # Steady must come before flipper in the result.
        order = [r["user_id"] for r in rows]
        self.assertIn(steady["id"], order)
        self.assertIn(flipper["id"], order)
        self.assertLess(order.index(steady["id"]), order.index(flipper["id"]),
                        "steady attender must rank above flip-flopper")
        steady_row = next(r for r in rows if r["user_id"] == steady["id"])
        flipper_row = next(r for r in rows if r["user_id"] == flipper["id"])
        self.assertEqual(steady_row["attended"], 1)
        self.assertEqual(flipper_row["attended"], 0)


class TestStreakResetsOnOutOrMaybe(IntegrationBase):
    """BUG #3: voting OUT or MAYBE at /erc resets current_streak.

    Previously only the ghost-mark flow reset streaks, so streaks inflated
    between ghost-marks. After the fix, ending a session as OUT/MAYBE
    counts as breaking the streak. No-shows (never voted) are still NOT
    penalised — those are handled deliberately by ghost-marking.
    """

    async def test_in_then_out_resets_streak(self):
        u = USERS[0]
        # Rollcall 1 — vote IN, end. Streak should be 1.
        await self.start_rc("RC1")
        await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        # Read streak from user_stats — directly via db helper.
        # No public reader, so query manually.
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT current_streak FROM user_stats WHERE chat_id=? AND user_id=?",
                    (CHAT_ID, u["id"]))
        streak_after_rc1 = cur.fetchone()[0]
        cur.close()
        self.assertEqual(streak_after_rc1, 1)

        # Rollcall 2 — vote OUT, end. Streak should drop to 0.
        await self.start_rc("RC2")
        await self.vote_out(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        cur = conn.cursor()
        cur.execute("SELECT current_streak FROM user_stats WHERE chat_id=? AND user_id=?",
                    (CHAT_ID, u["id"]))
        streak_after_rc2 = cur.fetchone()[0]
        cur.close()
        self.assertEqual(streak_after_rc2, 0,
                         "voting OUT at end of rollcall must reset streak")

    async def test_in_then_maybe_resets_streak(self):
        u = USERS[0]
        await self.start_rc("RC1")
        await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        await self.start_rc("RC2")
        await self.vote_maybe(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT current_streak FROM user_stats WHERE chat_id=? AND user_id=?",
                    (CHAT_ID, u["id"]))
        streak = cur.fetchone()[0]
        cur.close()
        self.assertEqual(streak, 0,
                         "voting MAYBE at end of rollcall must reset streak")

    async def test_no_show_does_NOT_reset_streak(self):
        """A user who never voted in a session shouldn't have their streak
        nuked — that's the ghost-marker's job for deliberate no-shows."""
        u = USERS[0]
        # Build streak of 1
        await self.start_rc("RC1")
        await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        # New rollcall, user never votes
        await self.start_rc("RC2")
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("SELECT current_streak FROM user_stats WHERE chat_id=? AND user_id=?",
                    (CHAT_ID, u["id"]))
        streak = cur.fetchone()[0]
        cur.close()
        self.assertEqual(streak, 1,
                         "no-vote (no participation) must NOT reset the streak")
