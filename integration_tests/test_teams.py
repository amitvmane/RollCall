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
        await self.vote_out(USERS[0])
        _svc().view(self._rc())
        self.assertIn(f"u{USERS[0]['id']}", [r["who_key"] for r in db.get_team_rows(rc.id)])
        # The next change saves the reconciled sheet.
        _svc().move(self._rc(), f"u{USERS[1]['id']}", None, "User2")
        self.assertNotIn(f"u{USERS[0]['id']}", [r["who_key"] for r in db.get_team_rows(rc.id)])

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

    async def test_other_rollcall_is_404(self):
        rc = await self._game_with(2)
        r = self._post(rc.id + 999, {"action": "form", "count": 2}, USERS[0]["id"])
        self.assertEqual(r.status_code, 404)
