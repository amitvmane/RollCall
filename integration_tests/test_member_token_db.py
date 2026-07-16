"""
Real-database round trips for the persistent member login token (/mytoken):
hash-only storage, one active code per user, redeem via the real API stack.
"""
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_INTEGRATION_ONLY"


class TestMemberLoginTokenDb(unittest.TestCase):

    def test_round_trip_and_revoke(self):
        import db
        from services.web import hash_login_token
        h = hash_login_token("secret-code-1")
        self.assertTrue(db.upsert_member_login_token(9001, h, "Alice", "alice"))
        row = db.get_member_login_token_by_hash(h)
        self.assertEqual(row["user_id"], 9001)
        self.assertEqual(row["first_name"], "Alice")
        self.assertEqual(row["username"], "alice")
        db.touch_member_login_token(9001)
        self.assertTrue(db.delete_member_login_token(9001))
        self.assertIsNone(db.get_member_login_token_by_hash(h))
        self.assertFalse(db.delete_member_login_token(9001))  # already gone

    def test_reissue_replaces_previous_code(self):
        import db
        from services.web import hash_login_token
        h1, h2 = hash_login_token("old-code"), hash_login_token("new-code")
        db.upsert_member_login_token(9002, h1, "Bob", None)
        db.upsert_member_login_token(9002, h2, "Bob", None)
        self.assertIsNone(db.get_member_login_token_by_hash(h1))
        self.assertEqual(db.get_member_login_token_by_hash(h2)["user_id"], 9002)
        db.delete_member_login_token(9002)

    def test_lookup_only_by_hash_never_plaintext(self):
        import db
        from services.web import hash_login_token
        db.upsert_member_login_token(9003, hash_login_token("plain-secret"), "C", None)
        self.assertIsNone(db.get_member_login_token_by_hash("plain-secret"))
        db.delete_member_login_token(9003)


class TestMemberTokenRedeemEndToEnd(unittest.TestCase):
    """Issue → redeem → replace → revoke through the real db and API app."""

    def setUp(self):
        from api.routes.tg_verify import _verify_buckets
        _verify_buckets.clear()

    def tearDown(self):
        import db
        db.delete_member_login_token(9100)

    def _client(self):
        from api.main import app
        return TestClient(app, raise_server_exceptions=False)

    def _issue(self, code, user_id=9100, name="Dana", username="dana"):
        import db
        from services.web import hash_login_token
        db.upsert_member_login_token(user_id, hash_login_token(code), name, username)

    def test_full_lifecycle(self):
        client = self._client()
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": BOT_TOKEN}):
            # Unknown code first
            res = client.post("/api/v1/auth/member-token", json={"token": "nope"})
            self.assertEqual(res.status_code, 401)

            # Issue and redeem
            self._issue("integration-code-1")
            res = client.post("/api/v1/auth/member-token",
                              json={"token": "integration-code-1"})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["verified"])
            self.assertEqual(body["user_id"], 9100)
            self.assertEqual(body["name"], "Dana")
            from api.identity import verify_identity_token
            self.assertEqual(verify_identity_token(body["id_token"]), 9100)

            # Reissue: old code dies, new code works (reusable, not single-use)
            # (clear the per-IP verify rate bucket — this lifecycle test makes
            # more calls than the 5/min brute-force guard allows)
            from api.routes.tg_verify import _verify_buckets
            _verify_buckets.clear()
            self._issue("integration-code-2")
            self.assertEqual(client.post("/api/v1/auth/member-token",
                                         json={"token": "integration-code-1"}).status_code, 401)
            self.assertEqual(client.post("/api/v1/auth/member-token",
                                         json={"token": "integration-code-2"}).status_code, 200)
            self.assertEqual(client.post("/api/v1/auth/member-token",
                                         json={"token": "integration-code-2"}).status_code, 200)

            # Revoke kills it
            _verify_buckets.clear()
            import db
            db.delete_member_login_token(9100)
            self.assertEqual(client.post("/api/v1/auth/member-token",
                                         json={"token": "integration-code-2"}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
