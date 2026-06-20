"""
Unit tests for services/web.py — magic-link web voting service layer.

Tests cover both per-rollcall token flow (/web/join/{token}) and
permanent group token flow (/web/group/{group_token}).

Patching strategy:
  - services.web.db.*       → mock all DB calls
  - services.web.manager    → mock rollcall manager
  - services.web.proxy_svc  → mock proxy service (async)
"""

import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(name="Alice", user_id=1, comment=""):
    from models import User
    u = User.__new__(User)
    u.name = name
    u.first_name = name
    u.username = None
    u.user_id = user_id
    u.comment = comment
    return u


def _make_rc(title="Friday Football", rc_id=42, web_token="abc123token",
             in_list=None, out_list=None, maybe_list=None, wait_list=None,
             limit=None, location=None, finalize_date=None, chat_id=100):
    rc = MagicMock()
    rc.id = rc_id
    rc.title = title
    rc.web_token = web_token
    rc.chat_id = chat_id
    rc.inList = in_list or []
    rc.outList = out_list or []
    rc.maybeList = maybe_list or []
    rc.waitList = wait_list or []
    rc.inListLimit = limit
    rc.location = location
    rc.event_fee = None
    rc.finalizeDate = finalize_date
    rc.timezone = "Asia/Kolkata"
    return rc


def _db_row(rc_id=42, chat_id=100, web_token="abc123token", is_active=True):
    return {
        "id": rc_id,
        "chat_id": chat_id,
        "web_token": web_token,
        "is_active": is_active,
        "title": "Friday Football",
    }


def _chat_row(chat_id=100, group_token="grouptoken000"):
    return {
        "chat_id": chat_id,
        "group_web_token": group_token,
        "shh_mode": False,
    }


# ---------------------------------------------------------------------------
# _serialize_web_rollcall
# ---------------------------------------------------------------------------

class TestSerializeWebRollcall(unittest.TestCase):

    def _call(self, rc):
        from services.web import _serialize_web_rollcall
        return _serialize_web_rollcall(rc)

    def test_basic_fields(self):
        rc = _make_rc()
        result = self._call(rc)
        self.assertEqual(result["rollcall_id"], 42)
        self.assertEqual(result["title"], "Friday Football")
        self.assertEqual(result["web_token"], "abc123token")
        self.assertIsNone(result["finalize_date"])
        self.assertIsNone(result["limit"])
        self.assertIsNone(result["location"])

    def test_empty_lists(self):
        rc = _make_rc()
        result = self._call(rc)
        self.assertEqual(result["in"], [])
        self.assertEqual(result["out"], [])
        self.assertEqual(result["maybe"], [])
        self.assertEqual(result["waiting"], [])

    def test_populated_in_list(self):
        alice = _make_user("Alice", comment="on time")
        bob = _make_user("Bob", comment="")
        rc = _make_rc(in_list=[alice, bob])
        result = self._call(rc)
        self.assertEqual(len(result["in"]), 2)
        self.assertEqual(result["in"][0]["name"], "Alice")
        self.assertEqual(result["in"][0]["comment"], "on time")
        self.assertEqual(result["in"][1]["name"], "Bob")
        self.assertEqual(result["in"][1]["comment"], "")

    def test_all_lists_populated(self):
        rc = _make_rc(
            in_list=[_make_user("Alice")],
            out_list=[_make_user("Bob")],
            maybe_list=[_make_user("Carol")],
            wait_list=[_make_user("Dave")],
        )
        result = self._call(rc)
        self.assertEqual(len(result["in"]), 1)
        self.assertEqual(len(result["out"]), 1)
        self.assertEqual(len(result["maybe"]), 1)
        self.assertEqual(len(result["waiting"]), 1)

    def test_finalize_date_formatted(self):
        from datetime import datetime
        fd = datetime(2026, 6, 21, 18, 0, 0)
        rc = _make_rc(finalize_date=fd)
        result = self._call(rc)
        self.assertIsNotNone(result["finalize_date"])
        self.assertIn("Sunday", result["finalize_date"])

    def test_limit_and_location(self):
        rc = _make_rc(limit=10, location="Central Park")
        result = self._call(rc)
        self.assertEqual(result["limit"], 10)
        self.assertEqual(result["location"], "Central Park")

    def test_missing_web_token_returns_empty_string(self):
        rc = _make_rc()
        del rc.web_token  # simulate attribute missing
        result = self._call(rc)
        self.assertEqual(result["web_token"], "")

    def test_none_comment_returns_empty_string(self):
        alice = _make_user("Alice", comment=None)
        alice.comment = None
        rc = _make_rc(in_list=[alice])
        result = self._call(rc)
        self.assertEqual(result["in"][0]["comment"], "")


# ---------------------------------------------------------------------------
# _resolve_rc
# ---------------------------------------------------------------------------

class TestResolveRc(unittest.TestCase):

    def _call(self, token):
        from services.web import _resolve_rc
        return _resolve_rc(token)

    def test_empty_token_raises_parameter_missing(self):
        from exceptions import parameterMissing
        with self.assertRaises(parameterMissing):
            self._call("")

    def test_none_token_raises_parameter_missing(self):
        from exceptions import parameterMissing
        with self.assertRaises(parameterMissing):
            self._call(None)

    def test_unknown_token_raises_incorrect_parameter(self):
        from exceptions import incorrectParameter
        with patch("services.web.db") as mock_db:
            mock_db.get_rollcall_by_web_token.return_value = None
            with self.assertRaises(incorrectParameter) as ctx:
                self._call("unknowntoken")
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_rollcall_not_in_manager_raises(self):
        """Token found in DB but rollcall no longer in manager (ended)."""
        from exceptions import incorrectParameter
        row = _db_row(rc_id=99)
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = []  # no active rollcalls
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_rollcall_by_web_token.return_value = row
            with self.assertRaises(incorrectParameter) as ctx:
                self._call("abc123token")
        self.assertIn("ended", str(ctx.exception).lower())

    def test_valid_token_returns_correct_triple(self):
        rc = _make_rc(rc_id=42, chat_id=100, web_token="abc123token")
        row = _db_row(rc_id=42, chat_id=100)
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc]
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_rollcall_by_web_token.return_value = row
            chat_id, idx, returned_rc = self._call("abc123token")
        self.assertEqual(chat_id, 100)
        self.assertEqual(idx, 0)
        self.assertIs(returned_rc, rc)

    def test_correct_rc_selected_when_multiple_active(self):
        rc1 = _make_rc(rc_id=10, web_token="tok1")
        rc2 = _make_rc(rc_id=20, web_token="tok2")
        row = _db_row(rc_id=20)
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc1, rc2]
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_rollcall_by_web_token.return_value = row
            chat_id, idx, returned_rc = self._call("tok2")
        self.assertEqual(idx, 1)
        self.assertIs(returned_rc, rc2)


# ---------------------------------------------------------------------------
# get_rollcall_by_token
# ---------------------------------------------------------------------------

class TestGetRollcallByToken(unittest.TestCase):

    def _call(self, token):
        from services.web import get_rollcall_by_token
        return get_rollcall_by_token(token)

    def test_returns_serialized_dict(self):
        rc = _make_rc()
        row = _db_row()
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc]
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_rollcall_by_web_token.return_value = row
            result = self._call("abc123token")
        self.assertEqual(result["rollcall_id"], 42)
        self.assertEqual(result["title"], "Friday Football")
        self.assertIn("in", result)
        self.assertIn("out", result)

    def test_invalid_token_propagates_exception(self):
        from exceptions import incorrectParameter
        with patch("services.web.db") as mock_db:
            mock_db.get_rollcall_by_web_token.return_value = None
            with self.assertRaises(incorrectParameter):
                self._call("badtoken")


# ---------------------------------------------------------------------------
# vote_by_token
# ---------------------------------------------------------------------------

class TestVoteByToken(unittest.IsolatedAsyncioTestCase):

    async def _call(self, token, name, vote_type, rc=None, row=None, mgr=None):
        from services.web import vote_by_token
        _rc = rc or _make_rc()
        _row = row or _db_row()
        _mgr = mgr or MagicMock()
        _mgr.get_rollcalls.return_value = [_rc]
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", _mgr), \
             patch("services.web.proxy_svc") as mock_proxy:
            mock_db.get_rollcall_by_web_token.return_value = _row
            mock_proxy.set_in_for = AsyncMock()
            mock_proxy.set_out_for = AsyncMock()
            mock_proxy.set_maybe_for = AsyncMock()
            result = await vote_by_token(token, name, vote_type)
            return result, mock_proxy

    async def test_vote_in_calls_set_in_for(self):
        result, proxy = await self._call("abc123token", "Alice", "in")
        proxy.set_in_for.assert_awaited_once_with(100, "Alice", rc_index=0)
        self.assertEqual(result["title"], "Friday Football")

    async def test_vote_out_calls_set_out_for(self):
        _, proxy = await self._call("abc123token", "Bob", "out")
        proxy.set_out_for.assert_awaited_once_with(100, "Bob", rc_index=0)

    async def test_vote_maybe_calls_set_maybe_for(self):
        _, proxy = await self._call("abc123token", "Carol", "maybe")
        proxy.set_maybe_for.assert_awaited_once_with(100, "Carol", rc_index=0)

    async def test_empty_name_raises(self):
        from exceptions import parameterMissing
        from services.web import vote_by_token
        with patch("services.web.db") as mock_db:
            mock_db.get_rollcall_by_web_token.return_value = _db_row()
            with self.assertRaises(parameterMissing):
                await vote_by_token("abc123token", "  ", "in")

    async def test_none_name_raises(self):
        from exceptions import parameterMissing
        from services.web import vote_by_token
        with patch("services.web.db") as mock_db:
            mock_db.get_rollcall_by_web_token.return_value = _db_row()
            with self.assertRaises(parameterMissing):
                await vote_by_token("abc123token", None, "in")

    async def test_invalid_vote_type_raises(self):
        from exceptions import incorrectParameter
        from services.web import vote_by_token
        rc = _make_rc()
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc]
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_rollcall_by_web_token.return_value = _db_row()
            with self.assertRaises(incorrectParameter):
                await vote_by_token("abc123token", "Alice", "abstain")

    async def test_invalid_token_raises(self):
        from exceptions import incorrectParameter
        from services.web import vote_by_token
        with patch("services.web.db") as mock_db:
            mock_db.get_rollcall_by_web_token.return_value = None
            with self.assertRaises(incorrectParameter):
                await vote_by_token("badtoken", "Alice", "in")

    async def test_name_whitespace_stripped(self):
        _, proxy = await self._call("abc123token", "  Alice  ", "in")
        proxy.set_in_for.assert_awaited_once_with(100, "Alice", rc_index=0)

    async def test_name_truncated_to_64_chars(self):
        long_name = "A" * 100
        _, proxy = await self._call("abc123token", long_name, "in")
        called_name = proxy.set_in_for.call_args[0][1]
        self.assertEqual(len(called_name), 64)

    async def test_returns_updated_rollcall_state(self):
        alice = _make_user("Alice")
        rc_after = _make_rc(in_list=[alice])
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = [rc_after]
        from services.web import vote_by_token
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr), \
             patch("services.web.proxy_svc") as mock_proxy:
            mock_db.get_rollcall_by_web_token.return_value = _db_row()
            mock_proxy.set_in_for = AsyncMock()
            result = await vote_by_token("abc123token", "Alice", "in")
        self.assertEqual(len(result["in"]), 1)
        self.assertEqual(result["in"][0]["name"], "Alice")


# ---------------------------------------------------------------------------
# get_group_web_token
# ---------------------------------------------------------------------------

class TestGetGroupWebToken(unittest.TestCase):

    def test_returns_token_from_chat(self):
        from services.web import get_group_web_token
        with patch("services.web.db") as mock_db:
            mock_db.get_or_create_chat.return_value = _chat_row(group_token="mygrouptoken")
            token = get_group_web_token(100)
        self.assertEqual(token, "mygrouptoken")
        mock_db.get_or_create_chat.assert_called_once_with(100)


# ---------------------------------------------------------------------------
# get_rollcalls_by_group_token
# ---------------------------------------------------------------------------

class TestGetRollcallsByGroupToken(unittest.TestCase):

    def _call(self, group_token, chat_row=None, rollcalls=None):
        from services.web import get_rollcalls_by_group_token
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = rollcalls or []
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_chat_by_group_web_token.return_value = chat_row
            return get_rollcalls_by_group_token(group_token)

    def test_empty_token_raises(self):
        from exceptions import parameterMissing
        from services.web import get_rollcalls_by_group_token
        with self.assertRaises(parameterMissing):
            get_rollcalls_by_group_token("")

    def test_none_token_raises(self):
        from exceptions import parameterMissing
        from services.web import get_rollcalls_by_group_token
        with self.assertRaises(parameterMissing):
            get_rollcalls_by_group_token(None)

    def test_unknown_token_raises(self):
        from exceptions import incorrectParameter
        with self.assertRaises(incorrectParameter) as ctx:
            self._call("badtoken", chat_row=None)
        self.assertIn("invalid", str(ctx.exception).lower())

    def test_no_active_rollcalls_returns_empty_list(self):
        result = self._call("grouptoken", chat_row=_chat_row(), rollcalls=[])
        self.assertEqual(result["group_token"], "grouptoken")
        self.assertEqual(result["rollcalls"], [])

    def test_single_rollcall_returned(self):
        rc = _make_rc(title="Saturday Game")
        result = self._call("grouptoken", chat_row=_chat_row(), rollcalls=[rc])
        self.assertEqual(len(result["rollcalls"]), 1)
        self.assertEqual(result["rollcalls"][0]["title"], "Saturday Game")

    def test_multiple_rollcalls_all_returned(self):
        rc1 = _make_rc(title="Morning Run", rc_id=1, web_token="tok1")
        rc2 = _make_rc(title="Evening Game", rc_id=2, web_token="tok2")
        rc3 = _make_rc(title="Weekend Trip", rc_id=3, web_token="tok3")
        result = self._call("grouptoken", chat_row=_chat_row(), rollcalls=[rc1, rc2, rc3])
        self.assertEqual(len(result["rollcalls"]), 3)
        titles = [r["title"] for r in result["rollcalls"]]
        self.assertIn("Morning Run", titles)
        self.assertIn("Evening Game", titles)
        self.assertIn("Weekend Trip", titles)

    def test_response_includes_group_token(self):
        result = self._call("mytoken123", chat_row=_chat_row(group_token="mytoken123"), rollcalls=[])
        self.assertEqual(result["group_token"], "mytoken123")

    def test_each_rollcall_has_web_token(self):
        rc = _make_rc(web_token="rc_specific_token")
        result = self._call("grouptoken", chat_row=_chat_row(), rollcalls=[rc])
        self.assertEqual(result["rollcalls"][0]["web_token"], "rc_specific_token")

    def test_correct_chat_id_queried(self):
        """Rollcalls are fetched for the chat the group_token belongs to."""
        from services.web import get_rollcalls_by_group_token
        mgr = MagicMock()
        mgr.get_rollcalls.return_value = []
        chat = _chat_row(chat_id=999)
        with patch("services.web.db") as mock_db, \
             patch("services.web.manager", mgr):
            mock_db.get_chat_by_group_web_token.return_value = chat
            get_rollcalls_by_group_token("grouptoken")
        mgr.get_rollcalls.assert_called_once_with(999)


if __name__ == "__main__":
    unittest.main()
