"""
Integration regression tests for the v7.8 bug fixes.

Each test corresponds to a real production bug we shipped (and in some cases
re-shipped). Keep these — they're the closest thing we have to a staging
environment for catching regressions of these specific behaviors.

Coverage:
- Ghost decrement on confirmed attendance, across all three finalize paths
  (ghost_no, ghost_done with empty selection, ghost_done with selection).
  Includes floor-at-zero invariant.
- Reconfirmation prompt no longer duplicates on /in spam.
- Reconfirmation prompt is skipped entirely when user is already in IN list.
- Duplicate /in raises alreadyInList with correct text, not duplicateProxy.
- /buzz spares voters from ALL active rollcalls when ::N is not given.
- /buzz with explicit ::N retains the per-rollcall narrow filter.
- _pending_reconf entries with a _ts field are pruned by _prune_pending.
"""
import time
from unittest.mock import patch

from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot

import db


class TestGhostDecrementAllPaths(IntegrationBase):
    """Three finalize paths must each decrement count for IN users who attended."""

    async def _setup_session_with_in_users(self, in_users, ghost_seed=None):
        """Start a rollcall, /in the given users, end it. Optionally seed ghost counts first.

        We set absent_limit to 100 so seeded ghost counts (which would otherwise
        trigger the reconf warning at limit=1) don't block /in from going through.
        The decrement logic under test is independent of the limit value.
        """
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.set_absent_limit(self.msg("/absent 100", ADMIN_USER))
        if ghost_seed:
            for uid, count in ghost_seed.items():
                for _ in range(count):
                    db.increment_ghost_count(CHAT_ID, uid, f"User{uid}")
        await self.start_rc()
        for u in in_users:
            await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        sessions = db.get_unprocessed_rollcalls(CHAT_ID, days=30)
        return sessions[0]["id"]

    async def test_ghost_no_decrements_all_in_users(self):
        """ghost_no callback ('everyone showed up') → all IN users -1 (floored at 0)."""
        u1, u2 = USERS[0], USERS[1]
        rc_db_id = await self._setup_session_with_in_users(
            [u1, u2], ghost_seed={u1["id"]: 2, u2["id"]: 3}
        )
        call = self.call(f"ghost_no_{rc_db_id}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(db.get_ghost_count(CHAT_ID, u1["id"]), 1)
        self.assertEqual(db.get_ghost_count(CHAT_ID, u2["id"]), 2)

    async def test_ghost_no_floors_at_zero(self):
        """Users with no prior ghosts stay at 0, never go negative."""
        u1 = USERS[0]
        rc_db_id = await self._setup_session_with_in_users([u1])  # no seed
        call = self.call(f"ghost_no_{rc_db_id}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(db.get_ghost_count(CHAT_ID, u1["id"]), 0)

    async def test_ghost_done_empty_selection_decrements_all(self):
        """ghost_done with no one selected = same outcome as ghost_no."""
        u1, u2 = USERS[0], USERS[1]
        rc_db_id = await self._setup_session_with_in_users(
            [u1, u2], ghost_seed={u1["id"]: 1, u2["id"]: 1}
        )
        # Don't seed _ghost_selections — empty
        call = self.call(f"ghost_done_{rc_db_id}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(db.get_ghost_count(CHAT_ID, u1["id"]), 0)
        self.assertEqual(db.get_ghost_count(CHAT_ID, u2["id"]), 0)

    async def test_ghost_done_with_selection_decrements_only_non_selected(self):
        """Selected users get +1; non-selected IN users get -1."""
        u1, u2 = USERS[0], USERS[1]
        rc_db_id = await self._setup_session_with_in_users(
            [u1, u2], ghost_seed={u1["id"]: 1, u2["id"]: 2}
        )
        # Mark u1 as ghost — u2 attended
        self.bs._ghost_selections[(CHAT_ID, rc_db_id)] = {u1["id"]}
        call = self.call(f"ghost_done_{rc_db_id}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(db.get_ghost_count(CHAT_ID, u1["id"]), 2, "u1 ghosted → +1")
        self.assertEqual(db.get_ghost_count(CHAT_ID, u2["id"]), 1, "u2 attended → -1")

    async def test_repeat_attendance_drains_count_to_zero(self):
        """Real-world: user ghosted once, attends 3 sessions → count goes 1 → 0 → 0 → 0."""
        u1 = USERS[0]
        # Initial ghost
        rc_db_id_1 = await self._setup_session_with_in_users([u1])
        self.bs._ghost_selections[(CHAT_ID, rc_db_id_1)] = {u1["id"]}
        await self.ghost_callback_handler(self.call(f"ghost_done_{rc_db_id_1}", ADMIN_USER))
        self.assertEqual(db.get_ghost_count(CHAT_ID, u1["id"]), 1)
        # 3 attendances via ghost_no
        for _ in range(3):
            rc_db_id = await self._setup_session_with_in_users([u1])
            await self.ghost_callback_handler(self.call(f"ghost_no_{rc_db_id}", ADMIN_USER))
        self.assertEqual(db.get_ghost_count(CHAT_ID, u1["id"]), 0,
                         "After 1 ghost + 3 attendances, count should be max(0, 1-3) = 0")


class TestReconfPromptSuppression(IntegrationBase):
    """Ghost reconfirmation prompt must not stack on /in spam or fire when already IN."""

    async def test_second_in_with_pending_reconf_sends_no_new_prompt(self):
        u = USERS[0]
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, u["id"], u["first_name"])
        await self.start_rc()
        # First /in triggers the warning
        await self.vote_in(u)
        first_count = self.sent_count()
        # Second /in with pending entry — should be silent no-op
        await self.vote_in(u)
        self.assertEqual(self.sent_count(), first_count,
                         "Repeat /in while reconf pending should not send another message")
        # And user is still NOT in IN list (commitment not made yet)
        self.assertEqual(len(self.rc(0).inList), 0)

    async def test_in_after_committing_does_not_re_warn(self):
        u = USERS[0]
        await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, u["id"], u["first_name"])
        await self.start_rc()
        await self.vote_in(u)
        # Commit
        await self.ghost_callback_handler(self.call(f"reconf_in_0_{u['id']}", u))
        self.assertEqual(len(self.rc(0).inList), 1)
        # Now /in again — should NOT fire another warning (already_in guard)
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(u)
        texts = self.sent_texts()
        warning_seen = any("ghosted" in t.lower() or "committing" in t.lower() for t in texts)
        self.assertFalse(warning_seen, f"No re-warning should fire; got: {texts}")


class TestDuplicateVoteMessage(IntegrationBase):
    """Real-user duplicate /in /out /maybe must NOT use the 'duplicate proxy' phrasing."""

    async def test_duplicate_in_says_already_in_not_duplicate_proxy(self):
        u = USERS[0]
        await self.start_rc()
        await self.vote_in(u)
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(u)  # duplicate
        texts = self.sent_texts()
        joined = " ".join(texts).lower()
        self.assertNotIn("duplicate proxy", joined,
                         "Real-user duplicate must not mention 'duplicate proxy'")
        self.assertIn("already in", joined,
                      f"Expected 'already IN' phrasing; got: {texts}")

    async def test_duplicate_out_says_already_out(self):
        u = USERS[0]
        await self.start_rc()
        await self.vote_out(u)
        get_mock_bot().send_message.reset_mock()
        await self.vote_out(u)
        joined = " ".join(self.sent_texts()).lower()
        self.assertNotIn("duplicate proxy", joined)
        self.assertIn("already out", joined)

    async def test_duplicate_maybe_says_already_maybe(self):
        u = USERS[0]
        await self.start_rc()
        await self.vote_maybe(u)
        get_mock_bot().send_message.reset_mock()
        await self.vote_maybe(u)
        joined = " ".join(self.sent_texts()).lower()
        self.assertNotIn("duplicate proxy", joined)
        self.assertIn("already maybe", joined)


class TestBuzzMultiRollcall(IntegrationBase):
    """/buzz default unions voters across active rollcalls; ::N narrows to one."""

    async def _setup_two_active_rollcalls(self):
        """Returns (u_voted_on_rc1, u_voted_on_rc2, u_silent)."""
        u_rc1 = USERS[0]
        u_rc2 = USERS[1]
        u_silent = USERS[2]
        # Seed all three in chat_members so /buzz knows them
        for u in (u_rc1, u_rc2, u_silent):
            db.upsert_chat_member(CHAT_ID, u["id"], u["first_name"], u.get("username"))
        await self.start_rc("Game 1")
        await self.start_rc("Game 2")
        await self.vote_in(u_rc1, rc_suffix="::1")
        await self.vote_in(u_rc2, rc_suffix="::2")
        return u_rc1, u_rc2, u_silent

    async def _mock_chat_members_active(self):
        """Make get_chat_member return active for everyone (otherwise buzz filters them out)."""
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"

    async def test_buzz_default_spares_voters_from_any_active_rollcall(self):
        u_rc1, u_rc2, u_silent = await self._setup_two_active_rollcalls()
        await self._mock_chat_members_active()
        get_mock_bot().send_message.reset_mock()
        await self.buzz_command(self.msg("/buzz", ADMIN_USER))
        joined = " ".join(self.sent_texts())
        self.assertNotIn(u_rc1.get("username"), joined,
                         f"u_rc1 voted on rollcall 1 — should be spared. Got: {joined}")
        self.assertNotIn(u_rc2.get("username"), joined,
                         f"u_rc2 voted on rollcall 2 — should be spared. Got: {joined}")
        self.assertIn(u_silent.get("username"), joined,
                      f"u_silent voted on neither — should be pinged. Got: {joined}")

    async def test_buzz_with_rc_suffix_only_spares_that_rollcall(self):
        u_rc1, u_rc2, u_silent = await self._setup_two_active_rollcalls()
        await self._mock_chat_members_active()
        get_mock_bot().send_message.reset_mock()
        await self.buzz_command(self.msg("/buzz ::1", ADMIN_USER))
        joined = " ".join(self.sent_texts())
        self.assertNotIn(u_rc1.get("username"), joined,
                         "u_rc1 voted on the targeted rollcall — spared")
        self.assertIn(u_rc2.get("username"), joined,
                      "u_rc2 voted on rollcall 2, not 1 — should be pinged when ::1 is targeted")
        self.assertIn(u_silent.get("username"), joined,
                      "u_silent voted on neither — should be pinged")


class TestPendingReconfTTL(IntegrationBase):
    """_pending_reconf entries must carry _ts and be pruned by _prune_pending."""

    def test_pending_reconf_entry_has_ts_after_in(self):
        """A real /in that triggers the warning must stamp _ts on the entry."""
        # Use sync setup since we're checking dict state, no need for full async
        u = USERS[0]
        db.increment_ghost_count(CHAT_ID, u["id"], u["first_name"])

        async def run():
            await self.toggle_ghost_tracking(self.msg("/toggle_ghost_tracking on", ADMIN_USER))
            await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
            await self.start_rc()
            await self.vote_in(u)

        import asyncio
        asyncio.run(run())
        entry = self.bs._pending_reconf.get((CHAT_ID, u["id"]))
        self.assertIsNotNone(entry, "reconf entry should exist after triggered /in")
        self.assertIn("_ts", entry, "entry must have _ts field for TTL pruning")
        self.assertGreater(entry["_ts"], time.time() - 5,
                           "_ts should be a recent unix timestamp")

    def test_prune_pending_drops_stale_reconf_entry(self):
        """_prune_pending must drop entries older than TTL."""
        from bot_state import _PENDING_TTL_SECONDS, _prune_pending
        stale_ts = time.time() - _PENDING_TTL_SECONDS - 10
        self.bs._pending_reconf[(CHAT_ID, 999)] = {"rc_number": 0, "comment": "", "_ts": stale_ts}
        self.bs._pending_reconf[(CHAT_ID, 1000)] = {"rc_number": 0, "comment": "", "_ts": time.time()}
        _prune_pending(self.bs._pending_reconf)
        self.assertNotIn((CHAT_ID, 999), self.bs._pending_reconf, "stale entry should be pruned")
        self.assertIn((CHAT_ID, 1000), self.bs._pending_reconf, "fresh entry must survive")
