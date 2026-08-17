"""Security headers on every web response (added 2026-08-17 adversarial pass)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from fastapi.testclient import TestClient


def _client():
    from api.main import create_app
    return TestClient(create_app())


class TestSecurityHeaders(unittest.TestCase):

    def test_baseline_headers_present(self):
        resp = _client().get("/api/v1/health")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")
        self.assertIn("Permissions-Policy", resp.headers)
        self.assertIn("Content-Security-Policy", resp.headers)

    def test_non_miniapp_paths_deny_framing(self):
        resp = _client().get("/api/v1/health")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", resp.headers["Content-Security-Policy"])

    def test_miniapp_allows_telegram_framing_only(self):
        """Regression: Telegram Web embeds the Mini App in an iframe, so a
        blanket DENY breaks it for web.telegram.org users. It must allow
        Telegram origins — and must NOT send X-Frame-Options, which has no
        allowlist form and would override the allowance in older browsers."""
        resp = _client().get("/miniapp/")
        csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors https://web.telegram.org", csp)
        self.assertNotIn("frame-ancestors 'none'", csp)
        self.assertIsNone(resp.headers.get("X-Frame-Options"))

    def test_csp_keeps_inline_handlers_working(self):
        """The whole front-end uses inline onclick= handlers and has no build
        step. A CSP without 'unsafe-inline' in script-src would silently kill
        every button on every page."""
        csp = _client().get("/api/v1/health").headers["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'self'", csp)

    def test_hsts_only_when_https_base_url(self):
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://roll.example.com"}):
            self.assertIn("Strict-Transport-Security", _client().get("/api/v1/health").headers)
        # Plain-HTTP or unset deployment must not pin the browser to a scheme
        # the host doesn't serve.
        with patch.dict(os.environ, {"WEB_BASE_URL": "http://localhost:8081"}):
            self.assertNotIn("Strict-Transport-Security", _client().get("/api/v1/health").headers)
        with patch.dict(os.environ, {"WEB_BASE_URL": ""}):
            self.assertNotIn("Strict-Transport-Security", _client().get("/api/v1/health").headers)

    def test_headers_applied_to_rate_limit_rejections(self):
        """The middleware is registered outermost so even responses that never
        reach a route (429s from the rate limiter) still carry headers."""
        from api import rate_limit
        rate_limit.reset_buckets_for_tests()
        with patch.dict(os.environ, {"REST_API_RATE_LIMIT_MAX_REQUESTS": "1",
                                     "REST_API_RATE_LIMIT_WINDOW_SECONDS": "60"}):
            c = _client()
            c.get("/api/v1/commands")
            resp = c.get("/api/v1/commands")
            self.assertEqual(resp.status_code, 429)
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        rate_limit.reset_buckets_for_tests()


if __name__ == "__main__":
    unittest.main()
