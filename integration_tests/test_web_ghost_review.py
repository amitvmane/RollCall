"""
Integration tests for the web ghost-review endpoints:
  GET  /web/group/{token}/ghost/sessions
  POST /web/group/{token}/ghost/review

Real DB, real services, FastAPI TestClient.

The after-game "who ghosted?" prompt only ever existed in Telegram, so an
admin running their group from the web page could not answer it — and
answering is what FORGIVES everyone who did turn up. These cover the two
halves that make the feature worth having: a late drop-out (someone who
moved to OUT too late to be replaced) can be marked at all, and marking
one must not cost anything to the people who actually played.
"""
import asyncio
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mock_helpers import reset_db

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"

CHAT_ID = -1001999000961
ADMIN_ID = 4001
PLAYED_ID = 4002    # said IN and turned up
GHOSTED_ID = 4003   # said IN and didn't
DROPPED_ID = 4004   # moved to OUT far too late to be replaced
OTHER_CHAT = -1001999000962


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    from api.main import app
    from api.identity import issue_identity_token
    import db
    return {"app": app, "issue_identity_token": issue_identity_token, "db": db}


class TestWebGhostReview(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.issue_identity_token = staticmethod(env["issue_identity_token"])
        cls.db = env["db"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.enterContext(patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}))

        import rollcall_manager
        from services.rollcalls import start_rollcall, end_rollcall
        from services.voting import vote_in, vote_out
        rollcall_manager.manager.clear_cache()

        self.chat = self.db.get_or_create_chat(CHAT_ID)
        self.token = self.chat["group_web_token"]
        self.db.set_web_admin(CHAT_ID, ADMIN_ID, "Admin")

        asyncio.run(start_rollcall(CHAT_ID, "Saturday Football", ADMIN_ID, "Admin"))
        asyncio.run(vote_in(CHAT_ID, PLAYED_ID, "Played", "playedtg"))
        asyncio.run(vote_in(CHAT_ID, GHOSTED_ID, "Ghosted", "ghostedtg"))
        # IN first, then OUT at the last minute — this is the case the whole
        # feature exists for, and it must leave them in the OUT list.
        asyncio.run(vote_in(CHAT_ID, DROPPED_ID, "Dropped", "droppedtg"))
        asyncio.run(vote_out(CHAT_ID, DROPPED_ID, "Dropped", "droppedtg"))
        asyncio.run(end_rollcall(CHAT_ID, 0, ADMIN_ID, "Admin"))

    def _admin_headers(self):
        return {"X-Identity-Token": self.issue_identity_token(ADMIN_ID)}

    def _sessions(self):
        with patch("api.routes.web.check_web_admin_live", return_value=True):
            r = self.client.get(f"/api/v1/web/group/{self.token}/ghost/sessions",
                                headers=self._admin_headers())
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    # ── Listing ──────────────────────────────────────────────────────────

    def test_ended_session_awaits_review(self):
        body = self._sessions()
        titles = [s["title"] for s in body["sessions"]]
        self.assertIn("Saturday Football", titles)

    def test_candidates_include_the_late_drop_out_flagged(self):
        s = self._sessions()["sessions"][0]
        by_name = {c["name"]: c for c in s["candidates"]}
        self.assertIn("Played", by_name)
        self.assertIn("Ghosted", by_name)
        self.assertIn("Dropped", by_name, "a late drop-out must be markable at all")
        self.assertFalse(by_name["Played"]["was_out"])
        self.assertTrue(by_name["Dropped"]["was_out"],
                        "the UI has to be able to show drop-outs separately")

    def test_requires_web_admin(self):
        with patch("api.routes.web.check_web_admin_live", return_value=False):
            r = self.client.get(f"/api/v1/web/group/{self.token}/ghost/sessions",
                                headers={"X-Identity-Token": self.issue_identity_token(PLAYED_ID)})
        self.assertEqual(r.status_code, 403)

    def test_requires_identity(self):
        r = self.client.get(f"/api/v1/web/group/{self.token}/ghost/sessions")
        self.assertEqual(r.status_code, 401)

    # ── Reviewing ────────────────────────────────────────────────────────

    def _review(self, rc_id, user_ids):
        with patch("api.routes.web.check_web_admin_live", return_value=True):
            return self.client.post(
                f"/api/v1/web/group/{self.token}/ghost/review",
                json={"id_token": self.issue_identity_token(ADMIN_ID),
                      "rollcall_id": rc_id, "ghost_user_ids": user_ids},
            )

    def test_marking_a_late_drop_out_records_a_ghost(self):
        rc_id = self._sessions()["sessions"][0]["rollcall_id"]
        r = self._review(rc_id, [DROPPED_ID])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["ghosts"], 1)
        self.assertEqual(self.db.get_ghost_count(CHAT_ID, DROPPED_ID), 1)

    def test_attendees_are_never_charged_for_someone_elses_no_show(self):
        """The people who turned up must not gain a ghost — and, having been
        reviewed, should have a past absence cleared rather than added to."""
        self.db.increment_ghost_count(CHAT_ID, PLAYED_ID, "Played")
        before = self.db.get_ghost_count(CHAT_ID, PLAYED_ID)

        rc_id = self._sessions()["sessions"][0]["rollcall_id"]
        body = self._review(rc_id, [GHOSTED_ID]).json()

        self.assertEqual(self.db.get_ghost_count(CHAT_ID, GHOSTED_ID), 1)
        self.assertEqual(self.db.get_ghost_count(CHAT_ID, PLAYED_ID), before - 1,
                         "an attendee should be forgiven one absence, not charged")
        self.assertGreaterEqual(body["forgiven"], 1)

    def test_a_reviewed_session_stops_being_pending(self):
        rc_id = self._sessions()["sessions"][0]["rollcall_id"]
        self._review(rc_id, [])
        ids = [s["rollcall_id"] for s in self._sessions()["sessions"]]
        self.assertNotIn(rc_id, ids)

    def test_reviewing_twice_is_refused(self):
        """Second submit must not double-count — the panel can be left open in
        another tab, and re-posting it used to be indistinguishable from a
        first review."""
        rc_id = self._sessions()["sessions"][0]["rollcall_id"]
        self._review(rc_id, [GHOSTED_ID])
        again = self._review(rc_id, [GHOSTED_ID])
        self.assertEqual(again.status_code, 404)
        self.assertEqual(self.db.get_ghost_count(CHAT_ID, GHOSTED_ID), 1)

    def test_cannot_review_another_groups_session(self):
        """An admin of group A posting group B's rollcall id would otherwise
        rewrite B's attendance."""
        import rollcall_manager
        from services.rollcalls import start_rollcall, end_rollcall
        from services.voting import vote_in
        self.db.get_or_create_chat(OTHER_CHAT)
        rollcall_manager.manager.clear_cache()
        asyncio.run(start_rollcall(OTHER_CHAT, "Not Yours", ADMIN_ID, "Admin"))
        asyncio.run(vote_in(OTHER_CHAT, PLAYED_ID, "Played", "playedtg"))
        asyncio.run(end_rollcall(OTHER_CHAT, 0, ADMIN_ID, "Admin"))
        other_id = self.db.get_unprocessed_rollcalls(OTHER_CHAT, days=365)[0]["id"]

        r = self._review(other_id, [PLAYED_ID])
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.db.get_ghost_count(OTHER_CHAT, PLAYED_ID), 0)


if __name__ == "__main__":
    unittest.main()
