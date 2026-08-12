"""
Regression: services.rollcalls._push_rollcall_started / _push_rollcall_ended
resolve the group's web-push token via manager.get_chat(chat_id) -- the same
6-field-subset cache dict that already caused /auto_buzz to silently never
fire (see test_reminder_dms_and_export.py). group_web_token isn't one of the
6 cached fields, so `chat.get("group_web_token")` always read None, the
`if not group_token: return` guard always fired, and web-push notifications
for rollcall start/end have likely never fired in production. Fixed to read
via db.get_or_create_chat (a fresh DB read) instead.
"""
from unittest.mock import AsyncMock, patch

import db
from helpers import IntegrationBase, ADMIN_USER, CHAT_ID
import services.rollcalls as rollcalls_svc


class TestPushNotificationTokenResolution(IntegrationBase):

    async def test_push_rollcall_started_resolves_real_group_token(self):
        # Populate (and lazily generate) the chat's real group_web_token.
        real_token = db.get_or_create_chat(CHAT_ID)["group_web_token"]
        self.assertTrue(real_token)

        # manager.get_chat's cache must still NOT carry group_web_token --
        # this pins the root cause so the test fails loudly if the cache
        # shape ever changes to include it (which would make this
        # regression test a false negative).
        self.assertNotIn("group_web_token", self.mgr.get_chat(CHAT_ID))

        # _push_rollcall_started does `from services import push as push_svc`
        # locally on every call, so patch the source attribute -- the local
        # import re-binds to it fresh each time this runs.
        with patch("services.push.notify_rollcall_started", new=AsyncMock()) as mock_notify:
            await rollcalls_svc._push_rollcall_started(CHAT_ID, "Friday Football")

        mock_notify.assert_awaited_once()
        called_token = mock_notify.await_args.args[0]
        self.assertEqual(called_token, real_token)

    async def test_push_rollcall_ended_resolves_real_group_token(self):
        real_token = db.get_or_create_chat(CHAT_ID)["group_web_token"]

        with patch("services.push.notify_rollcall_ended", new=AsyncMock()) as mock_notify:
            await rollcalls_svc._push_rollcall_ended(CHAT_ID, "Friday Football")

        mock_notify.assert_awaited_once()
        called_token = mock_notify.await_args.args[0]
        self.assertEqual(called_token, real_token)
