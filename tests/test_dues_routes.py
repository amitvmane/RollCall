"""
Functional tests for the dues REST API GET/DELETE routes.

Before the id_token-off-URLs migration, these 8 routes (my, summary, fund,
fund/history, tiers-list, settings, tiers-delete, close-preview) had ZERO
REST-layer test coverage — only their underlying services/dues.py functions
were tested. Each gets a 200-with-valid-header case and a 401-missing-token
case here, both exercising the real auth dependency chain (no mocking of
identity verification itself) so a wiring mistake in the header migration
would actually be caught.

/dues/qr is deliberately NOT covered here — it stays on a query param by
design (an <img src> can't carry headers) and gets its own short-lived-token
tests alongside that migration.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

os.environ.setdefault("TELEGRAM_TOKEN", "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY")

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

CHAT = {"chat_id": -100, "group_web_token": "grp123", "dues_enabled": True}


def _app():
    from api.main import create_app
    return create_app()


def _client():
    return TestClient(_app(), raise_server_exceptions=False)


def _good_token(user_id):
    from api import identity
    return identity.issue_identity_token(user_id)


def _reset_rate_limit():
    from api.rate_limit import reset_buckets_for_tests
    reset_buckets_for_tests()


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestDuesGetRoutesRequireHeaderIdentity(unittest.TestCase):
    """One 200 (valid X-Identity-Token header) + one 401 (no header at all)
    per route — the 401 case exercises the real identity_from_header +
    verify_identity_token chain, not a mock, so it actually proves a
    missing header is rejected end to end."""

    def setUp(self):
        _reset_rate_limit()
        self.token = _good_token(42)

    # ── /dues/my (member, not admin) ────────────────────────────────────
    def test_my_dues_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.dues_svc.my_dues", return_value={"balance": 100, "entries": []}), \
             patch("api.routes.dues.dues_svc.get_dues_settings", return_value={}):
            r = _client().get("/api/v1/web/group/grp123/dues/my", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["balance"], 100)

    def test_my_dues_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/my")
        self.assertEqual(r.status_code, 401)

    # ── /dues/summary (admin) ───────────────────────────────────────────
    def test_summary_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.check_web_admin_live", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.dues.dues_svc.all_dues", return_value={"balances": []}), \
             patch("api.routes.dues.dues_svc.fund_summary", return_value={"fund_balance": 0}):
            r = _client().get("/api/v1/web/group/grp123/dues/summary", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)

    def test_summary_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/summary")
        self.assertEqual(r.status_code, 401)

    # ── /dues/fund (member) ─────────────────────────────────────────────
    def test_fund_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.dues_svc.fund_summary", return_value={"fund_balance": 250}):
            r = _client().get("/api/v1/web/group/grp123/dues/fund", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["fund_balance"], 250)

    def test_fund_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/fund")
        self.assertEqual(r.status_code, 401)

    # ── /dues/fund/history (admin) ──────────────────────────────────────
    def test_fund_history_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.check_web_admin_live", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.dues.dues_svc.fund_history",
                   return_value={"transactions": [], "total": 0, "limit": 15, "offset": 0}), \
             patch("api.routes.dues.dues_svc.fund_summary", return_value={"fund_balance": 0}):
            r = _client().get("/api/v1/web/group/grp123/dues/fund/history", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)

    def test_fund_history_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/fund/history")
        self.assertEqual(r.status_code, 401)

    # ── /dues/tiers (admin, list) ───────────────────────────────────────
    def test_tiers_list_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.check_web_admin_live", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.dues.dues_svc.list_penalty_tiers", return_value={"tiers": []}):
            r = _client().get("/api/v1/web/group/grp123/dues/tiers", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)

    def test_tiers_list_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/tiers")
        self.assertEqual(r.status_code, 401)

    # ── /dues/settings (admin, GET) ─────────────────────────────────────
    def test_settings_get_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.check_web_admin_live", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.dues.dues_svc.get_dues_settings", return_value={}):
            r = _client().get("/api/v1/web/group/grp123/dues/settings", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)

    def test_settings_get_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/settings")
        self.assertEqual(r.status_code, 401)

    # ── /dues/tiers/{tier_name} (admin, DELETE) ─────────────────────────
    def test_tiers_delete_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.check_web_admin_live", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.dues.dues_svc.remove_penalty_tier", return_value=None):
            r = _client().delete("/api/v1/web/group/grp123/dues/tiers/late_short",
                                  headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 204)

    def test_tiers_delete_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().delete("/api/v1/web/group/grp123/dues/tiers/late_short")
        self.assertEqual(r.status_code, 401)

    # ── /dues/close-preview (admin) ─────────────────────────────────────
    def test_close_preview_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.check_web_admin_live", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.dues.dues_svc.close_preview", return_value={"available": False}):
            r = _client().get("/api/v1/web/group/grp123/dues/close-preview",
                               headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["available"])

    def test_close_preview_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/close-preview")
        self.assertEqual(r.status_code, 401)

    # ── /dues/qr-token (member) — mints the short-lived token the QR <img>
    # embeds, so the long-lived id_token never appears in that URL ────────
    def test_qr_token_200_with_valid_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/qr-token", headers={"X-Identity-Token": self.token})
        self.assertEqual(r.status_code, 200)
        short_token = r.json()["token"]
        self.assertNotEqual(short_token, self.token)
        # The minted token verifies to the same user id under its own scope...
        from api.identity import verify_scoped_token, verify_identity_token
        self.assertEqual(verify_scoped_token(short_token, "dues_qr"), 42)
        # ...but must NOT work as a general-purpose identity token — it's
        # scoped to this one purpose so a leak (it's embedded in a URL)
        # can't be replayed against other identity-gated endpoints.
        self.assertIsNone(verify_identity_token(short_token))

    def test_qr_token_is_short_lived_not_the_30_day_ttl(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/qr-token", headers={"X-Identity-Token": self.token})
        short_token = r.json()["token"]
        short_exp = int(short_token.split(".")[1])
        full_exp = int(self.token.split(".")[1])
        # 5-minute token must expire long before the 30-day one.
        self.assertLess(short_exp, full_exp)

    def test_qr_token_401_without_header(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/qr-token")
        self.assertEqual(r.status_code, 401)

    # ── /dues/qr (the image itself) ──────────────────────────────────────
    # The token routes above were covered; the route that actually renders
    # the PNG was not, and it 500'd in production on every single call —
    # qr_png() returns a BytesIO and Response only renders str/bytes, so
    # starlette raised "'_io.BytesIO' object has no attribute 'encode'".
    # Assert real PNG bytes come back, not just a 200.
    def _qr_token(self):
        from api.identity import issue_scoped_token
        return issue_scoped_token(42, "dues_qr", ttl_seconds=300)

    def test_qr_returns_real_png_bytes(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.dues_svc.get_dues_settings", return_value={"upi_vpa": "someone@upi"}):
            r = _client().get(f"/api/v1/web/group/grp123/dues/qr?id_token={self._qr_token()}&amount=250")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertTrue(r.content.startswith(b"\x89PNG"), "body is not a PNG")

    def test_qr_without_amount_also_renders(self):
        """amount=0 means 'payer types the amount' — it must not blow up and
        must not put a literal `am=None` in the UPI URL."""
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.dues_svc.get_dues_settings", return_value={"upi_vpa": "someone@upi"}):
            r = _client().get(f"/api/v1/web/group/grp123/dues/qr?id_token={self._qr_token()}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.content.startswith(b"\x89PNG"))

    def test_qr_rejects_a_plain_identity_token(self):
        """The image URL carries its token in the query string, so only the
        narrow dues_qr scope may open it."""
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT), \
             patch("api.routes.dues.dues_svc.get_dues_settings", return_value={"upi_vpa": "someone@upi"}):
            r = _client().get(f"/api/v1/web/group/grp123/dues/qr?id_token={self.token}")
        self.assertEqual(r.status_code, 401)

    # ── Sanity: an invalid (garbage, not just missing) header is also 401 ─
    def test_invalid_header_token_also_401(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/my", headers={"X-Identity-Token": "garbage.not.valid"})
        self.assertEqual(r.status_code, 401)
