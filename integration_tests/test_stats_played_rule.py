"""
One definition of "a game played", end to end through the real flows.

Reported 2026-10-19: "🏅 Heramb just played game #10" when the numbers didn't
add up. Causes, each pinned below:
  - cancelled (/xrc) games counted as played and in the denominator
  - a game a member was marked a no-show for counted as played
  - milestones and /stats read a member's own rows only, ignoring guest
    names merged into them, while the leaderboard folded them
  - a games milestone could be announced again after a no-show stepped the
    count back
  - streaks survived games the member skipped (user decision: a streak is
    consecutive group games)
  - /stats group top attendees lost real members' names
"""
import db
from helpers import IntegrationBase, ADMIN_USER, CHAT_ID, USERS
from services import badges, ghost as ghost_svc, stats as stats_svc
from services import identity as identity_svc

U = USERS[0]


class TestPlayedRule(IntegrationBase):

    async def _game(self, *voters, title="Game"):
        await self.start_rc(title)
        for v in voters:
            await self.vote_in(v)
        rc_id = self.rc().id
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        return rc_id

    async def _cancelled_game(self, *voters):
        await self.start_rc("Rained off")
        for v in voters:
            await self.vote_in(v)
        await self.cancel_roll_call(self.msg("/xrc rain", ADMIN_USER))

    def _played(self):
        return stats_svc.personal_stats(CHAT_ID, U["id"])["sessions_attended"]

    async def test_cancelled_game_is_not_played_nor_in_the_denominator(self):
        await self._game(U)
        await self._cancelled_game(U)
        p = stats_svc.personal_stats(CHAT_ID, U["id"])
        self.assertEqual((p["sessions_attended"], p["total_rollcalls_in_chat"]), (1, 1))
        lb = stats_svc.leaderboard(CHAT_ID)["entries"][0]
        self.assertEqual(lb["sessions_attended"], 1)
        self.assertEqual(stats_svc.group_stats(CHAT_ID)["total_rollcalls"], 1)

    async def test_no_show_is_not_played_and_breaks_the_streak(self):
        await self._game(U)
        rc2 = await self._game(U)
        self.assertEqual(self._played(), 2)
        ghost_svc.review_session(CHAT_ID, rc2, {U["id"]})
        self.assertEqual(self._played(), 1)
        self.assertEqual(db.get_user_streaks(CHAT_ID, U["id"]),
                         {"current_streak": 0, "best_streak": 1})
        hist = db.get_user_session_history(CHAT_ID, U["id"])
        self.assertEqual([h["status"] for h in hist], ["ghost", "in"])

    async def test_cancelled_game_does_not_break_a_streak(self):
        await self._game(U)
        await self._cancelled_game()
        await self._game(U)
        self.assertEqual(db.get_user_streaks(CHAT_ID, U["id"])["current_streak"], 2)

    async def test_merged_guest_games_count_everywhere_the_same(self):
        await self.start_rc("Guest game")
        await self.set_in_for(self.msg("/sif Ghosty", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        await self._game(U)
        identity_svc.link_identities(CHAT_ID, "Ghosty", canonical_user_id=U["id"],
                                     admin_user_id=ADMIN_USER["id"], admin_name="Admin")
        lb = {e["user_id"]: e["sessions_attended"] for e in stats_svc.leaderboard(CHAT_ID)["entries"]}
        self.assertEqual(self._played(), 2)
        self.assertEqual(lb[U["id"]], 2)
        self.assertEqual(db.get_user_streaks(CHAT_ID, U["id"])["current_streak"], 2)

    async def test_games_milestone_fires_at_the_real_tenth_game_once(self):
        badges.GAMES_MILESTONES, saved = (3,), badges.GAMES_MILESTONES
        try:
            await self._game(U)
            await self._cancelled_game(U)              # must not count
            await self._game(U)
            self.assertEqual(badges.collect_badges(CHAT_ID, [(U["id"], "U")]), [])
            rc3 = await self._game(U)                 # real 3rd game
            lines = self.bs.bot.send_message.call_args_list
            self.assertTrue(any("game #3" in str(c) for c in lines))
            # A no-show steps the count back; reaching 3 again is not re-announced.
            ghost_svc.review_session(CHAT_ID, rc3, {U["id"]})
            self.assertEqual(self._played(), 2)
            # Clear the absence PENALTY so their next /in isn't held for
            # reconfirmation; the no-show record itself stays.
            db.reset_ghost_count(CHAT_ID, U["id"])
            await self._game(U)
            self.assertEqual(self._played(), 3)
            self.assertFalse([l for l in badges.collect_badges(CHAT_ID, [(U["id"], "U")]) if "game #" in l])
        finally:
            badges.GAMES_MILESTONES = saved

    async def test_group_top_attendees_have_names(self):
        await self._game(U)
        top = stats_svc.group_stats(CHAT_ID)["top_attendees"]
        self.assertTrue(top and all(t["name"] for t in top))

    async def test_summary_dates_use_when_the_game_was_played(self):
        """A game closed days late (bot was down) is dated by when it was played."""
        from unittest.mock import patch
        row = {"id": 1, "title": "Sat", "in_count": 8,
               "finalize_date": "2026-10-10 18:00:00", "ended_at": "2026-10-13 07:00:00",
               "created_at": "2026-10-08 09:00:00"}
        with patch("db.get_rollcalls_between", return_value=[row]), \
             patch("db.get_attendance_between", return_value=[]):
            s = stats_svc.period_summary(CHAT_ID, days=7)["sessions"][0]
            wd = stats_svc.weekday_attendance(CHAT_ID)
        self.assertEqual(s["ended_at"][:10], "2026-10-10")
        self.assertEqual(wd[0]["weekday"], "Saturday")
