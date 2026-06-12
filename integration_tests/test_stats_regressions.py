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


# =========================================================================
# A.7 — Proxy users counted everywhere in /stats
# =========================================================================

class TestProxyCountedInStats(IntegrationBase):
    """Proxies (added via /sif /sof /smf) must show up in personal,
    leaderboard, and group stats — not only in /stats ghost."""

    async def test_proxy_personal_stats_finds_and_renders(self):
        await self.start_rc("RC1")
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        from handlers.stats import build_proxy_stats_text
        text = await build_proxy_stats_text(CHAT_ID, "Alice", "Alice")
        self.assertIn("Alice", text)
        self.assertIn("via /sif", text)
        # 1 of 1 rollcalls, attended once.
        self.assertIn("Voted in: 1 of 1", text)
        self.assertIn("Attended: 1 of 1", text)

    async def test_proxy_leaderboard_inclusion(self):
        await self.start_rc("RC1")
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        rows = db.get_leaderboard_by_attendance(CHAT_ID, limit=10)
        kinds = [r['kind'] for r in rows]
        self.assertIn('real', kinds)
        self.assertIn('proxy', kinds, "proxies must appear in the leaderboard")
        proxy_row = next(r for r in rows if r['kind'] == 'proxy')
        self.assertEqual(proxy_row['display_name'], 'Alice')
        self.assertEqual(proxy_row['attended'], 1)

    async def test_proxy_leaderboard_display_marks_proxies(self):
        await self.start_rc("RC1")
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        from handlers.stats import build_leaderboard_text
        text = await build_leaderboard_text(CHAT_ID)
        self.assertIn("Alice", text)
        self.assertIn("via /sif", text)

    async def test_group_stats_includes_proxies(self):
        await self.start_rc("RC1")
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Bob", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        totals = db.get_group_attendance_totals(CHAT_ID)
        self.assertEqual(totals['total_rollcalls'], 1)
        self.assertEqual(totals['real_attendance_slots'], 2)
        self.assertEqual(totals['proxy_attendance_slots'], 2)
        self.assertEqual(totals['real_participants'], 2)
        self.assertEqual(totals['proxy_participants'], 2)

        from handlers.stats import build_group_stats_text
        text = await build_group_stats_text(CHAT_ID)
        # Total attendance = 4 (2 real + 2 proxy)
        self.assertIn("4", text)
        # The line must distinguish members from proxies
        self.assertIn("Members:", text)
        self.assertIn("Proxies:", text)


# =========================================================================
# A.4 — Proxy resolved by name via /stats <name>
# =========================================================================

class TestProxyResolution(IntegrationBase):
    """resolve_user_for_stats falls through to proxy_users when the name
    doesn't match any real user."""

    async def test_proxy_name_resolves_to_proxy_kind(self):
        await self.start_rc("RC1")
        await self.set_in_for(self.msg("/sif Charlie", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        from handlers.stats import resolve_user_for_stats
        result = await resolve_user_for_stats(CHAT_ID, "Charlie")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "proxy")
        self.assertEqual(result[1], "Charlie")

    async def test_unknown_name_returns_none(self):
        from handlers.stats import resolve_user_for_stats
        result = await resolve_user_for_stats(CHAT_ID, "Nobody")
        self.assertIsNone(result)


# =========================================================================
# A.6 — Ambiguous-name resolution surfaces a hint
# =========================================================================

class TestAmbiguousNameResolution(IntegrationBase):
    """Two real users sharing a first_name in ended rollcalls should be
    flagged as ambiguous rather than silently picking one by recency."""

    async def test_two_users_with_same_first_name_returns_ambiguous(self):
        # Pick two distinct USERS but rename one of them to match the other's first_name.
        # The integration harness's USERS have distinct first_names, so we patch via raw DB.
        u1 = USERS[0]
        u2 = USERS[1]
        await self.start_rc("RC1")
        await self.vote_in(u1)
        await self.vote_in(u2)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        # Rewrite u2's first_name in the users table to match u1.
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute("UPDATE users SET first_name=? WHERE user_id=?", (u1["first_name"], u2["id"]))
        conn.commit()
        cur.close()

        from handlers.stats import resolve_user_for_stats
        result = await resolve_user_for_stats(CHAT_ID, u1["first_name"])
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "ambiguous", "two real users sharing a first_name must be flagged")
        self.assertEqual(result[1], 2)


# =========================================================================
# A.3 — resolve_user_for_stats only searches ENDED rollcalls
# =========================================================================

class TestResolveOnlyEndedRollcalls(IntegrationBase):
    """A user whose only presence is in an in-progress (active) rollcall
    should NOT be findable via /stats <name> — those queries are scoped to
    ended rollcalls so in-progress states don't shadow real history."""

    async def test_user_in_active_rollcall_only_is_not_resolvable(self):
        # Start a rollcall and vote in — do NOT end it.
        await self.start_rc("RC1")
        await self.vote_in(USERS[0])

        from handlers.stats import resolve_user_for_stats
        result = await resolve_user_for_stats(CHAT_ID, USERS[0]["first_name"])
        self.assertIsNone(result,
                          "user only present in active rollcalls must not be resolvable")
