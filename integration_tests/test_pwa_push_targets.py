"""
Per-member web push — the installed-app twin of the bot's DMs.

Before this, push only ever said "rollcall opened/closed" to the whole group.
A member who lives in the installed web app and never started the bot got
nothing when promoted off the waitlist, reminded to vote, or nudged for dues:
the DM failed and there was no other channel. These pin that each of those
three paths now also pushes to that member, and only to that member.

Also pins the SQLite subscribe upsert: it was `INSERT OR REPLACE`, which
deletes and re-inserts the row, so re-subscribing without an id_token wiped
the stored tg_user_id — silently turning a member's device back into an
anonymous one that per-member push can never reach.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import db
from helpers import IntegrationBase, CHAT_ID
import services.push as push_svc

ALICE, BOB = 111001, 222002


class TestPerMemberPush(IntegrationBase):

    def setUp(self):
        super().setUp()
        self.token = db.get_or_create_chat(CHAT_ID)["group_web_token"]

    def _subscribe(self, endpoint, uid=None):
        db.save_push_subscription(self.token, endpoint, "p256dh", "auth", tg_user_id=uid)

    async def test_resubscribe_without_identity_keeps_the_user(self):
        self._subscribe("https://push.example/alice", ALICE)
        self._subscribe("https://push.example/alice", None)   # bell tapped while signed out
        subs = db.get_push_subscriptions(self.token)
        self.assertEqual([s["tg_user_id"] for s in subs], [ALICE])

    async def test_notify_users_targets_only_named_members(self):
        self._subscribe("https://push.example/alice-phone", ALICE)
        self._subscribe("https://push.example/alice-laptop", ALICE)
        self._subscribe("https://push.example/bob", BOB)
        self._subscribe("https://push.example/anon", None)
        with patch.object(push_svc, "_dispatch", new=AsyncMock(return_value=2)) as disp, \
             patch.object(push_svc, "_load_or_create_vapid", return_value=("pem", "pub")):
            sent = await push_svc.notify_users(CHAT_ID, [ALICE], "t", "b", tag="x")
        self.assertEqual(sent, 2)
        endpoints = sorted(s["endpoint"] for s in disp.await_args.args[0])
        self.assertEqual(endpoints, ["https://push.example/alice-laptop",
                                     "https://push.example/alice-phone"])

    async def test_notify_users_ignores_proxy_names(self):
        self._subscribe("https://push.example/anon", None)
        with patch.object(push_svc, "_dispatch", new=AsyncMock()) as disp:
            sent = await push_svc.notify_users(CHAT_ID, ["Ravi (proxy)", None], "t", "b", tag="x")
        self.assertEqual(sent, 0)
        disp.assert_not_awaited()

    async def test_waitlist_promotion_pushes_the_promoted_member(self):
        from handlers.promotion import announce_promotions
        with patch("services.push.notify_users", new=AsyncMock()) as nu:
            await announce_promotions(CHAT_ID, [{"user_id": ALICE, "name": "Alice", "username": "alice"}],
                                      "Friday Football", 1)
            await asyncio.sleep(0)
        nu.assert_awaited_once()
        self.assertEqual(nu.await_args.args[:2], (CHAT_ID, [ALICE]))
        self.assertEqual(nu.await_args.kwargs["tag"], "promoted")

    async def test_vote_reminder_pushes_non_responders(self):
        import check_reminders
        cands = {"candidates": [{"user_id": ALICE}, {"user_id": "Guest"}]}
        with patch("services.lists.get_non_responders", return_value=cands), \
             patch("services.push.notify_users", new=AsyncMock()) as nu:
            await check_reminders._dm_non_responders(CHAT_ID, 1, "Friday Football", "closes in 2 hour(s)")
        nu.assert_awaited_once()
        self.assertEqual(nu.await_args.args[1], [ALICE, "Guest"])   # notify_users drops the non-int

    async def test_dues_reminder_pushes_each_debtor(self):
        from handlers.dues import _send_dues_dms
        result = {"dm_targets": [
            {"user_id": ALICE, "member_name": "Alice", "balance": 300, "is_proxy": False},
            {"user_id": BOB, "member_name": "Ravi", "balance": 150, "is_proxy": True},
        ]}
        with patch("services.push.notify_users", new=AsyncMock()) as nu:
            await _send_dues_dms(CHAT_ID, result)
        self.assertEqual([c.args[1] for c in nu.await_args_list], [[ALICE], [BOB]])
        self.assertIn("Ravi", nu.await_args_list[1].args[3])        # proxy owner told whose debt
        self.assertTrue(all(c.kwargs["tag"] == "dues" for c in nu.await_args_list))
