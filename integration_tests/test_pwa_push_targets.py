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
        self._subscribe("https://fcm.googleapis.com/alice", ALICE)
        self._subscribe("https://fcm.googleapis.com/alice", None)   # bell tapped while signed out
        subs = db.get_push_subscriptions(self.token)
        self.assertEqual([s["tg_user_id"] for s in subs], [ALICE])

    async def test_notify_users_targets_only_named_members(self):
        self._subscribe("https://fcm.googleapis.com/alice-phone", ALICE)
        self._subscribe("https://fcm.googleapis.com/alice-laptop", ALICE)
        self._subscribe("https://fcm.googleapis.com/bob", BOB)
        self._subscribe("https://fcm.googleapis.com/anon", None)
        with patch.object(push_svc, "_dispatch", new=AsyncMock(return_value=2)) as disp, \
             patch.object(push_svc, "_load_or_create_vapid", return_value=("pem", "pub")):
            sent = await push_svc.notify_users(CHAT_ID, [ALICE], "t", "b", tag="x")
        self.assertEqual(sent, 2)
        endpoints = sorted(s["endpoint"] for s in disp.await_args.args[0])
        self.assertEqual(endpoints, ["https://fcm.googleapis.com/alice-laptop",
                                     "https://fcm.googleapis.com/alice-phone"])

    async def test_notify_users_ignores_proxy_names(self):
        self._subscribe("https://fcm.googleapis.com/anon", None)
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


class TestGuestPush(IntegrationBase):
    """A guest has no Telegram: their id on a rollcall is their name, there is
    no DM, and the group message lands in a chat they aren't in. A browser that
    turned the bell on as a named guest is the only way to tell them."""

    def setUp(self):
        super().setUp()
        self.token = db.get_or_create_chat(CHAT_ID)["group_web_token"]

    def _sub(self, endpoint, guest=None, uid=None):
        db.save_push_subscription(self.token, endpoint, "p256dh", "auth",
                                  tg_user_id=uid, guest_name=guest)

    async def _sent_to(self, coro):
        with patch.object(push_svc, "_dispatch", new=AsyncMock(return_value=1)) as disp, \
             patch.object(push_svc, "_load_or_create_vapid", return_value=("pem", "pub")):
            await coro
        return sorted(s["endpoint"] for s in disp.await_args.args[0]) if disp.await_count else []

    async def test_promoted_guest_is_pushed_by_name(self):
        self._sub("https://fcm.googleapis.com/ravi", "Ravi  K")
        self._sub("https://fcm.googleapis.com/other", "Priya")
        self._sub("https://fcm.googleapis.com/anon")
        sent = await self._sent_to(push_svc.notify_users(CHAT_ID, ["ravi k"], "t", "b", tag="promoted"))
        self.assertEqual(sent, ["https://fcm.googleapis.com/ravi"])

    async def test_a_signed_in_device_is_never_reached_by_name(self):
        # A member is reached by id only; a name anyone can type must not
        # pull in their device.
        self._sub("https://fcm.googleapis.com/alice", "Alice", uid=ALICE)
        sent = await self._sent_to(push_svc.notify_users(CHAT_ID, ["Alice"], "t", "b", tag="x"))
        self.assertEqual(sent, [])

    async def test_subscribe_keeps_guest_name_only_without_identity(self):
        with patch.object(push_svc, "validate_endpoint"):
            push_svc.subscribe(self.token, "https://fcm.googleapis.com/g", "p", "a", guest_name=" Ravi ")
            push_svc.subscribe(self.token, "https://fcm.googleapis.com/m", "p", "a",
                               tg_user_id=ALICE, guest_name="Alice")
            push_svc.subscribe(self.token, "https://fcm.googleapis.com/g", "p", "a")   # no name: kept
        names = {s["endpoint"]: s["guest_name"] for s in db.get_push_subscriptions(self.token)}
        self.assertEqual(names, {"https://fcm.googleapis.com/g": "Ravi",
                                 "https://fcm.googleapis.com/m": None})
        with patch.object(push_svc, "validate_endpoint"):
            push_svc.subscribe(self.token, "https://fcm.googleapis.com/g", "p", "a", guest_name="Ravi K")
        names = {s["endpoint"]: s["guest_name"] for s in db.get_push_subscriptions(self.token)}
        self.assertEqual(names["https://fcm.googleapis.com/g"], "Ravi K")   # renamed

    async def test_waitlist_promotion_pushes_a_guest(self):
        from handlers.promotion import announce_promotions
        with patch("services.push.notify_users", new=AsyncMock()) as nu:
            await announce_promotions(CHAT_ID, [{"user_id": "Ravi", "name": "Ravi"}], "Friday Football", 1)
            await asyncio.sleep(0)
        nu.assert_awaited_once()
        self.assertEqual(nu.await_args.args[:2], (CHAT_ID, ["Ravi"]))

    async def test_vote_reminder_reaches_guests_who_have_not_voted(self):
        import check_reminders
        rc = await self.start_rc("Friday Football")
        from services.web import vote_by_token, _ensure_web_token
        await vote_by_token(_ensure_web_token(rc), "Voted Guest", "in")
        self._sub("https://fcm.googleapis.com/voted", "voted guest")
        self._sub("https://fcm.googleapis.com/quiet", "Quiet Guest")
        # Every member has voted — guests must still be reminded.
        with patch("services.lists.get_non_responders", return_value={"candidates": []}):
            sent = await self._sent_to(
                check_reminders._dm_non_responders(CHAT_ID, 1, "Friday Football", "closes soon"))
        self.assertEqual(sent, ["https://fcm.googleapis.com/quiet"])


class TestGuestDuesPush(IntegrationBase):

    async def test_guest_debtors_are_pushed_by_name(self):
        # Owned or not, a debt under a guest's name reaches the guest's own
        # device; members are still reached by id only.
        from handlers.dues import _send_dues_dms
        result = {"upi_vpa": "club@upi",
                  "dm_targets": [{"user_id": ALICE, "member_name": "Alice", "balance": 300, "is_proxy": False}],
                  "members_owed": [{"user_id": ALICE, "member_name": "Alice", "balance": 300},
                                   {"user_id": None, "member_name": "Ravi", "balance": 150}]}
        with patch("services.push.notify_users", new=AsyncMock()) as nu:
            await _send_dues_dms(CHAT_ID, result)
        self.assertEqual([c.args[1] for c in nu.await_args_list], [[ALICE], ["Ravi"]])
        self.assertIn("₹150", nu.await_args_list[1].args[3])
        self.assertIn("club@upi", nu.await_args_list[1].args[3])
