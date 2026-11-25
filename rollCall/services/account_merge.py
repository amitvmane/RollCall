"""
Merge an email-only account into a Telegram member's account.

How the duplicate happens: an existing Telegram member signs in by email
before linking it, and gets a second, local identity (identity_space). Votes,
stats and dues made under it are a different person as far as every table is
concerned. This moves all of it onto their Telegram id, once BOTH identities
have been proven on one device (services.email_auth: the Telegram sign-in
asked for the link, the email click proved the address).

What moves, and how conflicts resolve (the person voted in the same game as
both — keep the Telegram row):

  users (votes)        re-keyed; a game voted in twice keeps the Telegram vote
  user_stats           counters summed into the Telegram row, best streak = max
  ghost_records        ghost counts summed
  ghost_events, late_drops, push_subscriptions, identity_links,
  proxy_users, collector fields, scheduled_rollcalls      re-keyed
  rollcall_teams       re-keyed (who_key u<id>)
  chat_members         re-keyed; the Telegram row wins where both exist
  web_admins           re-keyed; the higher role wins, so no group loses an owner
  dues_entries         NEVER updated (append-only ledger): each group's balance
                       moves by a pair of compensating 'adjustment' entries
  login tokens         the local account's are deleted

Afterwards the local principal records merged_into_user_id, and identity
tokens already issued to the local id verify as the Telegram id
(api.identity) — so a device still signed in by email keeps working, as the
merged person, instead of quietly splitting them in two again.
"""

import logging
from typing import Optional

import db
from exceptions import incorrectParameter, insufficientPermissions
from identity_space import is_local_user, principal_of_local_user

_SUM_STATS = ("total_in", "total_out", "total_maybe", "total_waiting_to_in",
              "total_rollcalls", "total_response_seconds")


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def summary(local_uid: int) -> dict:
    """What a merge would move — shown before the person confirms."""
    ph = _ph()
    with db._cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS n, COUNT(DISTINCT r.chat_id) AS g FROM users u "
                       f"JOIN rollcalls r ON r.id = u.rollcall_id WHERE u.user_id = {ph}", (local_uid,))
        row = cursor.fetchone()
        votes, groups = (row["n"], row["g"]) if isinstance(row, dict) else (row[0], row[1])
    return {"votes": int(votes or 0), "groups": int(groups or 0)}


def affected_chats(local_uid: int) -> list:
    ph = _ph()
    chats = set()
    with db._cursor() as cursor:
        for sql in (f"SELECT DISTINCT r.chat_id FROM users u JOIN rollcalls r ON r.id = u.rollcall_id "
                    f"WHERE u.user_id = {ph}",
                    f"SELECT DISTINCT chat_id FROM chat_members WHERE user_id = {ph}",
                    f"SELECT DISTINCT chat_id FROM user_stats WHERE user_id = {ph}",
                    f"SELECT DISTINCT chat_id FROM web_admins WHERE tg_user_id = {ph}",
                    f"SELECT DISTINCT chat_id FROM dues_entries WHERE user_id = {ph}"):
            cursor.execute(sql, (local_uid,))
            for r in cursor.fetchall():
                chats.add(int(r["chat_id"] if isinstance(r, dict) else r[0]))
    return sorted(chats)


def _merge_rows(cursor, L: int, T: int) -> None:
    """Everything except the ledger, in the caller's transaction."""
    ph = _ph()
    ex = cursor.execute

    # Votes: one row per (rollcall, person).
    ex(f"DELETE FROM users WHERE user_id = {ph} AND rollcall_id IN "
       f"(SELECT rollcall_id FROM (SELECT rollcall_id FROM users WHERE user_id = {ph}) t)", (L, T))
    ex(f"UPDATE users SET user_id = {ph} WHERE user_id = {ph}", (T, L))

    # Counters: summed where both exist.
    cols = ", ".join(("chat_id", "best_streak") + _SUM_STATS)
    ex(f"SELECT {cols} FROM user_stats WHERE user_id = {ph}", (L,))
    for r in cursor.fetchall():
        r = dict(r) if isinstance(r, dict) else dict(zip(cols.split(", "), r))
        ex(f"SELECT 1 FROM user_stats WHERE chat_id = {ph} AND user_id = {ph}", (r["chat_id"], T))
        if cursor.fetchone():
            sets = ", ".join(f"{c} = COALESCE({c}, 0) + {ph}" for c in _SUM_STATS)
            ex(f"UPDATE user_stats SET {sets}, best_streak = CASE WHEN COALESCE(best_streak, 0) < {ph} "
               f"THEN {ph} ELSE best_streak END WHERE chat_id = {ph} AND user_id = {ph}",
               tuple(int(r[c] or 0) for c in _SUM_STATS)
               + (int(r["best_streak"] or 0), int(r["best_streak"] or 0), r["chat_id"], T))
            ex(f"DELETE FROM user_stats WHERE chat_id = {ph} AND user_id = {ph}", (r["chat_id"], L))
        else:
            ex(f"UPDATE user_stats SET user_id = {ph} WHERE chat_id = {ph} AND user_id = {ph}",
               (T, r["chat_id"], L))

    ex(f"SELECT chat_id, ghost_count FROM ghost_records WHERE user_id = {ph}", (L,))
    for r in cursor.fetchall():
        cid, n = (r["chat_id"], r["ghost_count"]) if isinstance(r, dict) else (r[0], r[1])
        ex(f"SELECT 1 FROM ghost_records WHERE chat_id = {ph} AND user_id = {ph}", (cid, T))
        if cursor.fetchone():
            ex(f"UPDATE ghost_records SET ghost_count = COALESCE(ghost_count, 0) + {ph} "
               f"WHERE chat_id = {ph} AND user_id = {ph}", (int(n or 0), cid, T))
            ex(f"DELETE FROM ghost_records WHERE chat_id = {ph} AND user_id = {ph}", (cid, L))
        else:
            ex(f"UPDATE ghost_records SET user_id = {ph} WHERE chat_id = {ph} AND user_id = {ph}", (T, cid, L))

    # One row per (rollcall, person) / (chat, person): the Telegram row wins.
    for table, scope in (("late_drops", "rollcall_id"), ("chat_members", "chat_id")):
        ex(f"DELETE FROM {table} WHERE user_id = {ph} AND {scope} IN "
           f"(SELECT {scope} FROM (SELECT {scope} FROM {table} WHERE user_id = {ph}) t)", (L, T))
        ex(f"UPDATE {table} SET user_id = {ph} WHERE user_id = {ph}", (T, L))
    ex(f"DELETE FROM rollcall_teams WHERE who_key = {ph} AND rollcall_id IN "
       f"(SELECT rollcall_id FROM (SELECT rollcall_id FROM rollcall_teams WHERE who_key = {ph}) t)",
       (f"u{L}", f"u{T}"))
    ex(f"UPDATE rollcall_teams SET who_key = {ph} WHERE who_key = {ph}", (f"u{T}", f"u{L}"))

    # Admin grants: the higher role survives, so an owner stays an owner.
    ex(f"SELECT chat_id, role FROM web_admins WHERE tg_user_id = {ph}", (L,))
    for r in cursor.fetchall():
        cid, role = (r["chat_id"], r["role"]) if isinstance(r, dict) else (r[0], r[1])
        ex(f"SELECT role FROM web_admins WHERE chat_id = {ph} AND tg_user_id = {ph}", (cid, T))
        mine = cursor.fetchone()
        if mine:
            if role == "owner":
                ex(f"UPDATE web_admins SET role = 'owner' WHERE chat_id = {ph} AND tg_user_id = {ph}", (cid, T))
            ex(f"DELETE FROM web_admins WHERE chat_id = {ph} AND tg_user_id = {ph}", (cid, L))
        else:
            ex(f"UPDATE web_admins SET tg_user_id = {ph} WHERE chat_id = {ph} AND tg_user_id = {ph}", (T, cid, L))

    for table, col in (("ghost_events", "user_id"), ("push_subscriptions", "tg_user_id"),
                       ("identity_links", "canonical_user_id"), ("proxy_users", "proxy_owner_id"),
                       ("rollcalls", "collector_uid"), ("game_closures", "collector_uid"),
                       ("chats", "last_collector_uid"), ("scheduled_rollcalls", "created_by_uid")):
        ex(f"UPDATE {table} SET {col} = {ph} WHERE {col} = {ph}", (T, L))
    ex(f"DELETE FROM member_login_tokens WHERE user_id = {ph}", (L,))
    ex(f"DELETE FROM web_direct_login_tokens WHERE tg_user_id = {ph}", (L,))


def _move_dues(L: int, T: int, actor_name: str) -> int:
    """Move each group's balance with compensating entries (append-only)."""
    ph = _ph()
    with db._cursor() as cursor:
        cursor.execute(f"SELECT chat_id AS c, COALESCE(SUM(amount), 0) AS b, MAX(member_name) AS n "
                       f"FROM dues_entries WHERE user_id = {ph} GROUP BY chat_id", (L,))
        balances = [(int(r["c"]), int(r["b"]), r["n"]) if isinstance(r, dict) else (int(r[0]), int(r[1]), r[2])
                    for r in cursor.fetchall()]
    moved = 0
    for cid, balance, name in balances:
        if not balance:
            continue
        target_name = db._member_display_name(cid, T)
        db.add_dues_entry(cid, None, L, name or "email account", "adjustment", -balance,
                          f"Account merged into {target_name}", T, actor_name)
        db.add_dues_entry(cid, None, T, target_name, "adjustment", balance,
                          f"Balance from merged email account ({name})", T, actor_name)
        db.log_admin_action(cid, T, actor_name, "account_merge_dues",
                            target_name=target_name, details=f"{balance}")
        moved += 1
    return moved


def merged_into(local_uid: int) -> Optional[int]:
    """The Telegram id a merged local account now resolves to, if any."""
    if not is_local_user(local_uid):
        return None
    try:
        with db._cursor() as cursor:
            cursor.execute(f"SELECT merged_into_user_id FROM principals WHERE id = {_ph()}",
                           (principal_of_local_user(local_uid),))
            row = cursor.fetchone()
            if row is None:
                return None
            v = row["merged_into_user_id"] if isinstance(row, dict) else row[0]
            return int(v) if v else None
    except Exception:
        logging.exception("account_merge.merged_into failed")
        return None


async def merge(local_uid: int, tg_uid: int, actor_name: str) -> dict:
    """Move everything from local_uid onto tg_uid. Caller has proven both."""
    if not is_local_user(local_uid):
        raise incorrectParameter("Only an email-only account can be merged.")
    if tg_uid <= 0 or is_local_user(tg_uid):
        raise insufficientPermissions("Merge into a Telegram account — sign in with Telegram first.")
    if merged_into(local_uid):
        raise incorrectParameter("That account has already been merged.")
    from rollcall_manager import manager
    chats = affected_chats(local_uid)
    # Sorted order, one pass: every other path takes one chat lock at a time,
    # so this can't deadlock against them.
    held = []
    try:
        for c in chats:
            lock = manager.get_chat_write_lock(c)
            await lock.acquire()
            held.append(lock)
        with db._cursor(commit=True) as cursor:
            _merge_rows(cursor, local_uid, tg_uid)
            cursor.execute(f"UPDATE principals SET merged_into_user_id = {_ph()} WHERE id = {_ph()}",
                           (tg_uid, principal_of_local_user(local_uid)))
        dues_groups = _move_dues(local_uid, tg_uid, actor_name)
        # Open rollcalls hold their voters in memory: reload from the rows
        # just re-keyed, inside the same locks.
        for cid in chats:
            for rc in manager.get_rollcalls(cid):
                rc._load_users_from_db()
    finally:
        for lock in reversed(held):
            lock.release()
    for cid in chats:
        db.log_admin_action(cid, tg_uid, actor_name, "account_merge", target_name=str(local_uid))
    logging.info("[account_merge] %s -> %s chats=%s dues_groups=%s", local_uid, tg_uid, chats, dues_groups)
    return {"groups": len(chats), "dues_groups": dues_groups}
