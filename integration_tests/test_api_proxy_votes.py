"""
Integration tests for the proxy-vote route (POST /chats/{id}/rollcalls/{n}/proxy-votes).

Real services + real DB via FastAPI TestClient. Auth + rate-limit reset
per test (mirrors the pattern in test_api_votes.py).
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa: F401
    import rollcall_manager
    from api.main import app
    return {"app": app, "manager": rollcall_manager.manager}


CHAT_ID = -1001999000301
ALICE = {"id": 100, "name": "Alice", "username": "alice"}


class ProxyVotesAPIBase(unittest.TestCase):

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

    def _proxy_vote(self, choice, name, comment=None, rc_number=1):
        return self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/{rc_number}/proxy-votes",
            json={
                "vote": choice,
                "proxy_name": name,
                "admin_user_id": ALICE["id"],
                "admin_name": ALICE["name"],
                "comment": comment,
            },
        )


class TestProxyIn(ProxyVotesAPIBase):

    def test_proxy_in_returns_201(self):
        r = self._proxy_vote("in", "Alex")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["action"], "added")
        self.assertEqual(body["user"]["name"], "Alex")
        self.assertTrue(body["user"]["is_proxy"])
        self.assertEqual(body["proxy_owner_id"], ALICE["id"])

    def test_proxy_in_with_comment(self):
        r = self._proxy_vote("in", "Alex", comment="bringing snacks")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["user"]["comment"], "bringing snacks")

    def test_proxy_in_duplicate_returns_409(self):
        self._proxy_vote("in", "Alex")
        r = self._proxy_vote("in", "Alex")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "duplicateProxy")

    def test_proxy_in_long_name_returns_422_pydantic(self):
        # Pydantic max_length=40 catches it before service
        r = self._proxy_vote("in", "X" * 60)
        self.assertEqual(r.status_code, 422)


class TestProxyOutAndMaybe(ProxyVotesAPIBase):

    def test_proxy_out_fresh(self):
        r = self._proxy_vote("out", "Alex")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["action"], "added")
        self.assertFalse(body["was_in"])
        out_names = [u["name"] for u in body["rollcall"]["out_list"]]
        self.assertIn("Alex", out_names)

    def test_proxy_out_promotes_waitlister(self):
        rc = self.manager.get_rollcall(CHAT_ID, 0)
        rc.inListLimit = 1
        rc.save()
        self._proxy_vote("in", "Alex")
        self._proxy_vote("in", "Brian")  # waitlists
        r = self._proxy_vote("out", "Alex")
        body = r.json()
        self.assertEqual(body["action"], "moved")
        self.assertEqual(body["promoted"]["name"], "Brian")

    def test_proxy_maybe_fresh(self):
        r = self._proxy_vote("maybe", "Alex")
        self.assertEqual(r.status_code, 201)
        maybe_names = [u["name"] for u in r.json()["rollcall"]["maybe_list"]]
        self.assertIn("Alex", maybe_names)


class TestProxyAuth(ProxyVotesAPIBase):

    def test_proxy_vote_requires_vote_scope(self):
        # Issue a read-only token and try
        from db import _hash_token, generate_api_token, insert_api_token
        ro = generate_api_token()
        insert_api_token(_hash_token(ro), CHAT_ID, "read",
                         label="readonly", issued_by_user_id=ALICE["id"])
        r = self.client.post(
            f"/api/v1/chats/{CHAT_ID}/rollcalls/1/proxy-votes",
            json={
                "vote": "in",
                "proxy_name": "Alex",
                "admin_user_id": ALICE["id"],
                "admin_name": ALICE["name"],
            },
            headers={"Authorization": f"Bearer {ro}"},
        )
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
