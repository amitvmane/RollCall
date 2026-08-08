"""
Real-SQLite tests for DB scalability Phase 2: WAL mode + get_connection()'s
per-worker-thread connection branch, and the two call sites now offloaded
through db._stats_executor (services/stats.py:bot_stats, db.get_idle_chats).

Uses unittest.IsolatedAsyncioTestCase (matching integration_tests/helpers.py's
IntegrationBase) since these specifically need to run inside a real asyncio
event loop to exercise run_in_executor.
"""

import threading
import time
import unittest

import db


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


class TestWalModeActive(unittest.TestCase):

    def test_journal_mode_is_wal(self):
        # integration_tests/conftest.py boots against a real file-backed
        # DB (not :memory:, which can't use WAL), so this reflects the
        # actual production configuration.
        row = db.db_conn.execute("PRAGMA journal_mode").fetchone()
        self.assertEqual(row[0].lower(), "wal")


class TestGetConnectionThreadSafety(unittest.TestCase):

    def test_worker_thread_gets_a_different_connection_than_main_thread(self):
        results = {}

        def _worker():
            results["conn"] = db.get_connection()
            results["thread_id"] = threading.get_ident()

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=5)

        self.assertIn("conn", results)
        self.assertIsNot(results["conn"], db.db_conn)

    def test_worker_thread_connection_can_actually_query(self):
        db.get_or_create_chat(CHAT)
        errors = []
        rows = []

        def _worker():
            try:
                conn = db.get_connection()
                cur = conn.cursor()
                cur.execute("SELECT chat_id FROM chats WHERE chat_id = ?", (CHAT,))
                rows.append(cur.fetchone())
                cur.close()
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=5)

        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chat_id"], CHAT)

    def test_worker_thread_reuses_the_same_connection_across_calls(self):
        # Long-lived per-thread, not reopened per call — matches the main
        # connection's own never-closed lifecycle (see get_connection()'s
        # docstring).
        seen = []

        def _worker():
            seen.append(db.get_connection())
            seen.append(db.get_connection())

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=5)

        self.assertEqual(len(seen), 2)
        self.assertIs(seen[0], seen[1])


class TestOffloadedBotStats(unittest.IsolatedAsyncioTestCase):

    async def test_bot_stats_offloaded_matches_direct_call(self):
        import asyncio
        from services import stats as stats_svc

        db.get_or_create_chat(CHAT)
        _mk_rollcall(CHAT, is_active=0)

        direct = stats_svc.bot_stats()
        offloaded = await asyncio.get_running_loop().run_in_executor(
            db._stats_executor, stats_svc.bot_stats
        )
        # Not asserting equality of the whole dict (wall-clock-sensitive
        # fields could tick between the two calls) — just that the
        # offloaded path returns the same shape and a sane count that
        # reflects the row just inserted.
        self.assertEqual(set(direct.keys()), set(offloaded.keys()))
        self.assertGreaterEqual(offloaded["total_rollcalls"], 1)

    async def test_get_idle_chats_offloaded_runs_without_error(self):
        import asyncio

        cutoff = "2000-01-01 00:00:00"  # far past — exercises the real query
        result = await asyncio.get_running_loop().run_in_executor(
            db._stats_executor, db.get_idle_chats, cutoff
        )
        self.assertIsInstance(result, list)

    async def test_concurrent_write_and_offloaded_read_both_succeed(self):
        # The actual point of WAL mode: a write on the main thread and an
        # offloaded read on a worker thread, concurrently, must both
        # complete without the reader blocking on / erroring from the
        # writer's transaction.
        import asyncio
        from services import stats as stats_svc

        db.get_or_create_chat(CHAT)

        async def _write():
            _mk_rollcall(CHAT, title=f"Concurrent-{time.time()}", is_active=0)
            return "write-done"

        async def _read():
            return await asyncio.get_running_loop().run_in_executor(
                db._stats_executor, stats_svc.bot_stats
            )

        write_result, read_result = await asyncio.gather(_write(), _read())
        self.assertEqual(write_result, "write-done")
        self.assertIn("total_rollcalls", read_result)
