"""
Tests for the Telegram Mini App auth endpoint.

Covers HMAC validation logic in isolation (no live bot token needed).
"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from urllib.parse import quote, urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

BOT_TOKEN = "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY"


def _make_init_data(user_id=111, chat_id=-100200, first_name="Alice",
                    username="alice", age_seconds=60, bot_token=BOT_TOKEN):
    """Build a valid-HMAC initData string for testing."""
    user_obj = json.dumps({"id": user_id, "first_name": first_name,
                           "username": username, "is_bot": False})
    chat_obj = json.dumps({"id": chat_id, "title": "Test Group", "type": "group"})

    auth_date = str(int(time.time()) - age_seconds)
    pairs = {
        "auth_date": auth_date,
        "chat":      quote(chat_obj),
        "user":      quote(user_obj),
    }

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    pairs["hash"] = sig
    return urlencode(pairs)


class TestValidateInitData(unittest.TestCase):
    """Unit-test _validate_init_data without hitting the DB."""

    def _call(self, init_data, bot_token=BOT_TOKEN):
        from api.routes.auth import _validate_init_data
        return _validate_init_data(init_data, bot_token)

    def test_valid_init_data_returns_pairs(self):
        data = _make_init_data()
        pairs = self._call(data)
        self.assertIn("auth_date", pairs)
        self.assertIn("user", pairs)

    def test_missing_hash_raises(self):
        data = "auth_date=1234567890&user=%7B%22id%22%3A1%7D"
        with self.assertRaises(ValueError, msg="Missing hash"):
            self._call(data)

    def test_wrong_hash_raises(self):
        data = _make_init_data() + "&hash=deadbeef"
        # Replace correct hash with garbage
        pairs = dict(p.split("=", 1) for p in data.split("&") if "=" in p)
        pairs["hash"] = "0" * 64
        bad = "&".join(f"{k}={v}" for k, v in pairs.items())
        with self.assertRaises(ValueError):
            self._call(bad)

    def test_stale_init_data_raises(self):
        data = _make_init_data(age_seconds=90_000)  # 25 h old
        with self.assertRaises(ValueError, msg="older than 24 hours"):
            self._call(data)

    def test_wrong_bot_token_raises(self):
        data = _make_init_data(bot_token=BOT_TOKEN)
        with self.assertRaises(ValueError):
            self._call(data, bot_token="999:WRONG_TOKEN")


class TestExtractIds(unittest.TestCase):
    """Unit-test _extract_ids."""

    def _call(self, pairs):
        from api.routes.auth import _extract_ids
        return _extract_ids(pairs)

    def test_extracts_user_and_chat_ids(self):
        user_obj = json.dumps({"id": 42, "first_name": "Bob"})
        chat_obj = json.dumps({"id": -100300, "title": "Group"})
        pairs = {"user": quote(user_obj), "chat": quote(chat_obj)}
        uid, cid = self._call(pairs)
        self.assertEqual(uid, 42)
        self.assertEqual(cid, -100300)

    def test_private_chat_fallback_to_receiver(self):
        user_obj = json.dumps({"id": 7, "first_name": "X"})
        receiver_obj = json.dumps({"id": 7, "first_name": "X"})
        pairs = {"user": quote(user_obj), "receiver": quote(receiver_obj)}
        uid, cid = self._call(pairs)
        self.assertEqual(uid, 7)
        self.assertEqual(cid, 7)

    def test_fallback_uses_user_id_when_no_chat(self):
        user_obj = json.dumps({"id": 99, "first_name": "Y"})
        pairs = {"user": quote(user_obj)}
        uid, cid = self._call(pairs)
        self.assertEqual(uid, 99)
        self.assertEqual(cid, 99)

    def test_missing_user_raises(self):
        chat_obj = json.dumps({"id": -1, "title": "G"})
        with self.assertRaises(ValueError):
            self._call({"chat": quote(chat_obj)})


if __name__ == "__main__":
    unittest.main()
