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
