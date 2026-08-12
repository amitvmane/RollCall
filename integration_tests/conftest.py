"""
Integration test conftest.
Uses a REAL SQLite database and the REAL handler functions.
Only mocks the Telegram bot API (no real network calls) and
aiohttp/check_reminders (no background loops in CI).
This file is NOT related to tests/conftest.py -- it lives in a sibling
directory so the unit-test mocks never interfere with these tests.
"""
import sys
import os
import tempfile
from unittest.mock import MagicMock, AsyncMock

# -- Path setup ---------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))  # helpers, conftest importable as top-level
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

# -- 1. telebot: mock the API layer, keep types semi-realistic ----------------
class _Markup:
    """Minimal InlineKeyboardMarkup stand-in."""
    def __init__(self, row_width=3):
        self.keyboard = []
    def add(self, *buttons):
        self.keyboard.extend(buttons)
    def row(self, *buttons):
        self.keyboard.extend(buttons)

class _Button:
    """Minimal InlineKeyboardButton stand-in."""
    def __init__(self, text="", callback_data="", url=None):
        self.text = text
        self.callback_data = callback_data

_telebot = MagicMock()
_async_telebot = MagicMock()
_telebot_types = MagicMock()
_telebot_types.InlineKeyboardMarkup = _Markup
_telebot_types.InlineKeyboardButton = _Button
_telebot.async_telebot = _async_telebot
_telebot.types = _telebot_types
sys.modules["telebot"] = _telebot
sys.modules["telebot.async_telebot"] = _async_telebot
sys.modules["telebot.types"] = _telebot_types

# -- 2. Import mock_bot from mock_helpers (single source of truth) ------------
# All tests import get_mock_bot from mock_helpers or conftest -- they must
# receive the SAME object. mock_helpers owns mock_bot; we wire it here.
from mock_helpers import mock_bot, get_mock_bot, reset_db  # noqa: F401

# Wire AsyncTeleBot() to return the shared mock_bot from mock_helpers
_async_telebot.AsyncTeleBot.return_value = mock_bot

# -- 3. config: real values, SQLite path resolved at import time --------------
# DATABASE_URL from the environment wins, so the same suite can be pointed at a
# real Postgres (e.g. postgresql://user:pass@127.0.0.1:5432/rollcall_test).
# Falls back to a temp-file SQLite db, which is what CI and local runs use.
_DB_FILE = os.path.join(tempfile.gettempdir(), "rollcall_integration.db")
_config = MagicMock()
_config.TELEGRAM_TOKEN = "test:token"
_config.ADMINS = [999]
_config.DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{_DB_FILE}"
_config.DEFAULT_ABSENT_LIMIT = 1
sys.modules["config"] = _config

# -- 4. Silence noisy deps ----------------------------------------------------
sys.modules["aiohttp"] = MagicMock()

# -- 5. Bootstrap the real database once --------------------------------------
import db as _db_module
_db_module.init_db()

# -- 5b. Postgres: hand the suite ONE shared connection -----------------------
# These tests were written against SQLite, where get_connection() returns a
# single long-lived connection and release_connection() is a no-op. ~30 of them
# call db.get_connection() directly and never release it, which drains the
# 5-slot PG pool within a handful of tests. Rather than rewrite every call site,
# mirror the SQLite contract here: one pooled connection for the whole session,
# release is a no-op. Production code is unaffected -- its three external
# call sites (runner.py, api/main.py, services/stats.py) already release.
if _db_module.db_type == "postgresql":
    import re as _re

    def _qmarks_to_pct(sql):
        """Rewrite SQLite '?' placeholders to psycopg2 '%s', ignoring any '?'
        inside a quoted SQL literal. Production code never needs this (it
        branches on db_type and already emits %s) -- this is only for the raw
        SQL that ~30 test call sites write inline."""
        if "?" not in sql:
            return sql
        out, quote = [], None
        for ch in sql:
            if quote:
                if ch == quote:
                    quote = None
                out.append(ch)
            elif ch in ("'", '"'):
                quote = ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
        return "".join(out)

    class _CursorProxy:
        """Cursor wrapper giving the SQLite-era tests portable behaviour:
        '?' placeholders, a working .lastrowid, and a rollback on failure so a
        bad statement cannot leave the shared connection in an aborted
        transaction that fails every later test."""

        _INSERT = _re.compile(r"^\s*INSERT\s", _re.I)

        def __init__(self, cur, conn):
            self._cur = cur
            self._conn = conn
            self._was_insert = False

        def execute(self, sql, params=None):
            self._was_insert = bool(self._INSERT.match(sql))
            try:
                return self._cur.execute(_qmarks_to_pct(sql), params)
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

        @property
        def lastrowid(self):
            # psycopg2 cursors have no lastrowid; lastval() is the session's
            # most recent sequence value, which is what these tests want.
            if not self._was_insert:
                return None
            try:
                self._cur.execute("SELECT lastval()")
                return self._cur.fetchone()[0]
            except Exception:
                self._conn.rollback()
                return None

        def __getattr__(self, name):
            return getattr(self._cur, name)

    class _ConnProxy:
        def __init__(self, conn):
            self._conn = conn

        def cursor(self, *a, **kw):
            return _CursorProxy(self._conn.cursor(*a, **kw), self._conn)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    _shared_pg_conn = _ConnProxy(_db_module.db_pool.getconn())

    def _get_shared_connection():
        return _shared_pg_conn

    def _release_noop(conn):  # noqa: ARG001 - signature must match
        return None

    _db_module.get_connection = _get_shared_connection
    _db_module.release_connection = _release_noop

# -- 6. Import check_reminders for real, patch out background loop ------------
import check_reminders as _cr_real
_cr_real.start = AsyncMock()
