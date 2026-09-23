"""Findings 2-5 from the 2026-09-23 audit.

Four separate defects, one shared shape: a rule that existed was not reached
on every path to the thing it protects.

  2  the last-owner guard read the count and wrote the role in two
     transactions, so two concurrent demotions both passed it
  3  revoking a grant deleted the row without consulting that guard at all
  4  push-unsubscribe deleted by endpoint alone, so any group's public link
     reached any group's subscription
  5  the persistent view counter incremented on a value from the request body
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


# ── Finding 3: revoke must not strand a chat ──────────────────────────────────

class TestRevokeKeepsTheLastOwner(unittest.TestCase):
    """A chat with no owner cannot be administered by anyone, and cannot be
    repaired from inside the app — only owners may promote. Deleting the row
    is the same outcome as demoting, so it answers to the same rule."""

    def setUp(self):
        from services import admin as admin_svc
        self.svc = admin_svc
        self.db = patch.object(admin_svc, "db").start()
        self.addCleanup(patch.stopall)

    def test_last_owner_is_not_revoked(self):
        self.db.get_web_admin_role.return_value = "owner"
        self.db.count_web_admin_owners.return_value = 1
        self.assertFalse(self.svc.revoke_admin(-100, 7))
        self.db.revoke_web_admin.assert_not_called()

    def test_an_owner_among_several_is_revoked(self):
        self.db.get_web_admin_role.return_value = "owner"
        self.db.count_web_admin_owners.return_value = 2
        self.assertTrue(self.svc.revoke_admin(-100, 7))
        self.db.revoke_web_admin.assert_called_once_with(-100, 7)

    def test_a_plain_admin_is_always_revocable(self):
        self.db.get_web_admin_role.return_value = "admin"
        self.db.count_web_admin_owners.return_value = 1
        self.assertTrue(self.svc.revoke_admin(-100, 7))
        self.db.revoke_web_admin.assert_called_once()

    def test_the_live_check_goes_through_the_service(self):
        """Reaching db.revoke_web_admin directly bypasses the rule entirely —
        which is exactly how this path lost it. Only the service may call it."""
        root = os.path.join(os.path.dirname(__file__), "..", "rollCall")
        offenders = []
        for sub, _dirs, files in os.walk(root):
            if "__pycache__" in sub:
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(sub, name)
                rel = os.path.relpath(path, root)
                if rel in ("db.py", os.path.join("services", "admin.py")):
                    continue
                with open(path, encoding="utf-8") as fh:
                    if "revoke_web_admin(" in fh.read():
                        offenders.append(rel)
        self.assertEqual(offenders, [], f"must call services.admin.revoke_admin instead: {offenders}")


# ── Finding 2: the demote race is serialised ──────────────────────────────────

class TestDemoteIsSerialised(unittest.TestCase):
    def test_route_holds_the_chat_write_lock(self):
        """count-then-write across two transactions is only safe if the pair
        is atomic. Asserted on the route because the services are sync and
        the lock is async — this is where it has to be taken."""
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "api", "routes", "web.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        fn = body.split("async def web_set_admin_role")[1].split("\n@router")[0]
        self.assertIn("get_chat_write_lock", fn)
        self.assertLess(fn.index("get_chat_write_lock"), fn.index("promote_to_owner"),
                        "the lock must be taken before the role change, not after")

    def test_the_boot_repair_is_marked_load_bearing(self):
        """The reconciler that repairs an ownerless chat runs every boot and
        reads like a spent migration. If it is ever deleted as one, finding 2
        stops being self-healing — so the warning has to survive too."""
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "db.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("DO NOT DELETE THIS AS A SPENT MIGRATION", body)


# ── Finding 4: unsubscribe is scoped to one group ─────────────────────────────

class TestUnsubscribeIsScopedToItsGroup(unittest.TestCase):
    def setUp(self):
        from services import push as push_svc
        self.svc = push_svc
        self.db = patch.object(push_svc, "_db").start()
        self.addCleanup(patch.stopall)

    def test_group_token_reaches_the_delete(self):
        self.db.delete_push_subscription.return_value = True
        self.svc.unsubscribe("https://push.example/abc", "grouptokenA")
        self.db.delete_push_subscription.assert_called_once_with(
            "https://push.example/abc", group_token="grouptokenA")

    def test_group_token_is_required_not_optional(self):
        """Optional would let a new call site silently get the old unscoped
        behaviour back, which is the bug itself."""
        with self.assertRaises(TypeError):
            self.svc.unsubscribe("https://push.example/abc")

    def test_expired_pruning_stays_unscoped(self):
        """When the push provider says an endpoint is gone it is gone in every
        group, so the server-initiated prune must NOT be narrowed."""
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "services", "push.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        prune = body.split("for endpoint in expired:")[1][:200]
        self.assertIn("delete_push_subscription(endpoint)", prune)
        self.assertNotIn("group_token", prune)


class TestDeleteSubscriptionSql(unittest.TestCase):
    def test_group_token_is_bound_not_interpolated(self):
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "db.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        fn = src.split("def delete_push_subscription")[1].split("\ndef ")[0]
        self.assertIn("AND group_token =", fn)
        # bound through the placeholder, never formatted into the statement
        self.assertNotIn('group_token = {group_token}', fn)
        self.assertIn("params.append(group_token)", fn)


# ── Finding 5: the view counter is not client-mintable ────────────────────────

class TestViewCounterCannotBeMinted(unittest.TestCase):
    def setUp(self):
        from services import presence
        self.p = presence
        presence._sessions.clear()
        presence._counted.clear()

    def test_new_session_ids_from_one_viewer_count_once(self):
        """The attack: POST fresh UUIDs in a loop and move a public number."""
        counted = sum(
            self.p.heartbeat("tok", f"session-{i}", viewer_key="203.0.113.9")
            for i in range(500)
        )
        self.assertEqual(counted, 1)

    def test_distinct_viewers_each_count(self):
        counted = sum(
            self.p.heartbeat("tok", f"s{i}", viewer_key=f"198.51.100.{i}")
            for i in range(8)
        )
        self.assertEqual(counted, 8)

    def test_presence_still_tracks_every_tab(self):
        """Tabs are what active_now measures, so one viewer with three tabs
        open is three — narrowing the COUNTER must not narrow this."""
        for i in range(3):
            self.p.heartbeat("tok", f"tab-{i}", viewer_key="203.0.113.9")
        self.assertEqual(self.p.active_count("tok"), 3)

    def test_the_same_viewer_counts_again_after_the_window(self):
        self.assertTrue(self.p.heartbeat("tok", "s1", viewer_key="203.0.113.9"))
        old = time.time() - self.p._COUNT_WINDOW - 60
        self.p._counted["tok"]["203.0.113.9"] = old
        self.assertTrue(self.p.heartbeat("tok", "s1", viewer_key="203.0.113.9"))

    def test_viewers_are_counted_per_group(self):
        self.assertTrue(self.p.heartbeat("tokA", "s", viewer_key="203.0.113.9"))
        self.assertTrue(self.p.heartbeat("tokB", "s", viewer_key="203.0.113.9"))

    def test_missing_client_address_falls_back_not_crashes(self):
        self.assertTrue(self.p.heartbeat("tok", "s1"))
        self.assertFalse(self.p.heartbeat("tok", "s1"))

    def test_both_dicts_are_pruned_on_their_own_clocks(self):
        """_counted outlives _sessions by design. Pruning it on the session
        TTL would reset every gate after 90s and hand the inflation back."""
        self.p.heartbeat("tok", "s1", viewer_key="203.0.113.9")
        stale = time.time() - self.p._SESSION_TTL - 10
        self.p._sessions["tok"]["s1"] = stale
        self.p._counted["tok"]["203.0.113.9"] = stale
        self.p.prune()
        self.assertNotIn("tok", self.p._sessions)          # session is stale
        self.assertIn("203.0.113.9", self.p._counted["tok"])  # gate is not

    def test_route_keys_on_the_request_not_the_body(self):
        path = os.path.join(os.path.dirname(__file__), "..", "rollCall", "api", "routes", "web.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        fn = body.split("async def web_group_heartbeat")[1].split("\n@router")[0]
        self.assertIn("request.client", fn)
        self.assertIn("viewer_key=viewer_key", fn)


if __name__ == "__main__":
    unittest.main()
