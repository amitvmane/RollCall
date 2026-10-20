"""
Regressions for the 2026-10-20 batch (real DB; only Telegram is mocked).

1. The end-of-game list's "Individual Fee" joined every digit in the fee
   text — "600 + 200" became 600200 — instead of reading it the way dues do.
2. Switching a weekly schedule back on within 2 days after its slot opened
   that stale rollcall immediately (outage catch-up). Also: SQLite stores
   "off" as the text "0", which bool() read as on.
3. Late drops: a member IN who moves OUT/MAYBE close to game time is
   recorded (no penalty) and shown in /stats, the web stats tab and portal.
"""
from datetime import datetime, timedelta

import pytz

import db
from check_reminders import _missed_within_days
from helpers import IntegrationBase, ADMIN_USER, CHAT_ID, USERS
from services import stats as stats_svc
from services.voting import LATE_DROP_HOURS

TZ = pytz.timezone("Asia/Kolkata")  # default chat timezone
U, V = USERS[0], USERS[1]


# ── 1. Individual fee ────────────────────────────────────────────────────────

class TestIndividualFee(IntegrationBase):

    async def _list_with_fee(self, fee):
        await self.start_rc("Paid game")
        await self.vote_in(U)
        await self.vote_in(V)
        rc = self.rc()
        rc.event_fee = fee
        return rc.finishList()

    async def test_reads_the_first_amount_like_dues(self):
        txt = await self._list_with_fee("600 + 200 shuttles")
        self.assertIn("Individual Fee: 300", txt)
        self.assertNotIn("600200", txt)

    async def test_thousands_separator(self):
        self.assertIn("Individual Fee: 600", await self._list_with_fee("₹1,200"))

    async def test_unreadable_fee_shows_no_per_head_figure(self):
        txt = await self._list_with_fee("1.2k")
        self.assertIn("Event Fee: 1.2k", txt)
        self.assertNotIn("Individual Fee", txt)


# ── 2. Re-enabling a schedule ────────────────────────────────────────────────

class TestReenableSchedule(IntegrationBase):

    def _weekly_slot_yesterday(self):
        from services import templates as tsvc
        tsvc.upsert_template(CHAT_ID, "wk", 1, "Admin", title="WK")
        now = datetime.now(TZ)
        slot = now - timedelta(days=1)
        tsvc.set_schedule(CHAT_ID, "wk", 1, "Admin", recurrence_type="weekly",
                          schedule_day=slot.strftime("%A").lower(),
                          schedule_time=slot.strftime("%H:%M"))
        return tsvc, now, slot

    def _would_catch_up(self, now):
        t = db.get_template(CHAT_ID, "wk")
        return _missed_within_days(t["schedule_day"], t["schedule_time"],
                                   t["last_scheduled_date"], now, 2)

    async def test_reenabled_schedule_does_not_fire_a_stale_slot(self):
        tsvc, now, _ = self._weekly_slot_yesterday()
        tsvc.disable_schedule(CHAT_ID, "wk", 1, "Admin")
        # Simulate a schedule last fired long ago (it was off for weeks).
        with db._cursor(commit=True) as cur:
            ph = "%s" if db.db_type == "postgresql" else "?"
            cur.execute(f"UPDATE templates SET last_scheduled_date = {ph} WHERE chatid = {ph} AND name = {ph}",
                        ("2026-01-01", CHAT_ID, "wk"))
        self.assertTrue(self._would_catch_up(now))      # the bug's precondition
        tsvc.enable_schedule(CHAT_ID, "wk", 1, "Admin")
        self.assertTrue(tsvc.get_schedule(CHAT_ID, "wk")["schedule_enabled"])
        self.assertFalse(self._would_catch_up(now))

    async def test_enabling_an_already_on_schedule_keeps_pending_catch_up(self):
        """An outage miss on a schedule nobody turned off is still owed."""
        tsvc, now, _ = self._weekly_slot_yesterday()
        with db._cursor(commit=True) as cur:
            ph = "%s" if db.db_type == "postgresql" else "?"
            cur.execute(f"UPDATE templates SET last_scheduled_date = {ph} WHERE chatid = {ph} AND name = {ph}",
                        ("2026-01-01", CHAT_ID, "wk"))
        tsvc.enable_schedule(CHAT_ID, "wk", 1, "Admin")
        self.assertTrue(self._would_catch_up(now))

    async def test_disabled_text_zero_is_read_as_off(self):
        from services.templates import _schedule_on
        for off in ("0", 0, False, None, "False", "false", ""):
            self.assertFalse(_schedule_on({"schedule_enabled": off}), off)
        for on in ("1", 1, True):
            self.assertTrue(_schedule_on({"schedule_enabled": on}), on)


# ── 3. Late drops ────────────────────────────────────────────────────────────

class TestLateDrops(IntegrationBase):

    async def _rc_at(self, hours_from_now):
        await self.start_rc("Game")
        rc = self.rc()
        if hours_from_now is not None:
            rc.finalizeDate = (datetime.now(TZ) + timedelta(hours=hours_from_now)).replace(tzinfo=None)
            rc.save()
        return rc

    async def _end(self):
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    def _count(self, user=U):
        return stats_svc.personal_stats(CHAT_ID, user["id"])["late_drops"]

    async def test_out_close_to_game_time_counts_once_game_is_played(self):
        await self._rc_at(2)
        await self.vote_in(U)
        await self.vote_out(U)
        self.assertEqual(self._count(), 0)          # game not played yet
        await self._end()
        self.assertEqual(self._count(), 1)
        board = stats_svc.group_stats(CHAT_ID)["late_drop_leaderboard"]
        self.assertEqual(board, [{"name": U["first_name"], "late_drops": 1}])

    async def test_maybe_counts_too(self):
        await self._rc_at(1)
        await self.vote_in(U)
        await self.vote_maybe(U)
        await self._end()
        self.assertEqual(self._count(), 1)

    async def test_after_game_time_still_counts(self):
        await self._rc_at(-1)
        await self.vote_in(U)
        await self.vote_out(U)
        await self._end()
        self.assertEqual(self._count(), 1)

    async def test_early_drop_is_not_late(self):
        await self._rc_at(LATE_DROP_HOURS + 2)
        await self.vote_in(U)
        await self.vote_out(U)
        await self._end()
        self.assertEqual(self._count(), 0)

    async def test_no_game_time_means_nothing_to_measure(self):
        await self._rc_at(None)
        await self.vote_in(U)
        await self.vote_out(U)
        await self._end()
        self.assertEqual(self._count(), 0)

    async def test_out_without_having_been_in_is_not_a_drop(self):
        await self._rc_at(1)
        await self.vote_out(U)
        await self._end()
        self.assertEqual(self._count(), 0)

    async def test_coming_back_in_cancels_it(self):
        await self._rc_at(1)
        await self.vote_in(U)
        await self.vote_out(U)
        await self.vote_in(U)
        await self._end()
        self.assertEqual(self._count(), 0)

    async def test_flip_flopping_is_one_late_drop_per_game(self):
        await self._rc_at(1)
        for _ in range(3):
            await self.vote_in(U)
            await self.vote_out(U)
        await self._end()
        self.assertEqual(self._count(), 1)

    async def test_cancelled_game_does_not_count(self):
        await self._rc_at(1)
        await self.vote_in(U)
        await self.vote_out(U)
        await self.cancel_roll_call(self.msg("/xrc rain", ADMIN_USER))
        self.assertEqual(self._count(), 0)
        self.assertEqual(stats_svc.group_stats(CHAT_ID)["late_drop_leaderboard"], [])

    async def test_shown_in_stats_web_and_portal(self):
        from handlers.stats import build_user_stats_text, _fmt_group_stats
        await self._rc_at(1)
        await self.vote_in(U)
        await self.vote_out(U)
        await self._end()
        self.assertIn("Late drops: 1", await build_user_stats_text(CHAT_ID, U["id"], U["first_name"]))
        self.assertIn("Late drops", _fmt_group_stats(stats_svc.group_stats(CHAT_ID)))
        token = db.get_or_create_chat(CHAT_ID)["group_web_token"]
        web = stats_svc.web_group_stats(token, lookup_user_id=U["id"])
        self.assertEqual(web["personal"]["late_drops"], 1)
        self.assertEqual(web["late_drop_leaderboard"][0]["late_drops"], 1)
        self.assertEqual(web["late_drop_hours"], LATE_DROP_HOURS)
