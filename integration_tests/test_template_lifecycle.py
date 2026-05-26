"""
Integration: 20-member scheduled template rollcall lifecycle.

Covers the real-world scenario of a weekly template rollcall with a large
group: auto-start, mass voting (IN/OUT/MAYBE/waitlist), comment votes,
proxy users, auto-close, ghost selection, stats accumulation, and
multi-rollcall routing.
"""
import asyncio
import time
from datetime import datetime, timedelta

import pytz

import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot

# Expand to 20 users by extending the base 10 with 10 more
USERS_20 = USERS + [
    {"id": i, "first_name": f"User{i}", "last_name": None, "username": f"user{i}"}
    for i in range(11, 21)
]


class TestTemplateLifecycleFull(IntegrationBase):
    """
    End-to-end: auto-start a template, all 20 members vote, then auto-close.
    """

    async def _setup_template(self, name="weekly", title="Sunday Game",
                               limit=None, event_day="sunday", event_time="17:00"):
        limit_part = f" limit={limit}" if limit else ""
        await self.set_template(self.msg(
            f'/set_template {name} "{title}" event_day={event_day} event_time={event_time}{limit_part}',
            ADMIN_USER
        ))
        return db.get_template(CHAT_ID, name)

    async def test_all_20_users_can_vote_in(self):
        tmpl = await self._setup_template()
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        for user in USERS_20:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 20)
        self.assertEqual(len(rc.waitList), 0)

    async def test_20_users_mixed_votes(self):
        tmpl = await self._setup_template()
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        # First 12 vote IN
        for user in USERS_20[:12]:
            await self.vote_in(user)
        # Next 4 vote OUT
        for user in USERS_20[12:16]:
            await self.vote_out(user)
        # Last 4 vote MAYBE
        for user in USERS_20[16:]:
            await self.vote_maybe(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 12)
        self.assertEqual(len(rc.outList), 4)
        self.assertEqual(len(rc.maybeList), 4)

    async def test_waitlist_with_20_users_limit_12(self):
        tmpl = await self._setup_template(limit=12)
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        for user in USERS_20:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 12)
        self.assertEqual(len(rc.waitList), 8)

    async def test_waitlist_promotion_when_player_drops_out(self):
        tmpl = await self._setup_template(limit=10)
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        for user in USERS_20:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.waitList), 10)
        # 3 players drop out → top 3 waitlisters promoted
        for user in USERS_20[:3]:
            await self.vote_out(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 10)
        self.assertEqual(len(rc.waitList), 7)

    async def test_comment_votes_preserved_in_list(self):
        tmpl = await self._setup_template()
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        self._clear_rate(USERS_20[0])
        await self.in_user(self.msg("/in I'll bring snacks", USERS_20[0]))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 1)
        self.assertEqual(rc.inList[0].comment, "I'll bring snacks")

    async def test_comment_update_does_not_duplicate_user(self):
        tmpl = await self._setup_template()
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        user = USERS_20[0]
        await self.vote_in(user)
        # Vote again with a different comment
        self._clear_rate(user)
        await self.in_user(self.msg("/in Updated comment", user))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 1)
        self.assertEqual(rc.inList[0].comment, "Updated comment")

    async def test_full_roster_panel_text_contains_all_names(self):
        tmpl = await self._setup_template()
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        for user in USERS_20[:10]:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        panel_text = rc.allList().replace("__RCID__", "1")
        for user in USERS_20[:10]:
            self.assertIn(user["first_name"], panel_text)


class TestAutoCloseLifecycle(IntegrationBase):
    """
    Auto-close via check_reminders.check(): rollcall with finalizeDate in the
    past triggers auto-end, sends finish list, prompts for ghost selection.
    """

    async def _start_and_populate(self, n_in=5):
        await self.start_rc("Test Event")
        for user in USERS_20[:n_in]:
            await self.vote_in(user)
        return self.mgr.get_rollcall(CHAT_ID, 0)

    async def _set_finalize_past(self, rc, seconds_ago=90):
        # check() truncates now to the minute boundary, so finalizeDate must be
        # at least one full minute in the past for the >= comparison to be True.
        tz = pytz.timezone("Asia/Kolkata")
        rc.finalizeDate = datetime.now(tz) - timedelta(seconds=seconds_ago)
        rc.save()

    async def test_auto_close_removes_rollcall_from_active(self):
        from check_reminders import check
        rc = await self._start_and_populate(n_in=5)
        await self._set_finalize_past(rc)
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)

    async def test_auto_close_sends_finish_message(self):
        from check_reminders import check
        rc = await self._start_and_populate(n_in=3)
        await self._set_finalize_past(rc)
        get_mock_bot().send_message.reset_mock()
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        texts = self.sent_texts()
        self.assertTrue(any("auto-closed" in t.lower() or "in:" in t.lower() for t in texts))

    async def test_auto_close_triggers_ghost_prompt_when_tracking_enabled(self):
        from check_reminders import check
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        rc = await self._start_and_populate(n_in=4)
        await self._set_finalize_past(rc)
        get_mock_bot().send_message.reset_mock()
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        texts = self.sent_texts()
        self.assertTrue(any("ghost" in t.lower() for t in texts))

    async def test_auto_close_no_ghost_prompt_when_tracking_disabled(self):
        from check_reminders import check
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking off", ADMIN_USER))
        rc = await self._start_and_populate(n_in=3)
        await self._set_finalize_past(rc)
        get_mock_bot().send_message.reset_mock()
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        texts = self.sent_texts()
        self.assertFalse(any("ghost" in t.lower() for t in texts))

    async def test_auto_close_marks_rollcall_inactive_in_db(self):
        from check_reminders import check
        rc = await self._start_and_populate(n_in=3)
        rc_db_id = rc.db_id
        await self._set_finalize_past(rc)
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        row = db.get_rollcall(rc_db_id)
        self.assertEqual(int(row["is_active"]), 0)

    async def test_auto_close_shh_mode_suppresses_finish_message(self):
        from check_reminders import check
        self.mgr.set_shh_mode(CHAT_ID, True)
        rc = await self._start_and_populate(n_in=3)
        await self._set_finalize_past(rc)
        get_mock_bot().send_message.reset_mock()
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        # In shh mode the finish list should NOT be sent, but ghost prompt still fires
        texts = self.sent_texts()
        self.assertFalse(any("in:" in t.lower() for t in texts))
        self.mgr.set_shh_mode(CHAT_ID, False)

    async def test_auto_close_full_ghost_selection_flow(self):
        """Auto-close → ghost_yes → select → ghost_done → counts incremented."""
        from check_reminders import check
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        rc = await self._start_and_populate(n_in=4)
        rc_db_id = rc.db_id
        await self._set_finalize_past(rc)
        rollcalls = self.mgr.get_rollcalls(CHAT_ID)
        await check(rollcalls, "Asia/Kolkata", CHAT_ID)
        # Simulate admin clicking "Yes, select ghosts"
        call = self.call(f"ghost_yes_{rc_db_id}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        # Select first two users as ghosts
        self.bs._ghost_selections[(CHAT_ID, rc_db_id)] = {USERS_20[0]["id"], USERS_20[1]["id"]}
        call = self.call(f"ghost_done_{rc_db_id}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertGreater(db.get_ghost_count(CHAT_ID, USERS_20[0]["id"]), 0)
        self.assertGreater(db.get_ghost_count(CHAT_ID, USERS_20[1]["id"]), 0)
        self.assertEqual(db.get_ghost_count(CHAT_ID, USERS_20[2]["id"]), 0)


class TestProxyVotingOnTemplateRC(IntegrationBase):
    """Proxy users via /sif on a template-started rollcall."""

    async def _auto_start(self):
        await self.set_template(self.msg(
            '/set_template sg "Sunday Game" event_day=sunday event_time=17:00',
            ADMIN_USER
        ))
        tmpl = db.get_template(CHAT_ID, "sg")
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)

    async def test_proxy_in_appears_in_in_list(self):
        await self._auto_start()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        names = [u.name for u in rc.inList]
        self.assertIn("Alice", names)

    async def test_real_and_proxy_coexist(self):
        await self._auto_start()
        await self.vote_in(USERS_20[0])
        await self.set_in_for(self.msg("/sif Bob", ADMIN_USER))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 2)

    async def test_proxy_counts_toward_limit(self):
        await self.set_template(self.msg(
            '/set_template lim "Limited" limit=3 event_day=sunday event_time=17:00',
            ADMIN_USER
        ))
        tmpl = db.get_template(CHAT_ID, "lim")
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)
        await self.vote_in(USERS_20[0])
        await self.vote_in(USERS_20[1])
        await self.vote_in(USERS_20[2])  # fills limit
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 3)
        # Proxy should land on waitlist
        await self.set_in_for(self.msg("/sif Charlie", ADMIN_USER))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.waitList), 1)

    async def test_proxy_out_sends_confirmation(self):
        await self._auto_start()
        await self.set_in_for(self.msg("/sif Dave", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.set_out_for(self.msg("/sof Dave", ADMIN_USER))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.outList), 1)
        self.assertEqual(len(rc.inList), 0)


class TestMultipleTemplateRollcalls(IntegrationBase):
    """Two template rollcalls open simultaneously; routing via ::N suffix."""

    async def _start_two_template_rcs(self):
        for name, title in [("rc1", "Game One"), ("rc2", "Game Two")]:
            await self.set_template(self.msg(
                f'/set_template {name} "{title}" event_day=sunday event_time=17:00',
                ADMIN_USER
            ))
            tmpl = db.get_template(CHAT_ID, name)
            from check_reminders import _auto_start_from_template
            await _auto_start_from_template(CHAT_ID, tmpl)

    async def test_two_template_rcs_both_active(self):
        await self._start_two_template_rcs()
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 2)

    async def test_default_vote_goes_to_first_rc(self):
        await self._start_two_template_rcs()
        await self.vote_in(USERS_20[0])
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 1)
        self.assertEqual(len(rc2.inList), 0)

    async def test_suffix_routes_to_second_rc(self):
        await self._start_two_template_rcs()
        self._clear_rate(USERS_20[0])
        await self.in_user(self.msg("/in ::2", USERS_20[0]))
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 0)
        self.assertEqual(len(rc2.inList), 1)

    async def test_different_users_split_between_two_rcs(self):
        await self._start_two_template_rcs()
        # Users 0-4 join RC1, users 5-9 join RC2
        for user in USERS_20[:5]:
            await self.vote_in(user)
        for user in USERS_20[5:10]:
            self._clear_rate(user)
            await self.in_user(self.msg("/in ::2", user))
        rc1 = self.mgr.get_rollcall(CHAT_ID, 0)
        rc2 = self.mgr.get_rollcall(CHAT_ID, 1)
        self.assertEqual(len(rc1.inList), 5)
        self.assertEqual(len(rc2.inList), 5)

    async def test_end_first_rc_second_renumbers_to_1(self):
        await self._start_two_template_rcs()
        await self.vote_in(USERS_20[0])  # goes to RC1
        self._clear_rate(USERS_20[1])
        await self.in_user(self.msg("/in ::2", USERS_20[1]))  # goes to RC2
        # End RC1
        await self.end_roll_call(self.msg("/erc ::1", ADMIN_USER))
        # RC2 is now at index 0 (renumbered to 1)
        remaining = self.mgr.get_rollcalls(CHAT_ID)
        self.assertEqual(len(remaining), 1)
        self.assertIn(USERS_20[1]["id"], {u.user_id for u in remaining[0].inList})


class TestStatsAccumulationAcrossSessions(IntegrationBase):
    """Stats built up over multiple auto-started template rollcalls."""

    async def _run_session(self, n_in=5, n_out=2):
        await self.start_rc("Session")
        for user in USERS_20[:n_in]:
            await self.vote_in(user)
        for user in USERS_20[n_in:n_in + n_out]:
            await self.vote_out(user)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    async def test_stats_accumulate_over_three_sessions(self):
        for _ in range(3):
            await self._run_session(n_in=5, n_out=0)
        # User0 should have total_in=3 after 3 sessions
        from handlers.stats import build_user_stats_text
        text = await build_user_stats_text(CHAT_ID, USERS_20[0]["id"], "User1")
        self.assertIn("3", text)

    async def test_group_stats_reflect_all_votes(self):
        await self._run_session(n_in=8, n_out=3)
        from handlers.stats import build_group_stats_text
        text = await build_group_stats_text(CHAT_ID)
        self.assertIn("8", text)
        self.assertIn("3", text)

    async def test_leaderboard_shows_top_participant(self):
        for _ in range(3):
            await self._run_session(n_in=10, n_out=0)
        from handlers.stats import build_leaderboard_text
        text = await build_leaderboard_text(CHAT_ID)
        # Leaderboard shows @username when available; users have username=f"user{id}"
        self.assertTrue(
            "user1" in text.lower() or "user2" in text.lower(),
            f"Expected a top user in leaderboard, got: {text}"
        )

    async def test_ghost_stats_after_two_ghosting_sessions(self):
        for i in range(2):
            db.increment_ghost_count(CHAT_ID, USERS_20[0]["id"], USERS_20[0]["first_name"])
        db.increment_ghost_count(CHAT_ID, USERS_20[1]["id"], USERS_20[1]["first_name"])
        from handlers.stats import build_ghost_stats_text
        text = await build_ghost_stats_text(CHAT_ID, self.mgr)
        self.assertIn("2", text)
        self.assertIn("User1", text)

    async def test_stats_command_group_scope_sends_message(self):
        await self._run_session(n_in=5, n_out=2)
        get_mock_bot().send_message.reset_mock()
        from handlers.stats import stats_command
        await stats_command(self.msg("/stats group", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)


class TestWaitlistEdgeCases(IntegrationBase):
    """Edge cases with waitlist and 20 members on a limited template rollcall."""

    async def _template_rc_with_limit(self, limit):
        await self.set_template(self.msg(
            f'/set_template game "Game" limit={limit} event_day=sunday event_time=17:00',
            ADMIN_USER
        ))
        tmpl = db.get_template(CHAT_ID, "game")
        from check_reminders import _auto_start_from_template
        await _auto_start_from_template(CHAT_ID, tmpl)

    async def test_all_20_join_limit_15_correct_counts(self):
        await self._template_rc_with_limit(15)
        for user in USERS_20:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 15)
        self.assertEqual(len(rc.waitList), 5)

    async def test_waitlist_order_preserved_on_promotion(self):
        await self._template_rc_with_limit(2)
        for user in USERS_20[:5]:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.waitList), 3)
        # First waiter should be USERS_20[2] (joined 3rd)
        first_waiter_id = rc.waitList[0].user_id
        self.assertEqual(first_waiter_id, USERS_20[2]["id"])
        # Player 0 drops out → first waiter gets promoted
        await self.vote_out(USERS_20[0])
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        in_ids = {u.user_id for u in rc.inList}
        self.assertIn(USERS_20[2]["id"], in_ids)

    async def test_multiple_dropouts_promote_in_order(self):
        await self._template_rc_with_limit(3)
        for user in USERS_20[:7]:
            await self.vote_in(user)
        # 3 in, 4 waiting
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.waitList), 4)
        # All 3 original players drop out → top 3 waiters get in
        for user in USERS_20[:3]:
            await self.vote_out(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 3)
        self.assertEqual(len(rc.waitList), 1)
        # The 3 promoted should be users 3, 4, 5 (first three who waited)
        in_ids = {u.user_id for u in rc.inList}
        for user in USERS_20[3:6]:
            self.assertIn(user["id"], in_ids)

    async def test_maybe_from_waitlist_frees_slot(self):
        await self._template_rc_with_limit(3)
        for user in USERS_20[:5]:
            await self.vote_in(user)
        # 3 in, 2 waiting
        # First IN user switches to MAYBE → frees slot → top waiter promoted
        await self.vote_maybe(USERS_20[0])
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 3)
        in_ids = {u.user_id for u in rc.inList}
        self.assertIn(USERS_20[3]["id"], in_ids)


class TestGhostTrackingWithLargeGroup(IntegrationBase):
    """Ghost reconfirmation and marking with multiple users."""

    async def test_multiple_ghost_users_all_need_reconf(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        # Mark 3 users as ghosts
        for user in USERS_20[:3]:
            db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        # All 3 should trigger reconf dialog when voting IN
        for user in USERS_20[:3]:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 0)

    async def test_clean_users_vote_in_despite_ghosts_present(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        # Ghost users 0-2; clean users 3-9
        for user in USERS_20[:3]:
            db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        # Clean users vote in normally
        for user in USERS_20[3:10]:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 7)

    async def test_ghost_reconf_all_three_commit(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        for user in USERS_20[:3]:
            db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        for user in USERS_20[:3]:
            await self.vote_in(user)
        # All 3 confirm commitment
        for user in USERS_20[:3]:
            call = self.call(f"reconf_in_0_{user['id']}", user)
            await self.ghost_callback_handler(call)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(len(rc.inList), 3)

    async def test_clear_absent_for_all_ghosted_users(self):
        for user in USERS_20[:5]:
            db.increment_ghost_count(CHAT_ID, user["id"], user["first_name"])
        # Clear each user's ghost count
        for user in USERS_20[:5]:
            await self.clear_absent(
                self.msg(f"/clear_absent {user['first_name']}", ADMIN_USER)
            )
        for user in USERS_20[:5]:
            self.assertEqual(db.get_ghost_count(CHAT_ID, user["id"]), 0)


class TestJoinOrderPreservation(IntegrationBase):
    """
    Order is a core guarantee of RollCall: users always appear in the list in
    the exact sequence they voted.  These tests verify that invariant is
    maintained across every path that can mutate the lists.
    """

    # ── helpers ────────────────────────────────────────────────────────────

    def _in_order(self):
        return [u.user_id for u in self.mgr.get_rollcall(CHAT_ID, 0).inList]

    def _wait_order(self):
        return [u.user_id for u in self.mgr.get_rollcall(CHAT_ID, 0).waitList]

    def _out_order(self):
        return [u.user_id for u in self.mgr.get_rollcall(CHAT_ID, 0).outList]

    def _maybe_order(self):
        return [u.user_id for u in self.mgr.get_rollcall(CHAT_ID, 0).maybeList]

    # ── IN list order ───────────────────────────────────────────────────────

    async def test_in_list_preserves_join_order(self):
        """10 users vote IN; list must reflect exact vote sequence."""
        await self.start_rc()
        for user in USERS_20[:10]:
            await self.vote_in(user)
        expected = [u["id"] for u in USERS_20[:10]]
        self.assertEqual(self._in_order(), expected)

    async def test_in_list_order_preserved_after_reload(self):
        """Order must survive a cache eviction + DB reload."""
        await self.start_rc()
        for user in USERS_20[:8]:
            await self.vote_in(user)
        # Force reload from DB
        self.mgr.reload_chat(CHAT_ID)
        expected = [u["id"] for u in USERS_20[:8]]
        self.assertEqual(self._in_order(), expected)

    async def test_late_voter_appended_at_end_of_in_list(self):
        """A user who joins later goes to the end, not the front."""
        await self.start_rc()
        for user in USERS_20[:5]:
            await self.vote_in(user)
        # User 5 joins last
        await self.vote_in(USERS_20[5])
        self.assertEqual(self._in_order()[-1], USERS_20[5]["id"])

    async def test_out_then_back_in_goes_to_end(self):
        """Re-joining after voting OUT earns a new slot at the tail."""
        await self.start_rc()
        for user in USERS_20[:5]:
            await self.vote_in(user)
        # User 0 drops out then re-joins
        await self.vote_out(USERS_20[0])
        await self.vote_in(USERS_20[0])
        order = self._in_order()
        self.assertEqual(order[-1], USERS_20[0]["id"])
        # Original order for users 1-4 is unchanged
        self.assertEqual(order[:4], [u["id"] for u in USERS_20[1:5]])

    async def test_maybe_then_in_goes_to_end(self):
        """Switching from MAYBE to IN also goes to the back of the IN list."""
        await self.start_rc()
        for user in USERS_20[:4]:
            await self.vote_in(user)
        # User 4 votes MAYBE, then IN
        await self.vote_maybe(USERS_20[4])
        await self.vote_in(USERS_20[4])
        order = self._in_order()
        self.assertEqual(order[-1], USERS_20[4]["id"])

    # ── WAITLIST order ──────────────────────────────────────────────────────

    async def test_waitlist_preserves_join_order(self):
        """Users join waitlist in the order they hit the limit."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 5", ADMIN_USER))
        for user in USERS_20[:10]:
            await self.vote_in(user)
        expected = [u["id"] for u in USERS_20[5:10]]
        self.assertEqual(self._wait_order(), expected)

    async def test_first_waiter_promoted_when_slot_opens(self):
        """The oldest waiter (position 1) is always promoted first."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for user in USERS_20[:6]:
            await self.vote_in(user)
        # Waitlist: users 3, 4, 5 in that order
        self.assertEqual(self._wait_order()[0], USERS_20[3]["id"])
        # User 0 drops out → user 3 promoted
        await self.vote_out(USERS_20[0])
        self.assertEqual(self._in_order()[-1], USERS_20[3]["id"])
        self.assertEqual(self._wait_order()[0], USERS_20[4]["id"])

    async def test_waitlist_order_preserved_after_partial_promotion(self):
        """Remaining waiters keep their relative order after some are promoted."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        for user in USERS_20[:7]:
            await self.vote_in(user)
        # Waitlist: users 2-6 (5 waiting)
        original_wait = self._wait_order()
        # Promote top 2
        await self.vote_out(USERS_20[0])
        await self.vote_out(USERS_20[1])
        remaining_wait = self._wait_order()
        # Remaining waiters are still in original relative order
        self.assertEqual(remaining_wait, original_wait[2:])

    async def test_limit_increase_promotes_in_waitlist_order(self):
        """Raising the limit promotes waitlisters in FIFO order."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for user in USERS_20[:8]:
            await self.vote_in(user)
        # Waitlist: users 3-7 in order
        waitlist_ids_before = self._wait_order()
        # Raise limit to 6 → top 3 waiters promoted in order
        await self.wait_limit(self.msg("/sl 6", ADMIN_USER))
        newly_in = self._in_order()[3:]  # positions 3-5 are the newly promoted
        self.assertEqual(newly_in, waitlist_ids_before[:3])

    # ── OUT / MAYBE list order ──────────────────────────────────────────────

    async def test_out_list_preserves_vote_order(self):
        """OUT list respects the order users voted out."""
        await self.start_rc()
        for user in USERS_20[:6]:
            await self.vote_out(user)
        expected = [u["id"] for u in USERS_20[:6]]
        self.assertEqual(self._out_order(), expected)

    async def test_maybe_list_preserves_vote_order(self):
        await self.start_rc()
        for user in USERS_20[:6]:
            await self.vote_maybe(user)
        expected = [u["id"] for u in USERS_20[:6]]
        self.assertEqual(self._maybe_order(), expected)

    # ── Panel text order ────────────────────────────────────────────────────

    async def test_panel_text_lists_in_users_in_order(self):
        """allList() must list IN users in the same sequence as inList."""
        await self.start_rc()
        join_seq = [USERS_20[i] for i in [3, 0, 7, 2, 5]]
        for user in join_seq:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        panel = rc.allList().replace("__RCID__", "1")
        in_section = panel.split("Out:")[0]  # everything before Out:
        # Find positions of each name in the panel text
        positions = [in_section.find(u["first_name"]) for u in join_seq]
        self.assertEqual(positions, sorted(positions),
                         "Names appear in panel in a different order than they joined")

    async def test_finish_list_maintains_in_order(self):
        """finishList() (used by /erc) must also honour join order."""
        await self.start_rc()
        join_seq = [USERS_20[i] for i in [4, 1, 9, 0]]
        for user in join_seq:
            await self.vote_in(user)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        finish = rc.finishList().replace("__RCID__", "1")
        in_section = finish.split("Out:")[0]
        positions = [in_section.find(u["first_name"]) for u in join_seq]
        self.assertEqual(positions, sorted(positions),
                         "Names in finishList appear out of order")

    # ── Order across DB reload ──────────────────────────────────────────────

    async def test_waitlist_order_survives_cache_reload(self):
        """Waitlist join order must be restored correctly from DB on reload."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for user in USERS_20[:7]:
            await self.vote_in(user)
        expected_wait = self._wait_order()
        self.mgr.reload_chat(CHAT_ID)
        self.assertEqual(self._wait_order(), expected_wait)

    async def test_mixed_status_order_survives_reload(self):
        """IN / OUT / MAYBE orders all survive a cache reload."""
        await self.start_rc()
        for user in USERS_20[:4]:
            await self.vote_in(user)
        for user in USERS_20[4:7]:
            await self.vote_out(user)
        for user in USERS_20[7:10]:
            await self.vote_maybe(user)
        exp_in = self._in_order()
        exp_out = self._out_order()
        exp_maybe = self._maybe_order()
        self.mgr.reload_chat(CHAT_ID)
        self.assertEqual(self._in_order(), exp_in)
        self.assertEqual(self._out_order(), exp_out)
        self.assertEqual(self._maybe_order(), exp_maybe)

    # ── Order with inline panel buttons ─────────────────────────────────────

    async def test_inline_button_votes_preserve_order(self):
        """Votes via inline buttons must respect the same join-order rules."""
        await self.start_rc()
        for user in USERS_20[:5]:
            await self.callback_handler(self.call("btn_in_1", user))
        expected = [u["id"] for u in USERS_20[:5]]
        self.assertEqual(self._in_order(), expected)

    async def test_inline_out_then_in_goes_to_end(self):
        """Inline OUT then IN puts the user at the back, same as command flow."""
        await self.start_rc()
        for user in USERS_20[:4]:
            await self.callback_handler(self.call("btn_in_1", user))
        # Each inline-button call goes through _is_rate_limited which stamps a new
        # timestamp, so clear before both OUT and IN.
        self._clear_rate(USERS_20[0])
        await self.callback_handler(self.call("btn_out_1", USERS_20[0]))
        self._clear_rate(USERS_20[0])
        await self.callback_handler(self.call("btn_in_1", USERS_20[0]))
        order = self._in_order()
        self.assertEqual(order[-1], USERS_20[0]["id"])
