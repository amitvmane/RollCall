"""The boot-time owner repair, on a fresh database and on a live one.

The repair (db._run_migrations, "DO NOT DELETE THIS AS A SPENT MIGRATION")
promotes each ownerless chat's earliest web-admin grant on every boot. On a
fresh database it used to run before web_admins existed, fail, and log a full
ERROR traceback — every first boot, and every boot under MEMORY_MODE. Now it is
skipped when the table is positively absent, and must still run otherwise.
"""
import logging
import sqlite3
from unittest.mock import MagicMock

import db


def _fresh():
    conn = sqlite3.connect(":memory:")
    return conn, conn.cursor()


def test_absent_table_is_reported_absent():
    _, cur = _fresh()
    assert db._table_definitely_absent(cur, "web_admins") is True


def test_present_table_is_reported_present():
    conn, cur = _fresh()
    cur.execute("CREATE TABLE web_admins (id INTEGER)")
    conn.commit()
    assert db._table_definitely_absent(cur, "web_admins") is False


def test_a_failed_probe_lets_the_repair_run():
    """The guard sits in front of a standing repair, so an unsure answer must
    be "might exist" — never a silent skip."""
    cur = MagicMock()
    cur.execute.side_effect = Exception("current transaction is aborted")
    assert db._table_definitely_absent(cur, "web_admins") is False


def test_fresh_database_boots_without_the_repair_error(caplog):
    conn, cur = _fresh()
    with caplog.at_level(logging.ERROR):
        db._run_migrations(conn, cur)
    assert not [r for r in caplog.records
                if "could not backfill web_admins.role" in r.getMessage()]
    # ...and the table the repair needs does exist once the boot finishes.
    assert db._table_definitely_absent(cur, "web_admins") is False


def test_the_repair_still_promotes_on_an_existing_table():
    conn, cur = _fresh()
    cur.execute("""CREATE TABLE web_admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
        tg_user_id INTEGER NOT NULL, tg_name TEXT, added_at TEXT,
        role TEXT DEFAULT 'admin', UNIQUE(chat_id, tg_user_id))""")
    cur.executemany(
        "INSERT INTO web_admins (chat_id, tg_user_id, tg_name) VALUES (?, ?, ?)",
        [(-100, 1, "first"), (-100, 2, "second")],
    )
    conn.commit()

    db._run_migrations(conn, cur)

    roles = dict(cur.execute(
        "SELECT tg_user_id, role FROM web_admins WHERE chat_id = -100").fetchall())
    assert roles == {1: "owner", 2: "admin"}
