"""
CORS_ALLOWED_ORIGINS defaults to WEB_BASE_URL (already required for web
voting) instead of a blanket "*" — identity tokens are long-lived (30 days)
and can end up in URLs (access logs, Referer headers); a wildcard origin
means a leaked token also grants cross-origin readable responses, not just
the ability to send the request.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _cors_allow_origins(app):
    from starlette.middleware.cors import CORSMiddleware
    for mw in app.user_middleware:
        if mw.cls is CORSMiddleware:
            return list(mw.kwargs["allow_origins"])
    raise AssertionError("CORSMiddleware not found on app")


class TestCorsDefault(unittest.TestCase):

    def _build_app(self, env):
        from api.main import create_app
        base_env = {"TELEGRAM_TOKEN": "123456:test"}
        with patch.dict(os.environ, {**base_env, **env}, clear=False):
            return create_app()

    def test_defaults_to_web_base_url_when_set(self):
        app = self._build_app({"WEB_BASE_URL": "https://rbot.example.com", "CORS_ALLOWED_ORIGINS": ""})
        self.assertEqual(_cors_allow_origins(app), ["https://rbot.example.com"])

    def test_strips_trailing_slash_from_web_base_url(self):
        app = self._build_app({"WEB_BASE_URL": "https://rbot.example.com/", "CORS_ALLOWED_ORIGINS": ""})
        self.assertEqual(_cors_allow_origins(app), ["https://rbot.example.com"])

    def test_falls_back_to_wildcard_when_neither_set(self):
        app = self._build_app({"WEB_BASE_URL": "", "CORS_ALLOWED_ORIGINS": ""})
        self.assertEqual(_cors_allow_origins(app), ["*"])

    def test_explicit_cors_env_wins_over_web_base_url(self):
        app = self._build_app({
            "WEB_BASE_URL": "https://rbot.example.com",
            "CORS_ALLOWED_ORIGINS": "https://other.example.com,https://third.example.com",
        })
        self.assertEqual(_cors_allow_origins(app), ["https://other.example.com", "https://third.example.com"])


if __name__ == "__main__":
    unittest.main()
