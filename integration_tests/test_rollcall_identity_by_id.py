"""
Regression: actions aimed at a rollcall must act on THAT rollcall.

Rollcalls used to be addressed by list position captured before the chat
lock. Ending or cancelling an earlier rollcall renumbers the rest, so a
panel tap, a confirmation button or a web page prepared before that acted on
whichever rollcall slid into the old position. Everything below now resolves
by rollcall db id inside the lock (services.common.resolve_rollcall_by_id_or_raise).

Also covers the voting/lifecycle fixes that shipped with it: stale panels lose
their buttons on /erc and /xrc, /set_status's re-add fallback is gone, the
/sif ghost-confirm is issuer-only, a verified web vote only replaces the
voter's own web-guest entry, move→IN on a full list is refused, a proxy keeps
its owner across a move, and limit changes keep the waitlist honest.
"""
import asyncio

from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot
import db


def _buttons(markup):
    return [getattr(b, "callback_data", "") for b in getattr(markup, "keyboard", []) or []]


class _Base(IntegrationBase):
    async def three_rollcalls(self):
        a = await self.start_rc("Alpha")
        await self.start_roll_call(self.msg("/src Bravo", ADMIN_USER))
        await self.start_roll_call(self.msg("/src Charlie", ADMIN_USER))
        return a, self.rc(1), self.rc(2)

    def names(self, rc, lst="inList"):
        return [u.name for u in getattr(rc, lst)]

    def answers(self):
        out = []
        for args, kwargs in get_mock_bot().answer_callback_query.call_args_list:
            out.append(str(args[1]) if len(args) > 1 else str(kwargs.get("text", "")))
        return out

    def stripped_msg_ids(self):
        ids = []
        for args, kwargs in get_mock_bot().edit_message_reply_markup.call_args_list:
            markup = kwargs.get("reply_markup", args[2] if len(args) > 2 else "x")
            if markup is None:
                ids.append(args[1] if len(args) > 1 else kwargs.get("message_id"))
        return ids


# ── Finding 1: stale panels ──────────────────────────────────────────────────

class TestPanelButtonsCarryRollcallId(_Base):

    async def test_keyboard_buttons_carry_the_db_id(self):
        a = await self.start_rc("Alpha")
        markup = await self.get_status_keyboard(1, CHAT_ID)
        datas = [d for d in _buttons(markup) if d]
        self.assertTrue(datas)
        for d in datas:
            self.assertTrue(d.endswith(f"_{a.id}"), d)

    async def test_erc_strips_the_ended_panels_buttons(self):
        a = await self.start_rc("Alpha")
        await self.start_roll_call(self.msg("/src Bravo", ADMIN_USER))
        panel_a = a.panel_msg_id
        self.assertIsInstance(panel_a, int)
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        self.assertIn(panel_a, self.stripped_msg_ids())

    async def test_xrc_strips_the_cancelled_panels_buttons(self):
        a = await self.start_rc("Alpha")
        await self.start_roll_call(self.msg("/src Bravo", ADMIN_USER))
        panel_a = a.panel_msg_id
        await self.cancel_roll_call(self.msg("/xrc ::1", ADMIN_USER))
        self.assertIn(panel_a, self.stripped_msg_ids())

    async def test_tap_on_ended_rollcalls_panel_does_not_vote_in_the_next(self):
        a = await self.start_rc("Alpha")
        await self.start_roll_call(self.msg("/src Bravo", ADMIN_USER))
        a_id = a.id
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        bravo = self.rc(0)
        self.assertEqual(bravo.title, "Bravo")
        self._clear_rate(USERS[0])
        await self.callback_handler(self.call(f"btn_in_1_{a_id}", USERS[0], message_id=a.panel_msg_id))
        self.assertEqual(self.names(self.rc(0)), [], "the vote leaked into Bravo")
        self.assertTrue(any("closed" in t for t in self.answers()))

    async def test_legacy_button_on_closed_panel_is_refused(self):
        """Panels posted before the id was added still say btn_in_1."""
        a = await self.start_rc("Alpha")
        await self.start_roll_call(self.msg("/src Bravo", ADMIN_USER))
        panel_a = a.panel_msg_id
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        self._clear_rate(USERS[0])
        await self.callback_handler(self.call("btn_in_1", USERS[0], message_id=panel_a))
        self.assertEqual(self.names(self.rc(0)), [])

    async def test_legacy_button_follows_its_own_panel_after_renumbering(self):
        await self.start_rc("Alpha")
        await self.start_roll_call(self.msg("/src Bravo", ADMIN_USER))
        panel_b = self.rc(1).panel_msg_id
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        # Bravo's old panel said "#2"; Bravo is #1 now and there is no #2.
        self._clear_rate(USERS[0])
        await self.callback_handler(self.call("btn_in_2", USERS[0], message_id=panel_b))
        self.assertEqual(self.names(self.rc(0)), ["User1"])

    async def test_vote_by_id_lands_after_renumbering(self):
        _, b, _ = await self.three_rollcalls()
        b_id = b.id
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        self._clear_rate(USERS[0])
        await self.callback_handler(self.call(f"btn_in_2_{b_id}", USERS[0]))
        self.assertEqual(self.names(self.rc(0)), ["User1"])   # Bravo, now #1
        self.assertEqual(self.names(self.rc(1)), [])          # Charlie, now #2


# ── Finding 2: confirmation flows ────────────────────────────────────────────

class TestConfirmationsResolveById(_Base):

    async def test_end_confirm_ends_the_rollcall_it_was_asked_about(self):
        _, b, _ = await self.three_rollcalls()
        b_id = b.id
        await self.callback_handler(self.call(f"btn_end_2_{b_id}", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))   # Alpha
        await self.callback_handler(self.call(f"btn_endconfirm_2_{b_id}", ADMIN_USER))
        titles = [r.title for r in self.mgr.get_rollcalls(CHAT_ID)]
        self.assertEqual(titles, ["Charlie"], "end-confirm closed the wrong rollcall")

    async def test_delete_user_confirm_targets_the_original_rollcall(self):
        _, b, c = await self.three_rollcalls()
        u = USERS[0]
        await self.vote_in(u, rc_suffix="::2")
        await self.vote_in(u, rc_suffix="::3")
        await self.delete_user(self.msg("/delete_user User1 ::2", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        await self.ghost_callback_handler(self.call(f"delconf_yes_1_{ADMIN_USER['id']}", ADMIN_USER))
        bravo, charlie = self.rc(0), self.rc(1)
        self.assertEqual((bravo.title, charlie.title), ("Bravo", "Charlie"))
        self.assertEqual(self.names(bravo), [])
        self.assertEqual(self.names(charlie), ["User1"], "deleted from the wrong rollcall")

    async def test_set_status_confirm_targets_the_original_rollcall(self):
        await self.three_rollcalls()
        u = USERS[0]
        await self.vote_in(u, rc_suffix="::2")
        await self.vote_in(u, rc_suffix="::3")
        await self.set_status_override(self.msg("/set_status User1 out ::2", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        await self.ghost_callback_handler(self.call(f"ovrd_yes_1_{ADMIN_USER['id']}", ADMIN_USER))
        bravo, charlie = self.rc(0), self.rc(1)
        self.assertEqual(self.names(bravo, "outList"), ["User1"])
        self.assertEqual(self.names(charlie), ["User1"], "moved in the wrong rollcall")

    async def test_set_status_confirm_never_re_adds_a_removed_member(self):
        await self.start_rc("Alpha")
        u = USERS[0]
        await self.vote_in(u)
        await self.set_status_override(self.msg("/set_status User1 out", ADMIN_USER))
        from services import admin as admin_svc
        async with self.mgr.get_chat_write_lock(CHAT_ID):
            admin_svc.delete_user_from_rollcall(CHAT_ID, 0, "User1", ADMIN_USER["id"], "Admin")
        await self.ghost_callback_handler(self.call(f"ovrd_yes_0_{ADMIN_USER['id']}", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(self.names(rc) + self.names(rc, "outList") + self.names(rc, "waitList"), [])

    async def test_ghost_reconfirm_votes_in_the_rollcall_it_asked_about(self):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        u = USERS[0]
        db.increment_ghost_count(CHAT_ID, u["id"], u["first_name"])
        await self.three_rollcalls()
        await self.vote_in(u, rc_suffix="::2")
        reconf = [d for d in _buttons(self.last_sent_markup()) if d.startswith("reconf_in_")]
        self.assertEqual(len(reconf), 1)
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        await self.ghost_callback_handler(self.call(reconf[0], u))
        self.assertEqual(self.names(self.rc(0)), ["User1"])   # Bravo
        self.assertEqual(self.names(self.rc(1)), [])          # Charlie

    async def test_in_while_reconf_pending_replies_with_a_hint(self):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        u = USERS[0]
        db.increment_ghost_count(CHAT_ID, u["id"], u["first_name"])
        await self.start_rc()
        await self.vote_in(u)
        await self.vote_in(u)
        self.assertIn("already have a confirmation waiting", self.sent_texts()[-1])


class TestProxyAddConfirm(_Base):

    async def _ghosted_proxy_prompt(self, name="Guest"):
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, -1, name, proxy_name=name)
        await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        datas = [d for d in _buttons(self.last_sent_markup()) if d.startswith("proxy_add_")]
        self.assertEqual(len(datas), 1)
        return datas[0]

    async def asyncSetUp(self):
        self.bs._pending_proxy_add.clear()

    async def test_only_the_issuing_admin_can_confirm(self):
        await self.start_rc()
        data = await self._ghosted_proxy_prompt()
        await self.ghost_callback_handler(self.call(data, USERS[0]))
        self.assertEqual(self.names(self.rc(0)), [])
        await self.ghost_callback_handler(self.call(data, ADMIN_USER))
        self.assertEqual(self.names(self.rc(0)), ["Guest"])
        self.assertEqual(self.rc(0).get_proxy_owner("Guest"), ADMIN_USER["id"])

    async def test_full_list_reports_waiting_not_in(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 1", ADMIN_USER))
        await self.vote_in(USERS[0])
        data = await self._ghosted_proxy_prompt()
        await self.ghost_callback_handler(self.call(data, ADMIN_USER))
        self.assertEqual(self.names(self.rc(0), "waitList"), ["Guest"])
        self.assertTrue(any("WAITING" in t for t in self.edited_texts()))


# ── Finding 6: move to IN ────────────────────────────────────────────────────

class TestMoveToIn(_Base):

    async def test_move_to_full_in_list_is_refused_and_changes_nothing(self):
        from exceptions import incorrectParameter
        from services import admin as admin_svc
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 1", ADMIN_USER))
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])            # waitlist #1
        await self.vote_in(USERS[2])            # waitlist #2
        with self.assertRaises(incorrectParameter):
            admin_svc.set_user_status(CHAT_ID, 0, "User2", "in", ADMIN_USER["id"], "Admin")
        rc = self.rc(0)
        rc._load_users_from_db()
        self.assertEqual(self.names(rc), ["User1"])
        self.assertEqual(self.names(rc, "waitList"), ["User2", "User3"], "waitlister lost their place")

    async def test_moving_a_proxy_keeps_its_owner(self):
        from services import admin as admin_svc
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Guest", ADMIN_USER))
        admin_svc.set_user_status(CHAT_ID, 0, "Guest", "out", ADMIN_USER["id"], "Admin")
        rc = self.rc(0)
        rc._load_users_from_db()
        self.assertEqual(self.names(rc, "outList"), ["Guest"])
        self.assertEqual(rc.get_proxy_owner("Guest"), ADMIN_USER["id"])

    async def test_namesakes_are_addressable_by_user_id(self):
        from services import admin as admin_svc
        await self.start_rc()
        a = {"id": 501, "first_name": "Rahul", "last_name": None, "username": None}
        b = {"id": 502, "first_name": "Rahul", "last_name": None, "username": None}
        await self.vote_in(a)
        await self.vote_in(b)
        admin_svc.set_user_status(CHAT_ID, 0, "", "out", ADMIN_USER["id"], "Admin", user_id=502)
        rc = self.rc(0)
        self.assertEqual([u.user_id for u in rc.inList], [501])
        self.assertEqual([u.user_id for u in rc.outList], [502])
        admin_svc.delete_user_from_rollcall(CHAT_ID, 0, "", ADMIN_USER["id"], "Admin", user_id=501)
        self.assertEqual([u.user_id for u in self.rc(0).inList], [])


# ── Findings 7 & 8: limit changes ────────────────────────────────────────────

class TestLimitChanges(_Base):

    async def test_clearing_the_limit_via_rest_service_promotes_waiters(self):
        from services import settings as settings_svc
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 1", ADMIN_USER))
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        settings_svc.set_rollcall_limit(CHAT_ID, 0, ADMIN_USER["id"], "Admin", 0)
        rc = self.rc(0)
        self.assertEqual(self.names(rc), ["User1", "User2"])
        self.assertEqual(self.names(rc, "waitList"), [])
        rc._load_users_from_db()
        self.assertEqual(self.names(rc), ["User1", "User2"])

    async def test_lowering_the_limit_puts_demoted_members_ahead_of_waiters(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 3", ADMIN_USER))
        for u in USERS[:4]:
            await self.vote_in(u)                 # User4 waits
        await self.set_in_for(self.msg("/sif Guest", ADMIN_USER))   # waits too
        await self.wait_limit(self.msg("/set_limit 1", ADMIN_USER))
        rc = self.rc(0)
        expected = ["User2", "User3", "User4", "Guest"]
        self.assertEqual(self.names(rc, "waitList"), expected)
        rc._load_users_from_db()
        self.assertEqual(self.names(rc, "waitList"), expected, "order not persisted")

    async def test_demoted_proxy_is_announced(self):
        await self.start_rc()
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg("/sif Guest", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.wait_limit(self.msg("/set_limit 1", ADMIN_USER))
        self.assertEqual(self.names(self.rc(0), "waitList"), ["Guest"])
        self.assertTrue(any(t.startswith("Guest moved from IN to WAITING") for t in self.sent_texts()),
                        self.sent_texts())


# ── Findings 3 & 5: magic-link web votes ─────────────────────────────────────

class TestWebVoteByToken(_Base):

    async def _token(self, rc):
        from services.web import _ensure_web_token
        return _ensure_web_token(rc)

    async def test_web_vote_lands_in_its_own_rollcall_after_renumbering(self):
        from services.web import vote_by_token
        _, b, _ = await self.three_rollcalls()
        token = await self._token(b)
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        await vote_by_token(token, "Walk-in", "in")
        self.assertEqual(self.names(self.rc(0)), ["Walk-in"])   # Bravo
        self.assertEqual(self.names(self.rc(1)), [])            # Charlie

    async def test_verified_vote_keeps_an_admin_added_namesake(self):
        from services.web import vote_by_token
        rc = await self.start_rc()
        token = await self._token(rc)
        await self.set_in_for(self.msg("/sif Priya", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, -1, "Priya", proxy_name="Priya")
        db.upsert_chat_member(CHAT_ID, 777, "Priya", "priya_real")
        out = await vote_by_token(token, "Priya", "in", tg_user_id=777)
        self.assertIsNone(out["replaced_guest"])
        names = self.names(self.rc(0))
        self.assertIn("Priya", names)                     # the guest
        self.assertEqual(len(names), 2)
        self.assertEqual(db.get_ghost_count_by_proxy_name(CHAT_ID, "Priya"), 1)

    async def test_verified_vote_never_matches_on_the_typed_name(self):
        from services.web import vote_by_token
        rc = await self.start_rc()
        token = await self._token(rc)
        await vote_by_token(token, "Someone", "in")       # a web guest
        db.upsert_chat_member(CHAT_ID, 778, "Mallory", "mallory")
        await vote_by_token(token, "Someone", "out", tg_user_id=778)
        self.assertIn("Someone", self.names(self.rc(0)))

    async def test_verified_vote_replaces_own_web_guest_entry(self):
        from services.web import vote_by_token
        rc = await self.start_rc()
        token = await self._token(rc)
        await vote_by_token(token, "Priya", "in")         # before verifying
        db.increment_ghost_count(CHAT_ID, -1, "Priya", proxy_name="Priya")
        db.upsert_chat_member(CHAT_ID, 779, "Priya", "priya")
        out = await vote_by_token(token, "Priya", "in", tg_user_id=779)
        self.assertEqual(out["replaced_guest"], {"name": "Priya", "status": "in"})
        rc = self.rc(0)
        self.assertEqual([u.user_id for u in rc.inList], [779])
        # A vote must never wipe ghost history as a side effect.
        self.assertEqual(db.get_ghost_count_by_proxy_name(CHAT_ID, "Priya"), 1)
        actions = [r.get("action_type") for r in db.get_admin_audit_log(CHAT_ID, limit=50)]
        self.assertIn("web_guest_replaced", actions)

    async def test_replacing_a_guest_frees_the_slot_for_the_waitlist(self):
        from services.web import vote_by_token
        rc = await self.start_rc()
        token = await self._token(rc)
        await self.wait_limit(self.msg("/set_limit 1", ADMIN_USER))
        await vote_by_token(token, "Priya", "in")         # IN as guest
        await self.vote_in(USERS[0])                      # waiting
        db.upsert_chat_member(CHAT_ID, 780, "Priya", "priya")
        out = await vote_by_token(token, "Priya", "out", tg_user_id=780)
        self.assertEqual([p["user_id"] for p in out["promoted"]], [USERS[0]["id"]])
        self.assertEqual(self.names(self.rc(0)), ["User1"])
