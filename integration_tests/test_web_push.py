"""
Integration tests for the web-push subscribe/unsubscribe endpoints:
  POST /web/group/{token}/push-subscribe
  POST /web/group/{token}/push-unsubscribe

Regression: push_unsubscribe used to accept any group_token in the path —
including one that doesn't resolve to a real chat — because it never looked
the token up (push_svc.unsubscribe only needs the endpoint, not the group).
push_subscribe already validated the token with a 404; unsubscribe now does
the same for consistency, even though it doesn't use the chat afterward.
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db

CHAT_ID = -1001999000900


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    from api.main import app
    import db
    return {"app": app, "db": db}


class TestWebPushSubscribe(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.db = env["db"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        chat = self.db.get_or_create_chat(CHAT_ID)
        self.token = chat["group_web_token"]

    def _sub_body(self, endpoint="https://push.example.com/abc123456789"):
        return {
            "endpoint": endpoint,
            "keys": {"p256dh": "p256dh-key-value", "auth": "auth-key-value"},
        }

    def test_subscribe_with_invalid_token_returns_404(self):
        resp = self.client.post(
            "/api/v1/web/group/not-a-real-token/push-subscribe",
            json=self._sub_body(),
        )
        self.assertEqual(resp.status_code, 404)

    def test_subscribe_with_valid_token_succeeds(self):
        resp = self.client.post(
            f"/api/v1/web/group/{self.token}/push-subscribe",
            json=self._sub_body(),
        )
        self.assertEqual(resp.status_code, 204)

    def test_unsubscribe_with_invalid_token_returns_404(self):
        resp = self.client.post(
            "/api/v1/web/group/not-a-real-token/push-unsubscribe",
            json={"endpoint": "https://push.example.com/abc123456789"},
        )
        self.assertEqual(resp.status_code, 404)

    def test_unsubscribe_with_valid_token_succeeds(self):
        endpoint = "https://push.example.com/xyz987654321"
        self.client.post(
            f"/api/v1/web/group/{self.token}/push-subscribe",
            json=self._sub_body(endpoint=endpoint),
        )
        resp = self.client.post(
            f"/api/v1/web/group/{self.token}/push-unsubscribe",
            json={"endpoint": endpoint},
        )
        self.assertEqual(resp.status_code, 204)


if __name__ == "__main__":
    unittest.main()


class TestPushSubscribeIdentity(unittest.TestCase):
    """The subscription's identity must come from a signed token, never from
    the request body.

    push-subscribe used to accept a raw `tg_user_id`, so anyone holding the
    group's magic link could file a subscription under another member's
    identity. Harmless while push is only ever broadcast per group — and
    exactly the sort of thing that stops being harmless the day a per-user
    notification lands. Guests must still be able to subscribe unlinked.
    """

    OWNER_ID = 7001
    IMPERSONATED_ID = 7002

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.db = env["db"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        import os
        from unittest.mock import patch
        reset_db()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.enterContext(patch.dict(
            os.environ,
            {"TELEGRAM_TOKEN": "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"},
        ))
        self.token = self.db.get_or_create_chat(CHAT_ID)["group_web_token"]

    def _subscribe(self, endpoint, **extra):
        return self.client.post(
            f"/api/v1/web/group/{self.token}/push-subscribe",
            json={"endpoint": endpoint,
                  "keys": {"p256dh": "p256dh-key-value", "auth": "auth-key-value"},
                  **extra},
        )

    def _stored_uid(self, endpoint):
        with self.db._cursor() as cur:
            ph = '%s' if self.db.db_type == 'postgresql' else '?'
            cur.execute(
                f"SELECT tg_user_id FROM push_subscriptions WHERE endpoint = {ph}",
                (endpoint,),
            )
            row = cur.fetchone()
        return None if row is None else (row[0] if not isinstance(row, dict) else row["tg_user_id"])

    def test_signed_token_links_the_subscription(self):
        from api.identity import issue_identity_token
        ep = "https://push.example.com/linked-000001"
        self.assertEqual(self._subscribe(ep, id_token=issue_identity_token(self.OWNER_ID)).status_code, 204)
        self.assertEqual(self._stored_uid(ep), self.OWNER_ID)

    def test_raw_tg_user_id_in_body_is_ignored(self):
        """The old field must not link anything — a stale cached client that
        still sends it gets an unlinked subscription, not a forged one."""
        ep = "https://push.example.com/rawid-000002"
        self.assertEqual(self._subscribe(ep, tg_user_id=self.IMPERSONATED_ID).status_code, 204)
        self.assertIsNone(self._stored_uid(ep))

    def test_forged_token_does_not_link(self):
        ep = "https://push.example.com/forged-000003"
        self.assertEqual(self._subscribe(ep, id_token="not.a.real.token").status_code, 204)
        self.assertIsNone(self._stored_uid(ep))

    def test_guest_without_any_token_still_subscribes(self):
        ep = "https://push.example.com/guest-000004"
        self.assertEqual(self._subscribe(ep).status_code, 204)
        self.assertIsNone(self._stored_uid(ep))

    def test_cannot_claim_another_members_identity(self):
        """A token for one member never links a subscription to a different one."""
        from api.identity import issue_identity_token
        ep = "https://push.example.com/mismatch-00005"
        self._subscribe(ep, id_token=issue_identity_token(self.OWNER_ID),
                        tg_user_id=self.IMPERSONATED_ID)
        self.assertEqual(self._stored_uid(ep), self.OWNER_ID)
