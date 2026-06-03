"""
Restart-recovery tests — simulate a bot restart by clearing in-memory state
and verifying the bot picks up where it left off from the database.

These don't actually fork the process; they reset the manager cache,
in-memory dicts, and any module-level state, then call recovery functions
(resume_reminder_loops, _load_users_from_db, etc.) and assert the world
is consistent.

Restart paths covered:
- Active rollcalls reload from DB into manager cache
- panel_msg_id round-trips through DB
- Reminder loops resume only for chats with finalizeDate set
- Ghost selections persist and reload mid-flight
- Pending action dicts expire under TTL
"""
import unittest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID, make_call


class TestRestartReloadsActiveRollcalls(IntegrationBase):
    async def test_active_rollcalls_reload_into_manager_cache(self):
        # Set up state.
        await self.start_rc("game night")
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        await self.vote_out(USERS[2])

        rc_before = self.rc(0)
        title_before = rc_before.title
        in_count_before = len(rc_before.inList)
        out_count_before = len(rc_before.outList)
        rc_id = rc_before.id

        # Simulate restart: clear all in-memory caches.
        self.mgr.clear_cache()
        self.bs._panel_msg_ids.clear()
        self.bs._rate_limits.clear()

        # Cold lookup should rehydrate from DB.
        rc_after = self.rc(0)
        self.assertIsNotNone(rc_after, "Rollcall should reload from DB after restart")
        self.assertEqual(rc_after.id, rc_id)
        self.assertEqual(rc_after.title, title_before)
        self.assertEqual(len(rc_after.inList), in_count_before)
        self.assertEqual(len(rc_after.outList), out_count_before)


class TestPanelMsgIdRoundTrip(IntegrationBase):
    async def test_panel_msg_id_persists_to_db_on_initial_send(self):
        # /src persists the panel msg_id via _persist_panel_msg_id (H4 fix).
        await self.start_rc("test")
        rc = self.rc(0)
        rc_db_id = rc.id

        # Simulate restart.
        self.mgr.clear_cache()
        self.bs._panel_msg_ids.clear()

        # Reload rc — should pick up panel_msg_id from DB.
        rc_after = self.rc(0)
        self.assertIsNotNone(rc_after.panel_msg_id,
                             f"panel_msg_id should survive restart but got None for rc {rc_db_id}")

    async def test_update_panel_uses_db_panel_msg_id_after_restart(self):
        # Start a rollcall, persist panel id, then restart and have a vote
        # discover the panel id from the model.
        await self.start_rc("test")
        rc = self.rc(0)
        original_panel_id = rc.panel_msg_id

        self.mgr.clear_cache()
        self.bs._panel_msg_ids.clear()

        # First /in after restart should edit the existing panel rather than
        # send a new one. _update_panel populates _panel_msg_ids from rc.panel_msg_id.
        from handlers.lifecycle import _update_panel
        rc_reloaded = self.rc(0)
        await _update_panel(CHAT_ID, 1, rc_reloaded)

        key = (CHAT_ID, 1)
        self.assertIn(key, self.bs._panel_msg_ids,
                      "Vote after restart should populate _panel_msg_ids from DB-stored value")
        self.assertEqual(self.bs._panel_msg_ids[key], original_panel_id,
                         "Restored panel id should match the original")


class TestReminderLoopResume(IntegrationBase):
    async def test_resume_only_for_chats_with_finalize_date(self):
        # Create two chats with rollcalls; only one has a finalizeDate.
        chat_a = CHAT_ID
        chat_b = CHAT_ID - 1

        rc_a = self.mgr.add_rollcall(chat_a, "no schedule")
        rc_b_chat = self.mgr.get_chat(chat_b)
        rc_b = self.mgr.add_rollcall(chat_b, "scheduled")
        rc_b.finalizeDate = datetime.now() + timedelta(hours=1)
        rc_b.save()

        # Simulate restart.
        self.mgr.clear_cache()

        # Stub asyncio.create_task so we can count which chats resumed.
        resumed_chats = []
        original_start = None

        async def _fake_start(rollcalls, tz, chat_id):
            resumed_chats.append(chat_id)

        with patch("check_reminders.start", side_effect=_fake_start):
            from check_reminders import resume_reminder_loops
            await resume_reminder_loops()
            # Give the scheduled tasks a tick to run.
            await asyncio.sleep(0)

        # Only chat_b should be resumed; chat_a has no finalizeDate so no loop is needed.
        self.assertIn(chat_b, resumed_chats,
                      f"chat {chat_b} should resume — it has an active rollcall with finalizeDate")
        self.assertNotIn(chat_a, resumed_chats,
                         f"chat {chat_a} should NOT resume — no finalizeDate set")


class TestGhostSelectionsPersist(IntegrationBase):
    async def test_ghost_selections_reload_mid_flight(self):
        # Start rollcall, vote IN, end it, open ghost prompt, select a ghost, restart.
        await self.start_rc("test")
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        rc_db_id = self.rc(0).id

        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        # Open ghost prompt
        await self.ghost_callback_handler(make_call(f"ghost_yes_{rc_db_id}", ADMIN_USER))
        # Select user[0] as ghost
        await self.ghost_callback_handler(
            make_call(f"ghost_tog_{rc_db_id}_{USERS[0]['id']}", ADMIN_USER)
        )

        # Confirm in-memory + DB-persisted state both have user[0]
        key = (CHAT_ID, rc_db_id)
        self.assertIn(USERS[0]["id"], self.bs._ghost_selections[key])
        from db import load_ghost_selections
        persisted = load_ghost_selections(CHAT_ID, rc_db_id)
        self.assertIsNotNone(persisted)
        self.assertIn(USERS[0]["id"], persisted)

        # Simulate restart — wipe in-memory selections.
        self.bs._ghost_selections.clear()

        # Toggling another ghost should reload prior selections from DB
        # (the togp/tog callbacks do load_ghost_selections() when key is missing).
        await self.ghost_callback_handler(
            make_call(f"ghost_tog_{rc_db_id}_{USERS[1]['id']}", ADMIN_USER)
        )
        self.assertIn(USERS[0]["id"], self.bs._ghost_selections[key],
                      "Prior ghost selection should reload from DB after restart")
        self.assertIn(USERS[1]["id"], self.bs._ghost_selections[key])


class TestPendingActionTTL(IntegrationBase):
    async def test_pending_delete_expires_via_prune(self):
        from bot_state import _pending_deletes, _prune_pending, _PENDING_TTL_SECONDS
        # Pretend an admin started /delete_user a long time ago.
        stale_ts = datetime.now().timestamp() - (_PENDING_TTL_SECONDS + 60)
        _pending_deletes[(CHAT_ID, ADMIN_USER["id"])] = {
            "name": "OldVictim", "rc_number": 0, "_ts": stale_ts,
        }
        fresh_ts = datetime.now().timestamp()
        _pending_deletes[(CHAT_ID, ADMIN_USER["id"] + 1)] = {
            "name": "Fresh", "rc_number": 0, "_ts": fresh_ts,
        }

        _prune_pending(_pending_deletes)

        self.assertNotIn((CHAT_ID, ADMIN_USER["id"]), _pending_deletes,
                         "Stale pending delete should be pruned")
        self.assertIn((CHAT_ID, ADMIN_USER["id"] + 1), _pending_deletes,
                      "Fresh pending delete should survive")


class TestErcLockSurvivesRestart(IntegrationBase):
    async def test_erc_lock_recreated_per_chat(self):
        # Lock the manager for chat A, then "restart" by clearing all caches.
        lock_before = self.mgr.get_erc_lock(CHAT_ID)
        # Simulate restart: drop the manager's lock map.
        self.mgr._erc_locks.clear()
        lock_after = self.mgr.get_erc_lock(CHAT_ID)
        self.assertIsNot(lock_before, lock_after,
                         "After restart, lock map should be fresh")
        # And the new lock should be usable.
        self.assertTrue(hasattr(lock_after, "acquire"))


if __name__ == "__main__":
    unittest.main()
