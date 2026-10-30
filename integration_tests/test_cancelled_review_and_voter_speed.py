"""
Regressions, 2026-10-29:

1. A CANCELLED game was offered for no-show review — in /mark_absent, the
   web "Review no-shows" panel, and the 7-day auto-forgive sweep (which
   treats an unreviewed session as "everyone attended" and forgives one
   absence each, i.e. forgave absences for a game nobody played). Old
   cancelled sessions were affected too, so the fix is in the shared query.
2. A ghost prompt posted before a cancel kept working buttons.
3. History (web turnout chart, /history) showed a cancelled game's IN votes
   as if it had been played.
4. Fastest Voters ranked by AVERAGE, so one slow vote pushed a member who
   voted in 1s out of the top 10; open rollcalls didn't count; and the
   quickest vote wasn't shown anywhere.
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot
from services.rollcalls import cancel_rollcall
from services import stats as stats_svc


def _ph():
    return '%s' if db.db_type == 'postgresql' else '?'


def _exec(sql, params=()):
    with db._cursor(commit=True) as cur:
        cur.execute(sql.replace('?', _ph()), params)


class _Base(IntegrationBase):

    async def _played_session(self, title="Played"):
        await self.start_rc(title)
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    async def _cancelled_session(self, title="Rained off"):
        await self.start_rc(title)
        for u in USERS[:3]:
            await self.vote_in(u)
        rc_db_id = self.rc(0).db_id
        await cancel_rollcall(CHAT_ID, 0, ADMIN_USER["id"], ADMIN_USER["first_name"])
        return rc_db_id


class TestCancelledNotReviewable(_Base):

    async def test_cancelled_session_not_pending_review(self):
        await self._played_session()
        cancelled_id = await self._cancelled_session()
        pending = {int(r["id"]) for r in db.get_unprocessed_rollcalls(CHAT_ID, days=30)}
        self.assertEqual(len(pending), 1, "the played game is still reviewable")
        self.assertNotIn(cancelled_id, pending)

    async def test_mark_absent_says_caught_up_when_only_cancelled(self):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self._cancelled_session()
        get_mock_bot().send_message.reset_mock()
        await self.mark_absent(self.msg("/mark_absent", ADMIN_USER))
        self.assertTrue(any("caught up" in t.lower() for t in self.sent_texts()))

    async def test_old_prompt_ghost_no_does_not_forgive_on_cancelled(self):
        """'No, all showed up' on a cancelled game must not forgive anyone."""
        user = USERS[0]
        db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        before = db.get_ghost_count(CHAT_ID, user["id"])
        cancelled_id = await self._cancelled_session()

        await self.ghost_callback_handler(self.call(f"ghost_no_{cancelled_id}", ADMIN_USER))

        self.assertEqual(db.get_ghost_count(CHAT_ID, user["id"]), before)
        self.assertTrue(any("cancelled" in t.lower() for t in self.edited_texts()))

    async def test_old_prompt_ghost_done_records_nothing_on_cancelled(self):
        user = USERS[1]
        cancelled_id = await self._cancelled_session()
        await self.ghost_callback_handler(self.call(f"ghost_tog_{cancelled_id}_{user['id']}", ADMIN_USER))
        await self.ghost_callback_handler(self.call(f"ghost_done_{cancelled_id}", ADMIN_USER))
        self.assertEqual(db.get_ghost_count(CHAT_ID, user["id"]), 0)


class TestCancelledInHistory(_Base):

    async def test_history_flags_cancelled(self):
        await self._played_session("Played")
        await self._cancelled_session("Rained off")
        by_title = {h["title"]: h for h in stats_svc.history(CHAT_ID)}
        self.assertTrue(by_title["Rained off"]["is_cancelled"])
        self.assertFalse(by_title["Played"]["is_cancelled"])

    async def test_history_command_marks_cancelled(self):
        from handlers.lists import history_command
        await self._cancelled_session("Rained off")
        get_mock_bot().send_message.reset_mock()
        await history_command(self.msg("/history", ADMIN_USER))
        text = "\n".join(self.sent_texts())
        self.assertIn("cancelled", text)
        self.assertNotIn("✅ 3", text, "a cancelled game's IN votes aren't turnout")


class TestFastestVoters(IntegrationBase):
    """Vote times are pinned by rewriting created_at, so these are exact."""

    def _session(self, delays):
        """One rollcall; delays = [(user, seconds to first vote), ...]."""
        rc_id = db.create_rollcall(CHAT_ID, "Game")
        _exec("UPDATE rollcalls SET created_at = ? WHERE id = ?", ("2026-10-01 10:00:00", rc_id))
        for u, secs in delays:
            db.add_or_update_user(rc_id, u["id"], u["first_name"], u.get("username"), "in")
            m, s = divmod(secs, 60)
            h, m = divmod(m, 60)
            _exec("UPDATE users SET created_at = ? WHERE rollcall_id = ? AND user_id = ?",
                  (f"2026-10-01 {10 + h:02d}:{m:02d}:{s:02d}", rc_id, u["id"]))
        return rc_id

    def test_ranked_by_median_not_average(self):
        quick, steady = USERS[0], USERS[1]
        # quick: usually 2s, one overnight vote → average ~1h, median 2s.
        for d in (2, 2, 1, 2, 3 * 3600):
            self._session([(quick, d), (steady, 60)])
        board = stats_svc.response_time_board(CHAT_ID)
        ids = [e["user_id"] for e in board["entries"]]
        self.assertEqual(ids[0], quick["id"])
        self.assertEqual(board["entries"][0]["median_response_seconds"], 2)
        self.assertGreater(board["entries"][0]["avg_response_seconds"], 60)

    def test_record_is_the_single_fastest_vote(self):
        a, b = USERS[0], USERS[1]
        for d in (30, 30, 30):
            self._session([(a, d), (b, 20)])
        self._session([(a, 1), (b, 20)])
        rec = stats_svc.response_time_board(CHAT_ID)["record"]
        self.assertEqual((rec["user_id"], rec["seconds"]), (a["id"], 1))

    def test_one_lucky_vote_does_not_outrank_regulars(self):
        regular, once = USERS[0], USERS[1]
        for _ in range(3):
            self._session([(regular, 10)])
        self._session([(regular, 10), (once, 1)])
        ids = [e["user_id"] for e in stats_svc.response_time_board(CHAT_ID)["entries"]]
        self.assertEqual(ids, [regular["id"]])

    def test_open_rollcall_counts(self):
        u = USERS[0]
        self._session([(u, 5)])   # never ended
        self.assertEqual(stats_svc.response_time_board(CHAT_ID)["record"]["seconds"], 5)

    def test_grouped_by_member_not_name(self):
        twin_a, twin_b = USERS[0], USERS[1]
        for _ in range(3):
            rc = self._session([(twin_a, 5), (twin_b, 50)])
            _exec("UPDATE users SET first_name = 'Abhi' WHERE rollcall_id = ?", (rc,))
        entries = stats_svc.response_time_board(CHAT_ID)["entries"]
        self.assertEqual(len(entries), 2, "two people named Abhi are two rows")
        self.assertEqual({e["display_name"] for e in entries}, {"Abhi"})


class TestVotePeakTimes(IntegrationBase):
    """'When people vote' — busiest weekday/hour, in the game's timezone."""

    def _vote_at(self, rc_id, user, utc):
        db.add_or_update_user(rc_id, user["id"], user["first_name"], user.get("username"), "in")
        _exec("UPDATE users SET created_at = ? WHERE rollcall_id = ? AND user_id = ?",
              (utc, rc_id, user["id"]))

    def _recent(self, days_ago, hh_mm_ss):
        from datetime import datetime, timedelta, timezone
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        return f"{d} {hh_mm_ss}"

    def test_peak_is_in_the_games_timezone(self):
        rc = db.create_rollcall(CHAT_ID, "Game")      # Asia/Kolkata, UTC+5:30
        # 03:40 UTC = 09:10 IST → the 9 AM bucket, not 3 AM.
        for i, u in enumerate(USERS[:3]):
            self._vote_at(rc, u, self._recent(2, f"03:4{i}:00"))
        rc2 = db.create_rollcall(CHAT_ID, "Game 2")
        self._vote_at(rc2, USERS[0], self._recent(2, "14:00:00"))   # 19:30 IST
        peak = stats_svc.voting_peak_times(CHAT_ID)
        self.assertEqual(peak["total_votes"], 4)
        self.assertEqual(peak["busiest_hour"], 9)
        self.assertEqual(peak["busiest_hour_label"], "9 AM")
        self.assertEqual(peak["top_slots"][0]["votes"], 3)
        self.assertEqual(peak["top_slots"][0]["share"], 75)
        self.assertEqual(sum(peak["by_hour"]), 4)

    def test_window_excludes_old_votes(self):
        rc = db.create_rollcall(CHAT_ID, "Old")
        self._vote_at(rc, USERS[0], self._recent(200, "03:40:00"))
        self.assertEqual(stats_svc.voting_peak_times(CHAT_ID)["total_votes"], 0)

    async def test_stats_group_shows_busiest_time(self):
        from handlers.stats import build_group_stats_text
        await self.start_rc("Game")
        for u in USERS[:2]:
            await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        text = await build_group_stats_text(CHAT_ID)
        self.assertIn("When people vote", text)
        self.assertIn("Busiest day", text)


class TestRepairGhostsOnCancelled(_Base):
    """Boot repair: no-shows marked on games later cancelled are undone once."""

    async def test_reverses_once_and_only_cancelled(self):
        ghost = USERS[0]
        played = None
        await self._played_session("Played")
        played = db.get_unprocessed_rollcalls(CHAT_ID, days=30)[0]["id"]
        cancelled = await self._cancelled_session("Rained off")

        # A real no-show on the played game, and one wrongly recorded on the
        # cancelled game (possible before 10.9, when it was still reviewable).
        for rc_id in (played, cancelled):
            db.increment_ghost_count(CHAT_ID, ghost["id"], ghost["first_name"])
            db.add_ghost_event(rc_id, CHAT_ID, ghost["id"], ghost["first_name"])
        self.assertEqual(db.get_ghost_count(CHAT_ID, ghost["id"]), 2)

        done = db.reverse_ghosts_on_cancelled_rollcalls()
        self.assertEqual([e["rollcall_id"] for e in done], [cancelled])
        self.assertEqual(db.get_ghost_count(CHAT_ID, ghost["id"]), 1, "the real no-show stays")

        self.assertEqual(db.reverse_ghosts_on_cancelled_rollcalls(), [], "second boot is a no-op")
        self.assertEqual(db.get_ghost_count(CHAT_ID, ghost["id"]), 1)
