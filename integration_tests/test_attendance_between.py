"""
Integration tests for db.get_attendance_between — the per-member counts the
monthly wrap-up card is built from.

Reported from production: the card showed a player with MORE games played
than the month contained. The aggregation was `GROUP BY first_name`, so two
different members who share a first name were summed into one row — two of
eight plus five of eight came out as eleven of eight. The group in question
had two players with the same name, which is what made it visible.

Real DB, real rows. The invariant these protect is small and absolute:
nobody can attend more sessions than happened.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"

CHAT_ID = -1001999000977
ADMIN_ID = 5001
# Same first name, different people. addIn only blocks a namesake when the
# USERNAME matches too, so distinct usernames is exactly the production shape
# that let both of them into the same session — and then into one summed row.
AMIT_A = 5002
AMIT_B = 5003
SOLO_ID = 5004

WINDOW_START = "2000-01-01 00:00:00"
WINDOW_END = "2099-01-01 00:00:00"


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import db
    return db


class TestAttendanceBetween(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.db = _import()

    def setUp(self):
        reset_db()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))
        import rollcall_manager
        rollcall_manager.manager.clear_cache()
        self.db.get_or_create_chat(CHAT_ID)

    def _session(self, title, in_users=(), proxies=()):
        """Run one rollcall: start → votes → end."""
        from services.rollcalls import start_rollcall, end_rollcall
        from services.voting import vote_in
        from services.proxy import set_in_for
        asyncio.run(start_rollcall(CHAT_ID, title, ADMIN_ID, "Admin"))
        for uid, name, uname in in_users:
            asyncio.run(vote_in(CHAT_ID, uid, name, uname))
        for pname in proxies:
            asyncio.run(set_in_for(CHAT_ID, ADMIN_ID, "Admin", pname))
        asyncio.run(end_rollcall(CHAT_ID, 0, ADMIN_ID, "Admin"))

    def _attendance(self):
        return self.db.get_attendance_between(CHAT_ID, WINDOW_START, WINDOW_END)

    # ── The reported bug ─────────────────────────────────────────────────

    def test_namesakes_are_not_summed_into_one_player(self):
        """Two different people called Amit, three sessions. Neither can have
        attended more than three, and they must not merge into one row."""
        self._session("G1", [(AMIT_A, "Amit", "amit_a"), (AMIT_B, "Amit", "amit_b")])
        self._session("G2", [(AMIT_A, "Amit", "amit_a")])
        self._session("G3", [(AMIT_A, "Amit", "amit_a"), (AMIT_B, "Amit", "amit_b")])

        rows = self._attendance()
        for r in rows:
            self.assertLessEqual(
                r["attended"], 3,
                f"{r['name']} attended {r['attended']} of 3 sessions — impossible",
            )
        self.assertEqual(len([r for r in rows if r["name"].startswith("Amit")]), 2,
                         f"the two Amits should be two rows, got {rows}")
        self.assertEqual(sorted(r["attended"] for r in rows), [2, 3])

    def test_namesakes_get_distinguishable_labels(self):
        """Splitting them is only half the fix — two rows both reading 'Amit'
        look like the same bug wearing a different hat."""
        self._session("G1", [(AMIT_A, "Amit", "amit_a"), (AMIT_B, "Amit", "amit_b")])
        self._session("G2", [(AMIT_A, "Amit", "amit_a"), (AMIT_B, "Amit", "amit_b")])
        names = [r["name"] for r in self._attendance()]
        self.assertEqual(len(set(names)), len(names), f"labels collide: {names}")

    def test_a_unique_name_is_left_alone(self):
        """No suffix noise for everyone else."""
        self._session("G1", [(SOLO_ID, "Ravi", "ravi")])
        rows = self._attendance()
        self.assertEqual(rows, [{"name": "Ravi", "attended": 1, "kind": "real"}])

    # ── Invariants the card depends on ───────────────────────────────────

    def test_nobody_exceeds_the_session_count(self):
        self._session("G1", [(AMIT_A, "Amit", "amit_a"), (AMIT_B, "Amit", "amit_b"), (SOLO_ID, "Ravi", "ravi")],
                      proxies=["Guest"])
        self._session("G2", [(AMIT_A, "Amit", "amit_a"), (SOLO_ID, "Ravi", "ravi")], proxies=["Guest"])
        sessions = len(self.db.get_rollcalls_between(CHAT_ID, WINDOW_START, WINDOW_END))
        for r in self._attendance():
            self.assertLessEqual(r["attended"], sessions,
                                 f"{r['name']}: {r['attended']} > {sessions} sessions")

    def test_proxies_are_counted_and_flagged_as_guests(self):
        """`kind` lets the card mark a guest as a guest — otherwise a reader
        sees a name they don't recognise sitting in the members list."""
        self._session("G1", proxies=["Guest 2"])
        self._session("G2", proxies=["Guest 2"])
        rows = self._attendance()
        self.assertEqual(rows, [{"name": "Guest 2", "attended": 2, "kind": "proxy"}])

    def test_merged_guest_counts_as_the_real_member_not_a_guest(self):
        """Once merged, the alias IS the member — the combined row must not
        still be labelled a guest."""
        self._session("G1", [(SOLO_ID, "Ravi", "ravi")])
        self._session("G2", proxies=["Ravi K"])
        from services import identity as identity_svc
        identity_svc.link_identities(CHAT_ID, "Ravi K", canonical_user_id=SOLO_ID,
                                     admin_user_id=ADMIN_ID, admin_name="Admin")
        rows = self._attendance()
        self.assertEqual(rows[0]["kind"], "real")

    def test_merged_proxy_folds_into_the_real_member(self):
        """A guest name merged into a member is the SAME person — that one
        should combine, and is the reason this can't just group by user_id
        and ignore proxies."""
        self._session("G1", [(SOLO_ID, "Ravi", "ravi")])
        self._session("G2", proxies=["Ravi K"])
        from services import identity as identity_svc
        identity_svc.link_identities(CHAT_ID, "Ravi K", canonical_user_id=SOLO_ID,
                                     admin_user_id=ADMIN_ID, admin_name="Admin")

        rows = self._attendance()
        self.assertEqual(len(rows), 1, f"merged aliases should be one row, got {rows}")
        self.assertEqual(rows[0]["attended"], 2)

    def test_rank_agrees_with_the_leaderboard_after_a_merge(self):
        """Rank used to be its own query over `users` only, ignoring proxy
        rows — so a member whose guest-era games had been merged in was ranked
        on fewer games than the leaderboard credited them with, and the portal
        showed a rank contradicting the board printed next to it."""
        # Ravi plays one session himself and one as a guest name; Amit plays one.
        self._session("G1", [(SOLO_ID, "Ravi", "ravi"), (AMIT_A, "Amit", "amit_a")])
        self._session("G2", proxies=["Ravi K"])
        from services import identity as identity_svc
        identity_svc.link_identities(CHAT_ID, "Ravi K", canonical_user_id=SOLO_ID,
                                     admin_user_id=ADMIN_ID, admin_name="Admin")

        board = self.db.get_leaderboard_by_attendance(CHAT_ID, limit=50)
        top = next(r for r in board if r.get("user_id") == SOLO_ID)
        self.assertEqual(top["attended"], 2, "merged guest games should count")
        expected = board.index(top) + 1
        self.assertEqual(self.db.get_user_rank_in_chat(CHAT_ID, SOLO_ID), expected)
        self.assertEqual(expected, 1, "2 games should outrank 1")

    def test_ordered_most_attended_first(self):
        self._session("G1", [(AMIT_A, "Amit", "amit_a"), (SOLO_ID, "Ravi", "ravi")])
        self._session("G2", [(SOLO_ID, "Ravi", "ravi")])
        rows = self._attendance()
        self.assertEqual([r["attended"] for r in rows], [2, 1])


if __name__ == "__main__":
    unittest.main()
