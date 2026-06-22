"""
Functional tests for the web voting REST API routes and page routes.

Uses FastAPI's TestClient to exercise the full HTTP stack including:
  - Public (no auth) per-rollcall endpoints: GET/POST /api/v1/web/{token}
  - Public (no auth) group endpoint: GET /api/v1/web/group/{group_token}
  - HTML page routes: GET /web/join/{token}, GET /web/group/{token}

Services are patched at their module path so tests run without a real
database or Telegram connection.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_WEB_RC_DICT = {
    "rollcall_id": 42,
    "web_token": "abc123rollcalltoken",
    "title": "Friday Football",
    "finalize_date": None,
    "limit": None,
    "location": None,
    "in": [],
    "out": [],
    "maybe": [],
    "waiting": [],
}

_WEB_RC_DICT_WITH_USERS = {
    "rollcall_id": 42,
    "web_token": "abc123rollcalltoken",
    "title": "Friday Football",
    "finalize_date": "Sunday, 21 Jun at 18:00",
    "limit": 10,
    "location": "Central Park",
    "in": [{"name": "Alice", "comment": "on time"}],
    "out": [{"name": "Bob", "comment": ""}],
    "maybe": [],
    "waiting": [],
}

_GROUP_DICT_EMPTY = {
    "group_token": "mygrouptoken000",
    "rollcalls": [],
}

_GROUP_DICT_ONE = {
    "group_token": "mygrouptoken000",
    "rollcalls": [_WEB_RC_DICT],
}

_GROUP_DICT_MULTI = {
    "group_token": "mygrouptoken000",
    "rollcalls": [
        {**_WEB_RC_DICT, "rollcall_id": 1, "title": "Morning Run",  "web_token": "tok1"},
        {**_WEB_RC_DICT, "rollcall_id": 2, "title": "Evening Game", "web_token": "tok2"},
        {**_WEB_RC_DICT, "rollcall_id": 3, "title": "Weekend Trip", "web_token": "tok3"},
    ],
}


def _app():
    from api.main import create_app
    return create_app()


def _client():
    return TestClient(_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# GET /api/v1/web/{token}  — per-rollcall state
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestGetWebRollcall(unittest.TestCase):

    def test_valid_token_returns_200(self):
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        self.assertEqual(resp.status_code, 200)

    def test_response_contains_title(self):
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        data = resp.json()
        self.assertEqual(data["title"], "Friday Football")

    def test_response_contains_lists(self):
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        data = resp.json()
        self.assertIn("in", data)
        self.assertIn("out", data)
        self.assertIn("maybe", data)
        self.assertIn("waiting", data)

    def test_response_contains_web_token(self):
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        self.assertEqual(resp.json()["web_token"], "abc123rollcalltoken")

    def test_response_with_users_and_metadata(self):
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT_WITH_USERS):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        data = resp.json()
        self.assertEqual(data["limit"], 10)
        self.assertEqual(data["location"], "Central Park")
        self.assertEqual(data["finalize_date"], "Sunday, 21 Jun at 18:00")
        self.assertEqual(len(data["in"]), 1)
        self.assertEqual(data["in"][0]["name"], "Alice")
        self.assertEqual(data["in"][0]["comment"], "on time")
        self.assertEqual(len(data["out"]), 1)

    def test_invalid_token_returns_422(self):
        from exceptions import incorrectParameter
        with patch("services.web.get_rollcall_by_token",
                   side_effect=incorrectParameter("invalid or expired")):
            resp = _client().get("/api/v1/web/badtoken")
        self.assertEqual(resp.status_code, 422)
        self.assertIn("invalid", resp.json()["detail"].lower())

    def test_no_auth_header_required(self):
        """Web endpoints are public — no Authorization header needed."""
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        self.assertNotEqual(resp.status_code, 401)
        self.assertNotEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# POST /api/v1/web/{token}/vote  — submit vote
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestVoteWebRollcall(unittest.TestCase):

    def _post(self, token, body, svc_return=None, svc_side_effect=None):
        mock_fn = AsyncMock(
            return_value=svc_return or _WEB_RC_DICT,
            side_effect=svc_side_effect,
        )
        with patch("services.web.vote_by_token", mock_fn):
            resp = _client().post(f"/api/v1/web/{token}/vote", json=body)
        return resp, mock_fn

    def test_vote_in_returns_200(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "Alice", "vote": "in"})
        self.assertEqual(resp.status_code, 200)

    def test_vote_out_returns_200(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "Bob", "vote": "out"})
        self.assertEqual(resp.status_code, 200)

    def test_vote_maybe_returns_200(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "Carol", "vote": "maybe"})
        self.assertEqual(resp.status_code, 200)

    def test_service_called_with_correct_args(self):
        _, mock_fn = self._post("mytok", {"name": "Dave", "vote": "in"})
        mock_fn.assert_awaited_once_with("mytok", "Dave", "in", tg_user_id=None)

    def test_response_is_updated_rollcall(self):
        updated = {**_WEB_RC_DICT, "in": [{"name": "Alice", "comment": ""}]}
        resp, _ = self._post("abc123rollcalltoken", {"name": "Alice", "vote": "in"},
                             svc_return=updated)
        data = resp.json()
        self.assertEqual(len(data["in"]), 1)
        self.assertEqual(data["in"][0]["name"], "Alice")

    def test_invalid_vote_type_returns_422(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "Alice", "vote": "abstain"})
        self.assertEqual(resp.status_code, 422)

    def test_empty_name_rejected_by_pydantic_422(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "", "vote": "in"})
        self.assertEqual(resp.status_code, 422)

    def test_name_too_long_rejected_by_pydantic_422(self):
        resp, _ = self._post("abc123rollcalltoken",
                             {"name": "A" * 65, "vote": "in"})
        self.assertEqual(resp.status_code, 422)

    def test_missing_name_field_returns_422(self):
        resp, _ = self._post("abc123rollcalltoken", {"vote": "in"})
        self.assertEqual(resp.status_code, 422)

    def test_missing_vote_field_returns_422(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "Alice"})
        self.assertEqual(resp.status_code, 422)

    def test_invalid_token_returns_422(self):
        from exceptions import incorrectParameter
        resp, _ = self._post("badtoken", {"name": "Alice", "vote": "in"},
                             svc_side_effect=incorrectParameter("invalid"))
        self.assertEqual(resp.status_code, 422)

    def test_no_auth_header_required(self):
        resp, _ = self._post("abc123rollcalltoken", {"name": "Alice", "vote": "in"})
        self.assertNotEqual(resp.status_code, 401)
        self.assertNotEqual(resp.status_code, 403)

    def test_name_at_max_length_accepted(self):
        """64-char name is exactly at the limit — should be accepted."""
        resp, _ = self._post("abc123rollcalltoken",
                             {"name": "A" * 64, "vote": "in"})
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# GET /api/v1/web/group/{group_token}  — group state
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestGetWebGroup(unittest.TestCase):

    def _get(self, group_token, svc_return=None, svc_side_effect=None):
        with patch("services.web.get_rollcalls_by_group_token",
                   return_value=svc_return,
                   side_effect=svc_side_effect):
            resp = _client().get(f"/api/v1/web/group/{group_token}")
        return resp

    def test_valid_token_empty_returns_200(self):
        resp = self._get("mygrouptoken000", svc_return=_GROUP_DICT_EMPTY)
        self.assertEqual(resp.status_code, 200)

    def test_empty_rollcalls_list(self):
        resp = self._get("mygrouptoken000", svc_return=_GROUP_DICT_EMPTY)
        data = resp.json()
        self.assertEqual(data["rollcalls"], [])
        self.assertEqual(data["group_token"], "mygrouptoken000")

    def test_single_rollcall_returned(self):
        resp = self._get("mygrouptoken000", svc_return=_GROUP_DICT_ONE)
        data = resp.json()
        self.assertEqual(len(data["rollcalls"]), 1)
        self.assertEqual(data["rollcalls"][0]["title"], "Friday Football")

    def test_multiple_rollcalls_returned(self):
        resp = self._get("mygrouptoken000", svc_return=_GROUP_DICT_MULTI)
        data = resp.json()
        self.assertEqual(len(data["rollcalls"]), 3)
        titles = [r["title"] for r in data["rollcalls"]]
        self.assertIn("Morning Run", titles)
        self.assertIn("Evening Game", titles)
        self.assertIn("Weekend Trip", titles)

    def test_each_rollcall_has_web_token(self):
        """Frontend needs web_token to vote on each individual rollcall."""
        resp = self._get("mygrouptoken000", svc_return=_GROUP_DICT_MULTI)
        data = resp.json()
        tokens = [r["web_token"] for r in data["rollcalls"]]
        self.assertIn("tok1", tokens)
        self.assertIn("tok2", tokens)
        self.assertIn("tok3", tokens)

    def test_invalid_group_token_returns_422(self):
        from exceptions import incorrectParameter
        resp = self._get("badtoken",
                         svc_side_effect=incorrectParameter("invalid"))
        self.assertEqual(resp.status_code, 422)

    def test_no_auth_header_required(self):
        resp = self._get("mygrouptoken000", svc_return=_GROUP_DICT_EMPTY)
        self.assertNotEqual(resp.status_code, 401)
        self.assertNotEqual(resp.status_code, 403)

    def test_group_route_does_not_conflict_with_rollcall_route(self):
        """GET /api/v1/web/group/{token} must NOT match /api/v1/web/{token}."""
        with patch("services.web.get_rollcalls_by_group_token",
                   return_value=_GROUP_DICT_EMPTY) as grp_mock, \
             patch("services.web.get_rollcall_by_token",
                   return_value=_WEB_RC_DICT) as rc_mock:
            resp = _client().get("/api/v1/web/group/mygrouptoken000")
        # group service called, NOT the single-rollcall service
        grp_mock.assert_called_once()
        rc_mock.assert_not_called()
        self.assertEqual(resp.status_code, 200)

    def test_rollcall_route_does_not_match_group_path(self):
        """GET /api/v1/web/{token} should not handle /api/v1/web/group/…."""
        with patch("services.web.get_rollcall_by_token",
                   return_value=_WEB_RC_DICT) as rc_mock, \
             patch("services.web.get_rollcalls_by_group_token",
                   return_value=_GROUP_DICT_EMPTY):
            _client().get("/api/v1/web/group/sometoken")
        rc_mock.assert_not_called()


# ---------------------------------------------------------------------------
# HTML page routes
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebPageRoutes(unittest.TestCase):

    def test_join_page_returns_200(self):
        resp = _client().get("/web/join/somerollcalltoken")
        self.assertEqual(resp.status_code, 200)

    def test_join_page_content_type_is_html(self):
        resp = _client().get("/web/join/somerollcalltoken")
        self.assertIn("text/html", resp.headers.get("content-type", ""))

    def test_join_page_contains_doctype(self):
        resp = _client().get("/web/join/somerollcalltoken")
        self.assertIn("<!DOCTYPE html>", resp.text)

    def test_group_page_returns_200(self):
        resp = _client().get("/web/group/somegrouptoken")
        self.assertEqual(resp.status_code, 200)

    def test_group_page_content_type_is_html(self):
        resp = _client().get("/web/group/somegrouptoken")
        self.assertIn("text/html", resp.headers.get("content-type", ""))

    def test_group_page_contains_doctype(self):
        resp = _client().get("/web/group/somegrouptoken")
        self.assertIn("<!DOCTYPE html>", resp.text)

    def test_both_pages_serve_same_html(self):
        """Same index.html serves both URL patterns; JS detects the mode."""
        join_resp  = _client().get("/web/join/tok1")
        group_resp = _client().get("/web/group/tok2")
        self.assertEqual(join_resp.text, group_resp.text)

    def test_page_references_api_endpoint(self):
        """Page must reference the external app.js which calls /api/v1/web/."""
        resp = _client().get("/web/join/sometoken")
        self.assertIn("/web/app.js", resp.text)

    def test_page_detects_join_mode(self):
        """Page must reference external app.js that handles join/group URL modes."""
        resp = _client().get("/web/join/sometoken")
        self.assertIn("/web/app.js", resp.text)

    def test_page_detects_group_mode(self):
        """Page must reference external app.js that handles join/group URL modes."""
        resp = _client().get("/web/join/sometoken")
        self.assertIn("/web/app.js", resp.text)

    def test_page_references_telegram_webapp_sdk(self):
        """Telegram WebApp SDK script tag must be present."""
        resp = _client().get("/web/join/sometoken")
        self.assertIn("telegram-web-app.js", resp.text)

    def test_page_has_vote_buttons(self):
        """IN/OUT/MAYBE buttons must be in the HTML."""
        resp = _client().get("/web/join/sometoken")
        self.assertIn("btn-in", resp.text)
        self.assertIn("btn-out", resp.text)
        self.assertIn("btn-maybe", resp.text)

    def test_page_has_name_input(self):
        resp = _client().get("/web/join/sometoken")
        self.assertIn("name-input", resp.text)

    def test_page_has_auto_refresh_logic(self):
        """Auto-refresh logic lives in app.js — verify the script is referenced."""
        resp = _client().get("/web/join/sometoken")
        self.assertIn("/web/app.js", resp.text)


# ---------------------------------------------------------------------------
# Route ordering — group must win over /{token} catch-all
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestRouteOrdering(unittest.TestCase):
    """
    Regression tests: /web/group/{token} is registered before /web/{token}
    in api/routes/web.py. FastAPI must route them correctly.
    """

    def test_group_api_endpoint_registered_before_catchall(self):
        from api.main import create_app
        app = create_app()
        paths = [r.path for r in app.routes]
        group_idx = next((i for i, p in enumerate(paths)
                          if p == "/api/v1/web/group/{group_token}"), None)
        catchall_idx = next((i for i, p in enumerate(paths)
                             if p == "/api/v1/web/{token}"), None)
        self.assertIsNotNone(group_idx, "group route not found")
        self.assertIsNotNone(catchall_idx, "per-rollcall route not found")
        self.assertLess(group_idx, catchall_idx,
                        "group route must be registered before /{token} catch-all")

    def test_vote_endpoint_present(self):
        from api.main import create_app
        app = create_app()
        paths = [r.path for r in app.routes]
        self.assertIn("/api/v1/web/{token}/vote", paths)

    def test_join_page_route_present(self):
        from api.main import create_app
        app = create_app()
        paths = [r.path for r in app.routes]
        self.assertIn("/web/join/{token}", paths)

    def test_group_page_route_present(self):
        from api.main import create_app
        app = create_app()
        paths = [r.path for r in app.routes]
        self.assertIn("/web/group/{group_token}", paths)


if __name__ == "__main__":
    unittest.main()
