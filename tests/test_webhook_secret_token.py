"""
Telegram echoes WEBHOOK_SECRET_TOKEN back as X-Telegram-Bot-Api-Secret-Token
on every genuine webhook call. webhook_handler must reject anything that
doesn't carry the matching value — without this, anyone who discovers the
(not itself secret) webhook URL could POST forged Updates.

Note: aiohttp is globally mocked in conftest.py (sys.modules["aiohttp"] =
MagicMock()), so web.Response(status=403).status is itself a MagicMock, not
a real int — assertions below check what web.Response was CALLED with,
not the mock object it returns.
"""
import importlib.util
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class _FakeRequest:
    def __init__(self, content_type="application/json", secret_header=None, body=None):
        self.content_type = content_type
        self.headers = {}
        if secret_header is not None:
            self.headers["X-Telegram-Bot-Api-Secret-Token"] = secret_header
        self._body = body if body is not None else {"update_id": 1}

    async def json(self):
        return self._body


class TestWebhookSecretToken(unittest.IsolatedAsyncioTestCase):

    async def test_missing_secret_header_rejected(self):
        import runner
        with patch.object(runner, "WEBHOOK_SECRET_TOKEN", "expected-secret"), \
             patch.object(runner, "web") as mock_web:
            await runner.webhook_handler(_FakeRequest(secret_header=None))
        mock_web.Response.assert_called_once_with(status=403)

    async def test_wrong_secret_header_rejected(self):
        import runner
        with patch.object(runner, "WEBHOOK_SECRET_TOKEN", "expected-secret"), \
             patch.object(runner, "web") as mock_web:
            await runner.webhook_handler(_FakeRequest(secret_header="wrong-value"))
        mock_web.Response.assert_called_once_with(status=403)

    async def test_correct_secret_header_accepted(self):
        import runner
        with patch.object(runner, "WEBHOOK_SECRET_TOKEN", "expected-secret"), \
             patch.object(runner, "bot", MagicMock(process_new_updates=AsyncMock())), \
             patch.object(runner, "web") as mock_web:
            await runner.webhook_handler(_FakeRequest(secret_header="expected-secret"))
        mock_web.Response.assert_called_once_with(status=200)

    async def test_wrong_content_type_still_rejected_before_secret_check(self):
        import runner
        with patch.object(runner, "WEBHOOK_SECRET_TOKEN", "expected-secret"), \
             patch.object(runner, "web") as mock_web:
            await runner.webhook_handler(_FakeRequest(content_type="text/plain", secret_header="expected-secret"))
        mock_web.Response.assert_called_once_with(status=403)


def _load_config_isolated(env):
    """Load rollCall/config.py fresh from disk with a specific environment,
    bypassing sys.modules entirely (it's globally mocked for every other
    test in this suite via conftest.py) so this test doesn't fight that."""
    config_path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "config.py")
    spec = importlib.util.spec_from_file_location("config_isolated_test", config_path)
    module = importlib.util.module_from_spec(spec)
    base_env = {"TELEGRAM_TOKEN": "123456:test", "DATABASE_URL": "sqlite:///:memory:"}
    with patch.dict(os.environ, {**base_env, **env}, clear=False):
        spec.loader.exec_module(module)
    return module


class TestWebhookSecretTokenConfig(unittest.TestCase):

    def test_none_when_webhook_disabled(self):
        cfg = _load_config_isolated({"WEBHOOK_URL": ""})
        self.assertIsNone(cfg.WEBHOOK_SECRET_TOKEN)

    def test_auto_generated_when_webhook_enabled_and_unset(self):
        cfg = _load_config_isolated({"WEBHOOK_URL": "https://example.com/webhook", "WEBHOOK_SECRET_TOKEN": ""})
        self.assertTrue(cfg.WEBHOOK_SECRET_TOKEN)
        self.assertGreaterEqual(len(cfg.WEBHOOK_SECRET_TOKEN), 32)

    def test_explicit_env_value_respected(self):
        cfg = _load_config_isolated({"WEBHOOK_URL": "https://example.com/webhook", "WEBHOOK_SECRET_TOKEN": "my-fixed-secret"})
        self.assertEqual(cfg.WEBHOOK_SECRET_TOKEN, "my-fixed-secret")


if __name__ == "__main__":
    unittest.main()
