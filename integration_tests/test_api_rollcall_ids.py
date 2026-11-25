"""
REST API: rollcalls are named by their permanent id.

The display number (#1, #2…) is a position — ending #1 makes #2 the new #1.
A client that read "#2" and then acted on "#2" could land on a different
rollcall. Every rollcall route now has a stable twin,

    /chats/{chat_id}/rollcalls/by-id/{rollcall_id}/...

and the number-based paths keep working (deprecated, with the same errors as
before) — api/rollcall_ref.py. Real services, real DB.
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db

CHAT_ID = -1001999000701
OTHER_CHAT = -1001999000702
ALICE = {"id": 100, "name": "Alice", "username": "alice"}
BOB = {"id": 200, "name": "Bob", "username": "bob"}


def _token(chat_id, scopes="read,vote,admin"):
    from db import _hash_token, generate_api_token, insert_api_token
    t = generate_api_token()
    insert_api_token(_hash_token(t), chat_id, scopes, label="test", issued_by_user_id=ALICE["id"])
    return t


class _Base(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import bot_state  # noqa: F401
        import rollcall_manager
        from api.main import app
        cls.manager = rollcall_manager.manager
        cls.client = TestClient(app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.auth = {"Authorization": f"Bearer {_token(CHAT_ID)}"}

    def start(self, title, chat_id=CHAT_ID, auth=None):
        r = self.client.post(f"/api/v1/chats/{chat_id}/rollcalls", headers=auth or self.auth,
                             json={"title": title, "started_by_user_id": ALICE["id"],
                                   "started_by_name": ALICE["name"]})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["id"]

    def vote(self, path, user=BOB, choice="in", auth=None):
        return self.client.post(path, headers=auth or self.auth,
                                json={"vote": choice, "user_id": user["id"], "first_name": user["name"]})

    def end(self, path):
        return self.client.request("DELETE", path, headers=self.auth,
                                   json={"ended_by_user_id": ALICE["id"], "ended_by_name": ALICE["name"]})

    def ins(self, rc_id):
        idx = next(i for i, rc in enumerate(self.manager.get_rollcalls(CHAT_ID)) if rc.id == rc_id)
        return [u.user_id for u in self.manager.get_rollcall(CHAT_ID, idx).inList]


class TestStableIds(_Base):

    def test_a_call_by_id_survives_renumbering(self):
        # The whole point: read the id, an earlier rollcall ends, act by id —
        # the vote still lands on the rollcall that was meant.
        self.start("First")
        second = self.start("Second")
        third = self.start("Third")
        self.assertEqual(self.end(f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{self.start_id_of('First')}")
                         .status_code, 200)
        r = self.vote(f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{second}/votes")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.ins(second), [BOB["id"]])
        self.assertEqual(self.ins(third), [])

    def start_id_of(self, title):
        return next(rc.id for rc in self.manager.get_rollcalls(CHAT_ID) if rc.title == title)

    def test_every_rollcall_route_has_a_by_id_twin(self):
        rc_id = self.start("Game")
        base = f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{rc_id}"
        admin = {"admin_user_id": ALICE["id"], "admin_name": ALICE["name"]}
        self.assertEqual(self.client.get(base, headers=self.auth).json()["id"], rc_id)
        self.assertEqual(self.vote(f"{base}/votes").status_code, 201)
        r = self.client.post(f"{base}/proxy-votes", headers=self.auth,
                             json={"vote": "in", "proxy_name": "Guesty", **admin})
        self.assertEqual(r.status_code, 201, r.text)
        for suffix, body in (("limit", {"limit": 5}), ("location", {"location": "Turf"}),
                             ("fee", {"fee": "600"}), ("title", {"title": "Renamed"}),
                             ("reminder", {"hours": None}), ("when", {"datetime_str": "cancel"})):
            r = self.client.put(f"{base}/settings/{suffix}", headers=self.auth, json={**body, **admin})
            self.assertIn(r.status_code, (200,), f"{suffix}: {r.text}")
        r = self.client.patch(f"{base}/users/Bob/status", headers=self.auth,
                              json={"new_status": "out", **admin})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.request("DELETE", f"{base}/users/Guesty", headers=self.auth, json=admin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.end(base).status_code, 200)
        self.assertEqual(self.client.get(base, headers=self.auth).status_code, 404)

    def test_responses_carry_the_id_and_old_paths_say_deprecated(self):
        rc_id = self.start("Game")
        new = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{rc_id}", headers=self.auth)
        old = self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/1", headers=self.auth)
        self.assertEqual((new.headers["X-Rollcall-Id"], old.headers["X-Rollcall-Id"]), (str(rc_id), str(rc_id)))
        self.assertNotIn("Deprecation", new.headers)
        self.assertEqual(old.headers["Deprecation"], "true")
        self.assertIn(f"/rollcalls/by-id/{rc_id}", old.headers["Link"])
        spec = self.client.app.openapi()["paths"]
        self.assertTrue(spec["/api/v1/chats/{chat_id}/rollcalls/{rc_number}/votes"]["post"].get("deprecated"))
        self.assertFalse(spec["/api/v1/chats/{chat_id}/rollcalls/by-id/{rollcall_id}/votes"]["post"].get("deprecated"))

    def test_old_number_paths_behave_exactly_as_before(self):
        self.assertEqual(self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/1", headers=self.auth)
                         .json()["error"], "rollCallNotStarted")
        self.start("Only")
        self.assertEqual(self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/9", headers=self.auth).status_code, 422)
        self.assertEqual(self.client.get(f"/api/v1/chats/{CHAT_ID}/rollcalls/0", headers=self.auth).status_code, 422)
        self.assertEqual(self.vote(f"/api/v1/chats/{CHAT_ID}/rollcalls/1/votes").status_code, 201)


class TestIdsAreScopedToTheChat(_Base):

    def test_another_chats_rollcall_id_is_not_found(self):
        other_auth = {"Authorization": f"Bearer {_token(OTHER_CHAT)}"}
        theirs = self.start("Theirs", chat_id=OTHER_CHAT, auth=other_auth)
        # Our token, our chat in the path, their rollcall id: not ours to touch.
        r = self.vote(f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{theirs}/votes")
        self.assertEqual(r.status_code, 404)
        # Their chat in the path with our token: refused by the token check.
        r = self.vote(f"/api/v1/chats/{OTHER_CHAT}/rollcalls/by-id/{theirs}/votes")
        self.assertEqual(r.status_code, 403)

    def test_no_token_learns_nothing_about_which_rollcalls_exist(self):
        rc_id = self.start("Game")
        for path in (f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{rc_id}",
                     f"/api/v1/chats/{CHAT_ID}/rollcalls/by-id/{rc_id + 999}"):
            r = self.client.get(path)
            self.assertIn(r.status_code, (401, 403), path)


if __name__ == "__main__":
    unittest.main()
