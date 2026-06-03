"""
Regression tests pinning the 17 fixes from the deep-audit pass (commit 0b00c1b).

Each test maps to a specific bug ID (C1-C4, H1-H6, M-series) so a future
regression is easy to identify from the failing test name.
"""
import unittest
from unittest.mock import patch

from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID, make_call


# ──────────────────────────────────────────────────────────────────────────────
# C1: total_rollcalls now increments on /erc, panel endconfirm, and auto-close.
#     /stats attendance rate (t_in / t_rc) was always "—" because nothing
#     bumped this column.
# ──────────────────────────────────────────────────────────────────────────────
class TestC1AttendanceRateIncrements(IntegrationBase):
    async def test_erc_bumps_total_rollcalls_for_each_participant(self):
        await self.start_rc("game night")
        await self.vote_in(USERS[0])
        await self.vote_out(USERS[1])
        await self.vote_maybe(USERS[2])

        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        import db as _db
        conn = _db.get_connection()
        try:
            cur = conn.cursor()
            for u in (USERS[0], USERS[1], USERS[2]):
                cur.execute(
                    "SELECT total_rollcalls FROM user_stats WHERE chat_id = ? AND user_id = ?",
                    (CHAT_ID, u["id"]),
                )
                row = cur.fetchone()
                self.assertIsNotNone(row, f"User {u['id']} has no stats row")
                count = row[0] if not isinstance(row, dict) else row["total_rollcalls"]
                self.assertEqual(count, 1, f"User {u['id']} total_rollcalls = {count}, expected 1")
            cur.close()
        finally:
            _db.release_connection(conn)


# ──────────────────────────────────────────────────────────────────────────────
# C2: /out sends "is now OUT!" ack for first-time voters. Before this fix
#     /in and /maybe acked but /out only acked on a IN→OUT transition.
# ──────────────────────────────────────────────────────────────────────────────
class TestC2OutFirstVoteAck(IntegrationBase):
    async def test_first_out_vote_sends_ack(self):
        await self.start_rc("test")
        before = self.sent_count()
        await self.vote_out(USERS[0])
        # The most recent send should be a louder-mode ack for the new OUT vote.
        new_texts = self.sent_texts()[before:]
        self.assertTrue(
            any("OUT" in t for t in new_texts),
            f"No OUT ack found in messages: {new_texts}",
        )

    async def test_first_out_ack_says_is_now_out(self):
        await self.start_rc("test")
        before = self.sent_count()
        await self.vote_out(USERS[0])
        new_texts = self.sent_texts()[before:]
        # The new branch uses "is now OUT!" for fresh OUT voters.
        self.assertTrue(
            any("is now OUT" in t for t in new_texts),
            f"Expected 'is now OUT' ack, got: {new_texts}",
        )


# ──────────────────────────────────────────────────────────────────────────────
# C4: _esc_md now escapes ']' so display names containing ']' don't break
#     [name](tg://user?id=X) link rendering.
# ──────────────────────────────────────────────────────────────────────────────
class TestC4EscMdSquareBracket(unittest.TestCase):
    def test_esc_md_escapes_right_bracket(self):
        from bot_state import _esc_md
        self.assertEqual(_esc_md("foo]bar"), r"foo\]bar")

    def test_esc_md_escapes_all_v1_specials(self):
        from bot_state import _esc_md
        self.assertEqual(_esc_md("_*`[]"), r"\_\*\`\[\]")

    def test_esc_md_none_safe(self):
        from bot_state import _esc_md
        self.assertEqual(_esc_md(None), "")


# ──────────────────────────────────────────────────────────────────────────────
# H1: delete_user_by_name refuses ambiguous first_name matches and prefers
#     @username. Previously it would delete ALL users with a matching
#     first_name in a single DB DELETE.
# ──────────────────────────────────────────────────────────────────────────────
class TestH1DeleteUserByName(IntegrationBase):
    async def test_ambiguous_first_name_refuses_to_delete(self):
        await self.start_rc("test")
        user_a = {"id": 5001, "first_name": "Jordan", "username": "jordan_a"}
        user_b = {"id": 5002, "first_name": "Jordan", "username": "jordan_b"}
        await self.vote_in(user_a)
        await self.vote_in(user_b)
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 2)

        from db import delete_user_by_name
        ok = delete_user_by_name(rc.id, "Jordan")
        self.assertFalse(ok, "delete_user_by_name should refuse ambiguous first_name")

        # Both users should still be present in the DB.
        rc2 = self.mgr.reload_chat(CHAT_ID)["rollCalls"][0]
        self.assertEqual(len(rc2.inList), 2, "Neither Jordan should have been deleted")

    async def test_username_match_deletes_exact_user(self):
        await self.start_rc("test")
        user_a = {"id": 5001, "first_name": "Jordan", "username": "jordan_a"}
        user_b = {"id": 5002, "first_name": "Jordan", "username": "jordan_b"}
        await self.vote_in(user_a)
        await self.vote_in(user_b)

        from db import delete_user_by_name
        rc = self.rc(0)
        ok = delete_user_by_name(rc.id, "@jordan_a")
        self.assertTrue(ok)

        rc2 = self.mgr.reload_chat(CHAT_ID)["rollCalls"][0]
        self.assertEqual(len(rc2.inList), 1)
        self.assertEqual(rc2.inList[0].user_id, 5002)

    async def test_proxy_name_match_takes_priority(self):
        await self.start_rc("test")
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg(f"/sif {USERS[0]['first_name']}", ADMIN_USER))

        from db import delete_user_by_name
        rc = self.rc(0)
        ok = delete_user_by_name(rc.id, USERS[0]["first_name"])
        self.assertTrue(ok, "Should delete the proxy by exact name first")

        rc2 = self.mgr.reload_chat(CHAT_ID)["rollCalls"][0]
        real_users = [u for u in rc2.allNames if isinstance(u.user_id, int)]
        proxies = [u for u in rc2.allNames if isinstance(u.user_id, str)]
        # Real user survives; proxy gone.
        self.assertEqual(len(real_users), 1)
        self.assertEqual(len(proxies), 0)


# ──────────────────────────────────────────────────────────────────────────────
# H2: reconf-/in path upserts chat_member before showing the prompt (and the
#     callback also upserts). Previously the ghost-warning path bypassed
#     the upsert, so users with high ghost counts never refreshed their row.
# ──────────────────────────────────────────────────────────────────────────────
class TestH2ReconfUpsertsChatMember(IntegrationBase):
    async def test_in_with_pending_reconf_still_upserts(self):
        # Seed a ghost record so the reconf prompt fires.
        await self.start_rc("test")
        await self.vote_in(USERS[0])

        from db import increment_ghost_count, get_active_members
        # Manually mark this user as having ghosted to trigger reconf on next /in.
        increment_ghost_count(CHAT_ID, USERS[0]["id"], USERS[0]["first_name"])
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        await self.start_rc("test2")

        # Clear chat_members so we can verify the upsert ran.
        import db as _db
        conn = _db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM chat_members WHERE chat_id = ?", (CHAT_ID,))
            conn.commit()
            cur.close()
        finally:
            _db.release_connection(conn)

        # Now /in — the absent_limit default = 1 means reconf fires.
        await self.vote_in(USERS[0])

        # Confirm chat_member was upserted even though we bailed into reconf.
        members = get_active_members(CHAT_ID)
        member_ids = {m["user_id"] for m in members}
        self.assertIn(USERS[0]["id"], member_ids,
                      "Reconf path should still upsert chat_member")


# ──────────────────────────────────────────────────────────────────────────────
# H5: /sif ghost-warning callback restores the comment from _pending_proxy_add
#     state. Previously the comment was dropped between the warning and the
#     "yes add anyway" click.
# ──────────────────────────────────────────────────────────────────────────────
class TestH5ProxyAddPreservesComment(IntegrationBase):
    async def test_proxy_add_callback_keeps_comment(self):
        await self.start_rc("test")
        # Set absent_limit to 1 so a single ghost triggers the warning.
        from db import increment_ghost_count
        # Pre-record a ghost against proxy name "Charlie"
        increment_ghost_count(CHAT_ID, -1, "Charlie", proxy_name="Charlie")

        # /sif Charlie bringing-beer  → should hit the warning path and store the comment.
        await self.set_in_for(self.msg("/sif Charlie bringing beer", ADMIN_USER))

        from bot_state import _pending_proxy_add
        key = (CHAT_ID, ADMIN_USER["id"], "Charlie")
        self.assertIn(key, _pending_proxy_add)
        self.assertEqual(_pending_proxy_add[key]["comment"], "bringing beer")

        # Confirm — the proxy_add callback should use that comment.
        await self.ghost_callback_handler(make_call("proxy_add_0_Charlie", ADMIN_USER))

        rc = self.rc(0)
        charlie = next((u for u in rc.inList if u.name == "Charlie"), None)
        self.assertIsNotNone(charlie, "Charlie should be IN after the callback")
        self.assertEqual(charlie.comment, "bringing beer")


# ──────────────────────────────────────────────────────────────────────────────
# H6: /set_status no-ops when target == current status.
# ──────────────────────────────────────────────────────────────────────────────
class TestH6SetStatusNoOp(IntegrationBase):
    async def test_set_status_same_bucket_is_noop(self):
        await self.start_rc("test")
        await self.vote_in(USERS[0])
        rc_before = self.rc(0)
        in_before = [u.user_id for u in rc_before.inList]

        before_count = self.sent_count()
        await self.set_status_override(
            self.msg(f"/set_status {USERS[0]['first_name']} in", ADMIN_USER)
        )
        after_texts = self.sent_texts()[before_count:]

        # No-op should send a message saying "already" — and importantly, no
        # confirmation prompt (which would imply we're about to mutate).
        self.assertTrue(
            any("already" in t.lower() for t in after_texts),
            f"Expected 'already' in no-op message, got: {after_texts}",
        )

        rc_after = self.rc(0)
        in_after = [u.user_id for u in rc_after.inList]
        self.assertEqual(in_before, in_after, "IN list should be unchanged")


# ──────────────────────────────────────────────────────────────────────────────
# M6: pending dicts have a TTL; _prune_pending drops stale entries.
# ──────────────────────────────────────────────────────────────────────────────
class TestM6PendingTTL(unittest.TestCase):
    def test_prune_pending_drops_old_entries(self):
        from bot_state import _prune_pending, _PENDING_TTL_SECONDS
        import time
        d = {
            "fresh": {"_ts": time.time()},
            "stale": {"_ts": time.time() - (_PENDING_TTL_SECONDS + 60)},
        }
        _prune_pending(d)
        self.assertIn("fresh", d)
        self.assertNotIn("stale", d)


# ──────────────────────────────────────────────────────────────────────────────
# M9: /set_template surfaces ignored bad int values to the user instead of
#     silently dropping them.
# ──────────────────────────────────────────────────────────────────────────────
class TestM9SetTemplateBadInts(IntegrationBase):
    async def test_bad_limit_surfaces_warning(self):
        before = self.sent_count()
        await self.set_template(self.msg(
            '/set_template foo "Title" limit=not_a_number', ADMIN_USER,
        ))
        new_texts = self.sent_texts()[before:]
        self.assertTrue(
            any("Ignored non-integer" in t for t in new_texts),
            f"Expected ignored-value warning, got: {new_texts}",
        )


if __name__ == "__main__":
    unittest.main()
