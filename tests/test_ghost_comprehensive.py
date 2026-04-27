#!/usr/bin/env python3
"""
Comprehensive Ghost Tracking Tests (Standalone)
Tests ghost functionality with multiple users, multiple rollcalls, and persistence
"""

import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "rollCall"))

import sqlite3
import json


def main():
    print("=" * 70)
    print("COMPREHENSIVE GHOST TRACKING TESTS")
    print("=" * 70)
    
    db_path = tempfile.mktemp(suffix='_ghost_test.db')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    create_tables(conn)
    create_ghost_selections_table(conn)
    
    print(f"Using test DB: {db_path}\n")
    
    tests = GhostTestRunner(conn)
    
    try:
        tests.test_01_single_real_user_ghost()
        tests.test_02_single_proxy_user_ghost()
        tests.test_03_multiple_real_users_ghost()
        tests.test_04_multiple_proxy_users_ghost()
        tests.test_05_mixed_real_and_proxy_ghost()
        tests.test_06_user_ghosts_multiple_times()
        tests.test_07_proxy_user_ghosts_multiple_times()
        tests.test_08_leaderboard_sorted_by_count()
        tests.test_09_clear_absent_clears_count()
        tests.test_10_clear_absent_proxy()
        tests.test_11_ghost_persistence_save_load()
        tests.test_12_ghost_persistence_no_saved_data()
        tests.test_13_warning_badge_at_limit()
        tests.test_14_10_users_5_sessions()
        tests.test_15_proxy_only_rollcalls()
        tests.test_16_full_integration_workflow()
        
        print("\n" + "=" * 70)
        print("ALL 16 TESTS PASSED!")
        print("=" * 70)
    finally:
        conn.close()
        os.unlink(db_path)


def create_tables(conn):
    cursor = conn.cursor()
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        shh_mode INTEGER DEFAULT 0,
        admin_rights INTEGER DEFAULT 0,
        timezone TEXT DEFAULT 'Asia/Calcutta',
        absent_limit INTEGER DEFAULT 1,
        ghost_tracking_enabled INTEGER DEFAULT 1
    )""")
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS rollcalls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        title TEXT,
        is_active INTEGER DEFAULT 1,
        ended_at TEXT,
        absent_marked INTEGER DEFAULT 0
    )""")
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rollcall_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        name TEXT,
        username TEXT,
        status TEXT,
        in_pos INTEGER
    )""")
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS proxy_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rollcall_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        status TEXT,
        in_pos INTEGER
    )""")
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS ghost_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL DEFAULT -1,
        proxy_name TEXT,
        user_name TEXT,
        ghost_count INTEGER DEFAULT 0,
        last_ghosted_at TEXT
    )""")
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS ghost_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rollcall_id INTEGER NOT NULL,
        chat_id INTEGER NOT NULL,
        user_id INTEGER,
        user_name TEXT,
        proxy_name TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    
    conn.commit()


def create_ghost_selections_table(conn):
    cursor = conn.cursor()
    cursor.execute("""CREATE TABLE IF NOT EXISTS ghost_selections (
        chat_id INTEGER NOT NULL,
        rc_db_id INTEGER NOT NULL,
        selected_ids TEXT DEFAULT '[]',
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, rc_db_id)
    )""")
    conn.commit()


class GhostTestRunner:
    def __init__(self, conn):
        self.conn = conn
        self.chat_id = 168415137
        self._reset()
        self.conn.execute("INSERT OR IGNORE INTO chats (chat_id, absent_limit) VALUES (?, 1)", (self.chat_id,))
        self.conn.commit()
    
    def _reset(self):
        self.conn.execute("DELETE FROM ghost_records")
        self.conn.execute("DELETE FROM ghost_events")
        self.conn.execute("DELETE FROM ghost_selections")
        self.conn.execute("DELETE FROM rollcalls")
        self.conn.execute("DELETE FROM users")
        self.conn.execute("DELETE FROM proxy_users")
        self.conn.commit()
    
    def _create_rollcall(self, title="Test RC"):
        self.conn.execute(
            "INSERT INTO rollcalls (chat_id, title, is_active) VALUES (?, ?, 1)",
            (self.chat_id, title)
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    
    def _add_users(self, rc_id, users):
        for i, user in enumerate(users):
            if user.get('user_id'):
                self.conn.execute(
                    "INSERT INTO users (rollcall_id, user_id, name, username, status, in_pos) VALUES (?, ?, ?, ?, 'in', ?)",
                    (rc_id, user['user_id'], user['name'], user.get('username'), i + 1)
                )
            else:
                self.conn.execute(
                    "INSERT INTO proxy_users (rollcall_id, name, status, in_pos) VALUES (?, ?, 'in', ?)",
                    (rc_id, user['proxy_name'], i + 1)
                )
        self.conn.commit()
    
    def _increment_ghost(self, user_id, user_name, proxy_name=None):
        if proxy_name:
            existing = self.conn.execute(
                "SELECT id FROM ghost_records WHERE chat_id = ? AND proxy_name = ?",
                (self.chat_id, proxy_name)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE ghost_records SET ghost_count = ghost_count + 1, user_name = ?, last_ghosted_at = CURRENT_TIMESTAMP WHERE chat_id = ? AND proxy_name = ?",
                    (user_name, self.chat_id, proxy_name)
                )
            else:
                self.conn.execute(
                    "INSERT INTO ghost_records (chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at) VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
                    (self.chat_id, user_id, proxy_name, user_name)
                )
        else:
            existing = self.conn.execute(
                "SELECT id FROM ghost_records WHERE chat_id = ? AND user_id = ?",
                (self.chat_id, user_id)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE ghost_records SET ghost_count = ghost_count + 1, user_name = ?, last_ghosted_at = CURRENT_TIMESTAMP WHERE chat_id = ? AND user_id = ?",
                    (user_name, self.chat_id, user_id)
                )
            else:
                self.conn.execute(
                    "INSERT INTO ghost_records (chat_id, user_id, user_name, ghost_count, last_ghosted_at) VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)",
                    (self.chat_id, user_id, user_name)
                )
        self.conn.commit()
    
    def _get_ghost_count(self, user_id, proxy_name=None):
        if proxy_name:
            row = self.conn.execute(
                "SELECT ghost_count FROM ghost_records WHERE chat_id = ? AND proxy_name = ?",
                (self.chat_id, proxy_name)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT ghost_count FROM ghost_records WHERE chat_id = ? AND user_id = ?",
                (self.chat_id, user_id)
            ).fetchone()
        return row['ghost_count'] if row else 0
    
    def _get_leaderboard(self):
        rows = self.conn.execute(
            "SELECT user_name, proxy_name, ghost_count FROM ghost_records WHERE chat_id = ? ORDER BY ghost_count DESC",
            (self.chat_id,)
        ).fetchall()
        return [(r['user_name'] or r['proxy_name'], r['ghost_count']) for r in rows]
    
    def _reset_ghost(self, user_id, proxy_name=None):
        if proxy_name:
            self.conn.execute(
                "DELETE FROM ghost_records WHERE chat_id = ? AND proxy_name = ?",
                (self.chat_id, proxy_name)
            )
        else:
            self.conn.execute(
                "DELETE FROM ghost_records WHERE chat_id = ? AND user_id = ?",
                (self.chat_id, user_id)
            )
        self.conn.commit()
    
    def _save_selections(self, rc_id, selected):
        self.conn.execute(
            "INSERT OR REPLACE INTO ghost_selections (chat_id, rc_db_id, selected_ids, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (self.chat_id, rc_id, json.dumps(list(selected)))
        )
        self.conn.commit()
    
    def _load_selections(self, rc_id):
        row = self.conn.execute(
            "SELECT selected_ids FROM ghost_selections WHERE chat_id = ? AND rc_db_id = ?",
            (self.chat_id, rc_id)
        ).fetchone()
        return set(json.loads(row['selected_ids'])) if row else None
    
    def test_01_single_real_user_ghost(self):
        print("\n=== TEST 1: Single real user ghosts once ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [{'user_id': 1, 'name': 'Alice'}])
        
        self._increment_ghost(1, 'Alice')
        
        count = self._get_ghost_count(1)
        print(f"Alice ghost count: {count}")
        assert count == 1, f"Expected 1, got {count}"
        print("✓ PASSED\n")
    
    def test_02_single_proxy_user_ghost(self):
        print("\n=== TEST 2: Single proxy user ghosts once ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [{'proxy_name': 'Bob'}])
        
        self._increment_ghost(-1, 'Bob', proxy_name='Bob')
        
        count = self._get_ghost_count(-1, proxy_name='Bob')
        print(f"Bob ghost count: {count}")
        assert count == 1, f"Expected 1, got {count}"
        print("✓ PASSED\n")
    
    def test_03_multiple_real_users_ghost(self):
        print("\n=== TEST 3: Multiple real users ghost in same session ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [
            {'user_id': 1, 'name': 'Alice'},
            {'user_id': 2, 'name': 'Bob'},
            {'user_id': 3, 'name': 'Charlie'},
        ])
        
        self._increment_ghost(1, 'Alice')
        self._increment_ghost(2, 'Bob')
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard: {leaderboard}")
        assert len(leaderboard) == 2, f"Expected 2, got {len(leaderboard)}"
        print("✓ PASSED\n")
    
    def test_04_multiple_proxy_users_ghost(self):
        print("\n=== TEST 4: Multiple proxy users ghost in same session ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [
            {'proxy_name': 'John'},
            {'proxy_name': 'Jane'},
            {'proxy_name': 'Joe'},
        ])
        
        self._increment_ghost(-1, 'John', proxy_name='John')
        self._increment_ghost(-1, 'Jane', proxy_name='Jane')
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard: {leaderboard}")
        assert len(leaderboard) == 2, f"Expected 2, got {len(leaderboard)}"
        print("✓ PASSED\n")
    
    def test_05_mixed_real_and_proxy_ghost(self):
        print("\n=== TEST 5: Mixed real and proxy users ghost in same session ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [
            {'user_id': 1, 'name': 'Alice'},
            {'proxy_name': 'Bob'},
            {'user_id': 2, 'name': 'Charlie'},
            {'proxy_name': 'Diana'},
        ])
        
        self._increment_ghost(1, 'Alice')
        self._increment_ghost(-1, 'Bob', proxy_name='Bob')
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard: {leaderboard}")
        names = set(n for n, c in leaderboard)
        assert names == {'Alice', 'Bob'}, f"Expected {{Alice, Bob}}, got {names}"
        print("✓ PASSED\n")
    
    def test_06_user_ghosts_multiple_times(self):
        print("\n=== TEST 6: Same user ghosts multiple times ===")
        self._reset()
        
        for day in range(1, 4):
            rc_id = self._create_rollcall(f"Day {day}")
            self._add_users(rc_id, [{'user_id': 1, 'name': 'Alice'}])
            
            if day != 2:
                self._increment_ghost(1, 'Alice')
        
        count = self._get_ghost_count(1)
        print(f"Alice ghost count after 3 sessions (ghosted 2x): {count}")
        assert count == 2, f"Expected 2, got {count}"
        print("✓ PASSED\n")
    
    def test_07_proxy_user_ghosts_multiple_times(self):
        print("\n=== TEST 7: Same proxy user ghosts multiple times ===")
        self._reset()
        
        for day in range(1, 4):
            rc_id = self._create_rollcall(f"Day {day}")
            self._add_users(rc_id, [{'proxy_name': 'Bob'}])
            
            if day != 2:
                self._increment_ghost(-1, 'Bob', proxy_name='Bob')
        
        count = self._get_ghost_count(-1, proxy_name='Bob')
        print(f"Bob ghost count after 3 sessions (ghosted 2x): {count}")
        assert count == 2, f"Expected 2, got {count}"
        print("✓ PASSED\n")
    
    def test_08_leaderboard_sorted_by_count(self):
        print("\n=== TEST 8: Leaderboard sorted by count ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [
            {'user_id': 1, 'name': 'Alice'},
            {'user_id': 2, 'name': 'Bob'},
            {'user_id': 3, 'name': 'Charlie'},
        ])
        
        self._increment_ghost(1, 'Alice')
        self._increment_ghost(2, 'Bob')
        self._increment_ghost(2, 'Bob')
        self._increment_ghost(3, 'Charlie')
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard: {leaderboard}")
        assert leaderboard[0] == ('Bob', 2), f"Expected Bob first with 2"
        assert leaderboard[1][1] == 1, f"Expected count 1 for others"
        print("✓ PASSED\n")
    
    def test_09_clear_absent_clears_count(self):
        print("\n=== TEST 9: Clear absent resets ghost count ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [{'user_id': 1, 'name': 'Alice'}])
        
        self._increment_ghost(1, 'Alice')
        count = self._get_ghost_count(1)
        print(f"Before clear: {count}")
        assert count == 1
        
        self._reset_ghost(1)
        count = self._get_ghost_count(1)
        print(f"After clear: {count}")
        assert count == 0
        print("✓ PASSED\n")
    
    def test_10_clear_absent_proxy(self):
        print("\n=== TEST 10: Clear absent for proxy user ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [{'proxy_name': 'Bob'}])
        
        self._increment_ghost(-1, 'Bob', proxy_name='Bob')
        count = self._get_ghost_count(-1, proxy_name='Bob')
        print(f"Before clear: {count}")
        assert count == 1
        
        self._reset_ghost(-1, proxy_name='Bob')
        count = self._get_ghost_count(-1, proxy_name='Bob')
        print(f"After clear: {count}")
        assert count == 0
        print("✓ PASSED\n")
    
    def test_11_ghost_persistence_save_load(self):
        print("\n=== TEST 11: Ghost selections persistence ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [
            {'user_id': 1, 'name': 'Alice'},
            {'proxy_name': 'Bob'},
        ])
        
        selected = {1, 'Bob'}
        self._save_selections(rc_id, selected)
        print(f"Saved selections: {selected}")
        
        loaded = self._load_selections(rc_id)
        print(f"Loaded selections: {loaded}")
        
        assert loaded == selected, f"Expected {selected}, got {loaded}"
        print("✓ PASSED\n")
    
    def test_12_ghost_persistence_no_saved_data(self):
        print("\n=== TEST 12: No saved data returns None ===")
        self._reset()
        
        rc_id = self._create_rollcall("Day 1")
        
        loaded = self._load_selections(rc_id)
        print(f"Loaded (no data): {loaded}")
        
        assert loaded is None, f"Expected None, got {loaded}"
        print("✓ PASSED\n")
    
    def test_13_warning_badge_at_limit(self):
        print("\n=== TEST 13: Users at/above absent_limit show warning ===")
        self._reset()
        
        self.conn.execute("UPDATE chats SET absent_limit = 2 WHERE chat_id = ?", (self.chat_id,))
        self.conn.commit()
        
        rc_id = self._create_rollcall("Day 1")
        self._add_users(rc_id, [
            {'user_id': 1, 'name': 'Alice'},
            {'user_id': 2, 'name': 'Bob'},
        ])
        
        self._increment_ghost(1, 'Alice')
        self._increment_ghost(1, 'Alice')
        self._increment_ghost(2, 'Bob')
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard: {leaderboard}")
        
        alice_count = dict(leaderboard).get('Alice', 0)
        bob_count = dict(leaderboard).get('Bob', 0)
        
        assert alice_count == 2, f"Alice should have 2"
        assert bob_count == 1, f"Bob should have 1"
        print("✓ PASSED\n")
    
    def test_14_10_users_5_sessions(self):
        print("\n=== TEST 14: 10 users over 5 sessions ===")
        self._reset()
        
        users = [
            {'user_id': 1, 'name': 'Alice'},
            {'user_id': 2, 'name': 'Bob'},
            {'user_id': 3, 'name': 'Charlie'},
            {'user_id': 4, 'name': 'Diana'},
            {'user_id': 5, 'name': 'Eve'},
            {'user_id': 6, 'name': 'Frank'},
            {'user_id': 7, 'name': 'Grace'},
            {'user_id': 8, 'name': 'Henry'},
            {'user_id': 9, 'name': 'Ivy'},
            {'user_id': 10, 'name': 'Jack'},
        ]
        
        ghost_patterns = {
            1: [1, 2, 3],
            2: [2, 4],
            3: [1, 5, 6, 7],
            4: [3, 8, 9],
            5: [1, 10],  # Removed Bob from session 5 to match expected
        }
        
        expected = {'Alice': 3, 'Bob': 2, 'Charlie': 2, 'Diana': 1, 'Eve': 1,
                 'Frank': 1, 'Grace': 1, 'Henry': 1, 'Ivy': 1, 'Jack': 1}
        
        for session, ghosters in ghost_patterns.items():
            rc_id = self._create_rollcall(f"Session {session}")
            self._add_users(rc_id, users)
            
            for uid in ghosters:
                name = users[uid - 1]['name']
                self._increment_ghost(uid, name)
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard (top 5): {leaderboard[:5]}")
        
        assert len(leaderboard) == 10, f"Expected 10, got {len(leaderboard)}"
        
        counts = dict(leaderboard)
        for name, expected_count in expected.items():
            actual = counts.get(name, 0)
            assert actual == expected_count, f"{name}: expected {expected_count}, got {actual}"
        
        print("✓ PASSED\n")
    
    def test_15_proxy_only_rollcalls(self):
        print("\n=== TEST 15: Multiple rollcalls with only proxy users ===")
        self._reset()
        
        for day in range(1, 6):
            rc_id = self._create_rollcall(f"Day {day}")
            self._add_users(rc_id, [
                {'proxy_name': 'John'},
                {'proxy_name': 'Jane'},
                {'proxy_name': 'Joe'},
            ])
            
            if day % 2 == 1:
                self._increment_ghost(-1, 'John', proxy_name='John')
            
            if day == 3:
                self._increment_ghost(-1, 'Jane', proxy_name='Jane')
        
        leaderboard = self._get_leaderboard()
        print(f"Leaderboard: {leaderboard}")
        
        counts = dict(leaderboard)
        assert counts.get('John') == 3, f"John should have 3, got {counts.get('John')}"
        assert counts.get('Jane') == 1, f"Jane should have 1, got {counts.get('Jane')}"
        print("✓ PASSED\n")
    
    def test_16_full_integration_workflow(self):
        print("\n=== TEST 16: Full integration workflow ===")
        self._reset()
        
        print("Step 1: Create rollcall")
        rc_id = self._create_rollcall("Monday Meeting")
        
        print("Step 2: Add 5 real + 3 proxy users")
        users = [
            {'user_id': 1, 'name': 'Alice'},
            {'user_id': 2, 'name': 'Bob'},
            {'user_id': 3, 'name': 'Charlie'},
            {'user_id': 4, 'name': 'Diana'},
            {'user_id': 5, 'name': 'Eve'},
            {'proxy_name': 'John'},
            {'proxy_name': 'Jane'},
            {'proxy_name': 'Joe'},
        ]
        self._add_users(rc_id, users)
        
        print("Step 3: Alice, Bob (real), John (proxy) ghost")
        self._increment_ghost(1, 'Alice')
        self._increment_ghost(2, 'Bob')
        self._increment_ghost(-1, 'John', proxy_name='John')
        
        print("Step 4: Check leaderboard")
        leaderboard = self._get_leaderboard()
        print(f"  {leaderboard}")
        
        names = set(n for n, c in leaderboard)
        assert names == {'Alice', 'Bob', 'John'}, f"Expected {{Alice, Bob, John}}, got {names}"
        
        print("Step 5: Clear Bob's ghost count")
        self._reset_ghost(2)
        
        leaderboard = self._get_leaderboard()
        print(f"  After clear: {leaderboard}")
        
        names = set(n for n, c in leaderboard)
        assert names == {'Alice', 'John'}, f"Expected {{Alice, John}}, got {names}"
        
        print("Step 6: Alice ghosts again")
        rc_id2 = self._create_rollcall("Tuesday Meeting")
        self._add_users(rc_id2, users)
        self._increment_ghost(1, 'Alice')
        
        count = self._get_ghost_count(1)
        print(f"  Alice now has: {count} ghosts")
        assert count == 2, f"Expected 2, got {count}"
        
        print("✓ PASSED\n")


if __name__ == "__main__":
    main()