"""
Unit tests for services/push.py — web-push fan-out.

Regression focus: expired subscriptions must be pruned from the DB on the
event-loop thread, never inside a thread-pool worker. The shared SQLite
connection is single-threaded, so a DB write from a webpush worker thread
could corrupt or interleave with the bot's own DB access.

Patching strategy:
  - services.push._db.delete_push_subscription → record call + calling thread
  - pywebpush.webpush (imported inside _send_one) → simulate ok / 410-expired
"""

import asyncio
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import services.push as push  # noqa: E402


def _sub(endpoint):
    return {"endpoint": endpoint, "p256dh": "p", "auth": "a"}


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class TestSendOne(unittest.TestCase):
    """_send_one returns a status and never touches the DB."""

    def test_ok_delivery_returns_ok_no_db(self):
        with patch.object(push._db, "delete_push_subscription") as del_mock, \
             patch("pywebpush.webpush", return_value=None):
            result = push._send_one(_sub("e1"), "{}", "pem")
        self.assertEqual(result, "ok")
        del_mock.assert_not_called()

    def test_expired_reports_endpoint_no_db(self):
        from pywebpush import WebPushException
        exc = WebPushException("gone")
        exc.response = _Resp(410)
        with patch.object(push._db, "delete_push_subscription") as del_mock, \
             patch("pywebpush.webpush", side_effect=exc):
            result = push._send_one(_sub("dead"), "{}", "pem")
        # Worker reports the expiry but must NOT delete from the DB itself.
        self.assertEqual(result, ("expired", "dead"))
        del_mock.assert_not_called()

    def test_other_failure_returns_failed(self):
        from pywebpush import WebPushException
        exc = WebPushException("boom")
        exc.response = _Resp(500)
        with patch.object(push._db, "delete_push_subscription"), \
             patch("pywebpush.webpush", side_effect=exc):
            result = push._send_one(_sub("e2"), "{}", "pem")
        self.assertEqual(result, "failed")


class TestDispatch(unittest.TestCase):
    """_dispatch prunes expired endpoints on the event-loop thread only."""

    def test_expired_pruned_on_event_loop_thread(self):
        main_thread = threading.current_thread().ident
        delete_threads = []

        def _record(endpoint):
            delete_threads.append((endpoint, threading.current_thread().ident))

        # Two subs: one delivers, one is expired (410).
        from pywebpush import WebPushException
        exc = WebPushException("gone")
        exc.response = _Resp(410)

        def _webpush(*, subscription_info, **_kw):
            if subscription_info["endpoint"] == "dead":
                raise exc
            return None

        with patch.object(push._db, "delete_push_subscription", side_effect=_record), \
             patch("pywebpush.webpush", side_effect=_webpush):
            sent = asyncio.run(
                push._dispatch([_sub("live"), _sub("dead")], "{}", "pem")
            )

        self.assertEqual(sent, 1)  # only "live" delivered
        self.assertEqual([ep for ep, _ in delete_threads], ["dead"])
        # The delete must run on the event-loop (main) thread, not a worker.
        self.assertEqual(delete_threads[0][1], main_thread)


if __name__ == "__main__":
    unittest.main()
