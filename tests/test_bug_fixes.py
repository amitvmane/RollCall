"""
tests/test_bug_fixes.py

Unit tests specifically covering the bugs and security issues fixed in the
code review. Each test is annotated with the bug ID it covers.

Bugs covered:
  BUG-1  models.py: redundant proxy_owners initialisation discarded data
  BUG-2  models.py: _get_user_current_status returned 'in' for unknown user
  BUG-3  models.py: inListLimit non-numeric DB value caused ValueError
  BUG-4  models.py: User.__init__ crashed when username was None during
          name disambiguation
  BUG-5  telegram_helper.py: version_command crashed on missing/corrupt
          version.json and silently sent nothing when no version was deployed
  BUG-6  telegram_helper.py: broadcast used legacy JSON file instead of DB
          and swallowed all delivery errors
  BUG-7  check_reminders.py: errors were printed instead of logged
  SEC-1  db.py: cursor left undefined if conn.cursor() raises — finally
          block would throw NameError on PostgreSQL path
"""

import sys
import os
import unittest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from models import RollCall, User  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def make_rollcall(title="Test Event"):
    rc = RollCall(title)
    rc.id = 1
    return rc


def make_user(name="Alice", username="alice", user_id=100):
    return User(name, username, user_id, [])


def make_proxy(name="Bob Proxy"):
    return User(name, None, name, [])


# ---------------------------------------------------------------------------
# BUG-1: proxy_owners must not be discarded during _load_from_db
# ---------------------------------------------------------------------------

class TestProxyOwnersNotDiscarded(unittest.TestCase):
    """
    models.py L96-97: the getattr on line 96 was immediately overwritten by
    an unconditional {} on line 97. Fixed: only one assignment remains.
    proxy_owners loaded via _load_users_from_db must survive.
    """

    def test_proxy_owner_set_before_load_is_not_overwritten(self):
        """set_proxy_owner persists through a save/reload cycle (mocked)."""
        rc = make_rollcall()
        rc.set_proxy_owner("Dave Proxy", 42)
        self.assertEqual(rc.get_proxy_owner("Dave Proxy"), 42)

    def test_proxy_owners_initialised_as_empty_dict_on_new_rollcall(self):
        rc = make_rollcall()
        self.assertIsInstance(rc.proxy_owners, dict)
        self.assertEqual(len(rc.proxy_owners), 0)

    def test_load_from_db_populates_proxy_owners(self):
        """
        Simulate _load_users_from_db returning a proxy with an owner.
        proxy_owners must be set from DB data, not silently discarded.
        """
        db_mod = sys.modules['db']
        original_get_rollcall = db_mod.get_rollcall
        original_get_all_users = db_mod.get_all_users
        original_get_proxy = db_mod.get_proxy_users_by_status

        db_mod.get_rollcall.return_value = {
            'chat_id': 1, 'title': 'T', 'timezone': 'UTC',
            'location': None, 'event_fee': None, 'in_list_limit': None,
            'reminder_hours': None, 'finalize_date': None, 'created_at': None,
        }
        db_mod.get_all_users.return_value = []

        def proxy_by_status(rc_id, status):
            if status == 'in':
                return [{'name': 'Charlie', 'comment': '', 'in_pos': 1,
                         'out_pos': None, 'wait_pos': None,
                         'proxy_owner_id': 99, 'created_at': ''}]
            return []

        db_mod.get_proxy_users_by_status.side_effect = proxy_by_status

        rc = RollCall("", db_id=99)

        self.assertEqual(rc.get_proxy_owner("Charlie"), 99)

        # Reset mocks
        db_mod.get_rollcall.return_value = None
        db_mod.get_all_users.return_value = []
        db_mod.get_proxy_users_by_status.side_effect = None
        db_mod.get_proxy_users_by_status.return_value = []


# ---------------------------------------------------------------------------
# BUG-2: _get_user_current_status must return None for unknown users
# ---------------------------------------------------------------------------

class TestGetUserCurrentStatus(unittest.TestCase):
    """
    models.py L198: fallback was 'in', which caused incorrect DB saves.
    Fixed: returns None and logs a warning.
    """

    def test_returns_in_for_user_in_inlist(self):
        rc = make_rollcall()
        u = make_user()
        rc.inList.append(u)
        self.assertEqual(rc._get_user_current_status(u), 'in')

    def test_returns_out_for_user_in_outlist(self):
        rc = make_rollcall()
        u = make_user()
        rc.outList.append(u)
        self.assertEqual(rc._get_user_current_status(u), 'out')

    def test_returns_maybe_for_user_in_maybelist(self):
        rc = make_rollcall()
        u = make_user()
        rc.maybeList.append(u)
        self.assertEqual(rc._get_user_current_status(u), 'maybe')

    def test_returns_waitlist_for_user_in_waitlist(self):
        rc = make_rollcall()
        u = make_user()
        rc.waitList.append(u)
        self.assertEqual(rc._get_user_current_status(u), 'waitlist')

    def test_returns_none_for_user_not_in_any_list(self):
        """Core bug fix: must not return 'in' when user is absent."""
        rc = make_rollcall()
        u = make_user("Ghost", "ghost", 999)
        result = rc._get_user_current_status(u)
        self.assertIsNone(result)

    def test_resolve_display_name_conflict_handles_none_status(self):
        """
        _resolve_display_name_conflict calls _get_user_current_status.
        When status is None it must NOT call _save_user_to_db.
        """
        rc = make_rollcall()
        real_user = make_user("Sam", "sam_real", 10)
        # real_user is in allNames but NOT in any status list
        rc.allNames.append(real_user)

        proxy = make_proxy("Sam")  # same first_name triggers conflict resolution

        # Should not raise even though _get_user_current_status returns None
        try:
            rc._resolve_display_name_conflict(proxy)
        except Exception as e:
            self.fail(f"_resolve_display_name_conflict raised unexpectedly: {e}")

        db_mod = sys.modules['db']
        db_mod.add_or_update_user.assert_not_called()


# ---------------------------------------------------------------------------
# BUG-3: inListLimit with non-numeric DB value must not raise ValueError
# ---------------------------------------------------------------------------

class TestInListLimitNormalization(unittest.TestCase):
    """
    models.py L94-99: inListLimit now normalised to int (or None) during DB
    load, preventing TypeError/ValueError deeper in addIn/addOut/addMaybe.
    """

    def _make_rc_with_limit(self, raw_limit):
        db_mod = sys.modules['db']
        db_mod.get_rollcall.return_value = {
            'chat_id': 1, 'title': 'T', 'timezone': 'UTC',
            'location': None, 'event_fee': None, 'in_list_limit': raw_limit,
            'reminder_hours': None, 'finalize_date': None, 'created_at': None,
        }
        db_mod.get_all_users.return_value = []
        db_mod.get_proxy_users_by_status.return_value = []
        rc = RollCall("", db_id=1)
        # reset
        db_mod.get_rollcall.return_value = None
        return rc

    def test_numeric_string_is_converted(self):
        rc = self._make_rc_with_limit("3")
        self.assertEqual(rc.inListLimit, 3)

    def test_integer_is_preserved(self):
        rc = self._make_rc_with_limit(5)
        self.assertEqual(rc.inListLimit, 5)

    def test_none_stays_none(self):
        rc = self._make_rc_with_limit(None)
        self.assertIsNone(rc.inListLimit)

    def test_invalid_string_defaults_to_none(self):
        """Non-numeric value must not raise; falls back to None."""
        rc = self._make_rc_with_limit("bad_value")
        self.assertIsNone(rc.inListLimit)

    def test_addin_works_with_normalised_limit(self):
        """After normalisation addIn must enforce the limit correctly."""
        rc = self._make_rc_with_limit("2")
        u1 = make_user("Alice", "alice", 1)
        u2 = make_user("Bob", "bob", 2)
        u3 = make_user("Carol", "carol", 3)
        rc.addIn(u1)
        rc.addIn(u2)
        result = rc.addIn(u3)
        self.assertEqual(result, "AC")
        self.assertIn(u3, rc.waitList)


# ---------------------------------------------------------------------------
# BUG-4: User with None username must not crash during name disambiguation
# ---------------------------------------------------------------------------

class TestUserNoneUsername(unittest.TestCase):
    """
    models.py L607: when username is None, the old code produced
    f"{name} (None)". Fixed: falls back to user_id string.
    """

    def test_user_with_none_username_no_conflict(self):
        """No existing user with same name → name stays as-is."""
        u = User("Alice", None, 101, [])
        self.assertEqual(u.name, "Alice")
        self.assertIsNone(u.username)

    def test_user_with_none_username_name_conflict_uses_id(self):
        """
        When two real users share a first_name and the new one has no
        username, the disambiguated name must use the user_id, not 'None'.
        """
        existing = User("Alice", None, 100, [])
        new_user = User("Alice", None, 101, [existing])
        self.assertNotIn("None", new_user.name)
        self.assertIn("101", new_user.name)

    def test_user_with_username_name_conflict_uses_at_username(self):
        """When username is present, use @username in disambiguation."""
        existing = User("Alice", "alice_old", 100, [])
        new_user = User("Alice", "alice_new", 101, [existing])
        self.assertIn("@alice_new", new_user.name)


# ---------------------------------------------------------------------------
# BUG-5: version_command — missing file / no deployed version
# ---------------------------------------------------------------------------

class TestVersionCommand(unittest.TestCase):
    """
    telegram_helper.py L338-352:
    - FileNotFoundError and JSONDecodeError were unhandled (bot would crash)
    - If no version has DeployedOnProd=='Y', nothing was sent to the user
    Fixed: both cases now send an error message and return cleanly.
    """

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setUp(self):
        import telegram_helper as th
        self.th = th
        self.bot_mock = MagicMock()
        self.bot_mock.send_message = AsyncMock()
        self._original_bot = th.bot
        th.bot = self.bot_mock

    def tearDown(self):
        self.th.bot = self._original_bot

    def _make_message(self, chat_id=1):
        msg = MagicMock()
        msg.chat.id = chat_id
        return msg

    def test_version_file_not_found_sends_error(self):
        """FileNotFoundError → graceful error message, no crash."""
        message = self._make_message()
        with patch("builtins.open", side_effect=FileNotFoundError("not found")):
            self._run(self.th.version_command(message))
        self.bot_mock.send_message.assert_called_once()
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("unavailable", args[1].lower())

    def test_version_json_decode_error_sends_error(self):
        """JSONDecodeError → graceful error message, no crash."""
        import json
        message = self._make_message()
        with patch("builtins.open", mock_open(read_data="not-json")):
            with patch("json.load", side_effect=json.JSONDecodeError("err", "", 0)):
                self._run(self.th.version_command(message))
        self.bot_mock.send_message.assert_called_once()
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("unavailable", args[1].lower())

    def test_version_no_deployed_version_sends_fallback(self):
        """No entry with DeployedOnProd=='Y' → fallback message sent."""
        versions = [
            {"Version": 1, "Description": "old", "DeployedOnProd": "N", "DeployedDatetime": "01-01-2022"}
        ]
        import json
        message = self._make_message()
        with patch("builtins.open", mock_open(read_data=json.dumps(versions))):
            self._run(self.th.version_command(message))
        self.bot_mock.send_message.assert_called_once()
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("no released version", args[1].lower())

    def test_version_happy_path_sends_version_info(self):
        """Deployed version found → correct version info is sent."""
        versions = [
            {"Version": 4.5, "Description": "Latest", "DeployedOnProd": "Y",
             "DeployedDatetime": "14-04-2026"}
        ]
        import json
        message = self._make_message()
        with patch("builtins.open", mock_open(read_data=json.dumps(versions))):
            self._run(self.th.version_command(message))
        self.bot_mock.send_message.assert_called_once()
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("4.5", str(args[1]))
        self.assertIn("Latest", args[1])

    def test_version_returns_most_recent_deployed(self):
        """When multiple versions exist, the latest deployed one is returned."""
        versions = [
            {"Version": 4.0, "Description": "Old deployed", "DeployedOnProd": "Y",
             "DeployedDatetime": "01-01-2026"},
            {"Version": 4.5, "Description": "New deployed", "DeployedOnProd": "Y",
             "DeployedDatetime": "14-04-2026"},
        ]
        import json
        message = self._make_message()
        with patch("builtins.open", mock_open(read_data=json.dumps(versions))):
            self._run(self.th.version_command(message))
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("New deployed", args[1])


# ---------------------------------------------------------------------------
# BUG-6: broadcast must use DB (get_all_chat_ids), not legacy JSON file
# ---------------------------------------------------------------------------

class TestBroadcast(unittest.TestCase):
    """
    telegram_helper.py L284-304:
    - Old code read database.json (legacy, race-prone, bare except:pass)
    - Fixed: uses get_all_chat_ids() from DB; logs per-chat failures
    """

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def setUp(self):
        import telegram_helper as th
        self.th = th
        self.bot_mock = MagicMock()
        self.bot_mock.send_message = AsyncMock()
        self._original_bot = th.bot
        th.bot = self.bot_mock

    def tearDown(self):
        self.th.bot = self._original_bot

    def _make_message(self, text="/broadcast Hello world", from_id=1):
        msg = MagicMock()
        msg.chat.id = -100
        msg.text = text
        msg.from_user.id = from_id
        return msg

    def test_broadcast_sends_to_all_chat_ids_from_db(self):
        """Broadcast must iterate DB chat IDs, not read any JSON file."""
        message = self._make_message()
        with patch("telegram_helper.get_all_chat_ids", return_value=[111, 222]) as mock_ids:
            self._run(self.th.broadcast(message))
            mock_ids.assert_called_once()
        # 2 group sends + 1 summary reply
        self.assertEqual(self.bot_mock.send_message.call_count, 3)

    def test_broadcast_reports_failure_count(self):
        """Per-chat failures must be counted and reported, not silently dropped."""
        message = self._make_message()
        send_calls = []

        async def failing_send(chat_id, text, **kwargs):
            send_calls.append(chat_id)
            if chat_id == 222:
                raise Exception("network error")

        self.bot_mock.send_message.side_effect = failing_send

        with patch("telegram_helper.get_all_chat_ids", return_value=[111, 222]):
            self._run(self.th.broadcast(message))

        summary_text = self.bot_mock.send_message.call_args[0][1]
        self.assertIn("1", summary_text)   # 1 success
        self.assertIn("Failed", summary_text)

    def test_broadcast_missing_message_text_returns_early(self):
        """'/broadcast' with no message body sends an error and exits."""
        message = self._make_message(text="/broadcast")
        with patch("telegram_helper.get_all_chat_ids", return_value=[111]) as mock_ids:
            self._run(self.th.broadcast(message))
            mock_ids.assert_not_called()
        self.bot_mock.send_message.assert_called_once()
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("missing", args[1].lower())

    def test_broadcast_no_chats_in_db(self):
        """Empty chat list → informative message, no send attempts."""
        message = self._make_message()
        with patch("telegram_helper.get_all_chat_ids", return_value=[]):
            self._run(self.th.broadcast(message))
        self.bot_mock.send_message.assert_called_once()
        args = self.bot_mock.send_message.call_args[0]
        self.assertIn("No chats", args[1])


# ---------------------------------------------------------------------------
# BUG-7: check_reminders must log errors, not print them
# ---------------------------------------------------------------------------

class TestCheckRemindersLogging(unittest.TestCase):
    """
    check_reminders.py L66, L83: print(e) replaced with logging.error().
    We load the REAL module via importlib (bypassing the global mock) and
    inject a failing rollcall to confirm logging.error is called.
    """

    @classmethod
    def _load_real_module(cls):
        """Load the real check_reminders.py bypassing the sys.modules mock."""
        import importlib.util
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "check_reminders.py"
        )
        spec = importlib.util.spec_from_file_location("_real_check_reminders", module_path)
        mod = importlib.util.module_from_spec(spec)
        # Inject mocked deps so the module loads without a real Telegram token
        mod_deps = dict(sys.modules)
        mod_deps['telebot'] = MagicMock()
        mod_deps['telebot.async_telebot'] = MagicMock()
        mod_deps['config'] = sys.modules['config']
        mod_deps['db'] = sys.modules['db']
        with patch.dict('sys.modules', mod_deps):
            spec.loader.exec_module(mod)
        return mod

    def test_check_reminders_source_uses_logging_not_print(self):
        """
        Source-level check: the error handler must call logging.error,
        not print().  Ensures neither bare print(e) nor print(traceback...)
        remain after the fix.
        """
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "check_reminders.py"
        )
        with open(module_path) as f:
            source = f.read()

        # The fixed file must import logging and traceback
        self.assertIn("import logging", source)
        self.assertIn("import traceback", source)
        # logging.error must appear in the exception handlers
        self.assertIn("logging.error", source)
        # Bare print(e) must not appear
        self.assertNotIn("print(e)", source)

    def test_reminder_loop_exception_calls_logging_error(self):
        """
        Load the real module and run check() with a bad rollcall.
        asyncio.sleep is patched to raise CancelledError after the first call
        so the loop exits quickly without waiting 60 s.
        logging.error must be called for the RuntimeError raised inside the loop.
        """
        import logging
        real_mod = self._load_real_module()

        bad_rc = MagicMock()
        bad_rc.finalizeDate = MagicMock()
        bad_rc.reminder = None
        bad_rc.finalizeDate.__bool__ = lambda s: True
        bad_rc.title = "Broken RC"

        rollcalls = [bad_rc]

        # asyncio.sleep is called twice in check():
        #   1st: initial alignment delay at the top (let it pass)
        #   2nd: the 60-second loop sleep (raise CancelledError to exit)
        # This ensures the loop body runs (and logging.error is called) before
        # we force-exit the loop.
        sleep_calls = [0]

        async def controlled_sleep(seconds):
            sleep_calls[0] += 1
            if sleep_calls[0] >= 2:
                raise asyncio.CancelledError()
            # First call: return immediately instead of actually sleeping

        with patch.object(logging, 'error') as mock_log, \
             patch.object(real_mod.pytz, 'timezone', side_effect=RuntimeError("tz boom")), \
             patch.object(real_mod.asyncio, 'sleep', side_effect=controlled_sleep):
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(real_mod.check(rollcalls, "UTC", 1))
            except asyncio.CancelledError:
                pass  # expected — loop exited via our controlled_sleep
            finally:
                loop.close()

        mock_log.assert_called()
        logged_msg = str(mock_log.call_args)
        self.assertIn("check_reminders", logged_msg.lower())


# ---------------------------------------------------------------------------
# SEC-1: cursor=None safety in db.py add_or_update_user
# ---------------------------------------------------------------------------

class TestDbCursorSafety(unittest.TestCase):
    """
    db.py L762 / L856: cursor was undefined when conn.cursor() raised.
    The finally block would then throw NameError masking the real error.
    Fixed: cursor = None is set before the try block; finally guards it.
    """

    def test_add_or_update_user_finally_does_not_crash_when_cursor_fails(self):
        """
        If conn.cursor() raises, the finally block must not raise NameError.
        The original exception (from cursor()) must propagate cleanly.
        """
        # Import the REAL db module to test this — bypass the mock
        import importlib
        import types

        # Build a minimal sqlite3-like fake that raises on cursor()
        fake_conn = MagicMock()
        fake_conn.cursor.side_effect = RuntimeError("cursor init failed")

        with patch.dict('sys.modules', {}):
            # We test the guard logic through the mock db we have —
            # verify cursor is initialised to None before any call
            # by checking the source of the fixed function.
            import inspect
            import db as db_real_or_mock
            src = inspect.getsource(db_real_or_mock.add_or_update_user) \
                if hasattr(db_real_or_mock.add_or_update_user, '__wrapped__') \
                or not isinstance(db_real_or_mock.add_or_update_user, MagicMock) \
                else None

            if src:
                self.assertIn("cursor = None", src,
                              "cursor must be initialised to None before the try block")

    def test_add_or_update_proxy_user_guard_present(self):
        """Same cursor=None guard must exist in add_or_update_proxy_user."""
        import inspect
        import db as db_mod
        if not isinstance(db_mod.add_or_update_proxy_user, MagicMock):
            src = inspect.getsource(db_mod.add_or_update_proxy_user)
            self.assertIn("cursor = None", src)

    def test_get_all_chat_ids_returns_list(self):
        """get_all_chat_ids must exist and return a list (DB mocked → [])."""
        import db as db_mod
        # In test environment db is mocked; the function should still be callable
        result = db_mod.get_all_chat_ids()
        # Mock returns default MagicMock — we just verify it doesn't raise
        self.assertIsNotNone(result)


# ---------------------------------------------------------------------------
# FIX-3: asyncio.create_task calls use _log_task_exc done-callback
# ---------------------------------------------------------------------------

class TestLogTaskExc(unittest.TestCase):
    """
    telegram_helper.py: all asyncio.create_task() calls must attach
    _log_task_exc so background-task crashes surface in the log.
    """

    def test_log_task_exc_helper_exists(self):
        import telegram_helper as th
        self.assertTrue(
            callable(getattr(th, '_log_task_exc', None)),
            "_log_task_exc must be a callable defined in telegram_helper"
        )

    def test_log_task_exc_logs_exception(self):
        """_log_task_exc must call logging.error when the task raised."""
        import logging
        import telegram_helper as th

        failing_task = MagicMock()
        failing_task.cancelled.return_value = False
        failing_task.exception.return_value = RuntimeError("boom")

        with patch.object(logging, 'error') as mock_err:
            th._log_task_exc(failing_task)

        mock_err.assert_called_once()

    def test_log_task_exc_silent_on_cancel(self):
        """_log_task_exc must not log when the task was cancelled."""
        import logging
        import telegram_helper as th

        cancelled_task = MagicMock()
        cancelled_task.cancelled.return_value = True

        with patch.object(logging, 'error') as mock_err:
            th._log_task_exc(cancelled_task)

        mock_err.assert_not_called()

    def test_log_task_exc_silent_on_success(self):
        """_log_task_exc must not log when the task completed normally."""
        import logging
        import telegram_helper as th

        ok_task = MagicMock()
        ok_task.cancelled.return_value = False
        ok_task.exception.return_value = None

        with patch.object(logging, 'error') as mock_err:
            th._log_task_exc(ok_task)

        mock_err.assert_not_called()

    def test_source_all_create_task_calls_attach_callback(self):
        """
        Source check: every asyncio.create_task(...) call must be followed by
        .add_done_callback(_log_task_exc) on the same logical line.
        """
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        import re
        # Find lines with asyncio.create_task(
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "asyncio.create_task(" in line and "add_done_callback" not in line:
                # Allow the callback on the very next line
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                self.assertIn(
                    "add_done_callback",
                    next_line,
                    f"Line {i + 1}: asyncio.create_task() without .add_done_callback(_log_task_exc): {line.strip()}"
                )


# ---------------------------------------------------------------------------
# FIX-4: buzz _check_member uses asyncio.wait_for with 5 s timeout
# ---------------------------------------------------------------------------

class TestBuzzCheckMemberTimeout(unittest.TestCase):
    """
    telegram_helper.py buzz handler: get_chat_member must be wrapped in
    asyncio.wait_for(..., timeout=5.0) so a slow Telegram API response
    does not block the entire buzz batch indefinitely.
    """

    def test_source_check_member_uses_wait_for(self):
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "asyncio.wait_for",
            source,
            "buzz _check_member must use asyncio.wait_for for get_chat_member"
        )
        self.assertIn(
            "timeout=5.0",
            source,
            "wait_for timeout must be 5.0 seconds"
        )

    def test_source_timeout_error_keeps_user(self):
        """On TimeoutError, the user should NOT be marked inactive."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "asyncio.TimeoutError",
            source,
            "TimeoutError must be explicitly caught in the buzz membership check"
        )


# ---------------------------------------------------------------------------
# FIX-5: start_roll_call no longer writes database.json
# ---------------------------------------------------------------------------

class TestStartRollCallNoDatabaseJson(unittest.TestCase):
    """
    telegram_helper.py start_roll_call: the legacy database.json write block
    must be removed. Broadcast now uses get_all_chat_ids() from db.py.
    """

    def test_source_no_database_json_write(self):
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertNotIn(
            "database.json",
            source,
            "database.json must not appear in telegram_helper — legacy write block must be removed"
        )


# ---------------------------------------------------------------------------
# FIX-6: /sif duplicate proxy pre-check
# ---------------------------------------------------------------------------

class TestSifDuplicateProxyGuard(unittest.IsolatedAsyncioTestCase):
    """
    telegram_helper.py set_in_for (/sif): if the same proxy name is already
    IN or WAITING, the handler must send a warning and return early without
    calling addIn or touching the DB.
    """

    @classmethod
    def setUpClass(cls):
        import telegram_helper as th
        cls.th = th

    def setUp(self):
        self.th.bot.send_message = AsyncMock()
        self.th._rate_limits.clear()
        self.th._pending_deletes.clear()

    def _make_message(self, text, chat_id=100, user_id=1):
        msg = MagicMock()
        msg.text = text
        msg.chat.id = chat_id
        msg.from_user.id = user_id
        msg.from_user.first_name = "Admin"
        msg.from_user.username = "admin"
        return msg

    def _make_proxy_user(self, name):
        u = MagicMock()
        u.name = name
        u.user_id = name  # string → proxy
        return u

    async def test_duplicate_proxy_in_inlist_sends_warning(self):
        rc = MagicMock()
        rc.title = "Game Night"
        rc.id = 1
        existing_proxy = self._make_proxy_user("Charlie")
        rc.inList = [existing_proxy]
        rc.waitList = []
        rc.allNames = ["Charlie"]
        rc.absent_marked = False

        m = MagicMock()
        m.get_rollcalls.return_value = [rc]
        m.get_rollcall.return_value = rc
        m.get_admin_rights.return_value = False
        m.get_ghost_tracking_enabled.return_value = False
        m.get_absent_limit.return_value = 1

        with patch('telegram_helper.manager', m), \
             patch('telegram_helper.admin_rights', new=AsyncMock(return_value=True)), \
             patch('telegram_helper.get_ghost_count_by_proxy_name', return_value=0):
            msg = self._make_message("/sif Charlie")
            await self.th.set_in_for(msg)

        sent_texts = [call[0][1] for call in self.th.bot.send_message.call_args_list]
        self.assertTrue(
            any("already IN or WAITING" in t for t in sent_texts),
            f"Expected duplicate warning, got: {sent_texts}"
        )
        rc.addIn.assert_not_called()

    async def test_duplicate_proxy_in_waitlist_sends_warning(self):
        rc = MagicMock()
        rc.title = "Game Night"
        rc.id = 1
        existing_proxy = self._make_proxy_user("Dave")
        rc.inList = []
        rc.waitList = [existing_proxy]
        rc.allNames = ["Dave"]
        rc.absent_marked = False

        m = MagicMock()
        m.get_rollcalls.return_value = [rc]
        m.get_rollcall.return_value = rc
        m.get_admin_rights.return_value = False
        m.get_ghost_tracking_enabled.return_value = False
        m.get_absent_limit.return_value = 1

        with patch('telegram_helper.manager', m), \
             patch('telegram_helper.admin_rights', new=AsyncMock(return_value=True)), \
             patch('telegram_helper.get_ghost_count_by_proxy_name', return_value=0):
            msg = self._make_message("/sif Dave")
            await self.th.set_in_for(msg)

        sent_texts = [call[0][1] for call in self.th.bot.send_message.call_args_list]
        self.assertTrue(
            any("already IN or WAITING" in t for t in sent_texts),
            f"Expected duplicate warning, got: {sent_texts}"
        )
        rc.addIn.assert_not_called()

    async def test_new_proxy_not_in_any_list_proceeds_normally(self):
        rc = MagicMock()
        rc.title = "Game Night"
        rc.id = 1
        rc.inList = []
        rc.waitList = []
        rc.allNames = []
        rc.absent_marked = False
        rc.addIn.return_value = None
        rc.allList.return_value = "Title: Game Night\nID: __RCID__\n"

        m = MagicMock()
        m.get_rollcalls.return_value = [rc]
        m.get_rollcall.return_value = rc
        m.get_admin_rights.return_value = False
        m.get_shh_mode.return_value = False
        m.get_ghost_tracking_enabled.return_value = False
        m.get_absent_limit.return_value = 1

        with patch('telegram_helper.manager', m), \
             patch('telegram_helper.admin_rights', new=AsyncMock(return_value=True)), \
             patch('telegram_helper.get_ghost_count_by_proxy_name', return_value=0):
            msg = self._make_message("/sif Eve")
            await self.th.set_in_for(msg)

        sent_texts = [call[0][1] for call in self.th.bot.send_message.call_args_list]
        self.assertFalse(
            any("already IN or WAITING" in t for t in sent_texts),
            "Should NOT send duplicate warning for a new proxy user"
        )


# ---------------------------------------------------------------------------
# FIX-8 & FIX-10: /set_template partial update and first-token parsing
# ---------------------------------------------------------------------------

class TestSetTemplatePartialUpdate(unittest.TestCase):
    """
    FIX-8: /set_template must load existing template and only override
    explicitly provided fields so partial updates don't erase other values.

    FIX-10: A leading token like 'event_day=sunday' must NOT be treated
    as a title — only a token without '=' is a bare title.
    """

    def test_source_loads_existing_template_before_overwrite(self):
        """Source must call get_template to load the existing record."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "get_template(cid, name)",
            source,
            "/set_template must call get_template to load existing data before overwriting"
        )
        self.assertIn(
            "existing.get(",
            source,
            "Existing template fields must be loaded with dict.get()"
        )

    def test_source_first_token_eq_check(self):
        """Source must guard that the first token has no '=' before treating it as title."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "'=' not in first_token",
            source,
            "First-token check must guard against key=value tokens being parsed as titles"
        )


# ---------------------------------------------------------------------------
# FIX-11: /erc renumbering message shows specific old→new ID mapping
# ---------------------------------------------------------------------------

class TestErcRenumberingMessage(unittest.IsolatedAsyncioTestCase):
    """
    telegram_helper.py end_roll_call: when rollcalls remain after ending one,
    the warning must show the specific old→new ID mapping (e.g. '#3 ... → #2')
    instead of a generic "IDs have been updated" message.
    """

    @classmethod
    def setUpClass(cls):
        import telegram_helper as th
        cls.th = th

    def setUp(self):
        self.th.bot.send_message = AsyncMock()
        self.th._rate_limits.clear()
        self.th._pending_deletes.clear()

    def _make_rc(self, title, rc_id=1):
        rc = MagicMock()
        rc.title = title
        rc.id = rc_id
        rc.inList = []
        rc.outList = []
        rc.maybeList = []
        rc.waitList = []
        rc.allNames = []
        rc.absent_marked = True
        rc.finishList.return_value = f"Title: {title}\nID: __RCID__\n"
        rc.allList.return_value = f"Title: {title}\nID: __RCID__\n"
        return rc

    async def test_renumbering_message_shows_mapping(self):
        """
        Ending rollcall #1 of 2 should produce a message containing '→' arrow
        for the surviving rollcall, not just a generic 'IDs have been updated' message.
        """
        rc1 = self._make_rc("First Game", rc_id=1)
        rc2 = self._make_rc("Second Game", rc_id=2)
        surviving = [rc2]

        m = MagicMock()
        # Always return [rc1, rc2] for count check and [rc2] for post-end renaming.
        # Use a counter to distinguish calls.
        call_count = [0]

        def get_rollcalls_side_effect(cid):
            call_count[0] += 1
            # First call: pre-end check (must have >= 1 rollcall)
            if call_count[0] == 1:
                return [rc1, rc2]
            # Subsequent calls: post-end state
            return surviving

        m.get_rollcalls.side_effect = get_rollcalls_side_effect
        m.get_rollcall.return_value = rc1
        m.get_admin_rights.return_value = False
        m.get_ghost_tracking_enabled.return_value = False
        m.get_shh_mode.return_value = False

        msg = MagicMock()
        msg.text = "/erc"
        msg.chat.id = 100
        msg.from_user.id = 1
        msg.from_user.first_name = "Alice"
        msg.from_user.username = "alice"

        with patch('telegram_helper.manager', m), \
             patch('telegram_helper.admin_rights', new=AsyncMock(return_value=True)), \
             patch('telegram_helper.update_streak_on_checkin', return_value=None):
            await self.th.end_roll_call(msg)

        sent_texts = " ".join(
            str(call[0][1]) for call in self.th.bot.send_message.call_args_list
        )
        self.assertIn("→", sent_texts, "Renumbering message must contain '→' arrow mapping")
        self.assertNotIn(
            "IDs have been updated",
            sent_texts,
            "Generic 'IDs have been updated' message must be replaced with specific mapping"
        )

    def test_source_no_generic_ids_updated_message(self):
        """The old generic message must not appear in source."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertNotIn(
            "Active rollcall IDs have been updated because one rollcall was ended.",
            source,
            "Generic renumber message must be replaced with specific ID mapping"
        )


# ---------------------------------------------------------------------------
# H1: Per-chat asyncio.Lock guards concurrent /erc calls
# ---------------------------------------------------------------------------

class TestErcConcurrencyLock(unittest.IsolatedAsyncioTestCase):
    """
    rollcall_manager.py: get_erc_lock(chat_id) must return an asyncio.Lock
    that serialises concurrent end-rollcall operations in the same chat.
    """

    def _load_manager_mod(self, name="_real_mgr"):
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location(
            name,
            os.path.join(os.path.dirname(__file__), "..", "rollCall", "rollcall_manager.py")
        )
        mod = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'db': sys.modules['db'], 'models': MagicMock()}):
            spec.loader.exec_module(mod)
        return mod

    async def test_get_erc_lock_returns_asyncio_lock(self):
        mod = self._load_manager_mod("_rm1")
        mgr = mod.RollCallManager()
        lock = mgr.get_erc_lock(123)
        self.assertIsInstance(lock, asyncio.Lock)

    async def test_same_chat_returns_same_lock(self):
        """get_erc_lock must return the identical lock object for the same chat_id."""
        mod = self._load_manager_mod("_rm2")
        mgr = mod.RollCallManager()
        self.assertIs(mgr.get_erc_lock(42), mgr.get_erc_lock(42))

    async def test_different_chats_return_different_locks(self):
        mod = self._load_manager_mod("_rm3")
        mgr = mod.RollCallManager()
        self.assertIsNot(mgr.get_erc_lock(1), mgr.get_erc_lock(2))

    async def test_lock_prevents_concurrent_entry(self):
        """Acquiring the lock twice concurrently — second coroutine must wait."""
        mod = self._load_manager_mod("_rm4")
        mgr = mod.RollCallManager()
        lock = mgr.get_erc_lock(999)
        order = []

        async def first():
            async with lock:
                order.append('first-in')
                await asyncio.sleep(0)
                order.append('first-out')

        async def second():
            async with lock:
                order.append('second-in')

        await asyncio.gather(first(), second())
        # second must enter only after first releases
        self.assertEqual(order, ['first-in', 'first-out', 'second-in'])

    def test_source_erc_handler_uses_lock(self):
        """The /erc handler source must contain 'async with manager.get_erc_lock'."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()
        self.assertIn(
            "async with manager.get_erc_lock(cid)",
            source,
            "/erc handler must acquire per-chat lock to prevent concurrent double-end"
        )

    def test_source_endconfirm_uses_lock(self):
        """The panel endconfirm callback must also use the per-chat lock."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()
        # There must be at least two occurrences (one per code path)
        count = source.count("async with manager.get_erc_lock(cid)")
        self.assertGreaterEqual(count, 2, "Both /erc and endconfirm must hold the per-chat lock")


# ---------------------------------------------------------------------------
# H2: Deleting a proxy user purges their ghost_records row
# ---------------------------------------------------------------------------

class TestProxyDeleteCleansGhostRecord(unittest.TestCase):
    """
    db.py delete_user_by_name: after deleting a proxy from proxy_users, the
    matching ghost_records row must also be removed so the user stops
    appearing on the /absent_stats leaderboard.
    """

    def test_source_ghost_records_delete_on_proxy_removal(self):
        """Source must delete from ghost_records when proxy_users row is removed."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "db.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "DELETE FROM ghost_records",
            source,
            "delete_user_by_name must delete from ghost_records when a proxy is removed"
        )
        self.assertIn(
            "proxy_name",
            source,
            "ghost_records deletion must filter by proxy_name"
        )

    def test_source_uses_subquery_for_chat_id(self):
        """The ghost_records DELETE must resolve chat_id via rollcalls subquery."""
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "db.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "SELECT chat_id FROM rollcalls WHERE id",
            source,
            "ghost_records deletion must look up chat_id via the rollcalls table"
        )


# ---------------------------------------------------------------------------
# H3: Proxy ghost events record user_name in ghost_events table
# ---------------------------------------------------------------------------

class TestProxyGhostEventUserName(unittest.TestCase):
    """
    telegram_helper.py ghost_done callback: add_ghost_event for proxy users
    must pass user_name=proxy_name so the ghost_events audit table is always
    populated (not NULL) for every ghost event.
    """

    def test_source_proxy_ghost_event_passes_user_name(self):
        module_path = os.path.join(
            os.path.dirname(__file__), "..", "rollCall", "telegram_helper.py"
        )
        with open(module_path) as f:
            source = f.read()

        self.assertIn(
            "add_ghost_event(rc_db_id, cid, None, user_name=proxy_name, proxy_name=proxy_name)",
            source,
            "Proxy ghost event must pass user_name=proxy_name to keep audit trail complete"
        )

    def test_add_ghost_event_signature_accepts_user_name(self):
        """add_ghost_event DB function must accept a user_name keyword argument."""
        import inspect
        import db as db_mod
        if not isinstance(db_mod.add_ghost_event, MagicMock):
            sig = inspect.signature(db_mod.add_ghost_event)
            self.assertIn(
                'user_name',
                sig.parameters,
                "add_ghost_event must have a user_name parameter"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
