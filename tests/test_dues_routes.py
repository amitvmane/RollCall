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

    # ── Sanity: an invalid (garbage, not just missing) header is also 401 ─
    def test_invalid_header_token_also_401(self):
        with patch("api.routes.dues._db.get_chat_by_group_web_token", return_value=CHAT):
            r = _client().get("/api/v1/web/group/grp123/dues/my", headers={"X-Identity-Token": "garbage.not.valid"})
        self.assertEqual(r.status_code, 401)
