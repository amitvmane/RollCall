"""
Integration tests for REST API vote routes.
Real services, real DB, FastAPI TestClient.
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    import rollcall_manager
    from api.main import app
    from db import _hash_token, generate_api_token, insert_api_token
    return {
        "app": app,
        "manager": rollcall_manager.manager,
        "generate_api_token": generate_api_token,
        "insert_api_token": insert_api_token,
        "_hash_token": _hash_token,
    }


CHAT_ID = -1001999000601
ALICE = {"id": 100, "name": "Alice", "username": "alice"}
BOB = {"id": 200, "name": "Bob", "username": "bob"}
CAROL = {"id": 300, "name": "Carol", "username": "carol"}
DAVE = {"id": 400, "name": "Dave", "username": "dave"}


class VotesAPIBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.manager = env["manager"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        # Auto-auth all requests for this base
        from db import _hash_token, generate_api_token, insert_api_token
        token = generate_api_token()
        insert_api_token(_hash_token(token), CHAT_ID, "read,vote,admin",
                         label="test", issued_by_user_id=ALICE["id"])
        self.client.headers["Authorization"] = f"Bearer {token}"
        # Start a rollcall for every test
        self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls",
            json={
                "title": "Match",
                "started_by_user_id": ALICE["id"],
                "started_by_name": ALICE["name"],
                "started_by_username": ALICE["username"],
            },
        )

    def _vote(self, choice, user, comment=None, rc_number=1):
        return self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/{rc_number}/votes",
            json={
                "vote": choice,
                "user_id": user["id"],
                "first_name": user["name"],
                "username": user["username"],
                "comment": comment,
            },
        )


class TestVoteIn(VotesAPIBase):

    def test_vote_in_returns_201_and_adds_user(self):
        r = self._vote("in", BOB)
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["action"], "added")
        self.assertEqual(body["user"]["name"], "Bob")
        self.assertEqual(body["rc_number_1based"], 1)
        self.assertEqual(body["rollcall"]["in_count"], 1)
        names_in = [u["name"] for u in body["rollcall"]["in_list"]]
        self.assertIn("Bob", names_in)

    def test_vote_in_with_comment_persists(self):
        r = self._vote("in", BOB, comment="running late")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["user"]["comment"], "running late")

    def test_vote_in_duplicate_returns_409(self):
        self._vote("in", BOB)
        r = self._vote("in", BOB)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "alreadyInList")


class TestVoteOut(VotesAPIBase):

    def test_vote_out_when_not_in_adds_to_out(self):
        r = self._vote("out", BOB)
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["action"], "added")
        self.assertFalse(body["was_in"])
        self.assertIsNone(body["promoted"])
        out_names = [u["name"] for u in body["rollcall"]["out_list"]]
        self.assertIn("Bob", out_names)

    def test_vote_out_after_in_moves(self):
        self._vote("in", BOB)
        r = self._vote("out", BOB)
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["action"], "moved")
        self.assertTrue(body["was_in"])

    def test_vote_out_promotes_waitlister(self):
        # Cap=1 then add Bob (IN) + Carol (WAIT), then Bob /out
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        self._vote("in", BOB)
        self._vote("in", CAROL)
        r = self._vote("out", BOB)
        body = r.json()
        self.assertEqual(body["action"], "moved")
        self.assertIsNotNone(body["promoted"])
        self.assertEqual(body["promoted"]["name"], "Carol")


class TestVoteMaybe(VotesAPIBase):

    def test_vote_maybe_adds_user(self):
        r = self._vote("maybe", BOB)
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["action"], "added")
        maybe_names = [u["name"] for u in body["rollcall"]["maybe_list"]]
        self.assertIn("Bob", maybe_names)


class TestVoteValidation(VotesAPIBase):

    def test_invalid_vote_choice_returns_422(self):
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1/votes",
            json={
                "vote": "invalid_choice",
                "user_id": BOB["id"],
                "first_name": BOB["name"],
            },
        )
        self.assertEqual(r.status_code, 422)

    def test_missing_user_id_returns_422(self):
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1/votes",
            json={"vote": "in", "first_name": BOB["name"]},
        )
        self.assertEqual(r.status_code, 422)

    def test_vote_on_nonexistent_rollcall_returns_422(self):
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/9/votes",
            json={
                "vote": "in",
                "user_id": BOB["id"],
                "first_name": BOB["name"],
            },
        )
        self.assertEqual(r.status_code, 422)

    def test_vote_with_no_active_rollcall_returns_404(self):
        # End the rollcall set up by setUp first
        self.client.request(
            "DELETE",
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1",
            json={"ended_by_user_id": ALICE["id"], "ended_by_name": ALICE["name"]},
        )
        r = self._vote("in", BOB)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "rollCallNotStarted")


class TestCrossStateConsistency(VotesAPIBase):
    """
    Confirms the API and the service layer share state — the same
    invariant the bot relies on. Sets up state via the API; reads it
    back via the service layer directly.
    """

    def test_api_vote_reflected_in_service_layer_state(self):
        from services.voting import vote_in
        from services.rollcalls import get_rollcall

        # Vote via API
        r = self._vote("in", BOB)
        self.assertEqual(r.status_code, 201)

        # Read via service layer directly
        rc = get_rollcall(CHAT_ID)
        in_names = [u["name"] for u in rc["in_list"]]
        self.assertIn("Bob", in_names)

    def test_service_vote_reflected_in_api(self):
        import asyncio
        from services.voting import vote_in

        # Vote via service layer
        async def _run():
            await vote_in(CHAT_ID, CAROL["id"], CAROL["name"], CAROL["username"])
        asyncio.run(_run())

        # Read via API
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/1")
        body = r.json()
        in_names = [u["name"] for u in body["in_list"]]
        self.assertIn("Carol", in_names)


if __name__ == "__main__":
    unittest.main()
