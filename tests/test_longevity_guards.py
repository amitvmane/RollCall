"""
Guards for a deployment that runs unattended for months.

These three defects share a shape: nothing fails, nothing logs, and the cost
is invisible on a young database. They only bite after the deployment has been
up long enough for a monotonically-growing table or an unpruned dict to get
big — which is exactly when nobody is watching it. A unit test is the only
layer that can see them, because at test time every one of these tables has
about four rows in it.

Added 2026-09-06 during the pre-freeze stability audit.
"""
import ast
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "rollCall")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestGrowingTableIndexes(unittest.TestCase):
    """Tables nothing ever prunes must be indexed on the column they're filtered by.

    ghost_events is the one that actually hurt: get_rollcall_history() counts
    ghosts with a correlated subquery evaluated once per row on the page, and
    its two siblings in that same SELECT (users, proxy_users) were indexed
    while it was not — so every /history page scanned the whole table ten
    times over, growing forever.
    """

    def setUp(self):
        self.db = _read("db.py")

    def test_ghost_events_indexed_on_rollcall_id(self):
        self.assertIn(
            "CREATE INDEX IF NOT EXISTS idx_ghost_events_rollcall ON ghost_events(rollcall_id)",
            self.db,
            "ghost_events(rollcall_id) index is gone — get_rollcall_history() "
            "goes back to a full scan per row returned, on a table that only grows.",
        )

    def test_admin_actions_indexed_on_chat_and_time(self):
        self.assertIn(
            "CREATE INDEX IF NOT EXISTS idx_admin_actions_chat_created ON admin_actions(chat_id, created_at)",
            self.db,
            "admin_actions(chat_id, created_at) index is gone — the audit log "
            "pages with WHERE chat_id ORDER BY created_at, so it needs both.",
        )

    def test_history_ghost_subquery_still_targets_the_indexed_column(self):
        """If the subquery is ever rewritten to filter ghost_events by something
        else, the index above stops covering it and silently stops helping."""
        self.assertIn(
            "FROM ghost_events g WHERE g.rollcall_id = r.id",
            self.db,
            "The ghost-count subquery changed shape — re-check that "
            "idx_ghost_events_rollcall still covers it.",
        )


class TestExpiredTokenPurge(unittest.TestCase):
    """Single-use login tokens must be reaped, not just expired.

    web_verify_tokens was purged from the start; web_direct_login_tokens
    (/weblogin, 7-day TTL) was not, so used and expired rows accumulated for
    the life of the deployment.
    """

    def setUp(self):
        self.runner = _read("runner.py")

    def test_both_token_tables_are_purged(self):
        for table in ("web_verify_tokens", "web_direct_login_tokens"):
            self.assertIn(
                f"DELETE FROM {table} WHERE expires_at <",
                self.runner,
                f"{table} is no longer purged by memory_prune_loop — dead rows "
                "will accumulate for the life of the deployment.",
            )

    def test_purge_covers_both_backends(self):
        """SQLite and Postgres need different date arithmetic; missing one means
        the purge silently no-ops on that backend."""
        self.assertIn("INTERVAL '30 days'", self.runner)
        self.assertIn("datetime('now', '-30 days')", self.runner)


class TestInMemoryStateIsBounded(unittest.TestCase):
    """Every module-level dict/set in bot_state.py must be bounded by the prune loop.

    This is a ratchet: it enumerates bot_state's module-level containers and
    fails when a new one appears that memory_prune_loop doesn't touch. The
    point is that the NEXT one gets caught at review time rather than after
    it has been leaking in production for a month, which is how
    _ghost_show_out was missed when its siblings were capped.
    """

    # Single-slot status records with a fixed set of keys — they cannot grow.
    FIXED_SIZE = {"_last_error_state", "_telegram_status"}

    def test_every_module_level_container_is_pruned(self):
        tree = ast.parse(_read("bot_state.py"))
        containers = []
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if not isinstance(t, ast.Name) or not t.id.startswith("_"):
                    continue
                if t.id.isupper() or t.id in self.FIXED_SIZE:
                    continue
                if isinstance(node.value, (ast.Dict, ast.Set)) or (
                    isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in ("dict", "set")
                ):
                    containers.append(t.id)

        self.assertTrue(containers, "found no containers — the AST walk broke, not the code")

        runner = _read("runner.py")
        prune_start = runner.index("async def memory_prune_loop")
        prune_end = runner.index("async def _post_connect_setup")
        prune_body = runner[prune_start:prune_end]

        unbounded = [name for name in containers if name not in prune_body]
        self.assertEqual(
            [], unbounded,
            "These bot_state containers are never bounded by memory_prune_loop: "
            f"{unbounded}. Each one grows for the life of the process. Either "
            "prune/cap it in the loop, or add it to FIXED_SIZE if it genuinely "
            "cannot grow.",
        )


if __name__ == "__main__":
    unittest.main()
