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

    def test_csp_allows_the_origins_the_pages_actually_load(self):
        """A CSP violation is a console message in the visitor's browser and
        NOTHING on the server, so getting this wrong fails in total silence.

        It did: 'self'-only script-src blocked telegram.org, so the Mini App
        SDK never loaded, window.Telegram never existed, and a member opening
        from Telegram was treated as an anonymous visitor — asked to sign in,
        then asked to paste a group link. The Login Widget could never render
        either, and every page fell back to system fonts.
        """
        csp = _client().get("/web/").headers["Content-Security-Policy"]
        directives = {
            d.strip().split(" ")[0]: d.strip()
            for d in csp.split(";") if d.strip()
        }
        self.assertIn("https://telegram.org", directives["script-src"],
                      "Mini App SDK + Login Widget script would be blocked")
        self.assertIn("https://oauth.telegram.org", directives["frame-src"],
                      "the Login Widget's iframe would be blocked")
        self.assertIn("https://fonts.googleapis.com", directives["style-src"],
                      "the webfont stylesheet would be blocked")
        self.assertIn("https://fonts.gstatic.com", directives["font-src"],
                      "the font files themselves would be blocked")

    def test_csp_did_not_become_a_free_for_all(self):
        """Allowing the four origins above must not turn into allowing any."""
        csp = _client().get("/web/").headers["Content-Security-Policy"]
        self.assertNotIn("script-src 'self' 'unsafe-inline' *", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        # Nothing may post a form off-origin, and no data: script sources.
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline' data:", csp)

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



class TestStaticCacheRevalidation(unittest.TestCase):
    """Cache-Control on code assets (added 2026-09-04).

    The deploy that day shipped correctly and still didn't reach anyone:
    Cloudflare had a 24 July copy of /shared/tokens.css and kept serving it,
    because StaticFiles sends validators but no Cache-Control and the edge
    filled the gap with its own four-hour default. The same URL returned the
    old or the new type scale depending on which edge node answered.

    This is the third delivery failure of the same shape in this app — the
    service worker pinning app.js, `make up` recreating from a stale image,
    now the CDN — each of which reported a successful deploy while the change
    sat invisible to users. Hence a test at the layer that can assert it.
    """

    def test_stylesheets_and_scripts_must_be_revalidated(self):
        for path in ("/shared/tokens.css", "/web/style.css", "/web/app.js"):
            with self.subTest(path=path):
                resp = _client().get(path)
                if resp.status_code == 404:
                    self.skipTest(f"{path} not mounted in this build")
                cc = resp.headers.get("Cache-Control", "")
                self.assertIn(
                    "no-cache", cc,
                    f"{path} left to the CDN's default TTL — a deploy would "
                    f"take up to 4h to reach visitors, and inconsistently",
                )

    def test_html_is_revalidated_too(self):
        """The HTML references the CSS, so pinning it stale pins the page."""
        resp = _client().get("/web/")
        self.assertIn("no-cache", resp.headers.get("Cache-Control", ""))

    def test_validators_are_present_so_revalidation_is_cheap(self):
        """no-cache is only affordable because an unchanged file answers 304
        with no body. That needs ETag or Last-Modified — without them every
        request would re-download the asset in full."""
        resp = _client().get("/web/style.css")
        self.assertTrue(
            resp.headers.get("ETag") or resp.headers.get("Last-Modified"),
            "no validator: revalidation would refetch the whole file",
        )

    def test_a_route_that_chose_no_store_keeps_it(self):
        """setdefault, not assignment.

        Routes that build a response per request — the per-group PWA manifest,
        the dues QR — send `no-store` deliberately. Revalidation is weaker than
        no-store, so overwriting it would let a CDN hold a response that was
        built for one group and one member. Driven through the middleware
        directly because those routes 404 without live data, and a test that
        skips protects nothing.
        """
        import asyncio
        from fastapi import Response as FastAPIResponse
        from api.security_headers import security_headers_middleware

        class _Req:
            class url:
                path = "/web/group/tok/manifest.json"

        async def _call_next(_req):
            r = FastAPIResponse(content="{}", media_type="application/manifest+json")
            r.headers["Cache-Control"] = "no-store"
            return r

        resp = asyncio.run(security_headers_middleware(_Req(), _call_next))
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_a_plain_asset_does_get_the_revalidate(self):
        """The other half of the setdefault: nothing set, so we set it."""
        import asyncio
        from fastapi import Response as FastAPIResponse
        from api.security_headers import security_headers_middleware

        class _Req:
            class url:
                path = "/shared/tokens.css"

        async def _call_next(_req):
            return FastAPIResponse(content="html{}", media_type="text/css")

        resp = asyncio.run(security_headers_middleware(_Req(), _call_next))
        self.assertEqual(resp.headers.get("Cache-Control"), "no-cache, must-revalidate")

    def test_api_responses_are_left_alone(self):
        resp = _client().get("/api/v1/health")
        self.assertNotIn("must-revalidate", resp.headers.get("Cache-Control", ""))
