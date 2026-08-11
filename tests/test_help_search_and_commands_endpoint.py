"""
Unit tests for the /help category/keyword search helpers (handlers/core.py)
and the GET /api/v1/commands endpoint (api/routes/commands.py) that backs
the /help/ web page. Both source directly from commands_registry.py.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


class TestHelpSearchHelpers(unittest.TestCase):

    def test_search_by_category_matches_case_insensitive_substring(self):
        import handlers.core as core
        results = core._search_by_category("dues")
        self.assertTrue(results)
        self.assertTrue(all(c["category"] == "Dues & Fund" for c in results))

    def test_search_by_category_no_match_returns_empty(self):
        import handlers.core as core
        self.assertEqual(core._search_by_category("zzzznotacategory"), [])

    def test_search_by_keyword_matches_summary_or_details(self):
        import handlers.core as core
        results = core._search_by_keyword("ghost")
        self.assertTrue(results)
        names = {c["name"] for c in results}
        self.assertIn("toggle_ghost_tracking", names)

    def test_search_by_keyword_respects_limit(self):
        import handlers.core as core
        # "the" is broad enough to hit way more than 3 commands' summaries/details.
        results = core._search_by_keyword("the", limit=3)
        self.assertLessEqual(len(results), 3)

    def test_quick_start_renders_real_examples(self):
        import handlers.core as core
        text = core._render_quick_start()
        self.assertIn("Quick start", text)
        # Every quick-start name must actually resolve in the registry --
        # a typo'd name here would silently render nothing for that line.
        from commands_registry import lookup_command
        for name in core._QUICK_START_NAMES:
            self.assertIsNotNone(lookup_command(name), f"quick-start references unknown command {name!r}")


class TestHelpWebUrl(unittest.TestCase):
    """/help (list views + the dead-end fallback) links to the web command
    reference when WEB_BASE_URL is configured, and omits it cleanly for
    self-hosted bots with no public web deployment -- same env-var/empty-
    string-if-unset convention as handlers.lifecycle._group_web_url."""

    def test_web_url_present_when_configured(self):
        import handlers.core as core
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rollcall.example.com/"}):
            self.assertEqual(core._help_web_url(), "https://rollcall.example.com/help")

    def test_web_url_empty_when_unset(self):
        import handlers.core as core
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEB_BASE_URL", None)
            self.assertEqual(core._help_web_url(), "")

    def test_list_view_includes_link_when_configured(self):
        import handlers.core as core
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rollcall.example.com"}):
            text = core._render_command_list({"user"}, core.USER_CATEGORY_ORDER, "header")
        self.assertIn("https://rollcall.example.com/help", text)

    def test_list_view_omits_link_when_unset(self):
        import handlers.core as core
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEB_BASE_URL", None)
            text = core._render_command_list({"user"}, core.USER_CATEGORY_ORDER, "header")
        self.assertNotIn("🌐", text)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestCommandsEndpoint(unittest.TestCase):

    def _client(self):
        from api.main import create_app
        return TestClient(create_app(), raise_server_exceptions=False)

    def test_returns_every_registered_command(self):
        from commands_registry import COMMANDS
        r = self._client().get("/api/v1/commands")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(len(data["commands"]), len(COMMANDS))
        returned_names = {c["name"] for c in data["commands"]}
        registry_names = {c["name"] for c in COMMANDS}
        self.assertEqual(returned_names, registry_names)

    def test_category_order_and_emoji_match_the_registry(self):
        from commands_registry import USER_CATEGORY_ORDER, ADMIN_CATEGORY_ORDER, CATEGORY_EMOJI
        r = self._client().get("/api/v1/commands")
        data = r.json()
        self.assertEqual(data["user_category_order"], USER_CATEGORY_ORDER)
        self.assertEqual(data["admin_category_order"], ADMIN_CATEGORY_ORDER)
        self.assertEqual(data["category_emoji"], CATEGORY_EMOJI)

    def test_every_category_used_by_a_command_has_an_emoji(self):
        """Regression guard: a future new category added to a command
        entry without a matching CATEGORY_EMOJI entry would render with a
        blank icon on both /help and the web page -- not a crash, just a
        silent visual gap. Catch it here instead."""
        from commands_registry import COMMANDS, CATEGORY_EMOJI
        used_categories = {c["category"] for c in COMMANDS}
        missing = used_categories - set(CATEGORY_EMOJI)
        self.assertEqual(missing, set(), f"categories missing an emoji: {missing}")

    def test_unauthenticated_access_allowed(self):
        # No token/header needed -- command docs aren't sensitive, matching
        # /help admin's existing no-auth-gate behavior in Telegram.
        r = self._client().get("/api/v1/commands")
        self.assertEqual(r.status_code, 200)

    def test_help_page_serves(self):
        r = self._client().get("/help/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("RollCall", r.text)


if __name__ == "__main__":
    unittest.main()
