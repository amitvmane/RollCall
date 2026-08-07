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

import pytest

import db
import services.identity as identity
from exceptions import incorrectParameter


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


def test_discard_hides_from_identities_and_suggestions_then_restores():
    chat = CHAT - 7
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 777, "Ajay", "ajayreal")
    rid = _mk_rollcall(chat)
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)",
        (rid, "Garbage2", "in"),
    )
    conn.commit()
    cur.close()

    # Before discard: shows up in both identities and suggestions (close to "Ajay"? no —
    # just confirm presence in the identities list first).
    names_before = {i["proxy_name"] for i in identity.list_all_identities(chat) if i["kind"] == "proxy"}
    assert "Garbage2" in names_before

    identity.discard_identity(chat, "Garbage2", admin_user_id=1, admin_name="Admin")

    names_after = {i["proxy_name"] for i in identity.list_all_identities(chat) if i["kind"] == "proxy"}
    assert "Garbage2" not in names_after
    assert "Garbage2" in identity.list_discarded(chat)

    # Discarding again is idempotent (no duplicate row, no error).
    identity.discard_identity(chat, "Garbage2", admin_user_id=1, admin_name="Admin")
    assert identity.list_discarded(chat).count("Garbage2") == 1

    result = identity.undiscard_identity(chat, "Garbage2", admin_user_id=1, admin_name="Admin")
    assert result == {"restored": True}
    names_restored = {i["proxy_name"] for i in identity.list_all_identities(chat) if i["kind"] == "proxy"}
    assert "Garbage2" in names_restored
    assert "Garbage2" not in identity.list_discarded(chat)


def test_alphabetical_ordering_of_identities_and_groups():
    chat = CHAT - 8
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 888, "Zack", "zackreal")
    rid = _mk_rollcall(chat)
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)", (rid, "Amit", "in"))
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)", (rid, "Bala", "in"))
    conn.commit()
    cur.close()

    names = [i["display_name"] for i in identity.list_all_identities(chat)]
    assert names == sorted(names, key=str.lower)


def test_case_and_whitespace_variants_auto_merge_permanently():
    """Case/whitespace-only variants (e.g. "amit" / "Amit" / " Amit ") auto-
    merge without any admin action, and this re-runs every time identities
    are listed — so a NEW variant added later (simulating /sif at a later
    date) gets swept up automatically too, not just a one-time cleanup.
    A proxy name that happens to exactly match a real member's name is
    NOT auto-merged (could be a coincidental shared first name)."""
    chat = CHAT - 9
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 999, "Real", "realuser")
    rid = _mk_rollcall(chat)
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)", (rid, "amit", "in"))
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)", (rid, "Amit", "in"))
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)", (rid, "Real", "in"))
    conn.commit()
    cur.close()

    ids = identity.list_all_identities(chat)
    proxies = {i["proxy_name"]: i["merged_into"] for i in ids if i["kind"] == "proxy"}
    merged = [n for n, m in proxies.items() if n.lower() == "amit" and m is not None]
    unmerged = [n for n, m in proxies.items() if n.lower() == "amit" and m is None]
    assert len(merged) == 1 and len(unmerged) == 1
    # "Real" exactly matches a real member's name but must stay unmerged.
    assert proxies["Real"] is None

    # Simulate a NEW case variant appearing later (e.g. /sif AMIT next month)
    # — must also auto-merge the next time identities are listed, with no
    # admin action, proving this isn't a one-time cleanup.
    rid2 = _mk_rollcall(chat)
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)", (rid2, "AMIT", "in"))
    conn.commit()
    cur.close()

    ids2 = identity.list_all_identities(chat)
    proxies2 = {i["proxy_name"]: i["merged_into"] for i in ids2 if i["kind"] == "proxy"}
    merged2 = [n for n, m in proxies2.items() if n.lower() == "amit" and m is not None]
    assert "AMIT" in merged2

    group = identity.list_identity_groups(chat)
    amit_group = [g for g in group if g["display_name"].strip().lower() == "amit"][0]
    assert set(a.lower() for a in amit_group["aliases"]) >= {"amit", "amit"}  # at least the lowercased forms present


def test_merge_into_non_member_user_id_rejected():
    """canonical_user_id is caller-supplied — merging a proxy's dues/
    attendance history onto an arbitrary Telegram id that has never been
    seen in this chat (typo, or bad-faith) must be rejected, not silently
    permanently combine two unrelated identities."""
    chat = CHAT - 10
    db.get_or_create_chat(chat)
    db.add_dues_entry(chat, None, None, "SomeProxy", "game_share", 50, None, 1, "Admin")

    with pytest.raises(incorrectParameter):
        identity.link_identities(chat, "SomeProxy", canonical_user_id=424242424242,
                                  admin_user_id=1, admin_name="Admin")

    # Confirm nothing was actually linked.
    assert db.get_dues_balance(chat, member_name="SomeProxy") == 50
    assert db.get_dues_balance(chat, user_id=424242424242) == 0


def test_get_proxy_name_activity_counts_sessions_and_finds_last_seen():
    """Powers the merge panel's recency/frequency sort — one grouped query
    across every proxy name in the chat. Unlike get_proxy_stats (which
    filters to r.is_active = FALSE), a name used only in a still-active
    rollcall must still be counted, not be invisible until the session
    ends."""
    chat = CHAT - 11
    db.get_or_create_chat(chat)
    rid1 = _mk_rollcall(chat, is_active=0)
    rid2 = _mk_rollcall(chat, is_active=0)
    rid3 = _mk_rollcall(chat, is_active=1)  # still active

    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status, updated_at) VALUES (?, ?, ?, ?)",
                (rid1, "SB7", "in", "2026-01-01 10:00:00"))
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status, updated_at) VALUES (?, ?, ?, ?)",
                (rid2, "SB7", "in", "2026-03-01 10:00:00"))
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status, updated_at) VALUES (?, ?, ?, ?)",
                (rid3, "SB7", "in", "2026-08-01 10:00:00"))
    # A second, unrelated name — confirms grouping is per-name, not global.
    cur.execute("INSERT INTO proxy_users (rollcall_id, name, status, updated_at) VALUES (?, ?, ?, ?)",
                (rid1, "Other", "in", "2026-01-01 10:00:00"))
    conn.commit()
    cur.close()

    activity = db.get_proxy_name_activity(chat)

    assert activity["SB7"]["count"] == 3
    assert activity["SB7"]["last_seen"] == "2026-08-01 10:00:00"
    assert activity["Other"]["count"] == 1


def test_multi_alias_merge_folds_identically_across_all_three_aggregators():
    """Phase 1 N+1 fix: get_ghost_leaderboard/get_leaderboard_by_attendance/
    get_all_dues_balances now batch-fetch identity_links once (via
    services.identity.get_canonical_map) instead of one get_identity_link
    query per proxy row. With 3 aliases merged into one real user, this
    proves the batch path still folds every alias's rows into a single
    combined entry — not just a single-alias merge (already covered by the
    existing per-function tests above), which wouldn't exercise the map
    holding multiple entries at once."""
    chat = CHAT - 12
    db.get_or_create_chat(chat)
    db.upsert_chat_member(chat, 777, "Real7", "real7user")

    rid = _mk_rollcall(chat)
    db.update_rollcall(rid, is_active=0)
    db.add_or_update_user(rid, 777, "Real7", "real7user", "in")
    db.increment_user_stat(chat, 777, "total_in")
    db.increment_user_stat(chat, 777, "total_rollcalls")
    db.increment_ghost_count(chat, 777, "Real7")

    conn = db.get_connection()
    cur = conn.cursor()
    for alias in ("Alias1", "Alias2", "Alias3"):
        cur.execute("INSERT INTO proxy_users (rollcall_id, name, status) VALUES (?, ?, ?)",
                    (rid, alias, "in"))
        db.increment_ghost_count(chat, -1, alias, proxy_name=alias)
    conn.commit()
    cur.close()
    for alias in ("Alias1", "Alias2", "Alias3"):
        db.add_dues_entry(chat, None, None, alias, "game_share", 10, None, 1, "Admin")

    for alias in ("Alias1", "Alias2", "Alias3"):
        identity.link_identities(chat, alias, canonical_user_id=777,
                                  admin_user_id=1, admin_name="Admin")

    attendance = db.get_leaderboard_by_attendance(chat)
    matching_attendance = [r for r in attendance if r.get("user_id") == 777]
    assert len(matching_attendance) == 1
    assert matching_attendance[0]["attended"] == 4  # Real7's own + 3 aliases

    ghosts = db.get_ghost_leaderboard(chat)
    matching_ghosts = [r for r in ghosts if r["user_id"] == 777]
    assert len(matching_ghosts) == 1
    assert matching_ghosts[0]["ghost_count"] == 4  # Real7's own + 3 aliases

    balances = db.get_all_dues_balances(chat)
    matching_balance = [r for r in balances if r["user_id"] == 777]
    assert len(matching_balance) == 1
    assert matching_balance[0]["balance"] == 30  # 3 aliases x 10, Real7 itself has no entry
