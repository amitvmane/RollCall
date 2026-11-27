"""
Lineup (services/lineup.py) — the two teams on a pitch: side size, formations,
placing players, how votes and team moves affect spots, publish (text or
picture), the web route, the partner API endpoint and the lineup.published
webhook. Real SQLite/Postgres, real handlers, mocked bot API.
"""
import json
import os
import random
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient

import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID
from mock_helpers import get_mock_bot

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


def _lu():
    from services import lineup
    return lineup


def _teams():
    from services import teams
    return teams


def _spots(lv, team_no):
    t = next(t for t in lv["teams"] if t["no"] == team_no)
    return {s["pos"]: s["key"] for s in t["slots"]}


def _where(lv, key):
    for t in lv["teams"]:
        for s in t["slots"]:
            if s["key"] == key:
                return t["no"], s["pos"]
        if any(p["key"] == key for p in t["subs"]):
            return t["no"], None
    return None


class _Base(IntegrationBase):

    def setUp(self):
        super().setUp()
        db.get_or_create_chat(CHAT_ID)
        db.update_chat_settings(CHAT_ID, teams_enabled=1, sport="football")

    def _rc(self):
        return self.mgr.get_rollcalls(CHAT_ID)[0]

    async def _game(self, n, teams=2, seed=1):
        await self.start_rc("Sunday Game")
        for u in USERS[:n]:
            await self.vote_in(u)
        rc = self._rc()
        _teams().form(rc, teams, "Admin", rng=random.Random(seed))
        return self._rc()


class TestLineupRules(_Base):

    async def test_teams_are_placed_when_made_side_is_half_the_in_list(self):
        rc = await self._game(9)
        lv = _lu().view(rc)
        self.assertTrue(lv["available"])
        self.assertEqual(lv["side"], 4, "9 IN → 4-a-side (half, rounded down)")
        self.assertTrue(lv["side_auto"])
        self.assertEqual([t["formation"] for t in lv["teams"]], ["2-1", "1-2"])
        self.assertEqual(lv["placed"], 8)
        self.assertEqual(sum(len(t["subs"]) for t in lv["teams"]), 1)

    async def test_no_sport_means_no_pitch(self):
        await self._game(9)
        db.update_chat_settings(CHAT_ID, sport=None)
        _teams().form(self._rc(), 2, "Admin")
        self.assertFalse(_lu().view(self._rc())["available"])

    async def test_two_teams_face_off_three_or_four_get_a_board_each(self):
        rc = await self._game(10)
        self.assertEqual(_lu().view(rc)["layout"], "faceoff")
        for count in (3, 4):
            _teams().form(self._rc(), count, "Admin", rng=random.Random(2))
            lv = _lu().view(self._rc())
            self.assertTrue(lv["available"], count)
            self.assertEqual(lv["layout"], "boards")
            self.assertEqual(len(lv["teams"]), count)
            self.assertEqual(lv["side"], 4, "10 IN in 3–4 teams → the 4-a-side minimum")
            self.assertEqual([t["formation"] for t in lv["teams"]],
                             [_lu().FORMATIONS[4][i % 3] for i in range(count)], "presets vary by team")
            self.assertEqual(lv["placed"], 10, "everyone placed when the teams are made")

    async def test_side_splits_the_in_list_by_the_number_of_teams(self):
        self.assertEqual(_lu().default_side(20, 3), 6)
        self.assertEqual(_lu().default_side(17, 2), 8)
        self.assertEqual(_lu().default_side(30, 4), 7)
        self.assertEqual(_lu().default_side(6, 3), 4)

    async def test_three_teams_place_move_formation_and_publish(self):
        rc = await self._game(10, teams=3)
        lv = _lu().view(rc)
        t3 = next(t for t in lv["teams"] if t["no"] == 3)
        key = next(s["key"] for s in t3["slots"] if s["key"])
        # A player dropped onto team 1's empty spot crosses over; the old spot empties.
        t1 = next(t for t in lv["teams"] if t["no"] == 1)
        empty = next((s["pos"] for s in t1["slots"] if not s["key"]), None)
        if empty is None:
            lv = _lu().place(self._rc(), t1["slots"][-1]["key"], 1, None, "Admin")
            empty = next(s["pos"] for s in next(t for t in lv["teams"] if t["no"] == 1)["slots"] if not s["key"])
        lv = _lu().place(self._rc(), key, 1, empty, "Admin")
        self.assertEqual(_where(lv, key), (1, empty))
        lv = _lu().set_formation(self._rc(), 3, "1-1-1", "Admin")
        self.assertEqual(next(t for t in lv["teams"] if t["no"] == 3)["formation"], "1-1-1")
        text = _lu().render_text(self._rc(), lv)
        self.assertIn("· 3 teams", text)
        self.assertIn("🟢 Team C (1-1-1)", text)
        from PIL import Image
        img = Image.open(_lu().render_png(self._rc(), lv))
        self.assertGreater(img.width, 1200, "three boards side by side")
        _teams().form(self._rc(), 4, "Admin")
        img = Image.open(_lu().render_png(self._rc(), _lu().view(self._rc())))
        self.assertGreater(img.height, img.width, "four boards in a 2×2 grid")

    async def test_board_spots_stay_on_the_half_pitch_and_apart(self):
        for side, forms in _lu().FORMATIONS.items():
            for f in forms:
                pts = [_lu().pitch_xy(0, pos, f, board=True) for pos, _r in _lu().slots(f)]
                for x, y in pts:
                    self.assertTrue(5 <= x <= 95 and 10 <= y <= 88, (f, x, y))
                self.assertEqual(len(set(pts)), len(pts), f)

    async def test_fewer_per_side_makes_subs_and_reflows_in_order(self):
        rc = await self._game(10)
        lv = _lu().set_side(rc, 4, "Admin")
        self.assertEqual(lv["side"], 4)
        self.assertFalse(lv["side_auto"])
        for t in lv["teams"]:
            self.assertEqual(len([s for s in t["slots"] if s["key"]]), 4)
            self.assertEqual(len(t["subs"]), 1)
        # Someone IN later doesn't resize a side someone chose.
        await self.set_in_for(self.msg("/sif Ann", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Bea", ADMIN_USER))
        self.assertEqual(_lu().view(self._rc())["side"], 4)

    async def test_each_team_picks_its_own_formation(self):
        rc = await self._game(10)
        lv = _lu().set_formation(rc, 2, "2-1-1", "Admin")
        self.assertEqual([t["formation"] for t in lv["teams"]], ["2-2", "2-1-1"])
        self.assertEqual(lv["placed"], 10, "re-seated, nobody lost")
        with self.assertRaises(Exception):
            _lu().set_formation(rc, 1, "2-3-1", "Admin")   # not a 5-a-side shape

    async def test_place_into_empty_spot_and_swap_on_drop(self):
        rc = await self._game(8)
        rc = self._rc()
        _lu().set_side(rc, 5, "Admin")                 # 4 per team → one empty spot each
        lv = _lu().view(rc)
        t1 = _spots(lv, 1)
        empty = next(p for p, k in t1.items() if k is None)
        mover = t1["0:0"]
        lv = _lu().place(rc, mover, 1, empty, "Admin")
        self.assertEqual(_where(lv, mover), (1, empty))
        self.assertIsNone(_spots(lv, 1)["0:0"], "the old spot is free")
        # Drop onto a player on the OTHER team: they trade places (and teams).
        other = _spots(lv, 2)["1:0"]
        lv = _lu().place(rc, mover, 2, "1:0", "Admin")
        self.assertEqual(_where(lv, mover), (2, "1:0"))
        self.assertEqual(_where(lv, other), (1, empty))
        # Drop on Subs.
        lv = _lu().place(rc, mover, 2, None, "Admin")
        self.assertEqual(_where(lv, mover), (2, None))

    async def test_drop_leaves_an_empty_spot_and_a_late_vote_is_a_sub(self):
        rc = await self._game(8)
        lv = _lu().view(rc)
        key = _spots(lv, 1)["1:0"]
        uid = int(key[1:])
        await self.vote_out(next(u for u in USERS if u["id"] == uid))
        lv = _lu().view(self._rc())
        self.assertIsNone(_spots(lv, 1)["1:0"], "the spot stays empty")
        await self.vote_in(USERS[9])
        lv = _lu().view(self._rc())
        self.assertEqual(_where(lv, f"u{USERS[9]['id']}"), (1, None), "a late vote is a sub")

    async def test_team_move_makes_a_sub_and_swap_trades_spots(self):
        rc = await self._game(8)
        lv = _lu().view(rc)
        a, b = _spots(lv, 1)["0:0"], _spots(lv, 2)["2:1"]
        _teams().swap(rc, a, b, "Admin")
        lv = _lu().view(self._rc())
        self.assertEqual((_where(lv, a), _where(lv, b)), ((2, "2:1"), (1, "0:0")))
        _teams().move(self._rc(), a, 1, "Admin")
        self.assertEqual(_where(_lu().view(self._rc()), a), (1, None))

    async def test_locked_teams_refuse_lineup_changes(self):
        rc = await self._game(8)
        _teams().set_locked(rc, True, is_admin=True)
        with self.assertRaises(Exception):
            _lu().set_side(self._rc(), 5, "x")
        with self.assertRaises(Exception):
            _lu().clear(self._rc(), "x")

    async def test_view_does_not_write(self):
        rc = await self._game(8)
        _lu().clear(rc, "Admin")
        before = db.get_team_rows(rc.id)
        _lu().view(self._rc())
        self.assertEqual(db.get_team_rows(rc.id), before)

    async def test_text_and_picture(self):
        rc = await self._game(9)
        _lu().set_side(rc, 5, "Admin")
        lv = _lu().view(self._rc())
        text = _lu().render_text(self._rc(), lv)
        self.assertIn("Lineup — Sunday Game · 5-a-side", text)
        self.assertIn("🔴 Team A (2-2)", text)
        self.assertIn("GK  ", text)
        self.assertIn("—", text, "the empty spot shows as —")
        png = _lu().render_png(self._rc(), lv).getvalue()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    async def test_shared_picture_says_made_with_rollcall(self):
        from PIL import Image
        await self._game(8)
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example/"}):
            self.assertEqual(_lu().made_with(), ("Made with RollCall · rc.example", "https://rc.example/"))
            self.assertTrue(_lu().credit_caption("⚽ Lineup").endswith("\nMade with RollCall — https://rc.example/"))
            content = _lu().publish_content(self._rc(), __import__("services.teams", fromlist=["x"]).view(self._rc()),
                                            "", as_picture=True)
            self.assertIn("https://rc.example/", content["caption"], "the picture can't be tapped — the caption links")
            # the credit sits in its own strip under the pitch: the bottom rows are not pitch green
            img = Image.open(content["photo"]).convert("RGB")
            self.assertNotEqual(img.getpixel((img.width // 2, img.height - 5)), (47, 138, 74))
        with patch.dict(os.environ, {"WEB_BASE_URL": ""}):
            self.assertEqual(_lu().made_with(), ("Made with RollCall", None))
            self.assertEqual(_lu().credit_caption("⚽ Lineup"), "⚽ Lineup", "no page, no dangling link")
        db.update_chat_settings(CHAT_ID, sport="cricket")
        png = _lu().render_png(self._rc(), _lu().view(self._rc())).getvalue()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    async def test_setting_the_sport_places_teams_already_made(self):
        db.update_chat_settings(CHAT_ID, sport=None)
        rc = await self._game(8)
        self.assertEqual(_lu().set_sport(CHAT_ID, "football", 1, "A"), "football")
        self.assertEqual(_lu().view(rc)["placed"], 0)
        _lu().autofill_open(CHAT_ID, "A")
        self.assertEqual(_lu().view(self._rc())["placed"], 8)
        with self.assertRaises(Exception):
            _lu().set_sport(CHAT_ID, "quidditch", 1, "A")


class TestLineupSurfaces(_Base):

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

    def _post(self, rc_id, body, uid):
        from api.identity import issue_identity_token
        return self.client.post(f"/api/v1/web/group/{self.token}/rollcalls/{rc_id}/teams",
                                json=body, headers={"X-Identity-Token": issue_identity_token(uid)})

    async def test_web_page_and_actions(self):
        rc = await self._game(8)
        page = self.client.get(f"/api/v1/web/group/{self.token}").json()
        lu = page["rollcalls"][0]["teams"]["lineup"]
        self.assertTrue(lu["available"])
        self.assertIn("Lineup — Sunday Game", lu["text"])
        self.assertEqual(page["sport"], "football")

        r = self._post(rc.id, {"action": "lineup_side", "side": 5}, USERS[0]["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["lineup"]["side"], 5)
        r = self._post(rc.id, {"action": "lineup_formation", "team_no": 1, "formation": "1-2-1"}, USERS[1]["id"])
        self.assertEqual(r.json()["lineup"]["teams"][0]["formation"], "1-2-1")
        key = r.json()["lineup"]["teams"][0]["slots"][0]["key"]
        r = self._post(rc.id, {"action": "lineup_place", "who_key": key, "team_no": 1, "position": ""}, USERS[1]["id"])
        self.assertEqual(_where(r.json()["lineup"], key), (1, None))
        r = self._post(rc.id, {"action": "lineup_fill"}, USERS[1]["id"])
        self.assertEqual(_where(r.json()["lineup"], key)[1] is not None, True)
        r = self._post(rc.id, {"action": "lineup_place", "who_key": key, "team_no": 1, "position": "9:9"}, USERS[1]["id"])
        self.assertEqual(r.status_code, 409)

    async def test_publish_picture_needs_admin_and_posts_a_photo(self):
        rc = await self._game(8)
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        r = self._post(rc.id, {"action": "publish", "post_as": "picture"}, USERS[0]["id"])
        self.assertEqual(r.status_code, 403)
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        get_mock_bot().reset_mock()
        r = self._post(rc.id, {"action": "publish", "post_as": "picture"}, ADMIN_USER["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(get_mock_bot().send_photo.called)
        self.assertIn("Published by", get_mock_bot().send_photo.call_args.kwargs["caption"])
        get_mock_bot().reset_mock()
        self._post(rc.id, {"action": "publish", "post_as": "text"}, ADMIN_USER["id"])
        self.assertIn("Lineup — Sunday Game", get_mock_bot().send_message.call_args.args[1])

    async def _details(self):
        from datetime import datetime as _dt
        rc = self._rc()
        rc.location = "Turf 3"
        rc.inListLimit = 12
        rc.event_fee = "200"
        rc.finalizeDate = _dt(2026, 11, 29, 19, 0)
        rc.timezone = "Asia/Kolkata"
        rc.save()
        return self._rc()

    async def test_publish_carries_the_game_details(self):
        await self._game(8)
        rc = await self._details()
        get_mock_bot().reset_mock()
        r = self._post(rc.id, {"action": "publish", "post_as": "text"}, ADMIN_USER["id"])
        self.assertEqual(r.status_code, 200, r.text)
        text = get_mock_bot().send_message.call_args.args[1]
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith("⚽ Lineup — Sunday Game"), lines[0])
        for want in ("🗓 Sun 29 Nov, 07:00 PM", "📍 Turf 3", "👥 8/12 IN", "💰 Fee 200"):
            self.assertIn(want, text)
        self.assertLess(text.index("📍 Turf 3"), text.index("Team A"), "details sit under the title")

        get_mock_bot().reset_mock()
        self._post(rc.id, {"action": "publish", "post_as": "picture"}, ADMIN_USER["id"])
        cap = get_mock_bot().send_photo.call_args.kwargs["caption"]
        for want in ("🗓 Sun 29 Nov, 07:00 PM", "📍 Turf 3", "👥 8/12 IN", "💰 Fee 200", "Published by"):
            self.assertIn(want, cap)

    async def test_lineup_command_and_copy_text_carry_the_details(self):
        from handlers.teams import lineup_cmd
        await self._game(8)
        rc = await self._details()
        want = ("🗓 Sun 29 Nov, 07:00 PM", "📍 Turf 3", "👥 8/12 IN", "💰 Fee 200")
        get_mock_bot().reset_mock()
        await lineup_cmd(self.msg("/lineup", USERS[0]))
        text = " ".join(self.sent_texts())
        for w in want:
            self.assertIn(w, text)
        get_mock_bot().reset_mock()
        await lineup_cmd(self.msg("/lineup pic", USERS[0]))
        cap = get_mock_bot().send_photo.call_args.kwargs["caption"]
        for w in want:
            self.assertIn(w, cap)
        page = self.client.get(f"/api/v1/web/group/{self.token}").json()
        copy = page["rollcalls"][0]["teams"]["lineup"]["text"]
        for w in want:
            self.assertIn(w, copy)
        self.assertEqual(copy.count("📍"), 1)
        # Publishing adds them once, not twice.
        get_mock_bot().reset_mock()
        self._post(rc.id, {"action": "publish", "post_as": "text"}, ADMIN_USER["id"])
        self.assertEqual(get_mock_bot().send_message.call_args.args[1].count("📍 Turf 3"), 1)

    async def test_plain_team_sheet_publish_carries_the_details_too(self):
        db.update_chat_settings(CHAT_ID, sport=None)
        await self._game(8)
        rc = await self._details()
        rc.event_fee = None
        rc.location = None
        rc.save()
        get_mock_bot().reset_mock()
        self._post(rc.id, {"action": "publish"}, ADMIN_USER["id"])
        text = get_mock_bot().send_message.call_args.args[1]
        self.assertTrue(text.startswith("⚽ Teams — Sunday Game\n🗓 Sun 29 Nov, 07:00 PM\n👥 8/12 IN"), text[:120])
        self.assertNotIn("📍", text, "only details that are set")
        self.assertNotIn("💰", text)

    async def test_sport_setting_is_admin_only_on_the_web(self):
        from api.identity import issue_identity_token
        db.update_chat_settings(CHAT_ID, sport=None)
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": issue_identity_token(USERS[0]["id"]), "sport": "football"})
        self.assertIn(r.status_code, (401, 403))
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": issue_identity_token(ADMIN_USER["id"]), "sport": "football"})
        self.assertEqual(r.status_code, 204, r.text)
        self.assertEqual(_lu().sport(CHAT_ID), "football")

    async def test_activity_sets_teams_and_sport_in_one_step(self):
        from api.identity import issue_identity_token
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": issue_identity_token(USERS[0]["id"]), "activity": "event"})
        self.assertIn(r.status_code, (401, 403))
        get_mock_bot().get_chat_member.return_value.status = "administrator"

        def patch_(v):
            r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                                  json={"id_token": issue_identity_token(ADMIN_USER["id"]), "activity": v})
            self.assertEqual(r.status_code, 204, r.text)
            return self.client.get(f"/api/v1/web/group/{self.token}").json()
        page = patch_("event")
        self.assertEqual((page["activity"], page["teams_enabled"]), ("event", False))
        self.assertIsNone(page["rollcalls"][0]["teams"] if page["rollcalls"] else None)
        page = patch_("game")
        self.assertEqual((page["activity"], page["teams_enabled"], page["sport"]), ("game", True, None))
        page = patch_("cricket")
        self.assertEqual((page["activity"], page["sport"]), ("cricket", "cricket"))
        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": issue_identity_token(ADMIN_USER["id"]), "activity": "chess"})
        self.assertEqual(r.status_code, 422)

    async def test_activity_from_telegram(self):
        from handlers.teams import sport_cmd
        await self._game(8)
        get_mock_bot().reset_mock()
        await sport_cmd(self.msg("/sport event", ADMIN_USER))
        self.assertFalse(_teams().enabled(CHAT_ID))
        self.assertEqual(_lu().activity(CHAT_ID), "event")
        self.assertIn("no teams", " ".join(self.sent_texts()))
        await sport_cmd(self.msg("/sport football", ADMIN_USER))
        self.assertTrue(_teams().enabled(CHAT_ID), "picking a sport turns teams on")
        self.assertEqual(_lu().activity(CHAT_ID), "football")
        await sport_cmd(self.msg("/sport off", ADMIN_USER))
        self.assertEqual(_lu().activity(CHAT_ID), "game", "off drops the sport, teams stay on")
        get_mock_bot().reset_mock()
        await sport_cmd(self.msg("/sport", USERS[0]))
        self.assertIn("Game", " ".join(self.sent_texts()))
        await sport_cmd(self.msg("/sport quidditch", ADMIN_USER))
        self.assertEqual(_lu().activity(CHAT_ID), "game")

    async def test_telegram_sport_command_is_admin_only(self):
        from handlers.teams import sport_cmd
        db.update_chat_settings(CHAT_ID, sport=None)
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        await sport_cmd(self.msg("/sport football", USERS[0]))
        self.assertIsNone(_lu().sport(CHAT_ID))
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        await sport_cmd(self.msg("/sport football", ADMIN_USER))
        self.assertEqual(_lu().sport(CHAT_ID), "football")

    async def test_partner_api_endpoint_and_webhook(self):
        rc = await self._game(8)
        tok = db.generate_api_token()
        db.insert_api_token(db._hash_token(tok), CHAT_ID, "read", label="t",
                            issued_by_user_id=ADMIN_USER["id"],
                            expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1))
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{rc.id}/lineup",
                            headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["available"])
        self.assertEqual(body["players_per_side"], 4)
        gk = body["teams"][0]["positions"][0]
        self.assertEqual((gk["position"], gk["role"]), ("0:0", "GK"))
        self.assertIsInstance(gk["player"]["user_id"], int)
        self.assertEqual(self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/1/lineup",
                                         headers={"Authorization": f"Bearer {tok}"}).status_code, 404,
                         "no number path for a new route")

        from services import webhooks
        with patch("services.webhooks._kick"):
            webhooks.create_endpoint(CHAT_ID, "https://partner.example.com/h", ["lineup.published"], ADMIN_USER["id"])
            webhooks._hooked_chats = None
            _teams().publish(self._rc(), is_admin=True)
            _lu().publish_content(self._rc(), _teams().view(self._rc()), "", as_picture=False)
        with db._cursor() as c:
            c.execute("SELECT event, payload FROM webhook_deliveries")
            rows = [dict(r) if isinstance(r, dict) else {"event": r[0], "payload": r[1]} for r in c.fetchall()]
        self.assertEqual([r["event"] for r in rows], ["lineup.published"])
        data = json.loads(rows[0]["payload"])["data"]
        self.assertEqual(data["rollcall_id"], rc.id)
        self.assertEqual(len(data["teams"]), 2)


class TestCricketRoles(_Base):

    def setUp(self):
        super().setUp()
        db.update_chat_settings(CHAT_ID, sport="cricket")

    async def test_roles_not_positions(self):
        rc = await self._game(8)
        lv = _lu().view(rc)
        self.assertTrue(lv["available"])
        self.assertEqual(lv["kind"], "roles")
        self.assertEqual(lv["placed"], 0, "roles are never guessed")
        self.assertEqual([r["code"] for r in lv["roles"]], ["WK", "BAT", "AR", "BOWL"])
        key = lv["teams"][0]["subs"][0]["key"]
        lv = _lu().place(rc, key, 1, "WK", "Admin")
        first = lv["teams"][0]["slots"][0]
        self.assertEqual((first["pos"], first["role"], first["key"]), ("WK:0", "WK", key))
        with self.assertRaises(Exception):
            _lu().place(rc, key, 1, "1:0", "Admin")          # a pitch spot isn't a role
        with self.assertRaises(Exception):
            _lu().set_formation(rc, 1, "2-3-1", "Admin")     # no formations in cricket
        text = _lu().render_text(self._rc(), _lu().view(self._rc()))
        self.assertIn("🧤 WK", text)
        self.assertIn("No role yet", text)
        png = _lu().render_png(self._rc(), _lu().view(self._rc())).getvalue()
        self.assertEqual(png[:4], b"\x89PNG")
        api = _lu().api_payload(self._rc(), _lu().view(self._rc()))
        self.assertEqual(api["teams"][0]["formation"], "")
        self.assertEqual(api["teams"][0]["positions"][0]["role"], "WK")

    async def test_switching_sport_does_not_mix_spots_and_roles(self):
        db.update_chat_settings(CHAT_ID, sport="football")
        rc = await self._game(8)
        self.assertEqual(_lu().view(rc)["placed"], 8)
        db.update_chat_settings(CHAT_ID, sport="cricket")
        self.assertEqual(_lu().view(self._rc())["placed"], 0, "a pitch spot isn't read as a role")
