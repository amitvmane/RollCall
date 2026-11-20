"""
Integration tests for teams (services/teams.py, handlers/teams.py and the
web route). Real SQLite, real handlers, mocked bot API.
"""
import os
import random
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz
from fastapi.testclient import TestClient

import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID
from mock_helpers import get_mock_bot

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


def _svc():
    from services import teams
    return teams


def _sizes(v):
    return [len(t["players"]) for t in v["teams"]]


def _keys(v):
    return sorted(p["key"] for t in v["teams"] for p in t["players"])


def _team_of(v, key):
    return next(t["no"] for t in v["teams"] for p in t["players"] if p["key"] == key)


class _Base(IntegrationBase):

    TEAMS_ON = True

    def setUp(self):
        super().setUp()
        db.get_or_create_chat(CHAT_ID)
        db.update_chat_settings(CHAT_ID, teams_enabled=1 if self.TEAMS_ON else 0)

    def _rc(self):
        return self.mgr.get_rollcalls(CHAT_ID)[0]

    async def _game_with(self, n_members, guest=None):
        await self.start_rc("Sunday Game")
        for u in USERS[:n_members]:
            await self.vote_in(u)
        if guest:
            await self.set_in_for(self.msg(f"/sif {guest}", ADMIN_USER))
        return self._rc()

    def _non_admin_group(self):
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"


class TestTeamRules(_Base):

    async def test_split_is_even_and_includes_guests(self):
        rc = await self._game_with(6, guest="Ann")
        v = _svc().form(rc, 2, "Admin", is_admin=True, rng=random.Random(1))
        self.assertTrue(v["formed"])
        self.assertEqual(sorted(_sizes(v)), [3, 4])
        expected = sorted([f"u{u['id']}" for u in USERS[:6]] + ["pann"])
        self.assertEqual(_keys(v), expected)

    async def test_three_teams_and_reshuffle_keep_everyone(self):
        rc = await self._game_with(7)
        v = _svc().form(rc, 3, "Admin", is_admin=True, rng=random.Random(2))
        self.assertEqual(sorted(_sizes(v)), [2, 2, 3])
        before = _keys(v)
        v = _svc().reshuffle(rc, "User1", rng=random.Random(3))
        self.assertEqual(v["count"], 3)
        self.assertEqual(_keys(v), before)

    async def test_needs_two_players(self):
        rc = await self._game_with(1)
        with self.assertRaises(Exception):
            _svc().form(rc, 2, "Admin", is_admin=True)

    async def test_drop_leaves_gap_and_new_player_fills_smallest_team(self):
        rc = await self._game_with(4)
        v = _svc().form(rc, 2, "Admin", is_admin=True, rng=random.Random(4))
        dropped = f"u{USERS[0]['id']}"
        gap_team = _team_of(v, dropped)

        await self.vote_out(USERS[0])
        v = _svc().view(self._rc())
        self.assertNotIn(dropped, _keys(v))
        self.assertEqual(sorted(_sizes(v)), [1, 2], "no reshuffle on a drop")

        await self.vote_in(USERS[5])
        v = _svc().view(self._rc())
        self.assertEqual(_team_of(v, f"u{USERS[5]['id']}"), gap_team)
        self.assertEqual(_sizes(v), [2, 2])

    async def test_waitlist_promotion_takes_the_dropped_slot(self):
        rc = await self._game_with(4)
        rc.inListLimit = 4
        rc.save()
        await self.vote_in(USERS[4])  # waitlisted
        rc = self._rc()
        v = _svc().form(rc, 2, "Admin", is_admin=True, rng=random.Random(5))
        self.assertNotIn(f"u{USERS[4]['id']}", _keys(v), "waitlist is not on the sheet")
        gap_team = _team_of(v, f"u{USERS[1]['id']}")
        await self.vote_out(USERS[1])
        v = _svc().view(self._rc())
        self.assertEqual(_team_of(v, f"u{USERS[4]['id']}"), gap_team)

    async def test_view_does_not_write(self):
        rc = await self._game_with(4)
        _svc().form(rc, 2, "Admin", is_admin=True)
        await self.vote_in(USERS[5])
        _svc().view(self._rc())
        self.assertNotIn(f"u{USERS[5]['id']}", [r["who_key"] for r in db.get_team_rows(rc.id)])
        # The next change saves the reconciled sheet.
        _svc().move(self._rc(), f"u{USERS[1]['id']}", None, "User2")
        self.assertIn(f"u{USERS[5]['id']}", [r["who_key"] for r in db.get_team_rows(rc.id)])

    async def test_move_cycles_and_targets(self):
        rc = await self._game_with(4)
        v = _svc().form(rc, 2, "Admin", is_admin=True)
        key = f"u{USERS[2]['id']}"
        start = _team_of(v, key)
        v = _svc().move(rc, key, None, "User1")
        self.assertEqual(_team_of(v, key), start % 2 + 1)
        v = _svc().move(rc, key, start, "User1")
        self.assertEqual(_team_of(v, key), start)
        with self.assertRaises(Exception):
            _svc().move(rc, "u999999", 1, "User1")
        with self.assertRaises(Exception):
            _svc().move(rc, key, 3, "User1")

    async def test_only_admin_changes_the_number_of_teams(self):
        from exceptions import insufficientPermissions
        rc = await self._game_with(6)
        with self.assertRaises(insufficientPermissions):
            _svc().form(rc, 3, "User1", is_admin=False)
        v = _svc().form(rc, 2, "User1", is_admin=False)
        self.assertEqual(v["count"], 2)

    async def test_lock_blocks_changes_and_unlock_reopens(self):
        from exceptions import incorrectParameter, insufficientPermissions
        rc = await self._game_with(4)
        _svc().form(rc, 2, "Admin", is_admin=True)
        with self.assertRaises(insufficientPermissions):
            _svc().set_locked(rc, True, is_admin=False)
        v = _svc().set_locked(rc, True, is_admin=True)
        self.assertEqual((v["locked"], v["locked_by"]), (True, "admin"))
        with self.assertRaises(incorrectParameter):
            _svc().move(rc, f"u{USERS[0]['id']}", None, "User1")
        with self.assertRaises(incorrectParameter):
            _svc().reshuffle(rc, "User1")
        _svc().publish(rc)  # publishing is still allowed
        v = _svc().set_locked(rc, False, is_admin=True)
        self.assertFalse(v["locked"])

    async def test_game_time_locks_until_admin_unlocks(self):
        rc = await self._game_with(4)
        _svc().form(rc, 2, "Admin", is_admin=True)
        tz = pytz.timezone(rc.timezone or "Asia/Kolkata")
        rc.finalizeDate = datetime.now(tz).replace(tzinfo=None) - timedelta(minutes=5)
        rc.save()
        v = _svc().view(rc)
        self.assertEqual((v["locked"], v["locked_by"]), (True, "game"))
        v = _svc().set_locked(rc, False, is_admin=True)
        self.assertFalse(v["locked"], "an admin unlock overrides the game-time lock")

    async def test_publish_text_lists_every_team(self):
        rc = await self._game_with(4, guest="Ann")
        _svc().form(rc, 2, "Admin", is_admin=True)
        v, text = _svc().publish(rc)
        self.assertIn("Team A", text)
        self.assertIn("Team B", text)
        self.assertIn("Ann", text)
        self.assertIsNotNone(v["published_at"])


class TestTelegramSheet(_Base):

    async def _tap(self, data, user=None):
        from handlers.teams import teams_callback
        get_mock_bot().reset_mock()
        await teams_callback(self.call(data, user))

    def _edited_text(self):
        args = get_mock_bot().edit_message_text.call_args
        return args.args[0] if args.args else args.kwargs.get("text")

    async def test_panel_has_teams_button_and_opens_sheet(self):
        rc = await self._game_with(4)
        from handlers.lifecycle import get_status_keyboard, callback_handler
        markup = await get_status_keyboard(1, CHAT_ID)
        datas = [b.callback_data for b in markup.keyboard]
        self.assertIn(f"btn_teams_1_{rc.id}", datas)
        get_mock_bot().reset_mock()
        await callback_handler(self.call(f"btn_teams_1_{rc.id}", USERS[0]))
        self.assertIn("No teams yet", self._edited_text())

    async def test_form_move_publish_flow(self):
        rc = await self._game_with(4, guest="Ann")
        await self._tap(f"tm:form:{rc.id}:2", USERS[0])
        self.assertIn("Team A", self._edited_text())
        self.assertFalse(get_mock_bot().send_message.called, "forming is silent")

        v = _svc().view(self._rc())
        start = _team_of(v, "pann")
        await self._tap(f"tm:mv:{rc.id}:{_svc().button_token('pann')}", USERS[1])
        self.assertEqual(_team_of(_svc().view(self._rc()), "pann"), start % 2 + 1)
        self.assertFalse(get_mock_bot().send_message.called, "moves are silent")

        await self._tap(f"tm:pub:{rc.id}", USERS[2])
        sent = get_mock_bot().send_message.call_args
        self.assertEqual(sent.args[0], CHAT_ID)
        self.assertIn("Team B", sent.args[1])
        self.assertIn("Published by User3", sent.args[1])

    async def test_non_admin_cannot_lock_or_change_count(self):
        rc = await self._game_with(4)
        await self._tap(f"tm:form:{rc.id}:2", USERS[0])
        self._non_admin_group()
        await self._tap(f"tm:lock:{rc.id}", USERS[0])
        alert = get_mock_bot().answer_callback_query.call_args
        self.assertIn("Only an admin", alert.args[1])
        await self._tap(f"tm:form:{rc.id}:3", USERS[0])
        self.assertEqual(_svc().view(self._rc())["count"], 2)

    async def test_teams_command_posts_the_sheet(self):
        from handlers.teams import teams_cmd
        await teams_cmd(self.msg("/teams", USERS[0]))
        self.assertTrue(any("No rollcall is open" in t for t in self.sent_texts()), self.sent_texts())
        rc = await self._game_with(3)
        get_mock_bot().reset_mock()
        await teams_cmd(self.msg("/teams", USERS[0]))
        sent = get_mock_bot().send_message.call_args
        self.assertIn("Teams — Sunday Game", sent.args[1])
        datas = [b.callback_data for b in sent.kwargs["reply_markup"].keyboard]
        self.assertIn(f"tm:form:{rc.id}:2", datas)
        get_mock_bot().reset_mock()
        await teams_cmd(self.msg("/teams ::5", USERS[0]))
        self.assertIn("::5", " ".join(self.sent_texts()))

    async def test_forged_rollcall_id_is_refused(self):
        rc = await self._game_with(2)
        await self._tap(f"tm:form:{rc.id + 999}:2", USERS[0])
        alert = get_mock_bot().answer_callback_query.call_args
        self.assertIn("ended", alert.args[1])
        self.assertEqual(db.get_team_rows(rc.id), [])


class TestWebTeams(_Base):

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

    def _post(self, rc_id, body, uid=None):
        return self.client.post(f"/api/v1/web/group/{self.token}/rollcalls/{rc_id}/teams",
                                json=body, headers=self._hdr(uid) if uid else {})

    async def test_member_flow_and_page_shows_teams(self):
        rc = await self._game_with(4)
        self.assertEqual(self._post(rc.id, {"action": "form", "count": 2}).status_code, 401)
        r = self._post(rc.id, {"action": "form", "count": 2}, USERS[0]["id"])
        self.assertEqual(r.status_code, 200, r.text)
        key = f"u{USERS[3]['id']}"
        target = _team_of(r.json(), key) % 2 + 1
        r = self._post(rc.id, {"action": "move", "who_key": key, "team_no": target}, USERS[1]["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_team_of(r.json(), key), target)

        page = self.client.get(f"/api/v1/web/group/{self.token}").json()
        self.assertTrue(page["rollcalls"][0]["teams"]["formed"])

        get_mock_bot().reset_mock()
        r = self._post(rc.id, {"action": "publish"}, USERS[2]["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Team A", get_mock_bot().send_message.call_args.args[1])

    async def test_lock_needs_web_admin(self):
        rc = await self._game_with(4)
        self._post(rc.id, {"action": "form", "count": 2}, USERS[0]["id"])
        self._non_admin_group()
        r = self._post(rc.id, {"action": "lock"}, USERS[0]["id"])
        self.assertEqual(r.status_code, 403, r.text)
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        r = self._post(rc.id, {"action": "lock"}, ADMIN_USER["id"])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["locked"])
        r = self._post(rc.id, {"action": "reshuffle"}, USERS[0]["id"])
        self.assertEqual(r.status_code, 409)

    async def test_off_hides_the_card_and_refuses(self):
        rc = await self._game_with(4)
        db.update_chat_settings(CHAT_ID, teams_enabled=0)
        page = self.client.get(f"/api/v1/web/group/{self.token}").json()
        self.assertIsNone(page["rollcalls"][0]["teams"])
        self.assertFalse(page["teams_enabled"])
        r = self._post(rc.id, {"action": "form", "count": 2}, USERS[0]["id"])
        self.assertEqual(r.status_code, 409)

    def test_settings_switch(self):
        from api.identity import issue_identity_token
        get_mock_bot().get_chat_member.return_value.status = "administrator"
        db.update_chat_settings(CHAT_ID, teams_enabled=0)
        r = self.client.patch(f"/api/v1/web/group/{self.token}/settings",
                              json={"id_token": issue_identity_token(ADMIN_USER["id"]), "teams_enabled": True})
        self.assertEqual(r.status_code, 204, r.text)
        self.assertTrue(_svc().enabled(CHAT_ID))
        self.assertTrue(self.client.get(f"/api/v1/web/group/{self.token}").json()["teams_enabled"])

    async def test_other_rollcall_is_404(self):
        rc = await self._game_with(2)
        r = self._post(rc.id + 999, {"action": "form", "count": 2}, USERS[0]["id"])
        self.assertEqual(r.status_code, 404)


class TestOptIn(_Base):
    """Teams are off until an admin turns them on (chats.teams_enabled)."""

    TEAMS_ON = False

    async def test_off_by_default_everywhere(self):
        from handlers.teams import teams_cmd, teams_callback
        from handlers.lifecycle import get_status_keyboard
        self.assertFalse(_svc().enabled(CHAT_ID))
        rc = await self._game_with(4)
        datas = [b.callback_data for b in (await get_status_keyboard(1, CHAT_ID)).keyboard]
        self.assertNotIn(f"btn_teams_1_{rc.id}", datas, "no Teams button while off")

        get_mock_bot().reset_mock()
        await teams_cmd(self.msg("/teams", USERS[0]))
        self.assertIn("Teams are off", get_mock_bot().send_message.call_args.args[1])

        get_mock_bot().reset_mock()
        await teams_callback(self.call(f"tm:form:{rc.id}:2", USERS[0]))
        self.assertIn("Teams are off", get_mock_bot().answer_callback_query.call_args.args[1])
        self.assertEqual(db.get_team_rows(rc.id), [])
        with self.assertRaises(Exception):
            _svc().form(rc, 2, "Admin", is_admin=True)

    async def test_admin_turns_on_and_off(self):
        from handlers.teams import teams_cmd
        from handlers.lifecycle import get_status_keyboard
        rc = await self._game_with(4)
        self._non_admin_group()
        await teams_cmd(self.msg("/teams on", USERS[0]))
        self.assertFalse(_svc().enabled(CHAT_ID), "members can't turn teams on in an admin-locked group")

        get_mock_bot().get_chat_member.return_value.status = "administrator"
        get_mock_bot().reset_mock()
        await teams_cmd(self.msg("/teams on", ADMIN_USER))
        self.assertTrue(_svc().enabled(CHAT_ID))
        self.assertIn("Teams on", get_mock_bot().send_message.call_args_list[0].args[1])
        datas = [b.callback_data for b in (await get_status_keyboard(1, CHAT_ID)).keyboard]
        self.assertIn(f"btn_teams_1_{rc.id}", datas)

        await teams_cmd(self.msg("/teams off", ADMIN_USER))
        self.assertFalse(_svc().enabled(CHAT_ID))


class TestStillVoting(_Base):
    """Teams made while people are still voting follow the IN list."""

    async def test_late_voters_are_spread_over_the_smallest_teams(self):
        rc = await self._game_with(2)
        v = _svc().form(rc, 2, "Admin", is_admin=True)
        self.assertEqual(_sizes(v), [1, 1])
        for u in USERS[2:7]:
            await self.vote_in(u)
        v = _svc().view(self._rc())
        self.assertEqual(sorted(_sizes(v)), [3, 4], "5 late IN votes fill both teams evenly")
        self.assertEqual(len(_keys(v)), 7)

    async def test_maybe_and_out_never_join(self):
        rc = await self._game_with(2)
        _svc().form(rc, 2, "Admin", is_admin=True)
        await self.vote_maybe(USERS[3])
        await self.vote_out(USERS[4])
        v = _svc().view(self._rc())
        self.assertEqual(_keys(v), sorted([f"u{USERS[0]['id']}", f"u{USERS[1]['id']}"]))

    async def test_view_shows_how_many_are_in_before_teams_exist(self):
        rc = await self._game_with(3)
        v = _svc().view(rc)
        self.assertFalse(v["formed"])
        self.assertEqual(v["players_in"], 3)


class TestParallelRollcalls(_Base):
    """Two open rollcalls keep separate teams on every surface."""

    async def _two_games(self):
        await self.start_rc("Morning")
        await self.start_rc("Evening")
        for u in USERS[:4]:
            await self.vote_in(u, rc_suffix="::1")
        for u in USERS[4:8]:
            await self.vote_in(u, rc_suffix="::2")
        rcs = self.mgr.get_rollcalls(CHAT_ID)
        return rcs[0], rcs[1]

    async def test_each_rollcall_has_its_own_sheet(self):
        a, b = await self._two_games()
        va = _svc().form(a, 2, "Admin", is_admin=True)
        vb = _svc().form(b, 3, "Admin", is_admin=True)
        self.assertEqual(_keys(va), sorted(f"u{u['id']}" for u in USERS[:4]))
        self.assertEqual(_keys(vb), sorted(f"u{u['id']}" for u in USERS[4:8]))
        self.assertEqual((va["count"], vb["count"]), (2, 3))

        before_a = db.get_team_rows(a.id)
        key = f"u{USERS[5]['id']}"
        _svc().move(b, key, None, "User1")
        _svc().reshuffle(b, "User1")
        self.assertEqual(db.get_team_rows(a.id), before_a, "changing B never touches A")

        await self.vote_in(USERS[8], rc_suffix="::2")
        self.assertNotIn(f"u{USERS[8]['id']}", _keys(_svc().view(self.mgr.get_rollcalls(CHAT_ID)[0])))
        self.assertIn(f"u{USERS[8]['id']}", _keys(_svc().view(self.mgr.get_rollcalls(CHAT_ID)[1])))

    async def test_same_person_in_both_games_is_on_both_sheets(self):
        a, b = await self._two_games()
        await self.vote_in(USERS[0], rc_suffix="::2")
        b = self.mgr.get_rollcalls(CHAT_ID)[1]
        _svc().form(a, 2, "Admin", is_admin=True)
        _svc().form(b, 2, "Admin", is_admin=True)
        key = f"u{USERS[0]['id']}"
        _svc().move(b, key, 1, "User1")
        _svc().move(a, key, 2, "User1")
        self.assertEqual(_team_of(_svc().view(self.mgr.get_rollcalls(CHAT_ID)[0]), key), 2)
        self.assertEqual(_team_of(_svc().view(self.mgr.get_rollcalls(CHAT_ID)[1]), key), 1)

    async def test_telegram_buttons_and_command_address_one_rollcall(self):
        from handlers.teams import teams_cmd, teams_callback
        a, b = await self._two_games()
        get_mock_bot().reset_mock()
        await teams_cmd(self.msg("/teams ::2", USERS[0]))
        sent = get_mock_bot().send_message.call_args
        self.assertIn("Teams — Evening", sent.args[1])
        datas = [x.callback_data for x in sent.kwargs["reply_markup"].keyboard]
        self.assertTrue(all(f":{b.id}" in d for d in datas if d.startswith("tm:")))

        await teams_callback(self.call(f"tm:form:{b.id}:2", USERS[0]))
        self.assertEqual(db.get_team_rows(a.id), [])
        self.assertEqual(len(db.get_team_rows(b.id)), 4)

        # A guest/member token from game B can't move anyone in game A.
        _svc().form(a, 2, "Admin", is_admin=True)
        before_a = db.get_team_rows(a.id)
        await teams_callback(self.call(f"tm:mv:{a.id}:u{USERS[5]['id']}", USERS[0]))
        self.assertEqual(db.get_team_rows(a.id), before_a)

    async def test_web_addresses_the_rollcall_in_the_url(self):
        from api.main import app
        from api.rate_limit import reset_buckets_for_tests
        from api.identity import issue_identity_token
        reset_buckets_for_tests()
        client = TestClient(app)
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        token = db.get_or_create_chat(CHAT_ID).get("group_web_token")
        if not token:
            from services.web import get_group_web_token
            token = get_group_web_token(CHAT_ID)
        hdr = {"X-Identity-Token": issue_identity_token(USERS[0]["id"])}
        a, b = await self._two_games()
        r = client.post(f"/api/v1/web/group/{token}/rollcalls/{b.id}/teams",
                        json={"action": "form", "count": 2}, headers=hdr)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(db.get_team_rows(a.id), [])
        # Moving a game-B player through game A's URL is refused.
        _svc().form(a, 2, "Admin", is_admin=True)
        r = client.post(f"/api/v1/web/group/{token}/rollcalls/{a.id}/teams",
                        json={"action": "move", "who_key": f"u{USERS[5]['id']}", "team_no": 1}, headers=hdr)
        self.assertEqual(r.status_code, 409)
        page = client.get(f"/api/v1/web/group/{token}").json()
        by_id = {r["rollcall_id"]: r for r in page["rollcalls"]}
        self.assertEqual(sorted(p["key"] for t in by_id[b.id]["teams"]["teams"] for p in t["players"]),
                         sorted(f"u{u['id']}" for u in USERS[4:8]))


class TestLockedReplacement(_Base):

    async def test_drop_while_locked_is_replaced_from_the_waitlist(self):
        rc = await self._game_with(4)
        rc.inListLimit = 4
        rc.save()
        await self.vote_in(USERS[4])  # waitlisted
        rc = self._rc()
        v = _svc().form(rc, 2, "Admin", is_admin=True, rng=random.Random(7))
        gap_team = _team_of(v, f"u{USERS[2]['id']}")
        _svc().set_locked(rc, True, is_admin=True)

        await self.vote_out(USERS[2])
        v = _svc().view(self._rc())
        self.assertTrue(v["locked"], "a drop doesn't unlock the teams")
        self.assertNotIn(f"u{USERS[2]['id']}", _keys(v))
        self.assertEqual(_team_of(v, f"u{USERS[4]['id']}"), gap_team,
                         "the promoted player takes the dropped player's team")
        self.assertEqual(_sizes(v), [2, 2])


class TestEndSummary(_Base):

    def _summary(self):
        return " ".join(self.sent_texts())

    async def test_teams_replace_the_in_list(self):
        rc = await self._game_with(4, guest="Ann")
        _svc().form(rc, 2, "Admin", is_admin=True)
        await self.vote_out(USERS[5])
        get_mock_bot().reset_mock()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        text = self._summary()
        self.assertIn("Teams:", text)
        self.assertIn("🔴 Team A", text)
        self.assertIn("🔵 Team B", text)
        self.assertIn("Ann", text)
        self.assertNotIn("In:\n", text, "the IN list is replaced by the teams")
        self.assertIn("Out:\n", text, "OUT is still listed")

    async def test_no_teams_keeps_the_usual_summary(self):
        await self._game_with(3)
        get_mock_bot().reset_mock()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        text = self._summary()
        self.assertIn("In:\n1. ", text)
        self.assertNotIn("Teams:", text)

    async def test_teams_turned_off_keeps_the_usual_summary(self):
        rc = await self._game_with(3)
        _svc().form(rc, 2, "Admin", is_admin=True)
        db.update_chat_settings(CHAT_ID, teams_enabled=0)
        get_mock_bot().reset_mock()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        self.assertNotIn("Teams:", self._summary())


class TestVacancies(_Base):
    """A drop leaves an empty spot on that team; the next person IN — by
    vote, /sif or waitlist, however much later — fills it."""

    async def _uneven(self):
        # A deliberately uneven sheet (5 v 2) made by hand, so "smallest
        # team" and "the team that lost a player" are different answers.
        rc = await self._game_with(7)
        _svc().form(rc, 2, "Admin", is_admin=True, rng=random.Random(1))
        for u in USERS[:7]:
            _svc().move(self._rc(), f"u{u['id']}", 1, "Admin")
        for u in USERS[5:7]:
            _svc().move(self._rc(), f"u{u['id']}", 2, "Admin")
        self.assertEqual(_sizes(_svc().view(self._rc())), [5, 2])

    async def test_later_in_vote_fills_the_dropped_players_team(self):
        await self._uneven()
        await self.vote_out(USERS[0])            # leaves Team A
        _svc().move(self._rc(), f"u{USERS[1]['id']}", 1, "Admin")  # an unrelated change in between
        await self.vote_in(USERS[8])             # much later
        v = _svc().view(self._rc())
        self.assertEqual(_team_of(v, f"u{USERS[8]['id']}"), 1, "fills A's empty spot, not the smaller B")
        self.assertEqual(_sizes(v), [5, 2])

    async def test_later_sif_guest_fills_the_spot(self):
        await self._uneven()
        await self.vote_out(USERS[1])
        await self.set_in_for(self.msg("/sif Zed", ADMIN_USER))
        v = _svc().view(self._rc())
        self.assertEqual(_team_of(v, "pzed"), 1)

    async def test_spot_is_filled_once_then_smallest_team(self):
        await self._uneven()
        await self.vote_out(USERS[0])
        await self.vote_in(USERS[8])
        await self.vote_in(USERS[9])
        v = _svc().view(self._rc())
        self.assertEqual(_team_of(v, f"u{USERS[8]['id']}"), 1)
        self.assertEqual(_team_of(v, f"u{USERS[9]['id']}"), 2, "no vacancy left: smallest team")

    async def test_returning_player_goes_back_to_their_team(self):
        await self._uneven()
        await self.vote_out(USERS[6])            # from Team B
        await self.vote_in(USERS[6])
        self.assertEqual(_team_of(_svc().view(self._rc()), f"u{USERS[6]['id']}"), 2)

    async def test_works_while_locked(self):
        await self._uneven()
        _svc().set_locked(self._rc(), True, is_admin=True)
        await self.vote_out(USERS[2])
        await self.vote_in(USERS[8])
        v = _svc().view(self._rc())
        self.assertTrue(v["locked"])
        self.assertEqual(_team_of(v, f"u{USERS[8]['id']}"), 1)

    async def test_reshuffle_clears_old_spots(self):
        await self._uneven()
        await self.vote_out(USERS[0])
        _svc().reshuffle(self._rc(), "Admin", rng=random.Random(2))
        keys = [r["who_key"] for r in db.get_team_rows(self._rc().id)]
        self.assertNotIn(f"u{USERS[0]['id']}", keys)
