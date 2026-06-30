"""
Tests for scripts/merge_db.py — merging a recovered history DB into the live DB.

Uses the real table schema (dumped from a production SQLite DB) so the
schema-driven column intersection and the re-ID / aggregate logic are exercised
against faithful structures. The merge script uses only stdlib sqlite3, so it
is independent of the (mocked) bot `db` module.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import merge_db  # noqa: E402


SCHEMA = """
CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, shh_mode INTEGER DEFAULT 0,
  admin_rights INTEGER DEFAULT 0, timezone TEXT DEFAULT 'Asia/Calcutta',
  absent_limit INTEGER DEFAULT 1, ghost_tracking_enabled INTEGER DEFAULT 1,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, group_web_token TEXT, group_name TEXT);
CREATE TABLE rollcalls (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
  title TEXT, in_list_limit INTEGER, reminder_hours INTEGER, finalize_date TIMESTAMP,
  timezone TEXT, location TEXT, event_fee TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  is_active INTEGER DEFAULT 1, ended_at TIMESTAMP, absent_marked INTEGER DEFAULT 0,
  panel_msg_id INTEGER, web_token TEXT,
  FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE);
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, rollcall_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL, first_name TEXT, username TEXT, status TEXT NOT NULL, comment TEXT,
  in_pos INTEGER, out_pos INTEGER, wait_pos INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE, UNIQUE(rollcall_id, user_id));
CREATE TABLE proxy_users (id INTEGER PRIMARY KEY AUTOINCREMENT, rollcall_id INTEGER NOT NULL,
  name TEXT NOT NULL, status TEXT NOT NULL, comment TEXT, proxy_owner_id INTEGER,
  in_pos INTEGER, out_pos INTEGER, wait_pos INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE, UNIQUE(rollcall_id, name));
CREATE TABLE rollcall_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, rollcall_id INTEGER NOT NULL,
  total_in INTEGER DEFAULT 0, total_out INTEGER DEFAULT 0, total_maybe INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE, UNIQUE(rollcall_id));
CREATE TABLE user_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL, total_in INTEGER DEFAULT 0, total_out INTEGER DEFAULT 0,
  total_maybe INTEGER DEFAULT 0, total_waiting_to_in INTEGER DEFAULT 0, total_rollcalls INTEGER DEFAULT 0,
  total_response_seconds INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0, current_streak INTEGER DEFAULT 0,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(chat_id, user_id));
CREATE TABLE proxy_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
  proxy_name TEXT NOT NULL, total_in INTEGER DEFAULT 0, total_out INTEGER DEFAULT 0,
  total_maybe INTEGER DEFAULT 0, total_rollcalls INTEGER DEFAULT 0, best_streak INTEGER DEFAULT 0,
  current_streak INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, UNIQUE(chat_id, proxy_name));
CREATE TABLE ghost_records (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL DEFAULT -1, proxy_name TEXT, user_name TEXT, ghost_count INTEGER DEFAULT 0,
  last_ghosted_at TIMESTAMP);
CREATE TABLE ghost_events (id INTEGER PRIMARY KEY AUTOINCREMENT, rollcall_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL, user_id INTEGER, proxy_name TEXT, user_name TEXT,
  ghosted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE);
CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT, chatid INTEGER NOT NULL, name TEXT NOT NULL,
  title TEXT, inlistlimit INTEGER, location TEXT, eventfee TEXT, offsetdays INTEGER, offsethours INTEGER,
  offsetminutes INTEGER, event_day TEXT, event_time TEXT, createdat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  schedule_day TEXT, schedule_time TEXT, schedule_enabled TEXT DEFAULT 0, last_scheduled_date TEXT,
  recurrence_type TEXT DEFAULT 'weekly', UNIQUE(chatid, name));
CREATE TABLE chat_members (chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, first_name TEXT,
  username TEXT, is_active INTEGER DEFAULT 1, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (chat_id, user_id));
CREATE TABLE admin_actions (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
  admin_id INTEGER NOT NULL, admin_name TEXT, action_type TEXT NOT NULL, target_name TEXT,
  rollcall_id INTEGER, details TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE web_view_stats (group_token TEXT PRIMARY KEY, view_count INTEGER NOT NULL DEFAULT 0,
  last_viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
"""


def _new_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def _exec(path, sql, params=()):
    conn = sqlite3.connect(path)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


class TestMergeDb(unittest.TestCase):
    def setUp(self):
        self.current = _new_db()
        self.history = _new_db()
        self.output = tempfile.mktemp(suffix=".db")

        # ── CURRENT (live, post-failure) ──────────────────────────────────────
        _exec(self.current, "INSERT INTO chats (chat_id, group_web_token) VALUES (100, 'cur100')")
        _exec(self.current, "INSERT INTO rollcalls (id, chat_id, title) VALUES (1, 100, 'Cur RC')")
        _exec(self.current, "INSERT INTO users (rollcall_id, user_id, first_name, status) VALUES (1, 10, 'Alice', 'in')")
        _exec(self.current, "INSERT INTO user_stats (chat_id, user_id, total_in, best_streak, current_streak) VALUES (100, 10, 5, 3, 2)")
        _exec(self.current, "INSERT INTO templates (chatid, name, title) VALUES (100, 'weekly', 'Current Weekly')")
        _exec(self.current, "INSERT INTO admin_actions (chat_id, admin_id, action_type) VALUES (100, 1, 'set_limit')")
        _exec(self.current, "INSERT INTO web_view_stats (group_token, view_count) VALUES ('cur100', 40)")

        # ── HISTORY (recovered, pre-failure) ──────────────────────────────────
        _exec(self.history, "INSERT INTO chats (chat_id, group_web_token) VALUES (100, 'old100')")
        _exec(self.history, "INSERT INTO chats (chat_id, group_web_token) VALUES (200, 'old200')")
        # ids 1 & 2 collide with current's id 1 — must be re-IDed
        _exec(self.history, "INSERT INTO rollcalls (id, chat_id, title) VALUES (1, 100, 'Old RC1')")
        _exec(self.history, "INSERT INTO rollcalls (id, chat_id, title) VALUES (2, 200, 'Old RC2')")
        _exec(self.history, "INSERT INTO users (rollcall_id, user_id, first_name, status) VALUES (1, 10, 'Alice', 'in')")
        _exec(self.history, "INSERT INTO users (rollcall_id, user_id, first_name, status) VALUES (2, 20, 'Bob', 'in')")
        _exec(self.history, "INSERT INTO proxy_users (rollcall_id, name, status) VALUES (1, 'Guest', 'in')")
        _exec(self.history, "INSERT INTO rollcall_stats (rollcall_id, total_in) VALUES (1, 2)")
        _exec(self.history, "INSERT INTO ghost_events (rollcall_id, chat_id, user_id, user_name) VALUES (2, 200, 20, 'Bob')")
        # aggregates: Alice overlaps (summed), Bob is new
        _exec(self.history, "INSERT INTO user_stats (chat_id, user_id, total_in, best_streak, current_streak) VALUES (100, 10, 7, 9, 4)")
        _exec(self.history, "INSERT INTO user_stats (chat_id, user_id, total_in, best_streak, current_streak) VALUES (200, 20, 3, 1, 1)")
        _exec(self.history, "INSERT INTO proxy_stats (chat_id, proxy_name, total_in, best_streak) VALUES (200, 'Guest', 4, 2)")
        _exec(self.history, "INSERT INTO ghost_records (chat_id, user_id, ghost_count) VALUES (100, 10, 2)")
        # template conflict on (100,'weekly') — current must win
        _exec(self.history, "INSERT INTO templates (chatid, name, title) VALUES (100, 'weekly', 'Old Weekly')")
        _exec(self.history, "INSERT INTO templates (chatid, name, title) VALUES (200, 'evt', 'Old Event')")
        _exec(self.history, "INSERT INTO admin_actions (chat_id, admin_id, action_type) VALUES (100, 1, 'old_a')")
        _exec(self.history, "INSERT INTO admin_actions (chat_id, admin_id, action_type) VALUES (200, 2, 'old_b')")
        _exec(self.history, "INSERT INTO web_view_stats (group_token, view_count) VALUES ('cur100', 10)")

    def tearDown(self):
        for p in (self.current, self.history, self.output):
            if p and os.path.exists(p):
                os.unlink(p)

    def _q(self, sql, params=()):
        conn = sqlite3.connect(self.output)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    def test_full_merge(self):
        merge_db.merge(self.current, self.history, self.output, dry_run=False)
        self.assertTrue(os.path.exists(self.output))

        # rollcalls: 1 current + 2 history = 3, all with distinct ids
        self.assertEqual(self._q("SELECT COUNT(*) FROM rollcalls")[0][0], 3)
        self.assertEqual(self._q("SELECT COUNT(DISTINCT id) FROM rollcalls")[0][0], 3)

        # No orphan children — every FK resolves (re-ID worked)
        for tbl in ("users", "proxy_users", "rollcall_stats", "ghost_events"):
            orphans = self._q(
                f"SELECT COUNT(*) FROM {tbl} WHERE rollcall_id NOT IN (SELECT id FROM rollcalls)"
            )[0][0]
            self.assertEqual(orphans, 0, f"{tbl} has orphaned rollcall_id")

        # history's Bob references a REMAPPED rollcall (not the literal old id 2)
        bob_rc = self._q("SELECT rollcall_id FROM users WHERE user_id = 20")[0][0]
        bob_chat = self._q("SELECT chat_id FROM rollcalls WHERE id = ?", (bob_rc,))[0][0]
        self.assertEqual(bob_chat, 200)

        # chats: 200 brought in; 100 keeps the LIVE token
        self.assertEqual(self._q("SELECT COUNT(*) FROM chats")[0][0], 2)
        self.assertEqual(self._q("SELECT group_web_token FROM chats WHERE chat_id=100")[0][0], "cur100")

        # user_stats Alice (overlap): total_in summed 5+7=12, best_streak max=9, current_streak kept=2
        a = self._q("SELECT total_in, best_streak, current_streak FROM user_stats WHERE chat_id=100 AND user_id=10")[0]
        self.assertEqual(tuple(a), (12, 9, 2))
        # Bob (new): inserted as-is
        self.assertEqual(self._q("SELECT total_in FROM user_stats WHERE chat_id=200 AND user_id=20")[0][0], 3)

        # proxy_stats new row inserted
        self.assertEqual(self._q("SELECT total_in FROM proxy_stats WHERE chat_id=200 AND proxy_name='Guest'")[0][0], 4)

        # ghost_records new row inserted
        self.assertEqual(self._q("SELECT ghost_count FROM ghost_records WHERE chat_id=100 AND user_id=10")[0][0], 2)

        # templates: live (100,'weekly') wins; (200,'evt') added → 2 total
        self.assertEqual(self._q("SELECT title FROM templates WHERE chatid=100 AND name='weekly'")[0][0], "Current Weekly")
        self.assertEqual(self._q("SELECT COUNT(*) FROM templates")[0][0], 2)

        # admin_actions appended: 1 + 2 = 3
        self.assertEqual(self._q("SELECT COUNT(*) FROM admin_actions")[0][0], 3)

        # web_view_stats summed for shared token
        self.assertEqual(self._q("SELECT view_count FROM web_view_stats WHERE group_token='cur100'")[0][0], 50)

        # integrity
        self.assertEqual(self._q("PRAGMA integrity_check")[0][0], "ok")

    def test_inputs_untouched(self):
        before_cur = sqlite3.connect(self.current).execute("SELECT COUNT(*) FROM rollcalls").fetchone()[0]
        before_hist = sqlite3.connect(self.history).execute("SELECT COUNT(*) FROM rollcalls").fetchone()[0]
        merge_db.merge(self.current, self.history, self.output, dry_run=False)
        self.assertEqual(sqlite3.connect(self.current).execute("SELECT COUNT(*) FROM rollcalls").fetchone()[0], before_cur)
        self.assertEqual(sqlite3.connect(self.history).execute("SELECT COUNT(*) FROM rollcalls").fetchone()[0], before_hist)

    def test_dry_run_writes_nothing(self):
        merge_db.merge(self.current, self.history, self.output, dry_run=True)
        self.assertFalse(os.path.exists(self.output))


if __name__ == "__main__":
    unittest.main()
