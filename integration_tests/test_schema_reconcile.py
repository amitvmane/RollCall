"""
Regression: the schema reconciler backfills columns missing on databases
created by older builds (e.g. rollcalls.absent_marked, which had no migration
and caused "no such column: absent_marked" at runtime).

Runs against the real db module (integration conftest wires a real SQLite DB),
calling db._reconcile_columns on a hand-built old-schema connection.
"""

import os
import sqlite3
import tempfile

import db


def _old_schema_conn():
    """A DB resembling an early build: core tables minus later-added columns."""
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE rollcalls (id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, title TEXT);
        CREATE TABLE chats (chat_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'Asia/Kolkata');
        CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT,
            chatid INTEGER NOT NULL, name TEXT NOT NULL);
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            rollcall_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT);
        """
    )
    conn.commit()
    return path, conn


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_reconcile_backfills_missing_columns():
    assert db.db_type == "sqlite"
    path, conn = _old_schema_conn()
    try:
        db._reconcile_columns(conn, conn.cursor())

        rc = _cols(conn, "rollcalls")
        # the exact column from the bug report, plus the other CREATE-only ones
        assert "absent_marked" in rc
        assert {"panel_msg_id", "web_token", "is_cancelled", "in_list_limit",
                "location", "event_fee", "ended_at"} <= rc

        assert {"group_web_token", "group_name", "ghost_tracking_enabled",
                "absent_limit"} <= _cols(conn, "chats")
        assert {"recurrence_type", "schedule_enabled", "schedule_day"} <= _cols(conn, "templates")
        assert {"in_pos", "out_pos", "wait_pos"} <= _cols(conn, "users")
    finally:
        conn.close()
        os.unlink(path)


def test_reconcile_is_idempotent():
    """Running twice must not raise and must not duplicate columns."""
    path, conn = _old_schema_conn()
    try:
        db._reconcile_columns(conn, conn.cursor())
        first = _cols(conn, "rollcalls")
        db._reconcile_columns(conn, conn.cursor())  # no-op second pass
        assert _cols(conn, "rollcalls") == first
    finally:
        conn.close()
        os.unlink(path)
