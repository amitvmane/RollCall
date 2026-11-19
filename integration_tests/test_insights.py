"""
Integration tests for bundled vote notices, weather alerts, group health,
recap and inline sharing (handlers/insights.py + their services).
Real SQLite, real handlers, mocked bot API; forecast services faked at the
one network function (services.weather._fetch_json).
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytz
from fastapi.testclient import TestClient

import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID
from mock_helpers import get_mock_bot


def _ins():
    import handlers.insights as ins
    return ins


# ── Fake forecast services ───────────────────────────────────────────────────

def _fake_fetch(rain_models=("ecmwf_ifs025", "gfs_seamless"), met_mm=0.0,
                geocode=True, calls=None):
    """A _fetch_json stand-in: Open-Meteo with chosen models rainy at every
    hour, MET Norway at met_mm, and a geocoder that finds (or doesn't)."""
    async def fetch(url, params):
        if calls is not None:
            calls.append(url)
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = [now + timedelta(hours=h) for h in range(0, 24 * 7)]
        if "geocoding" in url:
            return {"results": [{"name": "Indiranagar", "admin1": "Karnataka",
                                 "latitude": 12.97, "longitude": 77.64}]} if geocode else {}
        if "open-meteo" in url:
            hourly = {"time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours]}
            for m in ("ecmwf_ifs025", "gfs_seamless", "icon_seamless"):
                hourly[f"precipitation_{m}"] = [2.0 if m in rain_models else 0.0] * len(hours)
            hourly["precipitation_probability_best_match"] = [70] * len(hours)
            hourly["temperature_2m_best_match"] = [24.0] * len(hours)
            return {"hourly": hourly}
        if "met.no" in url:
            return {"properties": {"timeseries": [
                {"time": h.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "data": {"next_1_hours": {"details": {"precipitation_amount": met_mm}}}}
                for h in hours]}}
        return None
    return fetch


class _Base(IntegrationBase):

    def setUp(self):
        super().setUp()
        from services import weather
        weather._forecast_cache.clear()

    def _rc(self):
        return self.mgr.get_rollcalls(CHAT_ID)[0]

    def _set_game(self, hours_ahead=10, location="Indiranagar, Bengaluru"):
        rc = self._rc()
        tz = pytz.timezone(rc.timezone or "Asia/Kolkata")
        rc.finalizeDate = datetime.now(tz) + timedelta(hours=hours_ahead)
        rc.location = location
        rc.save()
        return rc


# ── Bundled vote notices ─────────────────────────────────────────────────────

class TestVoteDigest(_Base):

    async def test_bundled_is_quiet_and_summarises_only_changes(self):
        from services import vote_digest as vd
        await self.start_rc("Sunday Game")
        await self.vote_in(USERS[0])
        await _ins().vote_digest_cmd(self.msg("/vote_digest 6", ADMIN_USER))
        self.assertEqual(vd.mode(CHAT_ID), {"mode": "bundled", "hours": 6})
        self.assertTrue(self.mgr.get_shh_mode(CHAT_ID), "bundled must be silent between summaries")

        # Not due yet: nothing, however much moves.
        await self.vote_in(USERS[1])
        self.assertIsNone(vd.due_digest(CHAT_ID))

        later = datetime.now(timezone.utc) + timedelta(hours=7)
        text = vd.due_digest(CHAT_ID, now=later)
        self.assertIn("Sunday Game — 2 IN", text)
        self.assertIn("User2 → IN", text)
        self.assertNotIn("User1", text, "unchanged since the last snapshot")

        # Nothing moved in the next interval: no post.
        self.assertIsNone(vd.due_digest(CHAT_ID, now=later + timedelta(hours=7)))

    async def test_moves_and_removals_say_where_from(self):
        from services import vote_digest as vd
        await self.start_rc("Game")
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        vd.set_mode(CHAT_ID, "bundled", 1, ADMIN_USER["id"], "Admin")
        await self.vote_out(USERS[0])
        rc = self._rc()
        rc.inList = [u for u in rc.inList if u.user_id != USERS[1]["id"]]
        text = vd.build_digest(CHAT_ID, json.loads(db.get_system_config(f"vote_digest_snap:{CHAT_ID}")),
                               vd._snapshot(CHAT_ID))
        self.assertIn("User1: IN → OUT", text)
        self.assertIn("User2 removed (was IN)", text)

    async def test_shh_and_louder_end_bundling(self):
        from services import vote_digest as vd
        await _ins().vote_digest_cmd(self.msg("/vote_digest 3", ADMIN_USER))
        await self.shh(self.msg("/shh", ADMIN_USER))
        self.assertEqual(vd.mode(CHAT_ID)["mode"], "silent")
        await _ins().vote_digest_cmd(self.msg("/vote_digest 3", ADMIN_USER))
        await self.louder(self.msg("/louder", ADMIN_USER))
        self.assertEqual(vd.mode(CHAT_ID)["mode"], "each")

    async def test_off_and_bad_hours(self):
        from services import vote_digest as vd
        await _ins().vote_digest_cmd(self.msg("/vote_digest 99", ADMIN_USER))
        self.assertEqual(vd.mode(CHAT_ID)["mode"], "each", "out-of-range hours change nothing")
        await _ins().vote_digest_cmd(self.msg("/vote_digest 2", ADMIN_USER))
        await _ins().vote_digest_cmd(self.msg("/vote_digest off", ADMIN_USER))
        self.assertEqual(vd.mode(CHAT_ID), {"mode": "each", "hours": 0})
        self.assertFalse(self.mgr.get_shh_mode(CHAT_ID))

    async def test_non_admin_cannot_change_in_admin_locked_group(self):
        from services import vote_digest as vd
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        await _ins().vote_digest_cmd(self.msg("/vote_digest 6", USERS[0]))
        self.assertEqual(vd.mode(CHAT_ID)["mode"], "each")

    async def test_periodic_job_posts_due_summary(self):
        from services import vote_digest as vd
        import periodic_jobs
        await self.start_rc("Game")
        vd.set_mode(CHAT_ID, "bundled", 1, ADMIN_USER["id"], "Admin")
        await self.vote_in(USERS[2])
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
        db.set_system_config(f"vote_digest_sent:{CHAT_ID}", past)
        get_mock_bot().send_message.reset_mock()
        await periodic_jobs._vote_digests()
        self.assertTrue(any("User3 → IN" in t for t in self.sent_texts()), self.sent_texts())


# ── Teammates + recap ────────────────────────────────────────────────────────

class TestRecap(_Base):

    async def _play(self, *users):
        await self.start_rc("G")
        for u in users:
            await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    async def test_teammates_count_shared_games_by_identity(self):
        twin = {**USERS[3], "first_name": "User2"}  # same first name as USERS[1]
        await self._play(USERS[0], USERS[1], USERS[2])
        await self._play(USERS[0], USERS[1])
        await self._play(USERS[0], twin)
        mates = db.get_top_teammates(CHAT_ID, USERS[0]["id"], limit=5)
        self.assertEqual(mates[0], {"name": "User2", "games": 2})
        # The namesake is a different person: a separate entry, not a 3.
        self.assertEqual(sorted(m["games"] for m in mates if m["name"] == "User2"), [1, 2])
        self.assertIn({"name": "User3", "games": 1}, mates)

    async def test_recap_matches_stats_and_renders(self):
        from services import stats as stats_svc
        await self._play(USERS[0], USERS[1])
        await self._play(USERS[1])
        r = stats_svc.recap(CHAT_ID, USERS[0]["id"], "User1")
        personal = stats_svc.personal_stats(CHAT_ID, USERS[0]["id"])
        self.assertEqual(r["games_played"], personal["sessions_attended"])
        self.assertEqual(r["form"], ["in", "miss"])
        text = stats_svc.recap_text(r)
        self.assertIn("Games played: 1 of 2", text)
        self.assertIn("Played most with: User2 (1)", text)

    async def test_recap_command_in_group(self):
        await self._play(USERS[0])
        get_mock_bot().send_message.reset_mock()
        await _ins().recap_cmd(self.msg("/recap", USERS[0]))
        self.assertTrue(any("recap" in t and "Games played" in t for t in self.sent_texts()))


# ── Group health ─────────────────────────────────────────────────────────────

class TestGroupHealth(_Base):

    def test_empty_group_has_nothing_scheduled(self):
        from services import group_health
        r = group_health.check(CHAT_ID)
        self.assertEqual(r["warnings"], 1)
        self.assertIn("nothing scheduled", r["items"][0]["text"])

    async def test_open_game_missing_time_and_location(self):
        from services import group_health
        await self.start_rc("Sunday")
        texts = [i["text"] for i in group_health.check(CHAT_ID)["items"]]
        self.assertIn("'Sunday' has no game time", texts)
        self.assertIn("'Sunday' has no location", texts)

    async def test_dues_without_upi_and_big_balances(self):
        from services import group_health
        db.get_or_create_chat(CHAT_ID)
        db.update_chat_settings(CHAT_ID, dues_enabled=1)
        db.add_dues_entry(CHAT_ID, None, USERS[0]["id"], "User1", "share", 800, None, ADMIN_USER["id"], "Admin")
        texts = " | ".join(i["text"] for i in group_health.check(CHAT_ID)["items"])
        self.assertIn("No UPI ID set", texts)
        self.assertIn("1 member(s) owe more than ₹500", texts)

    async def test_command_is_admin_only_when_locked(self):
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        get_mock_bot().send_message.reset_mock()
        await _ins().group_health_cmd(self.msg("/group_health", USERS[0]))
        self.assertFalse(any("Group health" in t for t in self.sent_texts()))


# ── Weather ──────────────────────────────────────────────────────────────────

class TestWeather(_Base):

    def test_coordinates_in_text_are_used_without_lookup(self):
        from services import weather
        calls = []
        with patch("services.weather._fetch_json", _fake_fetch(calls=calls)):
            place = asyncio.run(weather.resolve_location("Turf 3 https://maps.example/@12.9716,77.5946,17z"))
        self.assertEqual((place["lat"], place["lon"]), (12.9716, 77.5946))
        self.assertEqual(calls, [])

    def test_place_lookup_is_cached(self):
        from services import weather
        calls = []
        with patch("services.weather._fetch_json", _fake_fetch(calls=calls)):
            # system_config outlives reset_db, so a fixed name would hit the
            # previous run's cache entry.
            import uuid
            place = f"Indiranagar {uuid.uuid4().hex[:8]}"
            a = asyncio.run(weather.resolve_location(place))
            b = asyncio.run(weather.resolve_location(place))
        self.assertEqual(a, b)
        self.assertEqual(len(calls), 1)

    async def test_agreement_counts_independent_models(self):
        await self.start_rc("Morning Game")
        rc = self._set_game(hours_ahead=10)
        with patch("services.weather._fetch_json", _fake_fetch(rain_models=("ecmwf_ifs025", "gfs_seamless"), met_mm=1.0)):
            from services import weather
            r = await weather.rollcall_weather(rc)
        self.assertTrue(r["ok"])
        self.assertEqual((r["result"]["rainy"], r["result"]["total"]), (3, 4))
        self.assertIn("3 of 4 forecasts expect rain", r["text"])
        self.assertIn("chance 70%", r["text"])

    async def test_missing_location_or_time_explains(self):
        from services import weather
        await self.start_rc("G")
        r = await weather.rollcall_weather(self._rc())
        self.assertFalse(r["ok"])
        self.assertIn("/location", r["text"])

    async def test_services_down_is_a_message_not_an_error(self):
        from services import weather
        await self.start_rc("G")
        rc = self._set_game(location="12.97,77.59")

        async def down(url, params):
            return None
        with patch("services.weather._fetch_json", down):
            r = await weather.rollcall_weather(rc)
        self.assertFalse(r["ok"])
        self.assertIn("aren't answering", r["text"])

    async def test_alerts_subcommand_toggles(self):
        from services import weather
        await _ins().weather_cmd(self.msg("/weather alerts on", ADMIN_USER))
        self.assertTrue(weather.alerts_enabled(CHAT_ID))
        await _ins().weather_cmd(self.msg("/weather alerts off", ADMIN_USER))
        self.assertFalse(weather.alerts_enabled(CHAT_ID))

    async def test_alert_job_warns_early_and_gives_all_clear_late(self):
        import periodic_jobs
        from services import weather
        await self.start_rc("Evening Game")
        weather.set_alerts(CHAT_ID, True, ADMIN_USER["id"], "Admin")

        rc = self._set_game(hours_ahead=12)
        get_mock_bot().send_message.reset_mock()
        with patch("services.weather._fetch_json", _fake_fetch(rain_models=("ecmwf_ifs025",))):
            await periodic_jobs._weather_alerts()
            await periodic_jobs._weather_alerts()  # stamped: no second post
        posts = [t for t in self.sent_texts() if "forecasts" in t]
        self.assertEqual(len(posts), 1, posts)
        self.assertIn("1 of 4 forecasts expect rain", posts[0])
        self.assertIn("1 of 4", weather.last_for(rc.id))

        self._set_game(hours_ahead=2)
        get_mock_bot().send_message.reset_mock()
        with patch("services.weather._fetch_json", _fake_fetch(rain_models=())):
            await periodic_jobs._weather_alerts()
        self.assertTrue(any("No rain expected" in t for t in self.sent_texts()), self.sent_texts())

    async def test_alert_job_stays_quiet_when_dry(self):
        import periodic_jobs
        from services import weather
        await self.start_rc("G")
        weather.set_alerts(CHAT_ID, True, ADMIN_USER["id"], "Admin")
        self._set_game(hours_ahead=12)
        get_mock_bot().send_message.reset_mock()
        with patch("services.weather._fetch_json", _fake_fetch(rain_models=())):
            await periodic_jobs._weather_alerts()
        self.assertFalse(any("forecast" in t.lower() for t in self.sent_texts()))


# ── Inline share ─────────────────────────────────────────────────────────────

class TestInlineShare(_Base):

    async def test_only_groups_the_user_still_belongs_to(self):
        await self.start_rc("Open Game")
        await self.vote_in(USERS[0])
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        await self.start_rc("Next Game")
        db.upsert_chat_member(CHAT_ID, USERS[0]["id"], "User1")
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.test"}):
            games = _ins().inline_games(USERS[0]["id"])
        self.assertEqual([g["title"] for g in games], ["Next Game"])
        self.assertTrue(games[0]["url"].startswith("https://rc.test/web/group/"))
        self.assertIn("✅ 0 in", games[0]["text"])

        # A stranger with no history sees nothing.
        self.assertEqual(_ins().inline_games(987654), [])

        # Left the group: their old groups' games are no longer offered.
        db.mark_member_inactive(CHAT_ID, USERS[0]["id"])
        self.assertEqual(_ins().inline_games(USERS[0]["id"]), [])


# ── Web routes ───────────────────────────────────────────────────────────────

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


class TestWebRoutes(_Base):

    def setUp(self):
        super().setUp()
        from api.main import app
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.client = TestClient(app)
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        self.token = db.get_or_create_chat(CHAT_ID).get("group_web_token")
        if not self.token:
            from services.web import get_group_web_token
            self.token = get_group_web_token(CHAT_ID)

    def _hdr(self, uid):
        from api.identity import issue_identity_token
        return {"X-Identity-Token": issue_identity_token(uid)}

    def test_health_needs_web_admin(self):
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        r = self.client.get(f"/api/v1/web/group/{self.token}/health", headers=self._hdr(USERS[0]["id"]))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/web/group/{self.token}/health").status_code, 401)

    def test_health_for_admin(self):
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        r = self.client.get(f"/api/v1/web/group/{self.token}/health", headers=self._hdr(ADMIN_USER["id"]))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("items", r.json())

    def test_recap_is_your_own(self):
        self.assertEqual(self.client.get(f"/api/v1/web/group/{self.token}/recap").status_code, 401)
        r = self.client.get(f"/api/v1/web/group/{self.token}/recap", headers=self._hdr(USERS[0]["id"]))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["games_played"], 0)

    async def test_weather_only_for_this_groups_rollcall(self):
        await self.start_rc("G")
        rc = self._set_game(location="12.97,77.59")
        with patch("services.weather._fetch_json", _fake_fetch()):
            ok = self.client.get(f"/api/v1/web/group/{self.token}/rollcalls/{rc.id}/weather",
                                 headers=self._hdr(USERS[0]["id"]))
            missing = self.client.get(f"/api/v1/web/group/{self.token}/rollcalls/{rc.id + 999}/weather",
                                      headers=self._hdr(USERS[0]["id"]))
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertTrue(ok.json()["ok"])
        self.assertEqual(missing.status_code, 404)
        # The page now shows the last forecast without fetching.
        page = self.client.get(f"/api/v1/web/group/{self.token}").json()
        self.assertIn("forecasts", page["rollcalls"][0]["weather"])

    def test_settings_patch_vote_notices_and_weather(self):
        from services import vote_digest as vd
        from services import weather
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        from api.identity import issue_identity_token
        tok = issue_identity_token(ADMIN_USER["id"])
        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": tok, "vote_notices": "bundled", "vote_digest_hours": 4,
                                    "weather_alerts": True})
        self.assertEqual(r.status_code, 204, r.text)
        self.assertEqual(vd.mode(CHAT_ID), {"mode": "bundled", "hours": 4})
        self.assertTrue(weather.alerts_enabled(CHAT_ID))
        page = self.client.get(f"/api/v1/web/group/{self.token}").json()
        self.assertEqual((page["vote_digest_hours"], page["shh_mode"], page["weather_alerts"]), (4, True, True))

        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": tok, "vote_notices": "bundled", "vote_digest_hours": 99})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
