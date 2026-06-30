#!/usr/bin/env python3
"""
Merge a recovered *history* RollCall SQLite DB into the *current* (live) DB.

Use case: the original server died and its DB was frozen at the moment of
failure. A replacement server ran on a fresh DB and accumulated new data. Once
the old disk is recovered you want one DB that contains BOTH the old history
and the new activity.

Because the two DBs cover NON-OVERLAPPING time periods (old ≤ failure, new ≥
failure) the merge is well defined:

  • rollcalls + their children (users, proxy_users, rollcall_stats,
    ghost_events) are RE-IDed and appended — surrogate ids collide otherwise.
  • per-member aggregates (user_stats, proxy_stats, ghost_records) are SUMMED;
    best_streak = max(); current_streak keeps the live server's value (the old
    streak ended at failure and is superseded).
  • chats/templates/chat_members are filled in where the live DB doesn't
    already have them (the live row always wins on conflict).
  • admin_actions are appended (audit history).
  • SKIPPED entirely (would break the running server's web/push, or are
    ephemeral/past): system_config (VAPID keys), push_subscriptions,
    web_verify_tokens, scheduled_rollcalls, api_tokens.

Safety: inputs are opened read-only. The result is written to a NEW --output
file (current.db is copied first, then history is folded in). Run with
--dry-run first to see the counts without producing output.

Usage:
    python scripts/merge_db.py --current live.db --history old.db --output merged.db
    python scripts/merge_db.py --current live.db --history old.db --dry-run

Then, with the bot STOPPED:
    cp merged.db data/rollcall.db && restart the bot
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

# Re-IDed parent and its FK children (child_table -> fk column referencing rollcalls.id)
RC_CHILDREN = {
    "users": "rollcall_id",
    "proxy_users": "rollcall_id",
    "rollcall_stats": "rollcall_id",
    "ghost_events": "rollcall_id",
}

# Aggregate (summed) integer columns per stats table
SUM_COLS = {
    "user_stats": [
        "total_in", "total_out", "total_maybe", "total_waiting_to_in",
        "total_rollcalls", "total_response_seconds",
    ],
    "proxy_stats": ["total_in", "total_out", "total_maybe", "total_rollcalls"],
}


def _cols(conn, table, schema="main"):
    rows = conn.execute(f"SELECT name FROM {schema}.pragma_table_info(?)", (table,)).fetchall()
    return [r[0] for r in rows]


def _common_cols(conn, table, exclude=()):
    """Columns present in BOTH databases (resilient to schema drift)."""
    main = _cols(conn, table, "main")
    src = _cols(conn, table, "src")
    common = [c for c in main if c in src and c not in exclude]
    return common


def _count(conn, schema, table):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {schema}.{table}").fetchone()[0]
    except sqlite3.Error:
        return 0


def merge(current_path, history_path, output_path, dry_run=False):
    if not os.path.isfile(current_path):
        sys.exit(f"error: --current not found: {current_path}")
    if not os.path.isfile(history_path):
        sys.exit(f"error: --history not found: {history_path}")

    # Work on a copy of current so the live file is never mutated.
    work_fd, work_path = tempfile.mkstemp(suffix=".db")
    os.close(work_fd)
    shutil.copyfile(current_path, work_path)

    conn = sqlite3.connect(work_path)
    conn.execute("PRAGMA foreign_keys=OFF")  # we manage FK remapping ourselves
    conn.execute("ATTACH DATABASE ? AS src", (history_path,))

    stats = {}
    try:
        conn.execute("BEGIN")

        # ── chats: bring back groups the live DB doesn't have yet ──────────────
        cols = _common_cols(conn, "chats")
        collist = ", ".join(cols)
        before = _count(conn, "main", "chats")
        conn.execute(
            f"INSERT INTO main.chats ({collist}) "
            f"SELECT {collist} FROM src.chats "
            f"WHERE chat_id NOT IN (SELECT chat_id FROM main.chats)"
        )
        stats["chats +"] = _count(conn, "main", "chats") - before

        # ── rollcalls: re-ID and append; build old_id -> new_id map ────────────
        rc_cols = _common_cols(conn, "rollcalls", exclude=("id",))
        rc_collist = ", ".join(rc_cols)
        placeholders = ", ".join("?" * len(rc_cols))
        rc_map = {}
        for row in conn.execute(
            f"SELECT id, {rc_collist} FROM src.rollcalls ORDER BY id"
        ).fetchall():
            old_id, values = row[0], row[1:]
            cur = conn.execute(
                f"INSERT INTO main.rollcalls ({rc_collist}) VALUES ({placeholders})",
                values,
            )
            rc_map[old_id] = cur.lastrowid
        stats["rollcalls +"] = len(rc_map)

        # ── rollcall children: append with remapped rollcall_id ────────────────
        for table, fk in RC_CHILDREN.items():
            cols = _common_cols(conn, table, exclude=("id",))
            collist = ", ".join(cols)
            placeholders = ", ".join("?" * len(cols))
            fk_idx = cols.index(fk)
            n = 0
            for row in conn.execute(f"SELECT {collist} FROM src.{table}").fetchall():
                values = list(row)
                old_rid = values[fk_idx]
                if old_rid not in rc_map:
                    continue  # orphan — parent rollcall wasn't copied
                values[fk_idx] = rc_map[old_rid]
                conn.execute(
                    f"INSERT INTO main.{table} ({collist}) VALUES ({placeholders})",
                    values,
                )
                n += 1
            stats[f"{table} +"] = n

        # ── user_stats / proxy_stats: sum aggregates on conflict ───────────────
        stats["user_stats ~"] = _merge_stats(
            conn, "user_stats", ("chat_id", "user_id"), SUM_COLS["user_stats"]
        )
        stats["proxy_stats ~"] = _merge_stats(
            conn, "proxy_stats", ("chat_id", "proxy_name"), SUM_COLS["proxy_stats"]
        )

        # ── ghost_records: sum ghost_count, keep latest last_ghosted_at ────────
        stats["ghost_records ~"] = _merge_ghost_records(conn)

        # ── templates: insert where (chatid, name) not present ─────────────────
        cols = _common_cols(conn, "templates", exclude=("id",))
        collist = ", ".join(cols)
        before = _count(conn, "main", "templates")
        conn.execute(
            f"INSERT INTO main.templates ({collist}) "
            f"SELECT {collist} FROM src.templates s "
            f"WHERE NOT EXISTS (SELECT 1 FROM main.templates m "
            f"                  WHERE m.chatid = s.chatid AND m.name = s.name)"
        )
        stats["templates +"] = _count(conn, "main", "templates") - before

        # ── chat_members: insert missing; keep most recent last_seen ───────────
        cols = _common_cols(conn, "chat_members")
        collist = ", ".join(cols)
        before = _count(conn, "main", "chat_members")
        conn.execute(
            f"INSERT INTO main.chat_members ({collist}) "
            f"SELECT {collist} FROM src.chat_members s "
            f"WHERE NOT EXISTS (SELECT 1 FROM main.chat_members m "
            f"                  WHERE m.chat_id = s.chat_id AND m.user_id = s.user_id)"
        )
        stats["chat_members +"] = _count(conn, "main", "chat_members") - before
        conn.execute(
            "UPDATE main.chat_members AS m SET last_seen = ("
            "  SELECT s.last_seen FROM src.chat_members s "
            "  WHERE s.chat_id = m.chat_id AND s.user_id = m.user_id) "
            "WHERE EXISTS (SELECT 1 FROM src.chat_members s "
            "  WHERE s.chat_id = m.chat_id AND s.user_id = m.user_id "
            "  AND s.last_seen > m.last_seen)"
        )

        # ── admin_actions: append the old audit trail ──────────────────────────
        cols = _common_cols(conn, "admin_actions", exclude=("id",))
        collist = ", ".join(cols)
        before = _count(conn, "main", "admin_actions")
        conn.execute(
            f"INSERT INTO main.admin_actions ({collist}) "
            f"SELECT {collist} FROM src.admin_actions"
        )
        stats["admin_actions +"] = _count(conn, "main", "admin_actions") - before

        # ── web_view_stats: sum view counts (best-effort) ──────────────────────
        try:
            n = 0
            for token, vc, lv in conn.execute(
                "SELECT group_token, view_count, last_viewed_at FROM src.web_view_stats"
            ).fetchall():
                conn.execute(
                    "INSERT INTO main.web_view_stats (group_token, view_count, last_viewed_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(group_token) DO UPDATE SET "
                    "  view_count = view_count + excluded.view_count",
                    (token, vc, lv),
                )
                n += 1
            stats["web_view_stats ~"] = n
        except sqlite3.Error:
            pass  # table may not exist on older history DBs

        # Integrity check before we keep anything.
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if ic != "ok":
            conn.execute("ROLLBACK")
            sys.exit(f"error: merged DB failed integrity_check: {ic}")

        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        os.unlink(work_path)
        raise

    conn.close()

    print("\nMerge summary (history → current):")
    for k in sorted(stats):
        print(f"  {k:<22} {stats[k]:>8}")
    print("  ('+' = rows added, '~' = rows summed/updated)")

    if dry_run:
        os.unlink(work_path)
        print("\nDRY RUN — no output written. Re-run with --output to produce the merged DB.")
    else:
        shutil.move(work_path, output_path)
        print(f"\n✅ Wrote merged DB → {output_path}")
        print("   Inputs were not modified. With the bot stopped:")
        print(f"     cp {output_path} data/rollcall.db   # then restart the bot")


def _merge_stats(conn, table, key_cols, sum_cols):
    """Upsert stats rows: sum totals, max(best_streak), keep live current_streak."""
    src_cols = _common_cols(conn, table, exclude=("id",))
    key_idx = [src_cols.index(k) for k in key_cols]
    has_best = "best_streak" in src_cols
    n = 0
    where = " AND ".join(f"{k} = ?" for k in key_cols)
    insert_collist = ", ".join(src_cols)
    insert_ph = ", ".join("?" * len(src_cols))
    for row in conn.execute(f"SELECT {', '.join(src_cols)} FROM src.{table}").fetchall():
        key_vals = [row[i] for i in key_idx]
        existing = conn.execute(
            f"SELECT 1 FROM main.{table} WHERE {where}", key_vals
        ).fetchone()
        if existing:
            sets = ", ".join(f"{c} = {c} + ?" for c in sum_cols)
            params = [row[src_cols.index(c)] for c in sum_cols]
            extra = ""
            if has_best:
                extra = ", best_streak = MAX(best_streak, ?)"
                params.append(row[src_cols.index("best_streak")])
            conn.execute(
                f"UPDATE main.{table} SET {sets}{extra} WHERE {where}",
                params + key_vals,
            )
        else:
            conn.execute(
                f"INSERT INTO main.{table} ({insert_collist}) VALUES ({insert_ph})",
                list(row),
            )
        n += 1
    return n


def _merge_ghost_records(conn):
    """Sum ghost_count per (chat_id, proxy_name) or (chat_id, user_id)."""
    n = 0
    for cid, uid, pname, uname, gc, last in conn.execute(
        "SELECT chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at "
        "FROM src.ghost_records"
    ).fetchall():
        if pname is not None:
            where, key = "chat_id = ? AND proxy_name = ?", (cid, pname)
        else:
            where, key = "chat_id = ? AND proxy_name IS NULL AND user_id = ?", (cid, uid)
        existing = conn.execute(
            f"SELECT id FROM main.ghost_records WHERE {where}", key
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE main.ghost_records SET ghost_count = ghost_count + ?, "
                "last_ghosted_at = MAX(COALESCE(last_ghosted_at,''), COALESCE(?,'')) "
                "WHERE id = ?",
                (gc, last, existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO main.ghost_records "
                "(chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cid, uid, pname, uname, gc, last),
            )
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser(description="Merge a history RollCall DB into the current DB.")
    ap.add_argument("--current", required=True, help="live DB (base; read-only)")
    ap.add_argument("--history", required=True, help="recovered old DB to fold in (read-only)")
    ap.add_argument("--output", help="path for the merged DB (required unless --dry-run)")
    ap.add_argument("--dry-run", action="store_true", help="report counts without writing output")
    args = ap.parse_args()
    if not args.dry_run and not args.output:
        ap.error("--output is required unless --dry-run is given")
    merge(args.current, args.history, args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
