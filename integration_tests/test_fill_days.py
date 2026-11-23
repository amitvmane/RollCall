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

    async def _game(self, players, limit=None, day="2026-11-08", waitlist=0, cancel=False, title="Game"):
        """A finished game on `day` (2026-11-08 is a Sunday)."""
        from handlers.settings import wait_limit
        await self.start_rc(title)
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

    def _day(self, name, data=None, limit="any"):
        data = data or stats_svc.fill_by_weekday(CHAT_ID)
        return next(w for w in data["weekdays"]
                    if w["weekday"] == name and (limit == "any" or w["limit"] == limit))

    async def test_filled_counts_the_waitlist_and_ignores_cancelled_games(self):
        await self._game(4, limit=4, waitlist=2)                 # Sun: full + 2 waiting
        await self._game(3, limit=4)                             # Sun: one short
        await self._game(4, limit=4, cancel=True)                # Sun: cancelled, not counted
        for _ in range(2):
            await self._game(2, limit=4, day="2026-11-11")       # Wed: half
            await self._game(5, day="2026-11-11")                # Wed: no limit
        data = stats_svc.fill_by_weekday(CHAT_ID)
        sun = self._day("Sunday", data)
        self.assertEqual((sun["limit"], sun["games"], sun["filled"], sun["fill_pct"]), (4, 2, 1, 50))
        self.assertEqual(sun["avg_in"], 3.5, "spots taken, capped at the limit — the waitlist isn't a spot")
        self.assertEqual(sun["peak_signups"], 6, "peak is demand: the waitlist counts")
        wed4, wed_open = self._day("Wednesday", data, 4), self._day("Wednesday", data, None)
        self.assertEqual((wed4["games"], wed4["filled"], wed4["avg_in"]), (2, 0, 2))
        self.assertEqual((wed_open["games"], wed_open["fill_pct"], wed_open["avg_in"]), (2, None, 5),
                         "the no-limit game is its own row, never blurred into the limited one")
        self.assertEqual([(w["weekday"], w["limit"]) for w in data["weekdays"]],
                         [("Wednesday", 4), ("Wednesday", None), ("Sunday", 4)], "week order, no-limit last")

    async def test_each_limit_is_its_own_row(self):
        """The reported bug: games at limit 2 and limit 8 on the same day were
        averaged into one row ('avg 2.2/7.5'), which answers nothing."""
        for _ in range(5):
            await self._game(2, limit=2)                         # Sun, limit 2: full
            await self._game(4, limit=8)                         # Sun, limit 8: short
        data = stats_svc.fill_by_weekday(CHAT_ID)
        small, big = self._day("Sunday", data, 2), self._day("Sunday", data, 8)
        self.assertEqual((small["games"], small["filled"], small["fill_pct"]), (5, 5, 100))
        self.assertEqual((big["games"], big["filled"], big["fill_pct"], big["avg_in"]), (5, 0, 0, 4))
        self.assertEqual((data["best"], data["best_limit"], data["worst"], data["worst_limit"]),
                         ("Sunday", 2, "Sunday", 8))
        self.assertFalse(data["single_limit"])

    async def test_a_game_with_no_known_day_is_kept_out_of_the_weekday_rows(self):
        """The reported bug: a weekend game whose vote opened on Monday was
        counted as a Monday game (created_at fallback). With no game time and
        no day in the title its day is unknown — guessing (the day voting
        opened, the day it was ended) would put it on the wrong weekday and
        could flip the tip. It is reported apart instead, per limit."""
        for _ in range(5):
            await self._game(2, limit=2)                         # Sun, known
        ph = _ph()
        for players in (2, 1, 2):
            rid = await self._game(players, limit=2)
            with db._cursor(commit=True) as cur:
                cur.execute(f"UPDATE rollcalls SET finalize_date = NULL, created_at = {ph}, ended_at = {ph} "
                            f"WHERE id = {ph}", ("2026-11-02 09:00:00", "2026-11-09 04:00:00", rid))  # Mon, Mon
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual([(w["weekday"], w["games"]) for w in data["weekdays"]], [("Sunday", 5)],
                         "no Monday row: neither when voting opened nor when it was ended dates a game")
        self.assertEqual(len(data["unknown_day"]), 1)
        u = data["unknown_day"][0]
        self.assertEqual((u["limit"], u["games"], u["filled"], u["fill_pct"], u["peak_signups"]), (2, 3, 2, 67, 2))
        self.assertEqual((data["undated_games"], data["total_games"]), (3, 5))
        self.assertEqual((data["best"], data["worst"]), ("Sunday", None), "unknown games never reach the tip")
        self.assertIn("/when", data["unknown_day_hint"])
        from handlers.stats import _fmt_fill_days
        text = _fmt_fill_days(data)
        self.assertIn("❔ *Day unknown* · limit 2 · 3 games · filled 2 (67%)", text)
        self.assertIn("Set one with /when", text)

    async def test_only_day_unknown_games_still_render(self):
        from handlers.stats import _fmt_fill_days
        rid = await self._game(2, limit=2)
        with db._cursor(commit=True) as cur:
            cur.execute(f"UPDATE rollcalls SET finalize_date = NULL WHERE id = {_ph()}", (rid,))
        text = _fmt_fill_days(stats_svc.fill_by_weekday(CHAT_ID))
        self.assertNotIn("No completed games yet", text)
        self.assertNotIn("No game had a limit", text)
        self.assertIn("Day unknown", text)

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

    async def test_best_and_worst_need_five_games_with_a_limit(self):
        for _ in range(5):
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
        for _ in range(5):
            await self._game(2, limit=2)
        get_mock_bot().send_message.reset_mock()
        await stats_command(self.msg("/stats days", USERS[0]))
        text = get_mock_bot().send_message.call_args.args[1]
        self.assertIn("Which game days fill up", text)
        self.assertIn("*Sun* · limit 2 · 5 games · filled 5 (100%) · avg 2 · peak 2", text)
        self.assertIn("Sundays fill most reliably", text)
        self.assertNotIn("at limit", text, "one limit in the group — the tip needn't name it")
        self.assertIn("Day = when the game was played", text)
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

    async def test_a_weekday_in_the_title_dates_a_game_with_no_game_time(self):
        """No game time, but 'Sunday Football' says when it was played."""
        ph = _ph()
        for title in ("Sunday Football", "Sat/Sun kickabout"):
            rid = await self._game(2, limit=2, title=title)
            with db._cursor(commit=True) as cur:
                cur.execute(f"UPDATE rollcalls SET finalize_date = NULL, ended_at = {ph} WHERE id = {ph}",
                            ("2026-11-10 08:00:00", rid))                # ended on a Tuesday
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual([w["weekday"] for w in data["weekdays"]], ["Sunday"],
                         "'Sunday Football' → Sunday, not the Tuesday it was ended")
        self.assertEqual([u["games"] for u in data["unknown_day"]], [1], "'Sat/Sun' names two days → unknown")
        self.assertEqual((data["title_dated_games"], data["undated_games"]), (1, 1))
        self.assertEqual(data["dating_note"], "1 game had no game time set — dated by the day in the title.")

    def test_title_weekday(self):
        cases = {"Sunday Football": 6, "SAT 7am turf": 5, "Weekly game (thurs)": 3, "Tue/Tue": 1,
                 "Sat/Sun": None, "Football": None, "Sunset league": None, "Saturn": None, None: None}
        for title, want in cases.items():
            self.assertEqual(stats_svc.title_weekday(title), want, title)

    async def test_the_tip_ignores_slots_with_too_few_games(self):
        """The reported prod case: Sunday at limit 18 (3 games, 67%) was called
        the most reliable over Sunday at limit 16 (44 games, 57%)."""
        for _ in range(3):
            await self._game(3, limit=3)                         # Sun, limit 3: 3 games, all full
        for players in (2, 2, 2, 1, 1):
            await self._game(players, limit=2)                   # Sun, limit 2: 5 games, 3 full
        for players in (2, 1, 1, 1, 1):
            await self._game(players, limit=2, day="2026-11-14")  # Sat, limit 2: 5 games, 1 full
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual(self._day("Sunday", data, 3)["fill_pct"], 100, "still shown, just not ranked")
        self.assertEqual((data["best"], data["best_limit"], data["worst"], data["worst_limit"]),
                         ("Sunday", 2, "Saturday", 2))

    async def test_one_off_slots_fold_into_one_line(self):
        from handlers.stats import _fmt_fill_days
        for _ in range(2):
            await self._game(2, limit=2)                         # Sun, limit 2: a regular slot
        await self._game(2, limit=5)                             # Sun, limit 5: once
        await self._game(2, limit=2, day="2026-11-10")           # Tue: once
        await self._game(3, day="2026-11-13")                    # Fri, no limit: once
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual([(w["weekday"], w["limit"]) for w in data["weekdays"]], [("Sunday", 2)])
        self.assertEqual((data["one_off_games"], data["one_off_days"], data["total_games"]),
                         (3, ["Tuesday", "Friday", "Sunday"], 5))
        self.assertEqual(data["one_off_note"],
                         "3 one-off games not shown — a day and limit played only once (Tue, Fri, Sun).")
        self.assertIn("➕ 3 one-off games not shown", _fmt_fill_days(data))

    async def test_one_offs_are_shown_when_there_is_nothing_else(self):
        await self._game(2, limit=2)
        await self._game(2, limit=2, day="2026-11-10")
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual((len(data["weekdays"]), data["one_off_games"], data["one_off_note"]), (2, 0, ""),
                         "a new group with only one-offs must not see an empty table")

    async def test_by_day_adds_every_limit_of_a_day_together(self):
        """'On average, which day fills more reliably?' — all of a day's limits
        together, days with fewer than 5 limited games left out."""
        from handlers.stats import _fmt_fill_days
        for players in (2, 2, 2, 1):
            await self._game(players, limit=2)                   # Sun, limit 2: 3/4
        await self._game(3, limit=3)                             # Sun, limit 3: 1/1 (one-off, still Sunday's)
        for players in (2, 1, 1, 1, 1):
            await self._game(players, limit=2, day="2026-11-14")  # Sat: 1/5
        await self._game(5, day="2026-11-14")                    # Sat, no limit: not counted
        for _ in range(4):
            await self._game(2, limit=2, day="2026-11-11")       # Wed: 4 games, under the floor
        data = stats_svc.fill_by_weekday(CHAT_ID)
        self.assertEqual([(d["weekday"], d["filled"], d["games"], d["fill_pct"]) for d in data["by_day"]],
                         [("Sunday", 4, 5, 80), ("Saturday", 1, 5, 20)])
        self.assertEqual(data["by_day_note"], "By day: Sun fills 80% (4/5) · Sat fills 20% (1/5)")
        self.assertIn("📊 By day: Sun fills 80% (4/5)", _fmt_fill_days(data))
