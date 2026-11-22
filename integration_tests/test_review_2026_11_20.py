"""
Regressions from the 2026-11-20 full review.

  - "Reply with ..." prompts captured ordinary chat long after they were
    abandoned (the UPI ones forever, and any message with an "@" in it).
  - The weather alert sweep's network calls ran inline in the minute tick
    that also fires scheduled rollcalls.
  - Weather and geocoder rows in system_config were never removed.
  - A team sheet with more than ~100 players could not be posted at all.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import db
from helpers import IntegrationBase, CHAT_ID


class TestPendingReplyPrompts(IntegrationBase):

    def test_expired_prompt_stops_capturing_and_is_dropped(self):
        from bot_state import _PENDING_TTL_SECONDS, _pending_live
        now = datetime.now().timestamp()
        d = {("c", 1): {"_ts": now}, ("c", 2): {"_ts": now - _PENDING_TTL_SECONDS - 5}}
        self.assertTrue(_pending_live(d, ("c", 1)))
        self.assertFalse(_pending_live(d, ("c", 2)))
        self.assertNotIn(("c", 2), d, "an expired prompt must be removed on sight")
        self.assertFalse(_pending_live(d, ("c", 3)))

    def test_upi_prompt_ignores_sentences_that_mention_someone(self):
        from handlers.dues import _looks_like_vpa_reply
        self.assertTrue(_looks_like_vpa_reply("amit@okaxis"))
        self.assertTrue(_looks_like_vpa_reply("  amit@okaxis \n"))
        self.assertFalse(_looks_like_vpa_reply("@ravi you in tomorrow?"))
        self.assertFalse(_looks_like_vpa_reply("pay me at amit@okaxis"))
        self.assertFalse(_looks_like_vpa_reply("no at sign"))

    def test_reply_filters_tolerate_messages_without_text(self):
        """A photo or sticker from someone with an armed prompt has text=None;
        the digit-reply filters called .strip() on it."""
        from types import SimpleNamespace
        from bot_state import _pending_payment_input, _pending_subsidy_input
        from handlers.dues import _is_subsidy_reply
        from handlers.payment_panel import _is_payment_reply
        key = (CHAT_ID, 4242)
        stamp = {"_ts": datetime.now().timestamp(), "rollcall_id": 1}
        _pending_subsidy_input[key] = dict(stamp)
        _pending_payment_input[key] = dict(stamp)
        try:
            def m(text):
                return SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID),
                                       from_user=SimpleNamespace(id=4242), text=text)
            for f in (_is_subsidy_reply, _is_payment_reply):
                self.assertFalse(f(m(None)))
                self.assertTrue(f(m("250")))
        finally:
            _pending_subsidy_input.pop(key, None)
            _pending_payment_input.pop(key, None)


class TestWeatherInTheSchedulerTick(IntegrationBase):

    async def test_a_hanging_forecast_service_costs_one_capped_tick(self):
        import periodic_jobs

        async def hanging():
            await asyncio.Event().wait()

        with patch.object(periodic_jobs, "_weather_alerts", hanging), \
             patch.object(periodic_jobs, "_WEATHER_TICK_BUDGET", 0.05):
            await asyncio.wait_for(periodic_jobs.run_periodic_jobs(), timeout=5)

    async def test_one_lookup_per_tick_the_next_game_on_the_next(self):
        import periodic_jobs
        from services import weather
        await self.start_rc("Game A")
        await self.start_rc("Game B")
        weather.set_alerts(CHAT_ID, True, 1, "Admin")
        import pytz
        for rc in self.mgr.get_rollcalls(CHAT_ID):
            rc.finalizeDate = datetime.now(pytz.timezone(rc.timezone or "Asia/Kolkata")) + timedelta(hours=10)
            rc.location = "12.97,77.59"
            rc.save()
        calls = []

        async def fake(rc):
            calls.append(rc.id)
            return {"ok": False, "text": ""}

        with patch.object(weather, "rollcall_weather", fake):
            await periodic_jobs._weather_alerts()
            self.assertEqual(len(calls), 1)
            await periodic_jobs._weather_alerts()
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(set(calls)), 2, "the second tick must look up the other game")
            await periodic_jobs._weather_alerts()
            self.assertEqual(len(calls), 2, "both stamped — nothing left to look up")


class TestConfigPrune(IntegrationBase):

    def _age(self, key, days):
        ph = "%s" if db.db_type == "postgresql" else "?"
        when = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        with db._cursor(commit=True) as cur:
            cur.execute(f"UPDATE system_config SET updated_at = {ph} WHERE key = {ph}", (when, key))

    def test_old_weather_and_geocoder_rows_go_everything_else_stays(self):
        import periodic_jobs
        for k in ("weather_early:1", "weather_late:1", "weather_warned:1",
                  "weather_last:1", "geo:abc", "weather_last:2", "vote_digest_sent:-1",
                  "weatherXlast:1"):
            db.set_system_config(k, "x")
        for k in ("weather_early:1", "weather_late:1", "weather_warned:1",
                  "weather_last:1", "geo:abc", "vote_digest_sent:-1", "weatherXlast:1"):
            self._age(k, 30)
        db.set_system_config("config_prune_last", "")

        periodic_jobs._prune_expired_config()

        for gone in ("weather_early:1", "weather_late:1", "weather_warned:1",
                     "weather_last:1", "geo:abc"):
            self.assertIsNone(db.get_system_config(gone), gone)
        # Recent, unrelated, and a key the old LIKE-with-underscore would hit.
        for kept in ("weather_last:2", "vote_digest_sent:-1", "weatherXlast:1"):
            self.assertEqual(db.get_system_config(kept), "x", kept)

    def test_runs_once_a_day(self):
        import periodic_jobs
        db.set_system_config("config_prune_last", "")
        periodic_jobs._prune_expired_config()
        db.set_system_config("geo:late", "x")
        self._age("geo:late", 30)
        periodic_jobs._prune_expired_config()
        self.assertEqual(db.get_system_config("geo:late"), "x")


class TestTeamsKeyboardSize(IntegrationBase):

    def _view(self, players):
        per = [players // 2 + players % 2, players // 2]
        teams, n = [], 0
        for no, size in enumerate(per, start=1):
            teams.append({"no": no, "label": "AB"[no - 1], "emoji": "🔴",
                          "players": [{"key": f"u{1000 + n + i}", "name": f"P{n + i}"}
                                      for i in range(size)]})
            n += size
        return {"formed": True, "count": 2, "locked": False, "locked_by": None,
                "published_at": None, "players_in": players, "teams": teams}

    def test_swap_picker_until_the_keyboard_would_be_refused(self):
        from handlers.teams import _sheet_text, pick_screen, teams_keyboard

        class _Rc:
            id = 1
            title = "Game"

        def buttons(markup):
            return list(markup.keyboard)   # the mock keyboard is flat

        small = self._view(20)
        sheet = buttons(teams_keyboard(1, small))
        self.assertLessEqual(len(sheet), 6, "the sheet never lists players")
        self.assertTrue(any(b.callback_data == "tm:sw:1:1" for b in sheet))
        self.assertLessEqual(len(buttons(pick_screen(_Rc(), small, 1)[1])), 100)

        big = self._view(200)   # 100 a team: one team's picker would be refused
        self.assertFalse(any(b.callback_data.startswith("tm:sw:") for b in buttons(teams_keyboard(1, big))))
        self.assertIn("web page", _sheet_text(_Rc(), big))
