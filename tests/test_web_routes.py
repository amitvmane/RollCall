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
        mock_fn.assert_awaited_once_with("mytok", "Dave", "in", tg_user_id=None, comment=None, username=None)

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
# Web → Telegram mirroring: web actions must reflect in the group chat
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebActionMirrorsToTelegram(unittest.TestCase):

    def test_web_vote_refreshes_telegram_panel(self):
        vote_fn = AsyncMock(return_value=_WEB_RC_DICT)
        mirror = AsyncMock()
        with patch("services.web.vote_by_token", vote_fn), \
             patch("services.web.locate_rollcall", return_value=(-100200, 2)), \
             patch("api.routes.web._mirror_panel_to_telegram", mirror):
            resp = _client().post("/api/v1/web/tok/vote",
                                  json={"name": "Alice", "vote": "in"})
        self.assertEqual(resp.status_code, 200)
        mirror.assert_awaited_once_with(-100200, 2)

    def test_web_vote_unresolvable_token_skips_mirror(self):
        vote_fn = AsyncMock(return_value=_WEB_RC_DICT)
        mirror = AsyncMock()
        with patch("services.web.vote_by_token", vote_fn), \
             patch("services.web.locate_rollcall", return_value=None), \
             patch("api.routes.web._mirror_panel_to_telegram", mirror):
            resp = _client().post("/api/v1/web/tok/vote",
                                  json={"name": "Alice", "vote": "in"})
        self.assertEqual(resp.status_code, 200)
        mirror.assert_not_awaited()


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

    def test_end_rollcall_route_present(self):
        from api.main import create_app
        app = create_app()
        paths = [r.path for r in app.routes]
        self.assertIn("/api/v1/web/group/{group_token}/end-rollcall", paths)


# ---------------------------------------------------------------------------
# Fee field in WebRollcallResponse
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebRollcallFeeField(unittest.TestCase):

    def test_fee_included_when_set(self):
        rc_with_fee = {**_WEB_RC_DICT, "fee": "₹150"}
        with patch("services.web.get_rollcall_by_token", return_value=rc_with_fee):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["fee"], "₹150")

    def test_fee_null_when_not_set(self):
        with patch("services.web.get_rollcall_by_token", return_value=_WEB_RC_DICT):
            resp = _client().get("/api/v1/web/abc123rollcalltoken")
        self.assertIsNone(resp.json().get("fee"))


# ---------------------------------------------------------------------------
# POST /api/v1/web/group/{token}/end-rollcall
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebEndRollcall(unittest.TestCase):

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def test_missing_id_token_returns_422(self):
        resp = _client().post("/api/v1/web/group/grp123/end-rollcall",
                              json={"rollcall_num": 1})
        self.assertEqual(resp.status_code, 422)

    def test_invalid_group_token_returns_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().post("/api/v1/web/group/badgrp/end-rollcall",
                                  json={"id_token": "tok", "rollcall_num": 1})
        self.assertEqual(resp.status_code, 404)

    def test_non_admin_returns_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post("/api/v1/web/group/grp123/end-rollcall",
                                  json={"id_token": "tok", "rollcall_num": 1})
        self.assertEqual(resp.status_code, 403)

    def test_invalid_id_token_returns_401(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.identity.verify_identity_token", return_value=None):
            resp = _client().post("/api/v1/web/group/grp123/end-rollcall",
                                  json={"id_token": "bad", "rollcall_num": 1})
        self.assertEqual(resp.status_code, 401)

    def test_end_rollcall_calls_service(self):
        import api.routes.web as _web_mod
        end_result = {
            "rc_number_ended_1based": 1,
            "ended": {},
            "ghost_eligible": False,
            "ghost_rc_db_id": None,
            "ended_by": {"id": 99, "name": "(web)", "username": None},
            "remaining": [],
            "renumbered": [],
        }
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.rollcalls.end_rollcall", new_callable=AsyncMock, return_value=end_result), \
             patch("api.telegram_mirror.mirror_panel_to_telegram", new_callable=AsyncMock):
            resp = _client().post("/api/v1/web/group/grp123/end-rollcall",
                                  json={"id_token": "tok", "rollcall_num": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ended"], 1)


class TestWebProxyVote(unittest.TestCase):
    """Web parity for /sif /sof /smf — admin-gated proxy voting on the group
    page (flow-audit #4)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def _body(self, **over):
        body = {"id_token": "tok", "rollcall_num": 1,
                "proxy_name": "Guest Ravi", "vote": "in"}
        body.update(over)
        return body

    def test_missing_id_token_returns_422(self):
        resp = _client().post("/api/v1/web/group/grp123/proxy-vote",
                              json={"rollcall_num": 1, "proxy_name": "x", "vote": "in"})
        self.assertEqual(resp.status_code, 422)

    def test_invalid_group_token_returns_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().post("/api/v1/web/group/badgrp/proxy-vote", json=self._body())
        self.assertEqual(resp.status_code, 404)

    def test_invalid_id_token_returns_401(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.identity.verify_identity_token", return_value=None):
            resp = _client().post("/api/v1/web/group/grp123/proxy-vote", json=self._body())
        self.assertEqual(resp.status_code, 401)

    def test_non_admin_returns_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post("/api/v1/web/group/grp123/proxy-vote", json=self._body())
        self.assertEqual(resp.status_code, 403)

    def test_unknown_vote_choice_returns_422(self):
        resp = _client().post("/api/v1/web/group/grp123/proxy-vote",
                              json=self._body(vote="banana"))
        self.assertEqual(resp.status_code, 422)

    def test_admin_proxy_vote_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Sunday",
                      "in": [{"name": "Guest Ravi"}], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.proxy.set_in_for", new_callable=AsyncMock, return_value={}) as svc, \
             patch.object(_web_mod, "_send_vote_notification", new_callable=AsyncMock) as notif, \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock) as mirror, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()):
            resp = _client().post("/api/v1/web/group/grp123/proxy-vote", json=self._body())
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["in"][0]["name"], "Guest Ravi")
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100)
        self.assertEqual(kwargs["admin_user_id"], 99)   # from the signed token
        self.assertEqual(kwargs["admin_name"], "Amit")
        self.assertEqual(kwargs["proxy_name"], "Guest Ravi")
        self.assertEqual(kwargs["rc_number"], 0)        # 1-based → 0-based
        notif.assert_awaited_once()
        mirror.assert_awaited_once()

    def test_out_vote_routes_to_set_out_for(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Sunday",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.proxy.set_out_for", new_callable=AsyncMock, return_value={}) as svc, \
             patch.object(_web_mod, "_send_vote_notification", new_callable=AsyncMock), \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock), \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()):
            resp = _client().post("/api/v1/web/group/grp123/proxy-vote",
                                  json=self._body(vote="out"))
        self.assertEqual(resp.status_code, 201)
        svc.assert_awaited_once()
        # No display info → falls back to the generic actor label
        self.assertEqual(svc.call_args.kwargs["admin_name"], "(web admin)")


class TestWebRemoveUser(unittest.TestCase):
    """Web parity for the admin console's Rollcalls-tab remove action —
    part of admin-console retirement (live voter management)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def _body(self, **over):
        body = {"id_token": "tok", "rollcall_num": 1, "name": "Ravi"}
        body.update(over)
        return body

    def test_missing_id_token_returns_422(self):
        resp = _client().post("/api/v1/web/group/grp123/rollcalls/remove-user",
                              json={"rollcall_num": 1, "name": "x"})
        self.assertEqual(resp.status_code, 422)

    def test_invalid_group_token_returns_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().post("/api/v1/web/group/badgrp/rollcalls/remove-user", json=self._body())
        self.assertEqual(resp.status_code, 404)

    def test_invalid_id_token_returns_401(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.identity.verify_identity_token", return_value=None):
            resp = _client().post("/api/v1/web/group/grp123/rollcalls/remove-user", json=self._body())
        self.assertEqual(resp.status_code, 401)

    def test_non_admin_returns_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post("/api/v1/web/group/grp123/rollcalls/remove-user", json=self._body())
        self.assertEqual(resp.status_code, 403)

    def test_admin_remove_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Sunday",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.admin.delete_user_from_rollcall", return_value={}) as svc, \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock) as mirror, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()):
            resp = _client().post("/api/v1/web/group/grp123/rollcalls/remove-user", json=self._body())
        self.assertEqual(resp.status_code, 200)
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs["chat_id"], -100)
        self.assertEqual(kwargs["admin_user_id"], 99)
        self.assertEqual(kwargs["admin_name"], "Amit")
        self.assertEqual(kwargs["name"], "Ravi")
        self.assertEqual(kwargs["rc_number"], 0)  # 1-based → 0-based
        mirror.assert_awaited_once()


class TestWebMoveUser(unittest.TestCase):
    """Web parity for the admin console's Rollcalls-tab move action."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def _body(self, **over):
        body = {"id_token": "tok", "rollcall_num": 1, "name": "Ravi", "new_status": "out"}
        body.update(over)
        return body

    def test_unknown_status_returns_422(self):
        resp = _client().post("/api/v1/web/group/grp123/rollcalls/move-user",
                              json=self._body(new_status="banana"))
        self.assertEqual(resp.status_code, 422)

    def test_non_admin_returns_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post("/api/v1/web/group/grp123/rollcalls/move-user", json=self._body())
        self.assertEqual(resp.status_code, 403)

    def test_admin_move_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Sunday",
                      "in": [], "out": [{"name": "Ravi"}], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.admin.set_user_status", return_value={}) as svc, \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock) as mirror, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()):
            resp = _client().post("/api/v1/web/group/grp123/rollcalls/move-user", json=self._body())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["out"][0]["name"], "Ravi")
        kwargs = svc.call_args.kwargs
        self.assertEqual(kwargs["name"], "Ravi")
        self.assertEqual(kwargs["new_status"], "out")
        self.assertEqual(kwargs["rc_number"], 0)
        # No display info → falls back to the generic actor label
        self.assertEqual(kwargs["admin_name"], "(web admin)")
        mirror.assert_awaited_once()


class TestWebAdminStatusLiveCheck(unittest.TestCase):
    """admin-status now live-checks Telegram on every load instead of
    trusting the web_admins cache forever — auto-grants a real Telegram
    admin (no /weblink needed) and auto-revokes anyone who's lost that
    role, with graceful fallback to the cache if Telegram is unreachable
    (so a brief outage doesn't lock an admin out of the web surface that's
    supposed to survive Telegram being down)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def _member(self, status, first_name="Amit"):
        m = MagicMock()
        m.status = status
        m.user = MagicMock()
        m.user.first_name = first_name
        return m

    def test_no_id_token_returns_false_without_calling_telegram(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock) as gcm:
            resp = _client().get("/api/v1/web/group/grp123/admin-status")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["is_admin"])
        gcm.assert_not_awaited()

    def test_real_admin_grants_and_caches(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web.verify_identity_token", return_value=99), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=True), \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock,
                   return_value=self._member("administrator")), \
             patch.object(_web_mod._db, "set_web_admin") as set_admin, \
             patch.object(_web_mod._db, "revoke_web_admin") as revoke:
            resp = _client().get("/api/v1/web/group/grp123/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertTrue(resp.json()["is_admin"])
        set_admin.assert_called_once_with(-100, 99, "Amit")
        revoke.assert_not_called()

    def test_creator_also_grants(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web.verify_identity_token", return_value=99), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=True), \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock,
                   return_value=self._member("creator")), \
             patch.object(_web_mod._db, "set_web_admin"):
            resp = _client().get("/api/v1/web/group/grp123/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertTrue(resp.json()["is_admin"])

    def test_demoted_member_revokes_stale_cache(self):
        """The core fix: someone who WAS a web admin but lost real Telegram
        admin status must be revoked, not grandfathered in forever — but
        only in a group that has actually locked itself down with
        /set_admins (see test_default_open_group_skips_live_check for the
        common default-open case, which must NOT revoke)."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web.verify_identity_token", return_value=99), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=True), \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock,
                   return_value=self._member("member")), \
             patch.object(_web_mod._db, "set_web_admin") as set_admin, \
             patch.object(_web_mod._db, "revoke_web_admin") as revoke:
            resp = _client().get("/api/v1/web/group/grp123/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertFalse(resp.json()["is_admin"])
        revoke.assert_called_once_with(-100, 99)
        set_admin.assert_not_called()

    def test_default_open_group_skips_live_check(self):
        """Regression test: in a default-open group (never ran /set_admins,
        the common case), Telegram admin/creator status was never the
        criterion for granting web-admin — /weblink hands it to any member.
        The live check must not run at all here, or it would silently
        revoke every non-Telegram-admin the moment they load the page."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web.verify_identity_token", return_value=99), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=False), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True) as cached, \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock) as gcm, \
             patch.object(_web_mod._db, "set_web_admin") as set_admin, \
             patch.object(_web_mod._db, "revoke_web_admin") as revoke:
            resp = _client().get("/api/v1/web/group/grp123/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertTrue(resp.json()["is_admin"])
        cached.assert_called_once_with(-100, 99)
        gcm.assert_not_awaited()
        set_admin.assert_not_called()
        revoke.assert_not_called()

    def test_telegram_unreachable_falls_back_to_cache_true(self):
        """Outage resilience: if the live check itself fails, don't lock an
        already-cached admin out — this page must keep working when
        Telegram is down, admin actions included."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web.verify_identity_token", return_value=99), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=True), \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock,
                   side_effect=Exception("Telegram unreachable")), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True) as cached:
            resp = _client().get("/api/v1/web/group/grp123/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertTrue(resp.json()["is_admin"])
        cached.assert_called_once_with(-100, 99)

    def test_telegram_unreachable_falls_back_to_cache_false(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.routes.web.verify_identity_token", return_value=99), \
             patch("rollcall_manager.manager.get_admin_rights", return_value=True), \
             patch("bot_state.bot.get_chat_member", new_callable=AsyncMock,
                   side_effect=Exception("Telegram unreachable")), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False):
            resp = _client().get("/api/v1/web/group/grp123/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertFalse(resp.json()["is_admin"])

    def test_invalid_group_token_false(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().get("/api/v1/web/group/badgrp/admin-status", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["is_admin"])


class TestWebTemplateSchedule(unittest.TestCase):
    """Self-serve recurring-schedule editor on the group web page — id_token
    + is_web_admin gated, no server/API-token access needed (unlike the
    separate token-gated /admin/ console routes in api/routes/templates.py)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def _tmpl(self, **over):
        t = {"name": "SundayGame", "title": "Sunday Game", "schedule_enabled": True,
             "schedule_day": "saturday", "schedule_time": "09:00", "recurrence_type": "weekly",
             "event_day": "sunday", "event_time": "06:30", "last_scheduled_date": None}
        t.update(over)
        return t

    # ── list ──────────────────────────────────────────────────────────────

    def test_list_requires_id_token(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.identity.verify_identity_token", return_value=None):
            resp = _client().get("/api/v1/web/group/grp123/templates", headers={"X-Identity-Token": "bad"})
        self.assertEqual(resp.status_code, 401)

    def test_list_requires_web_admin(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().get("/api/v1/web/group/grp123/templates", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 403)

    def test_list_invalid_group_token_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().get("/api/v1/web/group/badgrp/templates", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 404)

    def test_list_returns_templates_for_admin(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.list_templates", return_value=[self._tmpl()]):
            resp = _client().get("/api/v1/web/group/grp123/templates", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["name"], "SundayGame")

    # ── set schedule ─────────────────────────────────────────────────────

    def test_set_schedule_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule",
                json={"id_token": "tok", "recurrence_type": "weekly",
                      "schedule_day": "saturday", "schedule_time": "09:00"})
        self.assertEqual(resp.status_code, 403)

    def test_set_schedule_calls_service_and_mirrors_to_telegram(self):
        import api.routes.web as _web_mod
        updated = self._tmpl(schedule_day="thursday", schedule_time="18:00")
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.set_schedule", return_value=updated) as svc, \
             patch("services.templates.get_one_template", return_value=updated), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule",
                json={"id_token": "tok", "recurrence_type": "weekly",
                      "schedule_day": "thursday", "schedule_time": "18:00"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(svc.call_args.kwargs["chat_id"], -100)
        self.assertEqual(svc.call_args.kwargs["admin_user_id"], 99)
        self.assertEqual(svc.call_args.kwargs["admin_name"], "Amit")
        self.assertEqual(svc.call_args.kwargs["schedule_day"], "thursday")
        notify.assert_awaited_once()
        self.assertIn("SundayGame", notify.call_args[0][1])

    def test_set_schedule_expires_at_passes_through(self):
        import api.routes.web as _web_mod
        updated = self._tmpl(schedule_day="thursday", schedule_time="18:00")
        updated["schedule_expires_at"] = "2027-01-01"
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.set_schedule", return_value=updated) as svc, \
             patch("services.templates.get_one_template", return_value=updated), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule",
                json={"id_token": "tok", "recurrence_type": "weekly",
                      "schedule_day": "thursday", "schedule_time": "18:00",
                      "expires_at": "2027-01-01"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(svc.call_args.kwargs["expires_at"], "2027-01-01")
        self.assertEqual(resp.json()["schedule_expires_at"], "2027-01-01")

    def test_set_schedule_daily_recurrence_accepted(self):
        import api.routes.web as _web_mod
        updated = self._tmpl(schedule_day=None, schedule_time="09:00", recurrence_type="daily")
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.set_schedule", return_value=updated) as svc, \
             patch("services.templates.get_one_template", return_value=updated), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule",
                json={"id_token": "tok", "recurrence_type": "daily", "schedule_time": "09:00"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(svc.call_args.kwargs["recurrence_type"], "daily")

    def test_set_schedule_monthly_passes_monthly_day(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.set_schedule", return_value=self._tmpl()) as svc, \
             patch("services.templates.get_one_template", return_value=self._tmpl()), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule",
                json={"id_token": "tok", "recurrence_type": "monthly",
                      "schedule_time": "09:00", "monthly_day": 15})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(svc.call_args.kwargs["monthly_day"], 15)

    # ── enable / disable ─────────────────────────────────────────────────

    def test_enable_schedule(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.enable_schedule") as svc, \
             patch("services.templates.get_one_template", return_value=self._tmpl()), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule/enable",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 200)
        svc.assert_called_once_with(-100, "SundayGame", 99, "(web admin)")
        self.assertIn("enabled", notify.call_args[0][1])

    def test_disable_schedule(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.disable_schedule") as svc, \
             patch("services.templates.get_one_template", return_value=self._tmpl(schedule_enabled=False)), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule/disable",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 200)
        svc.assert_called_once_with(-100, "SundayGame", 99, "(web admin)")
        self.assertIn("disabled", notify.call_args[0][1])

    def test_disable_schedule_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post(
                "/api/v1/web/group/grp123/templates/SundayGame/schedule/disable",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 403)


class TestWebTemplateContentEditAndStart(unittest.TestCase):
    """Editing a template's content (title/location/fee/limit) and starting
    a rollcall from it on demand — both self-serve, same id_token +
    is_web_admin gate as the schedule editor above."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def _tmpl(self, **over):
        t = {"name": "SundayGame", "title": "Sunday Game", "location": "Turf 3",
             "fee": "1500", "limit": 16, "schedule_enabled": True,
             "schedule_day": "saturday", "schedule_time": "09:00", "recurrence_type": "weekly",
             "event_day": "sunday", "event_time": "06:30", "last_scheduled_date": None}
        t.update(over)
        return t

    # ── content edit ─────────────────────────────────────────────────────

    def test_update_content_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame",
                json={"id_token": "tok", "title": "New Title"})
        self.assertEqual(resp.status_code, 403)

    def test_update_content_calls_upsert_and_mirrors(self):
        import api.routes.web as _web_mod
        updated = self._tmpl(title="Sunday Futsal", location="Turf 5", fee="1800", limit=20)
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", return_value=updated) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame",
                json={"id_token": "tok", "title": "Sunday Futsal",
                      "location": "Turf 5", "fee": "1800", "limit": 20})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Sunday Futsal")
        self.assertEqual(svc.call_args.kwargs["chat_id"], -100)
        self.assertEqual(svc.call_args.kwargs["admin_user_id"], 99)
        self.assertEqual(svc.call_args.kwargs["admin_name"], "Amit")
        self.assertEqual(svc.call_args.kwargs["title"], "Sunday Futsal")
        self.assertEqual(svc.call_args.kwargs["location"], "Turf 5")
        self.assertEqual(svc.call_args.kwargs["fee"], "1800")
        self.assertEqual(svc.call_args.kwargs["limit"], 20)
        notify.assert_awaited_once()
        self.assertIn("SundayGame", notify.call_args[0][1])

    def test_update_content_offset_fields_pass_through(self):
        """offset_days/hours/minutes — the New Rollcall modal's Schedule ->
        Once path converts its exact "closes at" picker into an offset from
        the opening time and sends it here (existing template columns,
        see services.templates.start_template's offset fallback)."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", return_value=self._tmpl()) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/FridayMatch",
                json={"id_token": "tok", "title": "Friday Match",
                      "offset_days": 2, "offset_hours": 3, "offset_minutes": 30})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(svc.call_args.kwargs["offset_days"], 2)
        self.assertEqual(svc.call_args.kwargs["offset_hours"], 3)
        self.assertEqual(svc.call_args.kwargs["offset_minutes"], 30)

    def test_update_content_offset_fields_default_none_when_omitted(self):
        """Unlike title/location/fee/event_day/event_time, omitted offset
        fields must NOT be translated to a clearing sentinel — the
        Templates section's own edit form never sends them at all and must
        not accidentally wipe out an offset set via the New Rollcall modal."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", return_value=self._tmpl()) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/FridayMatch",
                json={"id_token": "tok", "title": "Friday Match"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(svc.call_args.kwargs["offset_days"])
        self.assertIsNone(svc.call_args.kwargs["offset_hours"])
        self.assertIsNone(svc.call_args.kwargs["offset_minutes"])

    def test_update_content_blank_fields_clear_not_preserve(self):
        """This route always receives the whole form, not a sparse patch —
        unlike the token-gated REST API's real partial-update contract. A
        field the client didn't send (None) must translate to upsert_
        template's explicit-clear signal ("" for strings, 0 for limit), NOT
        its preserve-existing signal (None) — otherwise an admin blanking a
        field in the edit form to intentionally clear it silently no-ops."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", return_value=self._tmpl()) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame",
                json={"id_token": "tok", "title": "Only Title Changes"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(svc.call_args.kwargs["title"], "Only Title Changes")
        self.assertEqual(svc.call_args.kwargs["location"], "")
        self.assertEqual(svc.call_args.kwargs["fee"], "")
        self.assertIsNone(svc.call_args.kwargs["limit"])
        self.assertEqual(svc.call_args.kwargs["event_day"], "")
        self.assertEqual(svc.call_args.kwargs["event_time"], "")

    def test_update_content_event_day_time_distinct_from_schedule(self):
        """event_day/event_time (when the game happens, used to auto-close)
        must reach upsert_template as their own kwargs — never conflated
        with schedule_day/schedule_time (when the template auto-opens)."""
        import api.routes.web as _web_mod
        updated = self._tmpl(event_day="sunday", event_time="07:00")
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", return_value=updated) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock):
            resp = _client().put(
                "/api/v1/web/group/grp123/templates/SundayGame",
                json={"id_token": "tok", "event_day": "sunday", "event_time": "07:00"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["event_day"], "sunday")
        self.assertEqual(resp.json()["event_time"], "07:00")
        self.assertEqual(svc.call_args.kwargs["event_day"], "sunday")
        self.assertEqual(svc.call_args.kwargs["event_time"], "07:00")
        # No schedule fields present in this schema at all — content-only route.
        self.assertNotIn("schedule_day", svc.call_args.kwargs)
        self.assertNotIn("schedule_time", svc.call_args.kwargs)

    # ── start now ────────────────────────────────────────────────────────

    def test_start_template_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post(
                "/api/v1/web/group/grp123/templates/SundayGame/start",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 403)

    def test_start_template_calls_service_and_mirrors_panel(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Sunday Game",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.start_template", new_callable=AsyncMock,
                   return_value={"rc_index": 0}) as svc, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()), \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock) as mirror:
            resp = _client().post(
                "/api/v1/web/group/grp123/templates/SundayGame/start",
                json={"id_token": "tok", "extra_title": "Extra"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["title"], "Sunday Game")
        self.assertEqual(svc.call_args.kwargs["chat_id"], -100)
        self.assertEqual(svc.call_args.kwargs["admin_user_id"], 99)
        self.assertEqual(svc.call_args.kwargs["admin_name"], "Amit")
        self.assertEqual(svc.call_args.kwargs["name"], "SundayGame")
        self.assertEqual(svc.call_args.kwargs["extra_title"], "Extra")
        mirror.assert_awaited_once_with(-100, 1, force_new=True)

    def test_start_template_rollcall_missing_after_create_500(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.start_template", new_callable=AsyncMock,
                   return_value={"rc_index": 0}), \
             patch("rollcall_manager.manager.get_rollcall", return_value=None):
            resp = _client().post(
                "/api/v1/web/group/grp123/templates/SundayGame/start",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 500)

    def test_start_template_invalid_group_token_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().post(
                "/api/v1/web/group/badgrp/templates/SundayGame/start",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 404)


class TestWebTemplateDelete(unittest.TestCase):
    """DELETE /web/group/{token}/templates/{name} — the web app and admin
    console previously had no way to delete a template at all (only the
    Telegram /delete_template command could); this is the web-portal route
    for it, same id_token + is_web_admin gate as every other template route."""

    def test_delete_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.delete_one_template",
                   return_value={"name": "SundayGame", "deleted": True}) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().request(
                "DELETE", "/api/v1/web/group/grp123/templates/SundayGame",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])
        self.assertEqual(svc.call_args.args[0], -100)
        self.assertEqual(svc.call_args.args[1], "SundayGame")
        self.assertEqual(svc.call_args.args[2], 99)
        self.assertEqual(svc.call_args.args[3], "Amit")
        notify.assert_awaited_once()
        self.assertIn("SundayGame", notify.call_args[0][1])

    def test_delete_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().request(
                "DELETE", "/api/v1/web/group/grp123/templates/SundayGame",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 403)

    def test_delete_invalid_group_token_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().request(
                "DELETE", "/api/v1/web/group/badgrp/templates/SundayGame",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 404)

    def test_delete_template_not_found_returns_422(self):
        import api.routes.web as _web_mod
        from exceptions import incorrectParameter
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.delete_one_template",
                   side_effect=incorrectParameter("Template 'Ghost' not found.")):
            resp = _client().request(
                "DELETE", "/api/v1/web/group/grp123/templates/Ghost",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 422)


class TestWebIdentityMerge(unittest.TestCase):
    """Merge Identities section on the group web page — fold a fragmented
    proxy name into a real member or another proxy so stats/dues/
    ghost-tracking count them once. Same id_token + is_web_admin gate as
    every other admin-only web route. See services/identity.py."""

    def test_list_requires_id_token(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch("api.identity.verify_identity_token", return_value=None):
            resp = _client().get("/api/v1/web/group/grp123/identities", headers={"X-Identity-Token": "bad"})
        self.assertEqual(resp.status_code, 401)

    def test_list_requires_web_admin(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().get("/api/v1/web/group/grp123/identities", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 403)

    def test_list_invalid_group_token_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().get("/api/v1/web/group/badgrp/identities", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 404)

    def test_list_returns_identities_and_groups(self):
        import api.routes.web as _web_mod
        identities = [{"kind": "user", "user_id": 1, "proxy_name": None,
                       "display_name": "Amit", "merged_into": None}]
        groups = [{"kind": "user", "user_id": 1, "proxy_name": None,
                   "aliases": ["Rex"], "display_name": "Amit"}]
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.list_all_identities", return_value=identities), \
             patch("services.identity.list_identity_groups", return_value=groups):
            resp = _client().get("/api/v1/web/group/grp123/identities", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["identities"][0]["display_name"], "Amit")
        self.assertEqual(resp.json()["groups"][0]["aliases"], ["Rex"])

    def test_list_suggestions(self):
        import api.routes.web as _web_mod
        suggestions = [{"alias_proxy_name": "Ajya", "candidate_kind": "proxy",
                        "candidate_user_id": None, "candidate_proxy_name": "Ajay",
                        "candidate_display_name": "Ajay", "score": 1}]
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.list_suggestions", return_value=suggestions):
            resp = _client().get("/api/v1/web/group/grp123/identities/suggestions", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["suggestions"][0]["alias_proxy_name"], "Ajya")

    def test_merge_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        group = {"kind": "user", "user_id": 1, "proxy_name": None,
                 "aliases": ["Rex"], "display_name": "Amit"}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.link_identities", return_value=group) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/merge",
                json={"id_token": "tok", "alias_proxy_name": "Rex", "canonical_user_id": 1})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["aliases"], ["Rex"])
        self.assertEqual(svc.call_args.args[0], -100)
        self.assertEqual(svc.call_args.args[1], "Rex")
        self.assertEqual(svc.call_args.kwargs["canonical_user_id"], 1)
        self.assertEqual(svc.call_args.kwargs["admin_user_id"], 99)
        self.assertEqual(svc.call_args.kwargs["admin_name"], "Amit")
        notify.assert_awaited_once()
        self.assertIn("Rex", notify.call_args[0][1])

    def test_merge_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/merge",
                json={"id_token": "tok", "alias_proxy_name": "Rex", "canonical_user_id": 1})
        self.assertEqual(resp.status_code, 403)

    def test_merge_self_merge_returns_422(self):
        import api.routes.web as _web_mod
        from exceptions import incorrectParameter
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.link_identities",
                   side_effect=incorrectParameter("Can't merge a name into itself.")):
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/merge",
                json={"id_token": "tok", "alias_proxy_name": "Rex", "canonical_proxy_name": "Rex"})
        self.assertEqual(resp.status_code, 422)

    def test_unmerge_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.unmerge_identity", return_value={"unmerged": True}) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/unmerge",
                json={"id_token": "tok", "alias_proxy_name": "Rex"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["unmerged"])
        self.assertEqual(svc.call_args.args[0], -100)
        self.assertEqual(svc.call_args.args[1], "Rex")
        notify.assert_awaited_once()

    def test_unmerge_noop_does_not_mirror(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.unmerge_identity", return_value={"unmerged": False}), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/unmerge",
                json={"id_token": "tok", "alias_proxy_name": "Ghost"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["unmerged"])
        notify.assert_not_awaited()

    def test_dismiss_suggestion_does_not_mirror(self):
        """Dismiss is silent admin housekeeping — never posted to the group,
        unlike merge/unmerge which change what members see in stats."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.dismiss_suggestion", return_value={"dismissed": True}) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/suggestions/dismiss",
                json={"id_token": "tok", "alias_proxy_name": "Ajya", "candidate_proxy_name": "Ajay"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["dismissed"])
        svc.assert_called_once()
        notify.assert_not_awaited()

    def test_dismiss_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/suggestions/dismiss",
                json={"id_token": "tok", "alias_proxy_name": "Ajya", "candidate_proxy_name": "Ajay"})
        self.assertEqual(resp.status_code, 403)

    def test_list_includes_discarded(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.list_all_identities", return_value=[]), \
             patch("services.identity.list_identity_groups", return_value=[]), \
             patch("services.identity.list_discarded", return_value=["2", "]"]):
            resp = _client().get("/api/v1/web/group/grp123/identities", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["discarded"], ["2", "]"])

    def test_discard_calls_service(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.discard_identity", return_value={"discarded": True}) as svc:
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/discard",
                json={"id_token": "tok", "alias_proxy_name": "2"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["discarded"])
        self.assertEqual(svc.call_args.args[0], -100)
        self.assertEqual(svc.call_args.args[1], "2")
        self.assertEqual(svc.call_args.kwargs["admin_user_id"], 99)

    def test_discard_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/discard",
                json={"id_token": "tok", "alias_proxy_name": "2"})
        self.assertEqual(resp.status_code, 403)

    def test_undiscard_calls_service(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.identity.undiscard_identity", return_value={"restored": True}) as svc:
            resp = _client().post(
                "/api/v1/web/group/grp123/identities/undiscard",
                json={"id_token": "tok", "alias_proxy_name": "2"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["restored"])
        svc.assert_called_once()


class TestWebGroupSettingsTimezone(unittest.TestCase):
    """Browser-based timezone detect/set — /web/group/{token}/settings PATCH.
    Telegram exposes no location signal for a group, so this is the only
    real auto-detect path (the admin's own browser)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def test_set_timezone_non_admin_403(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch.object(_web_mod, "require_identity", return_value=77):
            resp = _client().patch(
                "/api/v1/web/group/grp123/settings",
                json={"id_token": "tok", "timezone": "Asia/Kolkata"})
        self.assertEqual(resp.status_code, 403)

    def test_set_timezone_calls_service_and_mirrors(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch.object(_web_mod, "require_identity", return_value=99), \
             patch("services.settings.set_timezone", return_value={}) as svc, \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().patch(
                "/api/v1/web/group/grp123/settings",
                json={"id_token": "tok", "timezone": "America/New_York"})
        self.assertEqual(resp.status_code, 204)
        svc.assert_called_once_with(-100, "America/New_York", 99, "Amit")
        notify.assert_awaited_once()
        self.assertIn("America/New_York", notify.call_args[0][1])

    def test_omitted_timezone_does_not_call_service(self):
        """timezone absent from the request must not touch settings at all
        — only shh_mode (or whichever field was actually sent) should fire."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod, "require_identity", return_value=99), \
             patch("services.settings.set_timezone") as svc:
            resp = _client().patch(
                "/api/v1/web/group/grp123/settings",
                json={"id_token": "tok"})
        self.assertEqual(resp.status_code, 204)
        svc.assert_not_called()

    def test_group_response_includes_timezone(self):
        with patch(
            "services.web.get_rollcalls_by_group_token",
            return_value={
                "group_token": "grp123", "group_name": "Test", "rollcalls": [],
                "upcoming": [], "shh_mode": False, "dues_enabled": False,
                "bot_username": "", "timezone": "Asia/Kolkata",
            },
        ):
            resp = _client().get("/api/v1/web/group/grp123")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["timezone"], "Asia/Kolkata")


class TestWebMembersList(unittest.TestCase):
    """GET /web/group/{token}/members — powers the login-link picker (real
    members only; proxy/guest names never appear here since they aren't
    real chat_members rows)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def test_requires_web_admin(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=False), \
             patch("api.identity.verify_identity_token", return_value=77):
            resp = _client().get("/api/v1/web/group/grp123/members", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 403)

    def test_returns_member_list(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch.object(_web_mod._db, "get_active_members", return_value=[
                 {"user_id": 1, "first_name": "Amit", "username": "amit"},
                 {"user_id": 2, "first_name": "Priya", "username": None},
             ]):
            resp = _client().get("/api/v1/web/group/grp123/members", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        members = resp.json()["members"]
        self.assertEqual(len(members), 2)
        self.assertEqual(members[0]["first_name"], "Amit")
        self.assertEqual(members[0]["username"], "amit")
        self.assertIsNone(members[1]["username"])

    def test_invalid_group_token_404(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value=None):
            resp = _client().get("/api/v1/web/group/badgrp/members", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Unified "New Rollcall" flow — full-field start, and one-time schedule
# entries referencing a template by name via the existing `title` column
# (no new schema — see check_reminders.py::_fire_scheduled_rollcalls).
# ---------------------------------------------------------------------------

@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebStartRollcallFullFields(unittest.TestCase):
    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def test_full_fields_pass_through_to_service(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Match Day",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.rollcalls.start_rollcall", new_callable=AsyncMock,
                   return_value={"rc_index": 0}) as svc, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()), \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock):
            resp = _client().post(
                "/api/v1/web/group/grp123/start-rollcall",
                json={"id_token": "tok", "title": "Match Day", "location": "Court 3",
                      "fee": "200", "limit": 10, "event_day": "friday", "event_time": "19:00"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(svc.call_args.kwargs["location"], "Court 3")
        self.assertEqual(svc.call_args.kwargs["fee"], "200")
        self.assertEqual(svc.call_args.kwargs["limit"], 10)
        self.assertEqual(svc.call_args.kwargs["event_day"], "friday")
        self.assertEqual(svc.call_args.kwargs["event_time"], "19:00")

    def test_finalize_at_passes_through_to_service(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Match Day",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.rollcalls.start_rollcall", new_callable=AsyncMock,
                   return_value={"rc_index": 0}) as svc, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()), \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock):
            resp = _client().post(
                "/api/v1/web/group/grp123/start-rollcall",
                json={"id_token": "tok", "title": "Match Day",
                      "finalize_at": "2026-07-26T05:10:00.000Z"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(svc.call_args.kwargs["finalize_at"], "2026-07-26T05:10:00.000Z")

    def test_event_day_without_event_time_422(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99):
            resp = _client().post(
                "/api/v1/web/group/grp123/start-rollcall",
                json={"id_token": "tok", "title": "Match Day", "event_day": "friday"})
        self.assertEqual(resp.status_code, 422)

    def test_save_as_template_upserts_template(self):
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Match Day",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", return_value={}) as tmpl_svc, \
             patch("services.rollcalls.start_rollcall", new_callable=AsyncMock,
                   return_value={"rc_index": 0}), \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()), \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock):
            resp = _client().post(
                "/api/v1/web/group/grp123/start-rollcall",
                json={"id_token": "tok", "title": "Match Day", "location": "Court 3",
                      "save_as_template": "FridayMatch"})
        self.assertEqual(resp.status_code, 201)
        tmpl_svc.assert_called_once()
        self.assertEqual(tmpl_svc.call_args.kwargs["name"], "FridayMatch")
        self.assertEqual(tmpl_svc.call_args.kwargs["chat_id"], -100)
        self.assertEqual(tmpl_svc.call_args.kwargs["location"], "Court 3")

    def test_save_as_template_failure_does_not_block_start(self):
        """save_as_template is a convenience, not a precondition — if the
        template upsert fails, the rollcall must still start."""
        import api.routes.web as _web_mod
        serialized = {"rollcall_id": 5, "title": "Match Day",
                      "in": [], "out": [], "maybe": [], "waiting": []}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.upsert_template", side_effect=Exception("db down")), \
             patch("services.rollcalls.start_rollcall", new_callable=AsyncMock,
                   return_value={"rc_index": 0}) as rc_svc, \
             patch("services.web._serialize_web_rollcall", return_value=serialized), \
             patch("rollcall_manager.manager.get_rollcall", return_value=MagicMock()), \
             patch.object(_web_mod, "_mirror_panel_to_telegram", new_callable=AsyncMock):
            resp = _client().post(
                "/api/v1/web/group/grp123/start-rollcall",
                json={"id_token": "tok", "title": "Match Day", "save_as_template": "FridayMatch"})
        self.assertEqual(resp.status_code, 201)
        rc_svc.assert_awaited_once()


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestWebScheduledRollcallsTemplateReference(unittest.TestCase):
    """The one-time 'Schedule' path repurposes scheduled_rollcalls.title to
    hold a template NAME instead of a raw display title — no new column
    (see api/routes/web.py's create_scheduled_rollcall/list_scheduled_rollcalls
    and check_reminders.py's firing logic)."""

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

    def test_list_populates_template_fields_when_title_matches(self):
        import api.routes.web as _web_mod
        rows = [{"id": 1, "title": "FridayMatch", "scheduled_at": "2026-07-25T13:00:00Z",
                 "created_by_name": "Amit"}]
        tmpl = {"name": "FridayMatch", "title": "Friday Match Night", "location": "Court 3",
                "eventfee": "200", "inlistlimit": 10}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.get_upcoming_scheduled_rollcalls", return_value=rows), \
             patch("services.templates.get_template", return_value=tmpl):
            resp = _client().get("/api/v1/web/group/grp123/scheduled-rollcalls", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["items"][0]
        self.assertEqual(item["display_title"], "Friday Match Night")
        self.assertEqual(item["location"], "Court 3")
        self.assertEqual(item["fee"], "200")
        self.assertEqual(item["limit"], 10)

    def test_list_leaves_fields_none_when_title_is_bare(self):
        """Rows created before this feature (or after their template was
        deleted) have a plain display title with no matching template row —
        must not error, just fall back to showing the raw title."""
        import api.routes.web as _web_mod
        rows = [{"id": 2, "title": "Just A Title", "scheduled_at": "2026-07-25T13:00:00Z",
                 "created_by_name": "Amit"}]
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch("services.templates.get_upcoming_scheduled_rollcalls", return_value=rows), \
             patch("services.templates.get_template", return_value=None):
            resp = _client().get("/api/v1/web/group/grp123/scheduled-rollcalls", headers={"X-Identity-Token": "tok"})
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["items"][0]
        self.assertIsNone(item["display_title"])
        self.assertIsNone(item["location"])
        self.assertIsNone(item["fee"])
        self.assertIsNone(item["limit"])
        self.assertEqual(item["title"], "Just A Title")

    def test_create_announcement_uses_template_display_title(self):
        import api.routes.web as _web_mod
        tmpl = {"name": "FridayMatch", "title": "Friday Match Night"}
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value={"first_name": "Amit"}), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch.object(_web_mod._db, "create_scheduled_rollcall", return_value=7), \
             patch.object(_web_mod._db, "get_template", return_value=tmpl), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/scheduled-rollcalls",
                json={"id_token": "tok", "title": "FridayMatch",
                      "scheduled_at": "2026-07-25T13:00:00Z"})
        self.assertEqual(resp.status_code, 201)
        notify.assert_awaited_once()
        self.assertIn("Friday Match Night", notify.call_args[0][1])

    def test_create_announcement_converts_to_chat_timezone(self):
        """The announcement must show the chat's LOCAL time with a timezone
        label, not a bare substring of the raw UTC ISO string — a prior bug
        showed 10:40 IST as an unlabeled "5:10" (the UTC hour) because the
        old code just regex-sliced scheduled_at without ever converting it."""
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token",
                          return_value={"chat_id": -100, "timezone": "Asia/Kolkata"}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch.object(_web_mod._db, "create_scheduled_rollcall", return_value=7), \
             patch.object(_web_mod._db, "get_template", return_value=None), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/scheduled-rollcalls",
                # 05:10 UTC == 10:40 IST (+05:30)
                json={"id_token": "tok", "title": "Sunday Cricket",
                      "scheduled_at": "2026-07-26T05:10:00.000Z"})
        self.assertEqual(resp.status_code, 201)
        msg = notify.call_args[0][1]
        self.assertIn("10:40", msg)
        self.assertIn("IST", msg)
        self.assertNotIn("05:10", msg)

    def test_create_announcement_falls_back_to_raw_title(self):
        import api.routes.web as _web_mod
        with patch.object(_web_mod._db, "get_chat_by_group_web_token", return_value={"chat_id": -100}), \
             patch.object(_web_mod._db, "is_web_admin", return_value=True), \
             patch.object(_web_mod._db, "get_member_display_info", return_value=None), \
             patch("api.identity.verify_identity_token", return_value=99), \
             patch.object(_web_mod._db, "create_scheduled_rollcall", return_value=7), \
             patch.object(_web_mod._db, "get_template", return_value=None), \
             patch.object(_web_mod, "_send_event_notification", new_callable=AsyncMock) as notify:
            resp = _client().post(
                "/api/v1/web/group/grp123/scheduled-rollcalls",
                json={"id_token": "tok", "title": "Just A Title",
                      "scheduled_at": "2026-07-25T13:00:00Z"})
        self.assertEqual(resp.status_code, 201)
        notify.assert_awaited_once()
        self.assertIn("Just A Title", notify.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
