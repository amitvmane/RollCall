"""
Regressions from the 2026-10-28 security pass.

Each test pins one finding so it can't quietly come back:

- heartbeat accepted any group_token, so unauthenticated callers could grow
  the in-memory presence/view-gate dicts with made-up tokens (held 12 h).
- the dues QR token's scope was bare "dues_qr" — minted in one group it opened
  another group's QR (its UPI address). Covered in test_dues_routes.py next to
  the other QR tests; this file guards the supply-chain side instead:
- requirements.lock must stay hash-pinned and every install site must require
  hashes, or a re-uploaded wheel ships silently.
- every GitHub Action must be pinned to a full commit SHA, not a movable tag.
"""

import glob
import os
import re
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "rollCall"))

os.environ.setdefault("TELEGRAM_TOKEN", "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY")

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def _client():
    from api.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestHeartbeatOnlyForRealGroups(unittest.TestCase):
    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        from services import presence
        reset_buckets_for_tests()
        self.presence = presence
        presence._sessions.clear()
        presence._counted.clear()

    def test_unknown_group_token_is_404_and_stores_nothing(self):
        with patch("api.routes.web._db.get_chat_by_group_web_token", return_value=None), \
             patch("api.routes.web._db.increment_group_view_count") as inc:
            r = _client().post("/api/v1/web/group/made-up-token/heartbeat",
                               json={"session_id": "s1"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.presence._sessions, {})
        self.assertEqual(self.presence._counted, {})
        inc.assert_not_called()

    def test_real_group_still_counts(self):
        chat = {"chat_id": -100, "group_web_token": "grp123"}
        with patch("api.routes.web._db.get_chat_by_group_web_token", return_value=chat), \
             patch("api.routes.web._db.increment_group_view_count") as inc:
            r = _client().post("/api/v1/web/group/grp123/heartbeat",
                               json={"session_id": "s1"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.presence.active_count("grp123"), 1)
        inc.assert_called_once_with("grp123")


class TestLockIsHashPinned(unittest.TestCase):
    def test_every_pin_in_the_lock_has_a_hash(self):
        with open(os.path.join(ROOT, "requirements.lock")) as fh:
            text = fh.read()
        # A requirement line followed by its continuation lines, up to the
        # next requirement or comment block.
        blocks = re.findall(r"^([A-Za-z0-9_.\-]+==[^\s\\]+)((?:\s*\\\n\s+--hash=sha256:[0-9a-f]{64})*)",
                            text, re.M)
        self.assertGreater(len(blocks), 30, "lock parse found too few pins — format changed?")
        unhashed = [pin for pin, hashes in blocks if "--hash=sha256:" not in hashes]
        self.assertEqual(unhashed, [], f"pins without hashes: {unhashed}")

    def test_every_install_from_the_lock_requires_hashes(self):
        sites = glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))
        sites += [p for p in glob.glob(os.path.join(ROOT, "*")) if os.path.basename(p).lower() == "dockerfile"]
        offenders = []
        for path in sites:
            with open(path) as fh:
                for n, line in enumerate(fh, 1):
                    if re.search(r"pip3? install\b.*-r requirements\.lock", line) and "--require-hashes" not in line:
                        offenders.append(f"{os.path.relpath(path, ROOT)}:{n}")
        self.assertEqual(offenders, [], "install the lock with --require-hashes")


class TestActionsArePinnedToCommits(unittest.TestCase):
    def test_no_action_is_referenced_by_a_movable_tag(self):
        offenders = []
        for path in glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml")):
            with open(path) as fh:
                for n, line in enumerate(fh, 1):
                    m = re.match(r"\s*-?\s*uses:\s*([^\s#]+)", line)
                    if not m or m.group(1).startswith("./"):
                        continue
                    if not re.search(r"@[0-9a-f]{40}$", m.group(1)):
                        offenders.append(f"{os.path.basename(path)}:{n} {m.group(1)}")
        self.assertEqual(offenders, [], "pin actions to a full commit SHA (# vX.Y.Z comment)")


if __name__ == "__main__":
    unittest.main()
