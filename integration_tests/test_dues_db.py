"""
Dues & Treasury DB layer tests — real SQLite via the integration conftest.

Covers: append-only writers, balance aggregation across mixed member keys
(user_id vs name-keyed proxies), the game_closures UNIQUE double-close guard,
closeable-rollcall lookup, and the schema reconciler backfilling the new
dues columns onto an old-schema database.
"""

import os
import sqlite3
import tempfile
import time

import pytest

import db


# The integration DB file persists across pytest runs (fixed path in
# conftest), so chat ids must be unique per run for count/sum assertions.
CHAT = -(int(time.time() * 1000) % 10**12) - 10**14


def _mk_rollcall(chat_id=CHAT, title="Sunday Game", is_active=0, is_cancelled=0,
                 ended_at="2026-07-01T10:00:00Z"):
    """Insert a rollcall row directly and return its id."""
    db.get_or_create_chat(chat_id)  # PG enforces the rollcalls->chats FK; SQLite does not
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rollcalls (chat_id, title, is_active, is_cancelled, ended_at)"
        " VALUES (?, ?, ?, ?, ?)",
        # bool(): both columns are BOOLEAN on PG; SQLite stores them as 0/1
        (chat_id, title, bool(is_active), bool(is_cancelled), ended_at),
    )
    conn.commit()
    rc_id = cur.lastrowid
    cur.close()
    return rc_id


def _close(rc_id, chat_id=CHAT, **kw):
    args = dict(
        chat_id=chat_id, rollcall_id=rc_id, title="Sunday Game",
        ground_cost=600, in_count=7, subsidy=0, per_head=90,
        rounding_step=10, remainder=30, closed_by_uid=1, closed_by_name="Admin",
    )
    args.update(kw)
    return db.create_game_closure(**args)


# ── game_closures ────────────────────────────────────────────────────────────

def test_create_and_get_game_closure():
    rc_id = _mk_rollcall()
    closure_id = _close(rc_id)
    assert closure_id > 0

    row = db.get_game_closure(rc_id)
    assert row is not None
    assert row["ground_cost"] == 600
    assert row["per_head"] == 90
    assert row["remainder"] == 30
    assert row["collector_uid"] is None


def test_double_close_raises():
    rc_id = _mk_rollcall()
    _close(rc_id)
    with pytest.raises(Exception):
        _close(rc_id)  # UNIQUE(rollcall_id) violated


def test_write_game_closure_batch_is_atomic():
    """A failure partway through the batch (here: a dues_entries row that
    violates member_name's NOT NULL constraint) must roll back EVERYTHING,
    including the closure row already inserted earlier in the same
    transaction — a game must never end up "closed" with missing dues rows.
    """
    rc_id = _mk_rollcall(title="Atomic Test Game")
    closure = dict(
        chat_id=CHAT, rollcall_id=rc_id, title="Atomic Test Game",
        ground_cost=600, in_count=2, subsidy=0, per_head=300,
        rounding_step=10, remainder=0, closed_by_uid=1, closed_by_name="Admin",
    )
    dues_entries = [
        dict(chat_id=CHAT, rollcall_id=rc_id, user_id=101, member_name="Alice",
             entry_type="share", amount=300, memo=None,
             created_by_uid=1, created_by_name="Admin"),
        # Second row is invalid — member_name is NOT NULL. This must abort
        # the whole transaction, not just this one insert.
        dict(chat_id=CHAT, rollcall_id=rc_id, user_id=102, member_name=None,
             entry_type="share", amount=300, memo=None,
             created_by_uid=1, created_by_name="Admin"),
    ]

    with pytest.raises(Exception):
        db.write_game_closure_batch(closure, dues_entries, [])

    # Rollback proof: neither the closure row nor the first (valid) dues
    # entry may have survived.
    assert db.get_game_closure(rc_id) is None
    balance = db.get_dues_balance(CHAT, user_id=101)
    assert balance == 0


def test_update_game_closure_collector():
    rc_id = _mk_rollcall()
    _close(rc_id)
    assert db.update_game_closure_collector(rc_id, 42, "Ravi", collector_paid_ground=1)
    row = db.get_game_closure(rc_id)
    assert row["collector_uid"] == 42
    assert row["collector_name"] == "Ravi"
    assert row["collector_paid_ground"] == 1


def test_get_latest_game_closure():
    chat = CHAT - 1
    rc1 = _mk_rollcall(chat_id=chat, title="Game 1")
    rc2 = _mk_rollcall(chat_id=chat, title="Game 2")
    _close(rc1, chat_id=chat)
    _close(rc2, chat_id=chat, per_head=120)
    latest = db.get_latest_game_closure(chat)
    assert latest["rollcall_id"] == rc2
    assert latest["per_head"] == 120


def test_has_ever_been_collector_survives_newer_closure_by_someone_else():
    """A collector's standing shouldn't expire just because a later game
    closed with a different collector — has_ever_been_collector must check
    every closure for the chat, not just the latest one."""
    chat = CHAT - 2
    rc1 = _mk_rollcall(chat_id=chat, title="Game 1")
    rc2 = _mk_rollcall(chat_id=chat, title="Game 2")
    _close(rc1, chat_id=chat, collector_uid=42, collector_name="Ravi")
    _close(rc2, chat_id=chat, collector_uid=99, collector_name="Someone Else")

    assert db.has_ever_been_collector(chat, 42) is True
    assert db.has_ever_been_collector(chat, 99) is True
    assert db.has_ever_been_collector(chat, 12345) is False


# ── dues_entries ─────────────────────────────────────────────────────────────

def test_dues_balance_real_user():
    chat = CHAT - 2
    db.add_dues_entry(chat, None, 111, "Amit", "share", 90, None, 1, "Admin")
    db.add_dues_entry(chat, None, 111, "Amit", "penalty_late", 75, "late 20min", 1, "Admin")
    db.add_dues_entry(chat, None, 111, "Amit", "payment", -90, "received by Ravi", 1, "Admin")
    assert db.get_dues_balance(chat, user_id=111) == 75


def test_dues_balance_name_keyed_proxy():
    chat = CHAT - 3
    db.add_dues_entry(chat, None, None, "Guest Sanju", "share", 90, "unowned proxy", 1, "Admin")
    # Case-insensitive name key
    assert db.get_dues_balance(chat, member_name="guest sanju") == 90
    db.add_dues_entry(chat, None, None, "guest sanju", "payment", -90, None, 1, "Admin")
    assert db.get_dues_balance(chat, member_name="Guest Sanju") == 0


def test_all_dues_balances_mixed_keys():
    chat = CHAT - 4
    db.add_dues_entry(chat, None, 111, "Amit", "share", 90, None, 1, "Admin")
    db.add_dues_entry(chat, None, 222, "Ravi", "share", 90, None, 1, "Admin")
    db.add_dues_entry(chat, None, 222, "Ravi", "payment", -90, None, 1, "Admin")
    db.add_dues_entry(chat, None, None, "Guest", "share", 90, None, 1, "Admin")

    rows = db.get_all_dues_balances(chat)
    by_name = {r["member_name"]: r["balance"] for r in rows}
    assert by_name == {"Amit": 90, "Ravi": 0, "Guest": 90}

    nonzero = db.get_all_dues_balances(chat, nonzero_only=True)
    assert {r["member_name"] for r in nonzero} == {"Amit", "Guest"}


def test_dues_entries_pagination_and_rollcall_filter():
    chat = CHAT - 5
    rc_id = _mk_rollcall(chat_id=chat)
    for i in range(4):
        db.add_dues_entry(chat, rc_id, 111, "Amit", "share", 10 + i, None, 1, "Admin")

    assert db.count_dues_entries(chat) == 4
    page = db.get_dues_entries(chat, user_id=111, limit=2, offset=0)
    assert len(page) == 2
    assert page[0]["amount"] == 13  # newest first

    for_rc = db.get_dues_entries_for_rollcall(rc_id)
    assert len(for_rc) == 4
    assert for_rc[0]["amount"] == 10  # oldest first


# ── fund_transactions ────────────────────────────────────────────────────────

def test_fund_balance_and_history():
    chat = CHAT - 6
    db.add_fund_transaction(chat, None, "rounding", 30, None, 1, "Admin")
    db.add_fund_transaction(chat, None, "penalty", 75, "Amit late", 1, "Admin")
    db.add_fund_transaction(chat, None, "expense", -50, "new balls", 1, "Admin")
    assert db.get_fund_balance(chat) == 55
    assert db.count_fund_transactions(chat) == 3
    hist = db.get_fund_transactions(chat, limit=2)
    assert len(hist) == 2
    assert hist[0]["txn_type"] == "expense"  # newest first


# ── closeable rollcall lookup ────────────────────────────────────────────────

def test_latest_closeable_skips_active_cancelled_and_closed():
    chat = CHAT - 7
    _mk_rollcall(chat_id=chat, title="active", is_active=1)
    _mk_rollcall(chat_id=chat, title="cancelled", is_cancelled=1)
    closed = _mk_rollcall(chat_id=chat, title="already closed",
                          ended_at="2026-07-02T10:00:00Z")
    _close(closed, chat_id=chat)
    target = _mk_rollcall(chat_id=chat, title="closeable",
                          ended_at="2026-07-01T10:00:00Z")

    row = db.get_latest_closeable_rollcall(chat)
    assert row is not None
    assert row["id"] == target
    assert row["title"] == "closeable"


def test_latest_closeable_none_when_all_closed():
    chat = CHAT - 8
    rc_id = _mk_rollcall(chat_id=chat)
    _close(rc_id, chat_id=chat)
    assert db.get_latest_closeable_rollcall(chat) is None


# ── proxy owner in get_rollcall_in_users ─────────────────────────────────────

def test_get_rollcall_in_users_includes_proxy_owner():
    chat = CHAT - 9
    rc_id = _mk_rollcall(chat_id=chat)
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users (rollcall_id, user_id, first_name, username, status, in_pos)"
        " VALUES (?, ?, ?, ?, 'in', 1)",
        (rc_id, 111, "Amit", "amit_tg"),
    )
    cur.execute(
        "INSERT INTO proxy_users (rollcall_id, name, status, proxy_owner_id, in_pos)"
        " VALUES (?, 'Ravi friend', 'in', 222, 2)",
        (rc_id,),
    )
    cur.execute(
        "INSERT INTO proxy_users (rollcall_id, name, status, in_pos)"
        " VALUES (?, 'Walk-in Guest', 'in', 3)",
        (rc_id,),
    )
    conn.commit()
    cur.close()

    members = db.get_rollcall_in_users(rc_id)
    assert len(members) == 3
    real = [m for m in members if m["user_id"] is not None][0]
    assert real["first_name"] == "Amit"
    owned = [m for m in members if m.get("proxy_name") == "Ravi friend"][0]
    assert owned["proxy_owner_id"] == 222
    unowned = [m for m in members if m.get("proxy_name") == "Walk-in Guest"][0]
    assert unowned["proxy_owner_id"] is None


# ── schema reconciler backfills dues columns ─────────────────────────────────

# ── penalty_tiers ────────────────────────────────────────────────────────────

def test_upsert_and_get_penalty_tiers():
    chat = CHAT - 10
    db.upsert_penalty_tier(chat, "ditch", 200, "no-show")
    db.upsert_penalty_tier(chat, "late_short", 50, "under 15 min")
    tiers = db.get_penalty_tiers(chat)
    by_name = {t["name"]: t for t in tiers}
    assert "ditch" in by_name
    assert by_name["ditch"]["amount"] == 200
    assert "late_short" in by_name
    assert by_name["late_short"]["amount"] == 50


def test_upsert_updates_existing_tier():
    chat = CHAT - 11
    db.upsert_penalty_tier(chat, "ditch", 200, "no-show")
    db.upsert_penalty_tier(chat, "ditch", 250, "updated no-show")
    tier = db.get_penalty_tier(chat, "ditch")
    assert tier is not None
    assert tier["amount"] == 250


def test_get_penalty_tier_case_insensitive():
    chat = CHAT - 12
    db.upsert_penalty_tier(chat, "Late_Short", 50, None)
    assert db.get_penalty_tier(chat, "late_short") is not None
    assert db.get_penalty_tier(chat, "LATE_SHORT") is not None


def test_delete_penalty_tier():
    chat = CHAT - 13
    db.upsert_penalty_tier(chat, "ditch", 200, None)
    assert db.delete_penalty_tier(chat, "ditch") is True
    assert db.get_penalty_tier(chat, "ditch") is None
    assert db.delete_penalty_tier(chat, "ditch") is False  # already gone


def test_get_nth_game_closure():
    chat = CHAT - 14
    rc1 = _mk_rollcall(chat_id=chat, title="Game 1")
    rc2 = _mk_rollcall(chat_id=chat, title="Game 2")
    rc3 = _mk_rollcall(chat_id=chat, title="Game 3")
    _close(rc1, chat_id=chat, per_head=90)
    _close(rc2, chat_id=chat, per_head=100)
    _close(rc3, chat_id=chat, per_head=110)
    # n=0 → latest (Game 3)
    assert db.get_nth_game_closure(chat, 0)["per_head"] == 110
    # n=1 → second most recent (Game 2)
    assert db.get_nth_game_closure(chat, 1)["per_head"] == 100
    # n=2 → oldest (Game 1)
    assert db.get_nth_game_closure(chat, 2)["per_head"] == 90
    # n=3 → none
    assert db.get_nth_game_closure(chat, 3) is None


@pytest.mark.skipif(db.db_type != "sqlite",
                    reason="builds an old-schema DB with a raw sqlite3 connection")
def test_reconciler_adds_dues_columns_to_old_schema():
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chats (chat_id INTEGER PRIMARY KEY,
            timezone TEXT DEFAULT 'Asia/Kolkata');
        CREATE TABLE rollcalls (id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL, title TEXT);
        """
    )
    conn.commit()
    try:
        db._reconcile_columns(conn, conn.cursor())
        chat_cols = {r[1] for r in conn.execute("PRAGMA table_info(chats)")}
        assert {"upi_vpa", "dues_round_step", "penalty_late_t1", "penalty_late_t2",
                "penalty_late_t3", "penalty_ditch"} <= chat_cols
        rc_cols = {r[1] for r in conn.execute("PRAGMA table_info(rollcalls)")}
        assert {"collector_uid", "collector_name", "collector_paid_ground"} <= rc_cols
    finally:
        conn.close()
        os.unlink(path)
