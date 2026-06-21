"""
REST API route tests using FastAPI's TestClient.

Patching strategy:
  - `api.auth.lookup_api_token` and `api.auth._hash_token` control auth.
    These must be patched at the already-imported binding in api/auth.py,
    NOT at db.lookup_api_token (which was replaced by the conftest db_mock
    and bound into api.auth at import time).
  - Service functions are patched at their module path in services/*.
  - os.environ changes use patch.dict so rate-limit env reads aren't broken.
"""

import json as _json
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
# Shared fake tokens
# ---------------------------------------------------------------------------

_ADMIN_TOKEN = MagicMock()
_ADMIN_TOKEN.__getitem__ = lambda self, k: {
    "chat_id": 100,
    "scopes": ["read", "vote", "admin"],
    "label": "test-admin",
    "issued_by_user_id": 1,
}.get(k)
_ADMIN_TOKEN.get = lambda self, k, d=None: {
    "chat_id": 100,
    "scopes": ["read", "vote", "admin"],
    "label": "test-admin",
    "issued_by_user_id": 1,
}.get(k, d)

# Simpler approach: real dicts
_ADMIN_ROW = {
    "chat_id": 100,
    "scopes": ["read", "vote", "admin"],
    "label": "test-admin",
    "issued_by_user_id": 1,
}
_VOTE_ROW = {
    "chat_id": 100,
    "scopes": ["read", "vote"],
    "label": "test-vote",
    "issued_by_user_id": 1,
}
_READ_ROW = {
    "chat_id": 100,
    "scopes": ["read"],
    "label": "test-read",
    "issued_by_user_id": 1,
}

_BEARER = "Bearer faketoken"

_RC_DICT = {
    "id": 42,
    "number": 1,
    "rc_index": 0,
    "title": "Weekly Game",
    "in_list": [],
    "out_list": [],
    "maybe_list": [],
    "wait_list": [],
    "in_count": 0,
    "out_count": 0,
    "maybe_count": 0,
    "wait_count": 0,
    "limit": None,
    "location": None,
    "event_fee": None,
    "individual_fee": None,
    "timezone": "Asia/Kolkata",
    "finalize_date": None,
    "reminder_hours": None,
}

_USER_DICT = {
    "user_id": 1,
    "name": "Alice",
    "username": "alice",
    "comment": "",
    "is_proxy": False,
}

_VOTE_RESULT = {
    "action": "added",
    "rollcall": _RC_DICT,
    "user": _USER_DICT,
    "rc_number_1based": 1,
    "was_in": None,
    "promoted": None,
}

_TMPL = {
    "name": "weekly",
    "title": "Weekly Game",
    "limit": None,
    "location": None,
    "fee": None,
    "offset_days": None,
    "offset_hours": None,
    "offset_minutes": None,
    "event_day": "friday",
    "event_time": "19:00",
    "schedule_day": None,
    "schedule_time": None,
    "schedule_enabled": False,
    "recurrence_type": "weekly",
    "last_scheduled_date": None,
}


def _make_client(token_row=_ADMIN_ROW):
    """Create a TestClient with the given token row pre-patched into auth."""
    from api.main import create_app
    app = create_app()
    client = TestClient(app, raise_server_exceptions=False)
    return client, token_row


def _auth_patches(token_row):
    """Context manager that patches auth so the given token row is returned."""
    return patch("api.auth.lookup_api_token", return_value=token_row), \
           patch("api.auth._hash_token", return_value="hashed")


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestRollcallRoutes(unittest.TestCase):

    def _headers(self):
        return {"Authorization": _BEARER}

    def _app(self):
        from api.main import create_app
        return create_app()

    def test_list_rollcalls_empty(self):
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.list_rollcalls", return_value=[]):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/rollcalls", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_rollcalls_with_data(self):
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.list_rollcalls", return_value=[_RC_DICT]):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/rollcalls", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["title"], "Weekly Game")

    def test_get_rollcall_found(self):
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.get_rollcall", return_value=_RC_DICT):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/rollcalls/1", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "Weekly Game")

    def test_get_rollcall_not_found_returns_404(self):
        from exceptions import rollCallNotStarted
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.get_rollcall", side_effect=rollCallNotStarted("not active")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/rollcalls/1", headers=self._headers())
        self.assertEqual(resp.status_code, 404)
        self.assertIn("rollCallNotStarted", resp.json()["error"])

    def test_start_rollcall_created_201(self):
        auth_a, auth_b = _auth_patches(_ADMIN_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.start_rollcall", new_callable=AsyncMock, return_value=_RC_DICT):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls",
                headers=self._headers(),
                json={"title": "Weekly Game", "started_by_user_id": 1, "started_by_name": "Admin"},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["title"], "Weekly Game")

    def test_start_rollcall_max_reached_409(self):
        from exceptions import amountOfRollCallsReached
        auth_a, auth_b = _auth_patches(_ADMIN_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.start_rollcall",
                   new_callable=AsyncMock,
                   side_effect=amountOfRollCallsReached("max")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls",
                headers=self._headers(),
                json={"title": "X", "started_by_user_id": 1, "started_by_name": "A"},
            )
        self.assertEqual(resp.status_code, 409)

    def test_end_rollcall_requires_admin_scope(self):
        """Token with vote-only scope should be rejected on DELETE."""
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            # DELETE with JSON body via content= parameter
            resp = client.request(
                "DELETE",
                "/api/v1/chats/100/rollcalls/1",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=_json.dumps({"ended_by_user_id": 1, "ended_by_name": "Admin"}),
            )
        self.assertEqual(resp.status_code, 403)

    def test_missing_auth_header_returns_401(self):
        auth_a, auth_b = _auth_patches(None)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/rollcalls")
        self.assertEqual(resp.status_code, 401)

    def test_chat_id_mismatch_returns_403(self):
        """Token is bound to chat 100 but URL targets chat 999."""
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.rollcalls.list_rollcalls", return_value=[]):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get(
                "/api/v1/chats/999/rollcalls",
                headers=self._headers(),
            )
        self.assertEqual(resp.status_code, 403)

    def test_rc_number_ge_1_required(self):
        """rc_number of 0 should be rejected by FastAPI path validation."""
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/rollcalls/0", headers=self._headers())
        self.assertEqual(resp.status_code, 422)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestVoteRoutes(unittest.TestCase):

    def _headers(self):
        return {"Authorization": _BEARER}

    def _app(self):
        from api.main import create_app
        return create_app()

    def test_cast_vote_in(self):
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.voting.vote_in", new_callable=AsyncMock, return_value=_VOTE_RESULT):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "in", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["action"], "added")

    def test_cast_vote_out(self):
        out_resp = {**_VOTE_RESULT, "action": "moved", "was_in": True}
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.voting.vote_out", new_callable=AsyncMock, return_value=out_resp):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "out", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["action"], "moved")

    def test_cast_vote_maybe(self):
        maybe_resp = {**_VOTE_RESULT, "action": "added", "was_in": False}
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.voting.vote_maybe", new_callable=AsyncMock, return_value=maybe_resp):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "maybe", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 201)

    def test_already_in_list_returns_409(self):
        from exceptions import alreadyInList
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.voting.vote_in",
                   new_callable=AsyncMock,
                   side_effect=alreadyInList("already in")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "in", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 409)

    def test_vote_rollcall_not_found_returns_404(self):
        from exceptions import rollCallNotStarted
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.voting.vote_in",
                   new_callable=AsyncMock,
                   side_effect=rollCallNotStarted("not active")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "in", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_invalid_vote_type_rejected_by_pydantic_422(self):
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "flying", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 422)

    def test_rc_number_must_be_positive_422(self):
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/0/votes",
                headers=self._headers(),
                json={"vote": "in", "user_id": 1, "first_name": "Alice"},
            )
        self.assertEqual(resp.status_code, 422)

    def test_vote_with_comment(self):
        """Comment field is optional but accepted."""
        with_comment = {**_VOTE_RESULT, "user": {**_USER_DICT, "comment": "bringing snacks"}}
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.voting.vote_in", new_callable=AsyncMock, return_value=with_comment):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/votes",
                headers=self._headers(),
                json={"vote": "in", "user_id": 1, "first_name": "Alice", "comment": "bringing snacks"},
            )
        self.assertEqual(resp.status_code, 201)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestProxyVoteRoutes(unittest.TestCase):

    def _headers(self):
        return {"Authorization": _BEARER}

    def _app(self):
        from api.main import create_app
        return create_app()

    def test_proxy_vote_in(self):
        prx_resp = {
            "action": "added",
            "rollcall": _RC_DICT,
            "user": {**_USER_DICT, "user_id": "ProxyBob", "is_proxy": True},
            "proxy_owner_id": 1,
            "rc_number_1based": 1,
            "was_in": None,
            "promoted": None,
        }
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.proxy.set_in_for", new_callable=AsyncMock, return_value=prx_resp):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/proxy-votes",
                headers=self._headers(),
                json={"vote": "in", "admin_user_id": 1, "admin_name": "Admin", "proxy_name": "ProxyBob"},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["action"], "added")

    def test_proxy_vote_out(self):
        prx_resp = {
            "action": "added",
            "rollcall": _RC_DICT,
            "user": {**_USER_DICT, "user_id": "ProxyBob", "is_proxy": True},
            "proxy_owner_id": 1,
            "rc_number_1based": 1,
            "was_in": False,
            "promoted": None,
        }
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.proxy.set_out_for", new_callable=AsyncMock, return_value=prx_resp):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/proxy-votes",
                headers=self._headers(),
                json={"vote": "out", "admin_user_id": 1, "admin_name": "Admin", "proxy_name": "ProxyBob"},
            )
        self.assertEqual(resp.status_code, 201)

    def test_proxy_duplicate_returns_409(self):
        from exceptions import duplicateProxy
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.proxy.set_in_for",
                   new_callable=AsyncMock,
                   side_effect=duplicateProxy("already there")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/proxy-votes",
                headers=self._headers(),
                json={"vote": "in", "admin_user_id": 1, "admin_name": "Admin", "proxy_name": "ProxyBob"},
            )
        self.assertEqual(resp.status_code, 409)

    def test_proxy_repeat_name_returns_409(self):
        from exceptions import repeatlyName
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b, \
             patch("services.proxy.set_in_for",
                   new_callable=AsyncMock,
                   side_effect=repeatlyName("name conflict")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/rollcalls/1/proxy-votes",
                headers=self._headers(),
                json={"vote": "in", "admin_user_id": 1, "admin_name": "Admin", "proxy_name": "ProxyBob"},
            )
        self.assertEqual(resp.status_code, 409)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestStatsRoutes(unittest.TestCase):

    def _headers(self):
        return {"Authorization": _BEARER}

    def _app(self):
        from api.main import create_app
        return create_app()

    def test_chat_settings_200(self):
        settings = {
            "timezone": "Asia/Kolkata",
            "shh_mode": False,
            "admin_rights": False,
            "ghost_tracking_enabled": True,
            "absent_limit": 1,
        }
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.settings.get_chat_settings", return_value=settings):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/settings", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["timezone"], "Asia/Kolkata")

    def test_ghost_settings_200(self):
        ghost = {"ghost_tracking_enabled": True, "absent_limit": 2}
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.ghost.get_ghost_settings", return_value=ghost):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/ghost/settings", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ghost_tracking_enabled"])

    def test_set_timezone_invalid_422(self):
        from exceptions import incorrectParameter
        auth_a, auth_b = _auth_patches(_ADMIN_ROW)
        with auth_a, auth_b, \
             patch("services.settings.set_timezone", side_effect=incorrectParameter("bad")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.put(
                "/api/v1/chats/100/settings/timezone",
                headers=self._headers(),
                json={"timezone": "Fake/Zone", "admin_user_id": 1, "admin_name": "A"},
            )
        self.assertEqual(resp.status_code, 422)

    def test_ghost_leaderboard_200(self):
        board = [{"name": "Alice", "user_id": 1, "is_proxy": False, "ghost_count": 3}]
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.ghost.ghost_leaderboard", return_value=board):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/ghost/leaderboard", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["ghost_count"], 3)

    def test_group_stats_200(self):
        gstats = {
            "total_rollcalls": 10,
            "total_attendances": 50,
            "unique_participants": 8,
            "top_attendees": [],
            "ghost_leaderboard": [],
        }
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.stats.group_stats", return_value=gstats):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/stats/group", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total_rollcalls"], 10)

    def test_history_200(self):
        rows = [{"id": 1, "title": "G", "ended_at": "2024-01-01",
                 "in_count": 5, "out_count": 1, "maybe_count": 0}]
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.stats.history", return_value=rows):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/history", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["title"], "G")

    def test_leaderboard_200(self):
        board = {
            "total_rollcalls_in_chat": 5,
            "entries": [{"rank": 1, "display_name": "Alice", "username": None,
                         "user_id": 1, "kind": "real", "sessions_attended": 10,
                         "total_sessions_voted": 10, "attendance_rate": 80.0,
                         "voting_rate": 80.0}],
        }
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.stats.leaderboard", return_value=board):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/stats/leaderboard", headers=self._headers())
        self.assertEqual(resp.status_code, 200)

    def test_set_limit_admin_only(self):
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.put(
                "/api/v1/chats/100/rollcalls/1/settings/limit",
                headers=self._headers(),
                json={"limit": 5, "admin_user_id": 1, "admin_name": "A"},
            )
        self.assertEqual(resp.status_code, 403)

    def test_toggle_ghost_requires_admin(self):
        auth_a, auth_b = _auth_patches(_VOTE_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.put(
                "/api/v1/chats/100/ghost/settings/tracking",
                headers=self._headers(),
                json={"enabled": True, "admin_user_id": 1, "admin_name": "A"},
            )
        self.assertEqual(resp.status_code, 403)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestTemplateRoutes(unittest.TestCase):

    def _headers(self):
        return {"Authorization": _BEARER}

    def _app(self):
        from api.main import create_app
        return create_app()

    def test_list_templates_200(self):
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.templates.list_templates", return_value=[_TMPL]):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/templates", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()[0]["name"], "weekly")

    def test_get_template_found_200(self):
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.templates.get_one_template", return_value=_TMPL):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/templates/weekly", headers=self._headers())
        self.assertEqual(resp.status_code, 200)

    def test_get_template_not_found_422(self):
        from exceptions import incorrectParameter
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.templates.get_one_template", side_effect=incorrectParameter("not found")):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/templates/ghost", headers=self._headers())
        self.assertEqual(resp.status_code, 422)

    def test_upsert_template_200(self):
        auth_a, auth_b = _auth_patches(_ADMIN_ROW)
        with auth_a, auth_b, \
             patch("services.templates.upsert_template", return_value=_TMPL):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.put(
                "/api/v1/chats/100/templates/weekly",
                headers=self._headers(),
                json={"admin_user_id": 1, "admin_name": "Admin", "title": "Weekly Game"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "weekly")

    def test_delete_template_200(self):
        auth_a, auth_b = _auth_patches(_ADMIN_ROW)
        with auth_a, auth_b, \
             patch("services.templates.delete_one_template",
                   return_value={"name": "weekly", "deleted": True}):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.request(
                "DELETE",
                "/api/v1/chats/100/templates/weekly",
                headers={**self._headers(), "Content-Type": "application/json"},
                content=_json.dumps({"admin_user_id": 1, "admin_name": "Admin"}),
            )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["deleted"])

    def test_start_template_201(self):
        auth_a, auth_b = _auth_patches(_ADMIN_ROW)
        with auth_a, auth_b, \
             patch("services.templates.start_template", new_callable=AsyncMock, return_value=_RC_DICT):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/chats/100/templates/weekly/start",
                headers=self._headers(),
                json={"admin_user_id": 1, "admin_name": "Admin"},
            )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["title"], "Weekly Game")

    def test_read_scope_cannot_upsert_template(self):
        """Read-only token should get 403 on template mutation."""
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.put(
                "/api/v1/chats/100/templates/weekly",
                headers=self._headers(),
                json={"admin_user_id": 1, "admin_name": "Admin"},
            )
        self.assertEqual(resp.status_code, 403)

    def test_schedule_get_200(self):
        sched = {
            "name": "weekly",
            "schedule_day": "friday",
            "schedule_time": "19:00",
            "schedule_enabled": True,
            "recurrence_type": "weekly",
            "last_scheduled_date": None,
        }
        auth_a, auth_b = _auth_patches(_READ_ROW)
        with auth_a, auth_b, \
             patch("services.templates.get_schedule", return_value=sched):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.get("/api/v1/chats/100/templates/weekly/schedule", headers=self._headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["schedule_day"], "friday")


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestHealthRoute(unittest.TestCase):

    def test_health_returns_200(self):
        from api.main import create_app
        client = TestClient(create_app(), raise_server_exceptions=False)
        resp = client.get("/api/v1/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(resp.json()["api_version"], "v1")


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestMiniAppAuthRoute(unittest.TestCase):
    """Tests for POST /api/v1/auth/telegram/miniapp."""

    def _app(self):
        from api.main import create_app
        return create_app()

    def _make_init_data(self, bot_token="123:TEST"):
        import hashlib
        import hmac
        import json
        import time
        from urllib.parse import quote, urlencode
        user_obj = json.dumps({"id": 1, "first_name": "Alice", "is_bot": False})
        chat_obj = json.dumps({"id": -100, "title": "Group", "type": "group"})
        auth_date = str(int(time.time()) - 5)
        pairs = {
            "auth_date": auth_date,
            "chat": quote(chat_obj),
            "user": quote(user_obj),
        }
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        sig = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        pairs["hash"] = sig
        return urlencode(pairs)

    def test_miniapp_auth_issues_token(self):
        init_data = self._make_init_data("123:TEST")
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "123:TEST"}), \
             patch("api.routes.auth.generate_api_token", return_value="rawtoken123"), \
             patch("api.routes.auth._hash_token", return_value="hashedtoken"), \
             patch("api.routes.auth.insert_api_token") as mock_insert:
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/auth/telegram/miniapp",
                json={"init_data": init_data},
            )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["token"], "rawtoken123")
        self.assertEqual(body["user_id"], 1)
        self.assertEqual(body["chat_id"], -100)
        self.assertEqual(body["expires_in"], 3600)
        mock_insert.assert_called_once()
        # Verify scopes include both read and vote
        insert_kwargs = mock_insert.call_args.kwargs
        self.assertEqual(insert_kwargs.get("scopes"), "read,vote")

    def test_miniapp_auth_wrong_token_401(self):
        init_data = self._make_init_data("123:CORRECT")
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "999:WRONG"}):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/auth/telegram/miniapp",
                json={"init_data": init_data},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("HMAC", resp.json()["detail"])

    def test_miniapp_auth_no_bot_token_503(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TELEGRAM_TOKEN", "API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/auth/telegram/miniapp",
                json={"init_data": "auth_date=1234&hash=abc"},
            )
        self.assertEqual(resp.status_code, 503)

    def test_miniapp_auth_stale_data_401(self):
        """initData older than 24 h should be rejected."""
        import hashlib
        import hmac as hmac_mod
        import time
        from urllib.parse import quote, urlencode
        user_obj = '{"id":1,"first_name":"Alice","is_bot":false}'
        auth_date = str(int(time.time()) - 90_000)  # 25 h ago
        pairs = {"auth_date": auth_date, "user": quote(user_obj)}
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac_mod.new(b"WebAppData", b"123:TEST", hashlib.sha256).digest()
        sig = hmac_mod.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        pairs["hash"] = sig
        init_data = urlencode(pairs)

        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "123:TEST"}):
            client = TestClient(self._app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/v1/auth/telegram/miniapp",
                json={"init_data": init_data},
            )
        self.assertEqual(resp.status_code, 401)
        self.assertIn("24 hours", resp.json()["detail"])


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestRateLimitMiddleware(unittest.TestCase):
    """Tests for the rate-limiting middleware."""

    def test_health_endpoint_bypasses_rate_limit(self):
        """Health endpoint is always allowed through regardless of request count."""
        from api.main import create_app
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        client = TestClient(create_app(), raise_server_exceptions=False)
        for _ in range(5):
            resp = client.get("/api/v1/health")
            self.assertEqual(resp.status_code, 200)

    def test_unauthenticated_flood_throttled(self):
        """Flood of unauthenticated requests should be throttled."""
        from api.main import create_app
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()

        client = TestClient(create_app(), raise_server_exceptions=False)
        # Use a very low limit to trigger throttling without 60+ requests
        with patch.dict(os.environ, {
            "REST_API_RATE_LIMIT_MAX_REQUESTS": "3",
            "REST_API_RATE_LIMIT_WINDOW_SECONDS": "60",
        }):
            statuses = []
            for _ in range(5):
                resp = client.get("/api/v1/chats/100/rollcalls")
                statuses.append(resp.status_code)
        # First N requests get 401 (no auth); once rate-limited, get 429
        self.assertIn(429, statuses, f"Expected 429 among {statuses}")


if __name__ == "__main__":
    unittest.main()
