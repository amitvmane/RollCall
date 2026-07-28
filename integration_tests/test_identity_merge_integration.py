"""
Identity merge — real-SQLite integration tests.

Merge-aware aggregation (get_dues_balance, get_all_dues_balances,
get_dues_entries, get_leaderboard_by_attendance, get_ghost_leaderboard)
lives in db.py itself, so it can't be meaningfully unit-tested against the
mocked `db` module the rest of tests/ uses — these exercise the real SQL +
Python-merge logic end to end, and that unmerge cleanly reverts it (nothing
is ever rewritten, only a link row added/removed — see services/identity.py).
"""

import time

import db
import services.identity as identity


CHAT = -(int(time.time() * 1000) % 10**12) - 10**14


def _mk_rollcall(chat_id, title="Game", is_active=0):
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rollcalls (chat_id, title, is_active) VALUES (?, ?, ?)",
        (chat_id, title, is_active),
    )
    conn.commit()
    rc_id = cur.lastrowid
    cur.close()
    return rc_id


def test_dues_balance_combines_after_merge_and_reverts_after_unmerge():
    chat = CHAT - 1
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 111, "Real", "realuser")

    db.add_dues_entry(chat, None, 111, "Real", "game_share", 100, None, 1, "Admin")
    db.add_dues_entry(chat, None, None, "Proxy1", "game_share", 50, None, 1, "Admin")

    assert db.get_dues_balance(chat, user_id=111) == 100
    assert db.get_dues_balance(chat, member_name="Proxy1") == 50

    identity.link_identities(chat, "Proxy1", canonical_user_id=111,
                              admin_user_id=1, admin_name="Admin")

    assert db.get_dues_balance(chat, user_id=111) == 150
    assert db.get_dues_balance(chat, member_name="Proxy1") == 150
    all_balances = db.get_all_dues_balances(chat)
    assert len(all_balances) == 1
    assert all_balances[0]["balance"] == 150

    identity.unmerge_identity(chat, "Proxy1", admin_user_id=1, admin_name="Admin")

    assert db.get_dues_balance(chat, user_id=111) == 100
    assert db.get_dues_balance(chat, member_name="Proxy1") == 50
    all_balances = db.get_all_dues_balances(chat)
    assert len(all_balances) == 2


def test_dues_entries_interleave_and_paginate_across_merged_group():
    chat = CHAT - 2
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 222, "Real2", "real2user")
    db.add_dues_entry(chat, None, 222, "Real2", "game_share", 10, None, 1, "Admin")
    db.add_dues_entry(chat, None, None, "P2", "game_share", 20, None, 1, "Admin")
    db.add_dues_entry(chat, None, None, "P2", "penalty", 5, "late", 1, "Admin")

    identity.link_identities(chat, "P2", canonical_user_id=222,
                              admin_user_id=1, admin_name="Admin")

    entries = db.get_dues_entries(chat, user_id=222, limit=15, offset=0)
    assert sorted(e["amount"] for e in entries) == [5, 10, 20]

    page1 = db.get_dues_entries(chat, user_id=222, limit=2, offset=0)
    page2 = db.get_dues_entries(chat, user_id=222, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 1


def test_nonzero_only_filters_a_merged_group_whose_net_balance_is_zero():
    chat = CHAT - 3
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 333, "Real3", "real3user")
    db.add_dues_entry(chat, None, 333, "Real3", "game_share", 90, None, 1, "Admin")
    db.add_dues_entry(chat, None, None, "P3", "game_share", 10, None, 1, "Admin")

    identity.link_identities(chat, "P3", canonical_user_id=333,
                              admin_user_id=1, admin_name="Admin")
    # Reversal against the real user's own identity nets the merged group to 0.
    db.add_dues_entry(chat, None, 333, "Real3", "adjustment", -100, None, 1, "Admin")

    nz = db.get_all_dues_balances(chat, nonzero_only=True)
    assert nz == []


def test_ghost_leaderboard_combines_after_merge():
    chat = CHAT - 4
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 444, "Real4", "real4user")
    db.increment_ghost_count(chat, 444, "Real4")
    db.increment_ghost_count(chat, -1, "P4", proxy_name="P4")
    db.increment_ghost_count(chat, -1, "P4", proxy_name="P4")

    assert db.get_ghost_count(chat, 444) == 1
    assert db.get_ghost_count_by_proxy_name(chat, "P4") == 2

    identity.link_identities(chat, "P4", canonical_user_id=444,
                              admin_user_id=1, admin_name="Admin")

    assert identity.combined_ghost_count(chat, user_id=444) == 3
    board = db.get_ghost_leaderboard(chat)
    matching = [r for r in board if r["user_id"] == 444]
    assert len(matching) == 1
    assert matching[0]["ghost_count"] == 3

    identity.unmerge_identity(chat, "P4", admin_user_id=1, admin_name="Admin")
    assert identity.combined_ghost_count(chat, user_id=444) == 1


def test_leaderboard_combines_proxy_into_real_user():
    chat = CHAT - 5
    db.get_or_create_chat(chat)
    rid = _mk_rollcall(chat)
    db.update_rollcall(rid, is_active=0)
    db.upsert_chat_member(chat, 555, "Real5", "real5user")
    db.add_or_update_user(rid, 555, "Real5", "real5user", "in")
    db.increment_user_stat(chat, 555, "total_in")
    db.increment_user_stat(chat, 555, "total_rollcalls")

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)",
        (rid, "P5", "in"),
    )
    conn.commit()
    cur.close()

    before = db.get_leaderboard_by_attendance(chat)
    assert len(before) == 2

    identity.link_identities(chat, "P5", canonical_user_id=555,
                              admin_user_id=1, admin_name="Admin")

    after = db.get_leaderboard_by_attendance(chat)
    matching = [r for r in after if r.get("user_id") == 555]
    assert len(matching) == 1
    assert matching[0]["attended"] == 2
    assert matching[0]["display_name"] == "Real5"


def test_proxy_to_proxy_merge_then_merge_into_real_user_flattens():
    """Ajya, Aju -> Ajay (proxy canonical) -> real user: all three must end
    up pointing directly at the real user after the cascade."""
    chat = CHAT - 6
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 666, "Ajay", "ajayreal")

    identity.link_identities(chat, "Ajya", canonical_proxy_name="Ajay",
                              admin_user_id=1, admin_name="Admin")
    identity.link_identities(chat, "Aju", canonical_proxy_name="Ajay",
                              admin_user_id=1, admin_name="Admin")
    identity.link_identities(chat, "Ajay", canonical_user_id=666,
                              admin_user_id=1, admin_name="Admin")

    for alias in ("Ajya", "Aju", "Ajay"):
        resolved = identity.resolve_canonical(chat, proxy_name=alias)
        assert resolved == {"kind": "user", "user_id": 666, "proxy_name": None}

    group = identity.get_alias_group(chat, user_id=666)
    assert set(group["aliases"]) == {"Ajya", "Aju", "Ajay"}
