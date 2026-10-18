"""
One browser, several groups — web push per (endpoint, group).

`push_subscriptions.endpoint` used to be UNIQUE on its own, and a browser has
exactly one push endpoint. So turning on the bell in group B re-pointed the
row away from group A — A silently stopped notifying while its bell (which
read browser permission, not the server) still showed ON. Turning a bell off
also called the browser-level unsubscribe, killing every group at once.
"""
import os
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mock_helpers import reset_db

CHAT_A, CHAT_B = -1001999000910, -1001999000911
EP = "https://push.example.com/one-browser-0001"
UID = 7101


def _import():
    import bot_state  # noqa: F401  warm conftest mocks
    from api.main import app
    import db
    return app, db


class TestOneBrowserManyGroups(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app, cls.db = _import()
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.enterContext(patch.dict(
            os.environ, {"TELEGRAM_TOKEN": "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"}))
        self.a = self.db.get_or_create_chat(CHAT_A)["group_web_token"]
        self.b = self.db.get_or_create_chat(CHAT_B)["group_web_token"]

    def _post(self, token, action, **body):
        return self.client.post(f"/api/v1/web/group/{token}/{action}", json={"endpoint": EP, **body})

    def _subscribe(self, token, **extra):
        return self._post(token, "push-subscribe",
                          keys={"p256dh": "p256dh-key-value", "auth": "auth-key-value"}, **extra)

    def _on(self, token):
        r = self._post(token, "push-status")
        self.assertEqual(r.status_code, 200)
        return r.json()["subscribed"]

    def test_second_group_does_not_steal_the_first(self):
        self._subscribe(self.a)
        self._subscribe(self.b)
        self.assertEqual(len(self.db.get_push_subscriptions(self.a)), 1)
        self.assertEqual(len(self.db.get_push_subscriptions(self.b)), 1)
        self.assertTrue(self._on(self.a))
        self.assertTrue(self._on(self.b))

    def test_turning_one_group_off_leaves_the_other_on(self):
        self._subscribe(self.a)
        self._subscribe(self.b)
        self.assertEqual(self._post(self.a, "push-unsubscribe").status_code, 204)
        self.assertFalse(self._on(self.a))
        self.assertTrue(self._on(self.b))
        self._subscribe(self.a)                       # and back on again
        self.assertTrue(self._on(self.a))

    def test_status_for_unknown_group_is_404(self):
        self.assertEqual(self._post("not-a-real-token", "push-status").status_code, 404)

    def test_relink_attaches_identity_in_every_group(self):
        from api.identity import issue_identity_token
        self._subscribe(self.a)
        self._subscribe(self.b)
        r = self.client.post("/api/v1/web/push-relink", json={"endpoint": EP},
                             headers={"X-Identity-Token": issue_identity_token(UID)})
        self.assertEqual(r.status_code, 204)
        for token in (self.a, self.b):
            self.assertEqual([s["tg_user_id"] for s in self.db.get_push_subscriptions(token)], [UID])

    def test_relink_needs_a_real_identity(self):
        self._subscribe(self.a)
        r = self.client.post("/api/v1/web/push-relink", json={"endpoint": EP},
                             headers={"X-Identity-Token": "forged.123.sig"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual([s["tg_user_id"] for s in self.db.get_push_subscriptions(self.a)], [None])

    def test_relink_does_not_reassign_a_linked_device(self):
        from api.identity import issue_identity_token
        self._subscribe(self.a, id_token=issue_identity_token(UID))
        self.client.post("/api/v1/web/push-relink", json={"endpoint": EP},
                         headers={"X-Identity-Token": issue_identity_token(UID + 1)})
        self.assertEqual([s["tg_user_id"] for s in self.db.get_push_subscriptions(self.a)], [UID])


class TestSqliteMigrationFromPerBrowserSchema(unittest.TestCase):
    """The live SQLite table has endpoint UNIQUE inline; SQLite can't drop that,
    so the migration rebuilds. Pinned to sqlite on purpose — it runs on its own
    connection, so the Postgres CI job exercises it too without a PG server."""

    OLD = """CREATE TABLE push_subscriptions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        group_token TEXT NOT NULL,
        endpoint   TEXT NOT NULL UNIQUE,
        p256dh     TEXT NOT NULL,
        auth       TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        active     INTEGER NOT NULL DEFAULT 1,
        tg_user_id INTEGER DEFAULT NULL
    )"""

    def setUp(self):
        _, self.db = _import()
        self.conn = sqlite3.connect(":memory:")
        self.cur = self.conn.cursor()
        self.cur.execute(self.OLD)
        self.cur.executemany(
            "INSERT INTO push_subscriptions (group_token, endpoint, p256dh, auth, active, tg_user_id) "
            "VALUES (?, ?, 'p', 'a', ?, ?)",
            [("tokA", "https://e/1", 1, 42), ("tokB", "https://e/2", 0, None)])
        self.conn.commit()
        self.enterContext(patch.object(self.db, "db_type", "sqlite"))

    def tearDown(self):
        self.conn.close()

    def _migrate(self):
        self.db._migrate_push_subscriptions_per_group(self.conn, self.cur)

    def test_rows_survive_and_a_second_group_is_allowed(self):
        self._migrate()
        self.cur.execute("SELECT group_token, endpoint, active, tg_user_id FROM push_subscriptions ORDER BY id")
        self.assertEqual(self.cur.fetchall(),
                         [("tokA", "https://e/1", 1, 42), ("tokB", "https://e/2", 0, None)])
        self.cur.execute("INSERT INTO push_subscriptions (group_token, endpoint, p256dh, auth) "
                         "VALUES ('tokB', 'https://e/1', 'p', 'a')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.cur.execute("INSERT INTO push_subscriptions (group_token, endpoint, p256dh, auth) "
                             "VALUES ('tokA', 'https://e/1', 'p', 'a')")

    def test_idempotent(self):
        self._migrate()
        self._migrate()
        self.cur.execute("SELECT COUNT(*) FROM push_subscriptions")
        self.assertEqual(self.cur.fetchone()[0], 2)

    def test_leftover_half_built_table_is_discarded(self):
        self.cur.execute("CREATE TABLE push_subscriptions_new (id INTEGER, junk TEXT)")
        self.cur.execute("INSERT INTO push_subscriptions_new VALUES (99, 'x')")
        self._migrate()
        self.cur.execute("SELECT COUNT(*) FROM push_subscriptions")
        self.assertEqual(self.cur.fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
