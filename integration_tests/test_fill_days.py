"""
/stats days and the web "Which days fill up" card (services.stats.fill_by_weekday):
which weekday's games reach their IN limit. Real database, real handlers.
"""
import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID
from mock_helpers import get_mock_bot
import services.stats as stats_svc


def _ph():
    return "%s" if db.db_type == "postgresql" else "?"


class TestFillDays(IntegrationBase):

    async def _game(self, players, limit=None, day="2026-11-08", waitlist=0, cancel=False):
        """A finished game on `day` (2026-11-08 is a Sunday)."""
        from handlers.settings import wait_limit
        await self.start_rc("Game")
        if limit:
            await wait_limit(self.msg(f"/sl {limit}", ADMIN_USER))
        for u in USERS[:players + waitlist]:
            await self.vote_in(u)
        rid = self.mgr.get_rollcalls(CHAT_ID)[0].id
        if cancel:
            from handlers.lifecycle import cancel_roll_call
            await cancel_roll_call(self.msg("/cancel_roll_call", ADMIN_USER))
        else:
            await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        with db._cursor(commit=True) as cur:
            cur.execute(f"UPDATE rollcalls SET finalize_date = {_ph()} WHERE id = {_ph()}",
                        (f"{day} 18:00:00", rid))
        return rid

    def _day(self, name, data=None):
        data = data or stats_svc.fill_by_weekday(CHAT_ID)
        return next(w for w in data["weekdays"] if w["weekday"] == name)

    async def test_filled_counts_the_waitlist_and_ignores_cancelled_games(self):
        await self._game(4, limit=4, waitlist=2)                 # Sun: full + 2 waiting
        await self._game(3, limit=4)                             # Sun: one short
        await self._game(4, limit=4, cancel=True)                # Sun: cancelled, not counted
        await self._game(2, limit=4, day="2026-11-11")           # Wed: half
        await self._game(5, day="2026-11-11")                    # Wed: no limit
        data = stats_svc.fill_by_weekday(CHAT_ID)
        sun, wed = self._day("Sunday", data), self._day("Wednesday", data)
        self.assertEqual((sun["games"], sun["limited_games"], sun["filled"], sun["fill_pct"]), (2, 2, 1, 50))
        self.assertEqual(sun["avg_in"], 3.5, "spots taken, capped at the limit — the waitlist isn't a spot")
        self.assertEqual(sun["avg_limit"], 4)
        self.assertEqual((wed["games"], wed["limited_games"], wed["filled"]), (2, 1, 0))
        self.assertEqual(wed["avg_in"], 2, "against the limit, so the no-limit game doesn't blur it")
        self.assertEqual([w["weekday"] for w in data["weekdays"]], ["Wednesday", "Sunday"], "week order")

    async def test_a_late_drop_out_still_counts_towards_filling(self):
        rid = await self._game(3, limit=4)
        with db._cursor(commit=True) as cur:
            cur.execute(f"INSERT INTO late_drops (rollcall_id, chat_id, user_id, user_name, minutes_before) "
                        f"VALUES ({_ph()}, {_ph()}, {_ph()}, {_ph()}, {_ph()})",
                        (rid, CHAT_ID, USERS[8]["id"], "User9", 30))
        self.assertEqual(self._day("Sunday")["filled"], 1)

    async def test_time_to_fill_is_the_last_spot_taken(self):
        rid = await self._game(3, limit=3, waitlist=1)
        ph = _ph()
        with db._cursor(commit=True) as cur:
            cur.execute(f"UPDATE rollcalls SET created_at = {ph} WHERE id = {ph}", ("2026-11-07 08:00:00", rid))
            for uid, at in ((1, "2026-11-07 08:01:00"), (2, "2026-11-07 09:00:00"),
                            (3, "2026-11-07 14:00:00"), (4, "2026-11-07 20:00:00")):
                cur.execute(f"UPDATE users SET created_at = {ph} WHERE rollcall_id = {ph} AND user_id = {ph}",
                            (at, rid, uid))
        self.assertEqual(self._day("Sunday")["median_fill_seconds"], 6 * 3600, "3rd spot at 14:00, opened 08:00")
        self.assertEqual(stats_svc.fill_duration(6 * 3600), "~6h")

    async def test_best_and_worst_need_two_games_with_a_limit(self):
        for _ in range(2):
            await self._game(2, limit=2)                         # Sun 2/2
            await self._game(1, limit=2, day="2026-11-13")       # Fri 0/2
        await self._game(2, limit=2, day="2026-11-11")           # Wed: one game only
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual((data["best"], data["worst"]), ("Sunday", "Friday"))

    async def test_window_leaves_older_games_out(self):
        rid = await self._game(2, limit=2)
        with db._cursor(commit=True) as cur:
            cur.execute(f"UPDATE rollcalls SET ended_at = {_ph()} WHERE id = {_ph()}", ("2020-01-01 00:00:00", rid))
        self.assertEqual(stats_svc.fill_by_weekday(CHAT_ID)["total_games"], 1)
        self.assertEqual(stats_svc.fill_by_weekday(CHAT_ID, days=90)["total_games"], 0)

    async def test_stats_days_command(self):
        from handlers.stats import stats_command
        await self._game(2, limit=2)
        await self._game(2, limit=2)
        get_mock_bot().send_message.reset_mock()
        await stats_command(self.msg("/stats days", USERS[0]))
        text = get_mock_bot().send_message.call_args.args[1]
        self.assertIn("Which days fill up", text)
        self.assertIn("*Sun*  2 games · filled 2/2 (100%) · avg 2/2", text)
        self.assertIn("Sundays fill most reliably", text)
        get_mock_bot().send_message.reset_mock()
        await stats_command(self.msg("/stats days soon", USERS[0]))
        self.assertIn("Usage: /stats days", get_mock_bot().send_message.call_args.args[1])

    async def test_no_limit_anywhere_says_so(self):
        from handlers.stats import _fmt_fill_days
        await self._game(3)
        self.assertIn("No game had a limit", _fmt_fill_days(stats_svc.fill_by_weekday(CHAT_ID)))
        self.assertIn("No completed games yet",
                      _fmt_fill_days({"days": None, "weekdays": [], "best": None, "worst": None,
                                      "total_games": 0, "limited_games": 0}))
