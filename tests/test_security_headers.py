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

    def test_web_app_allows_telegram_framing(self):
        """The group web page IS the Mini App now — the Telegram menu button
        points at it. Telegram Web embeds Mini Apps in an iframe, so serving
        /web with frame-ancestors 'none' renders a blank panel inside
        web.telegram.org, and the only sign of it is a console message in a
        client we don't control."""
        for path in ("/web/", "/web/group/sometoken"):
            with self.subTest(path=path):
                resp = _client().get(path)
                csp = resp.headers.get("Content-Security-Policy", "")
                self.assertIn("frame-ancestors https://web.telegram.org", csp)
                self.assertNotIn("frame-ancestors 'none'", csp)
                self.assertIsNone(resp.headers.get("X-Frame-Options"))

    def test_web_app_still_refuses_everyone_else(self):
        """Allowing Telegram must not become allowing anyone: an attacker's
        page still cannot embed the voting UI."""
        csp = _client().get("/web/").headers.get("Content-Security-Policy", "")
        self.assertNotIn("frame-ancestors *", csp)
        self.assertNotIn("'self'", csp.split("frame-ancestors")[1])

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


class TestRetiredMiniapp(unittest.TestCase):
    """The standalone Mini App is retired, but MINIAPP_URL lives in each
    deployment's .env and the menu button is registered with Telegram at
    startup — so a deployment that upgrades without editing its env would
    have every member's "Open RollCall" button land on a 404."""

    def test_miniapp_redirects_to_the_web_app(self):
        for path in ("/miniapp", "/miniapp/", "/miniapp/index.html"):
            with self.subTest(path=path):
                resp = _client().get(path, follow_redirects=False)
                self.assertIn(resp.status_code, (301, 302, 307, 308),
                              f"{path} returned {resp.status_code}, not a redirect")
                self.assertTrue(resp.headers.get("location", "").startswith("/web"),
                                f"{path} redirected to {resp.headers.get('location')}")


if __name__ == "__main__":
    unittest.main()

