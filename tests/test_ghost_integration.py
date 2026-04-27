"""
Integration tests for Ghost Tracking
Uses real SQLite database (not mocked) to test production code paths
"""

import sys
import os
import unittest
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


class TestGhostIntegrationSQLite(unittest.TestCase):
    """Integration tests using real SQLite database"""

    @classmethod
    def setUpClass(cls):
        import importlib
        import db as db_module
        
        for mod_name in list(sys.modules.keys()):
            if mod_name == 'db' or mod_name.startswith('db.'):
                del sys.modules[mod_name]
        
        import db
        cls.db = db
        
        db_path = tempfile.mktemp(suffix='_integration.db')
        cls.db_path = db_path
        
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
        
        db.db_type = 'sqlite'
        db.db_conn = db.sqlite3.connect(db_path)
        db.db_conn.row_factory = db.sqlite3.Row
        
        db.create_tables()
        
        if hasattr(db, 'create_ghost_selections_table'):
            db.create_ghost_selections_table()
        
        cls.chat_id = 168415137
        
        cursor = db.db_conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO chats (chat_id, absent_limit) VALUES (?, 1)", (cls.chat_id,))
        db.db_conn.commit()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.db_conn.close()
        except:
            pass
        try:
            os.unlink(cls.db_path)
        except:
            pass

    def setUp(self):
        self.db.db_conn.execute("DELETE FROM ghost_records")
        self.db.db_conn.execute("DELETE FROM ghost_events")
        self.db.db_conn.execute("DELETE FROM ghost_selections")
        self.db.db_conn.execute("DELETE FROM rollcalls")
        self.db.db_conn.execute("DELETE FROM users")
        self.db.db_conn.execute("DELETE FROM proxy_users")
        self.db.db_conn.commit()

    def test_save_ghost_selections_sqlite(self):
        """Test: Save ghost selections to SQLite"""
        print("\n=== TEST: Save ghost selections (SQLite) ===")
        
        result = self.db.save_ghost_selections(self.chat_id, 1, {'user1', 'user2'})
        print(f"Save result: {result}")
        
        self.assertTrue(result)
        
        loaded = self.db.load_ghost_selections(self.chat_id, 1)
        print(f"Loaded: {loaded}")
        
        self.assertEqual(loaded, {'user1', 'user2'})
        print("✓ PASSED\n")

    def test_load_ghost_selections_none(self):
        """Test: Load ghost selections when none exist"""
        print("\n=== TEST: Load ghost selections (none) ===")
        
        loaded = self.db.load_ghost_selections(self.chat_id, 999)
        print(f"Loaded: {loaded}")
        
        self.assertIsNone(loaded)
        print("✓ PASSED\n")

    def test_increment_ghost_count_real_user_sqlite(self):
        """Test: Increment ghost count for real user in SQLite"""
        print("\n=== TEST: Increment ghost count (real user, SQLite) ===")
        
        result = self.db.increment_ghost_count(self.chat_id, 1, 'Alice')
        print(f"Increment result: {result}")
        
        count = self.db.get_ghost_count(self.chat_id, 1)
        print(f"Ghost count: {count}")
        
        self.assertEqual(count, 1)
        self.assertTrue(result)
        print("✓ PASSED\n")

    def test_increment_ghost_count_proxy_user_sqlite(self):
        """Test: Increment ghost count for proxy user in SQLite"""
        print("\n=== TEST: Increment ghost count (proxy user, SQLite) ===")
        
        result = self.db.increment_ghost_count(self.chat_id, -1, 'Bob', proxy_name='Bob')
        print(f"Increment result: {result}")
        
        count = self.db.get_ghost_count_by_proxy_name(self.chat_id, 'Bob')
        print(f"Ghost count: {count}")
        
        self.assertEqual(count, 1)
        self.assertTrue(result)
        print("✓ PASSED\n")

    def test_increment_ghost_count_increments(self):
        """Test: Ghost count increments correctly"""
        print("\n=== TEST: Ghost count increments ===")
        
        for _ in range(3):
            self.db.increment_ghost_count(self.chat_id, 1, 'Alice')
        
        count = self.db.get_ghost_count(self.chat_id, 1)
        print(f"Ghost count after 3x: {count}")
        
        self.assertEqual(count, 3)
        print("✓ PASSED\n")

    def test_ghost_leaderboard_sqlite(self):
        """Test: Ghost leaderboard in SQLite"""
        print("\n=== TEST: Ghost leaderboard (SQLite) ===")
        
        self.db.increment_ghost_count(self.chat_id, 1, 'Alice')
        self.db.increment_ghost_count(self.chat_id, 2, 'Bob')
        self.db.increment_ghost_count(self.chat_id, 2, 'Bob')
        
        leaderboard = self.db.get_ghost_leaderboard(self.chat_id)
        print(f"Leaderboard: {[(e.get('user_name') or e.get('proxy_name'), e['ghost_count']) for e in leaderboard]}")
        
        self.assertEqual(len(leaderboard), 2)
        
        counts = {e.get('user_name'): e['ghost_count'] for e in leaderboard}
        self.assertEqual(counts.get('Alice'), 1)
        self.assertEqual(counts.get('Bob'), 2)
        print("✓ PASSED\n")

    def test_reset_ghost_count_sqlite(self):
        """Test: Reset ghost count in SQLite"""
        print("\n=== TEST: Reset ghost count (SQLite) ===")
        
        self.db.increment_ghost_count(self.chat_id, 1, 'Alice')
        
        count = self.db.get_ghost_count(self.chat_id, 1)
        print(f"Before reset: {count}")
        self.assertEqual(count, 1)
        
        result = self.db.reset_ghost_count(self.chat_id, 1)
        print(f"Reset result: {result}")
        
        count = self.db.get_ghost_count(self.chat_id, 1)
        print(f"After reset: {count}")
        
        self.assertEqual(count, 0)
        print("✓ PASSED\n")

    def test_reset_ghost_count_proxy_sqlite(self):
        """Test: Reset ghost count for proxy user in SQLite"""
        print("\n=== TEST: Reset ghost count (proxy user, SQLite) ===")
        
        self.db.increment_ghost_count(self.chat_id, -1, 'Bob', proxy_name='Bob')
        
        count = self.db.get_ghost_count_by_proxy_name(self.chat_id, 'Bob')
        print(f"Before reset: {count}")
        self.assertEqual(count, 1)
        
        result = self.db.reset_ghost_count(self.chat_id, -1, proxy_name='Bob')
        print(f"Reset result: {result}")
        
        count = self.db.get_ghost_count_by_proxy_name(self.chat_id, 'Bob')
        print(f"After reset: {count}")
        
        self.assertEqual(count, 0)
        print("✓ PASSED\n")

    def test_mixed_real_and_proxy_ghost(self):
        """Test: Mixed real and proxy users"""
        print("\n=== TEST: Mixed real and proxy ===")
        
        self.db.increment_ghost_count(self.chat_id, 1, 'Alice')
        self.db.increment_ghost_count(self.chat_id, -1, 'Bob', proxy_name='Bob')
        
        leaderboard = self.db.get_ghost_leaderboard(self.chat_id)
        print(f"Leaderboard: {[(e.get('user_name') or e.get('proxy_name'), e['ghost_count']) for e in leaderboard]}")
        
        self.assertEqual(len(leaderboard), 2)
        
        names = {e.get('user_name') or e.get('proxy_name') for e in leaderboard}
        self.assertEqual(names, {'Alice', 'Bob'})
        print("✓ PASSED\n")

    def test_end_to_end_ghost_tracking_session(self):
        """Test: Complete ghost tracking session"""
        print("\n=== TEST: End-to-end ghost tracking ===")
        
        cursor = self.db.db_conn.cursor()
        
        rc_id = 1
        cursor.execute(
            "INSERT INTO rollcalls (chat_id, title, is_active) VALUES (?, 'Test RC', 1)",
            (self.chat_id,)
        )
        self.db.db_conn.commit()
        
        users = [
            {'user_id': 1, 'name': 'Alice', 'username': 'alice'},
            {'user_id': 2, 'name': 'Bob', 'username': 'bob'},
        ]
        
        for u in users:
            cursor.execute(
                "INSERT INTO users (rollcall_id, user_id, first_name, username, status, in_pos) VALUES (?, ?, ?, ?, 'in', ?)",
                (rc_id, u['user_id'], u['name'], u.get('username'), u['user_id'])
            )
        self.db.db_conn.commit()
        
        self.db.increment_ghost_count(self.chat_id, 1, 'Alice')
        self.db.add_ghost_event(rc_id, self.chat_id, 1, 'Alice')
        
        self.db.increment_ghost_count(self.chat_id, 2, 'Bob')
        self.db.add_ghost_event(rc_id, self.chat_id, 2, 'Bob')
        
        leaderboard = self.db.get_ghost_leaderboard(self.chat_id)
        print(f"Final leaderboard: {[(e['user_name'], e['ghost_count']) for e in leaderboard]}")
        
        self.assertEqual(len(leaderboard), 2)
        
        self.db.reset_ghost_count(self.chat_id, 1)
        
        leaderboard = self.db.get_ghost_leaderboard(self.chat_id)
        names = {e['user_name'] for e in leaderboard}
        self.assertEqual(names, {'Bob'})
        print("✓ PASSED\n")


class TestRollCallIntegrationSQLite(unittest.TestCase):
    """Integration tests for full rollcall flow with SQLite"""

    @classmethod
    def setUpClass(cls):
        import db
        
        db_path = tempfile.mktemp(suffix='_rc_integration.db')
        cls.db_path = db_path
        
        os.environ['DATABASE_URL'] = f'sqlite:///{db_path}'
        
        db.db_type = 'sqlite'
        db.db_conn = db.sqlite3.connect(db_path)
        db.db_conn.row_factory = db.sqlite3.Row
        
        db.create_tables()
        
        cls.chat_id = 168415137
        cls.admin_id = 999
        
        cursor = db.db_conn.cursor()
        cursor.execute("INSERT INTO chats (chat_id, absent_limit, ghost_tracking_enabled) VALUES (?, 1, 1)", (cls.chat_id,))
        db.db_conn.commit()

    @classmethod
    def tearDownClass(cls):
        try:
            db.db_conn.close()
        except:
            pass
        try:
            os.unlink(cls.db_path)
        except:
            pass

    def test_create_and_end_rollcall(self):
        """Test: Create and end rollcall"""
        print("\n=== TEST: Create and end rollcall ===")
        
        import db
        
        rc_id = db.create_rollcall(self.chat_id, "Test Session")
        print(f"Created RC ID: {rc_id}")
        
        self.assertIsNotNone(rc_id)
        
        rc = db.get_rollcall(rc_id)
        print(f"Rollcall: {rc}")
        
        self.assertIsNotNone(rc)
        
        db.end_rollcall(rc_id)
        
        rc = db.get_rollcall(rc_id)
        print(f"After end: {rc['ended_at']}")
        
        self.assertIsNotNone(rc['ended_at'])
        print("✓ PASSED\n")

    def test_add_users_and_ghost(self):
        """Test: Add users, mark some as ghosts"""
        print("\n=== TEST: Add users and mark ghosts ===")
        
        import db
        
        rc_id = db.create_rollcall(self.chat_id, "Test Session")
        
        db.add_or_update_user(rc_id, 1, "Alice", "alice", "in", None)
        db.add_or_update_user(rc_id, 2, "Bob", "bob", "in", None)
        
        in_users = db.get_rollcall_in_users(rc_id)
        print(f"IN users: {len(in_users)}")
        
        self.assertEqual(len(in_users), 2)
        
        db.increment_ghost_count(self.chat_id, 1, "Alice")
        
        leaderboard = db.get_ghost_leaderboard(self.chat_id)
        print(f"Ghost leaderboard: {[(e['user_name'], e['ghost_count']) for e in leaderboard]}")
        
        self.assertEqual(len(leaderboard), 1)
        print("✓ PASSED\n")


if __name__ == "__main__":
    print("=" * 70)
    print("INTEGRATION TESTS (Real SQLite)")
    print("=" * 70)
    unittest.main(verbosity=2)