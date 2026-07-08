"""
Handler-level integration tests for the dues system.

Covers scenarios NOT reachable by service-layer tests:
  - /settle_dues against a LIVE in-memory rollcall (active path through service)
  - Double-close guard raising duesGameAlreadyClosed through the handler
  - /set_treasury_upi handler → DB persisted → confirmation posted
  - /set_collector with UPI arg stored on rollcall via handler
  - /my_dues UPI display routing against real DB settings
  - /mark_paid by a non-admin collector (not blocked by admin check)
  - /cancel_game_dues then re-close (compensating entries + fresh closure)

Uses IntegrationBase: real SQLite, real handlers, mocked Telegram bot API.
"""
import sys
import os
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import db
from services import dues as dues_svc

from helpers import IntegrationBase, CHAT_ID, ADMIN_ID, ADMIN_USER, USERS, make_message
from mock_helpers import get_mock_bot


# Non-admin member who will act as collector in payment tests
COLLECTOR_USER = USERS[0]   # id=1, first_name="User1"
REGULAR_USER   = USERS[1]   # id=2, first_name="User2"


def _enable_dues(chat_id=CHAT_ID, upi="test@upi", step=10):
    """Seed the minimum dues settings for a chat."""
    db.get_or_create_chat(chat_id)
    db.update_chat_settings(
        chat_id,
        dues_enabled=1,
        dues_round_step=step,
        upi_vpa=upi,
    )
    dues_svc.seed_default_penalty_tiers(chat_id)


# ── /settle_dues — active in-memory rollcall path ────────────────────────────

class TestCloseGameActivePath(IntegrationBase):
    """
    /settle_dues when there IS an active rollcall in memory.

    The service must end the RC, write dues entries, and post the announcement.
    """

    async def test_close_game_active_rc_posts_announcement(self):
        _enable_dues(upi="pay@test")
        # Start RC and vote 3 members in
        rc = await self.start_rc("Friday Futsal")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)

        await self.dues_settle_dues(self.msg("/settle_dues"))

        texts = self.sent_texts()
        ann = next((t for t in texts if "₹" in t and "Friday Futsal" in t), None)
        self.assertIsNotNone(ann, f"Expected close announcement with ₹ and title; got: {texts}")
        self.assertIn("₹100", ann)   # 300 / 3 = 100, step=10 → 100 exact

    async def test_close_game_active_rc_ends_rollcall(self):
        _enable_dues()
        rc = await self.start_rc("Saturday Game")
        rc.event_fee = "600"
        for u in USERS[:6]:
            await self.vote_in(u)

        self.assertIsNotNone(self.mgr.get_rollcall(CHAT_ID, 0))

        await self.dues_settle_dues(self.msg("/settle_dues"))

        # RC must be gone from manager after close
        self.assertIsNone(self.mgr.get_rollcall(CHAT_ID, 0))

    async def test_close_game_active_rc_writes_dues_entries(self):
        _enable_dues()
        rc = await self.start_rc("Sunday Match")
        rc.event_fee = "500"
        for u in USERS[:5]:
            await self.vote_in(u)

        await self.dues_settle_dues(self.msg("/settle_dues"))

        # 5 share entries written (500/5=100)
        balances = db.get_all_dues_balances(CHAT_ID)
        self.assertEqual(len(balances), 5)
        for row in balances:
            self.assertEqual(row["balance"], 100)

    async def test_close_game_active_rc_creates_closure_row(self):
        _enable_dues()
        rc = await self.start_rc("Closure Test")
        rc.event_fee = "400"
        for u in USERS[:4]:
            await self.vote_in(u)

        await self.dues_settle_dues(self.msg("/settle_dues"))

        # Exactly one game_closure must exist
        closures = db.get_fund_transactions(CHAT_ID, limit=100)  # sanity check fund
        closure = db.get_nth_game_closure(CHAT_ID, 0)
        self.assertIsNotNone(closure)
        self.assertEqual(closure["title"], "Closure Test")
        self.assertEqual(closure["per_head"], 100)   # 400 / 4

    async def test_close_game_active_rc_upi_in_announcement(self):
        _enable_dues(upi="group@hdfc")
        rc = await self.start_rc("UPI Active Test")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)

        await self.dues_settle_dues(self.msg("/settle_dues"))

        texts = self.sent_texts()
        ann = next((t for t in texts if "group@hdfc" in t), None)
        self.assertIsNotNone(ann, f"UPI should appear in close announcement; got: {texts}")


# ── /settle_dues — double-close guard ────────────────────────────────────────

class TestDoubleCloseGuard(IntegrationBase):
    """Second /settle_dues on the same game must post an error, not a second closure."""

    async def test_double_close_posts_error(self):
        _enable_dues()
        rc = await self.start_rc("Double Test")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)

        await self.dues_settle_dues(self.msg("/settle_dues"))
        bot = get_mock_bot()
        first_send_count = bot.send_message.call_count

        # Second close — no active RC now, and the DB closure already exists
        # Service raises duesGameAlreadyClosed → reply_error posts it
        await self.dues_settle_dues(self.msg("/settle_dues"))

        texts = self.sent_texts()
        new_texts = texts[first_send_count:]
        error_msg = next((t for t in new_texts if "already" in t.lower() or "No game" in t), None)
        self.assertIsNotNone(
            error_msg,
            f"Expected double-close error message; new texts: {new_texts}",
        )

    async def test_double_close_does_not_add_second_closure(self):
        _enable_dues()
        rc = await self.start_rc("Single Closure")
        rc.event_fee = "200"
        for u in USERS[:2]:
            await self.vote_in(u)

        await self.dues_settle_dues(self.msg("/settle_dues"))
        await self.dues_settle_dues(self.msg("/settle_dues"))  # should be no-op

        # Still only one closure row (second call fails at service layer)
        c1 = db.get_nth_game_closure(CHAT_ID, 0)
        c2 = db.get_nth_game_closure(CHAT_ID, 1)
        self.assertIsNotNone(c1)
        self.assertIsNone(c2, "Only one closure row should exist after double-close attempt")


# ── /set_treasury_upi handler → DB → confirmation ────────────────────────────

class TestSetTreasuryUPIHandler(IntegrationBase):
    """Full handler → service → DB → announcement chain for /set_treasury_upi."""

    async def test_set_treasury_upi_persists_to_db(self):
        _enable_dues()
        await self.dues_set_treasury_upi(self.msg("/set_treasury_upi treasurer@icici"))

        row = db.get_or_create_chat(CHAT_ID)
        self.assertEqual(row.get("treasury_upi"), "treasurer@icici")

    async def test_set_treasury_upi_posts_confirmation(self):
        _enable_dues()
        await self.dues_set_treasury_upi(self.msg("/set_treasury_upi fund@sbi"))

        texts = self.sent_texts()
        self.assertTrue(
            any("fund@sbi" in t for t in texts),
            f"Confirmation should mention the VPA; got: {texts}",
        )

    async def test_set_treasury_upi_invalid_rejected(self):
        _enable_dues()
        await self.dues_set_treasury_upi(self.msg("/set_treasury_upi NOTAUPI"))

        row = db.get_or_create_chat(CHAT_ID)
        # DB must not be updated with an invalid value
        self.assertNotEqual(row.get("treasury_upi"), "NOTAUPI")

    async def test_set_treasury_upi_admin_only(self):
        _enable_dues()
        # Enable admin-rights enforcement so non-admins are actually blocked
        self.mgr.set_admin_rights(CHAT_ID, True)
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"

        await self.dues_set_treasury_upi(
            self.msg("/set_treasury_upi treasurer@sbi", user=REGULAR_USER)
        )

        texts = self.sent_texts()
        self.assertTrue(
            any("permission" in t.lower() or "admin" in t.lower() or "only" in t.lower()
                for t in texts),
            f"Non-admin should get permission error; got: {texts}",
        )
        # Restore for other tests
        self.mgr.set_admin_rights(CHAT_ID, False)


# ── /set_collector with UPI arg ───────────────────────────────────────────────

class TestSetCollectorWithUPIHandler(IntegrationBase):
    """
    /set_collector Name upi@bank stores the UPI on the rollcall (active path)
    and on the game_closure row (post-close path).
    """

    async def test_set_collector_upi_stored_on_active_rc(self):
        _enable_dues()
        rc = await self.start_rc("Collector UPI Test")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)

        await self.dues_set_collector(
            self.msg(f"/set_collector User1 paid user1@paytm")
        )

        # In-memory RC must carry the UPI
        live_rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(getattr(live_rc, "collector_upi", None), "user1@paytm")

    async def test_set_collector_upi_in_close_announcement(self):
        _enable_dues(upi="fallback@grp")
        rc = await self.start_rc("Collector UPI Close")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)

        await self.dues_set_collector(self.msg("/set_collector User1 paid user1@axis"))
        get_mock_bot().send_message.reset_mock()

        await self.dues_settle_dues(self.msg("/settle_dues"))

        texts = self.sent_texts()
        ann = next((t for t in texts if "₹" in t), None)
        self.assertIsNotNone(ann)
        self.assertIn("user1@axis", ann,
                      "Collector UPI should appear in close announcement")
        self.assertNotIn("fallback@grp", ann,
                         "Group fallback UPI must not appear when collector UPI is set")

    async def test_set_collector_upi_stored_on_closure(self):
        _enable_dues()
        rc = await self.start_rc("Closure UPI Test")
        rc.event_fee = "400"
        for u in USERS[:4]:
            await self.vote_in(u)

        await self.dues_set_collector(self.msg("/set_collector User1 collector1@upi"))
        await self.dues_settle_dues(self.msg("/settle_dues"))

        closure = db.get_nth_game_closure(CHAT_ID, 0)
        self.assertIsNotNone(closure)
        self.assertEqual(closure.get("collector_upi"), "collector1@upi")

    async def test_set_collector_no_upi_closure_has_none(self):
        _enable_dues()
        rc = await self.start_rc("No UPI Test")
        rc.event_fee = "200"
        for u in USERS[:2]:
            await self.vote_in(u)

        await self.dues_set_collector(self.msg("/set_collector User1"))
        await self.dues_settle_dues(self.msg("/settle_dues"))

        closure = db.get_nth_game_closure(CHAT_ID, 0)
        self.assertIsNone(closure.get("collector_upi"))


# ── /my_dues UPI display routing against real DB settings ────────────────────

class TestMyDuesUPIDisplayIntegration(IntegrationBase):
    """
    /my_dues must show the correct UPI line(s) based on live DB settings.
    Tested end-to-end: real DB settings → handler → message text.
    """

    def _seed_balance(self, uid=USERS[0]["id"], name="User1"):
        """Write a share entry so the user has a positive balance."""
        db.add_dues_entry(CHAT_ID, None, uid, name, "share", 90, None, ADMIN_ID, "Admin")

    async def test_my_dues_shows_both_upi_when_distinct(self):
        _enable_dues(upi="game@hdfc")
        db.update_chat_settings(CHAT_ID, treasury_upi="fund@sbi")
        self._seed_balance()

        await self.dues_my_dues(self.msg("/my_dues", user=USERS[0]))

        texts = self.sent_texts()
        combined = "\n".join(texts)
        self.assertIn("game@hdfc", combined, f"Game UPI missing; got: {texts}")
        self.assertIn("fund@sbi", combined, f"Treasury UPI missing; got: {texts}")

    async def test_my_dues_shows_single_upi_when_no_treasury(self):
        _enable_dues(upi="only@upi")
        self._seed_balance()

        await self.dues_my_dues(self.msg("/my_dues", user=USERS[0]))

        texts = self.sent_texts()
        combined = "\n".join(texts)
        self.assertIn("only@upi", combined, f"UPI missing; got: {texts}")

    async def test_my_dues_shows_treasury_only_when_no_game_upi(self):
        _enable_dues(upi=None)
        db.update_chat_settings(CHAT_ID, upi_vpa=None, treasury_upi="treasury@paytm")
        self._seed_balance()

        await self.dues_my_dues(self.msg("/my_dues", user=USERS[0]))

        texts = self.sent_texts()
        combined = "\n".join(texts)
        self.assertIn("treasury@paytm", combined, f"Treasury UPI missing; got: {texts}")

    async def test_my_dues_no_upi_line_when_neither_set(self):
        _enable_dues(upi=None)
        db.update_chat_settings(CHAT_ID, upi_vpa=None, treasury_upi=None)
        self._seed_balance()

        await self.dues_my_dues(self.msg("/my_dues", user=USERS[0]))

        texts = self.sent_texts()
        combined = "\n".join(texts)
        self.assertNotIn("@", combined,
                         f"No UPI should appear when neither is set; got: {texts}")

    async def test_my_dues_same_upi_not_duplicated(self):
        """When treasury_upi == upi_vpa, show only one line."""
        _enable_dues(upi="same@upi")
        db.update_chat_settings(CHAT_ID, treasury_upi="same@upi")
        self._seed_balance()

        await self.dues_my_dues(self.msg("/my_dues", user=USERS[0]))

        texts = self.sent_texts()
        combined = "\n".join(texts)
        self.assertEqual(combined.count("same@upi"), 1,
                         f"Same UPI should appear exactly once; got: {combined}")

    async def test_my_dues_zero_balance_no_upi_line(self):
        """When balance is 0, no UPI line should appear even if UPIs are configured."""
        _enable_dues(upi="pay@upi")
        db.update_chat_settings(CHAT_ID, treasury_upi="fund@upi")
        # No dues entry → zero balance

        await self.dues_my_dues(self.msg("/my_dues", user=USERS[0]))

        texts = self.sent_texts()
        # Either "no outstanding dues" or nothing containing UPI
        combined = "\n".join(texts)
        self.assertNotIn("pay@upi", combined)
        self.assertNotIn("fund@upi", combined)


# ── /mark_paid by a non-admin collector ──────────────────────────────────────

class TestMarkPaidByCollector(IntegrationBase):
    """
    A non-admin who is the current game's collector must be allowed to
    call /mark_paid; a random non-admin must be blocked.
    """

    async def _setup_game_with_collector(self):
        """Start RC, close it, set collector = COLLECTOR_USER."""
        _enable_dues()
        rc = await self.start_rc("Collector Payment Test")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_settle_dues(self.msg("/settle_dues"))
        # Now set collector on the closed game via service (post-close path)
        dues_svc.set_collector(
            CHAT_ID,
            COLLECTOR_USER["first_name"],
            False,
            ADMIN_ID,
            "Admin",
            collector_upi=None,
        )

    async def test_collector_can_mark_paid(self):
        await self._setup_game_with_collector()
        get_mock_bot().send_message.reset_mock()

        # Collector (non-admin) marks another player as paid
        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"
        await self.dues_mark_paid(
            self.msg(f"/mark_paid {USERS[1]['first_name']}", user=COLLECTOR_USER)
        )

        texts = self.sent_texts()
        # Should post a payment announcement, not a permission error
        paid_msg = next(
            (t for t in texts
             if "paid" in t.lower() or "payment" in t.lower() or "✅" in t),
            None,
        )
        self.assertIsNotNone(
            paid_msg,
            f"Collector should be able to mark payment; got: {texts}",
        )

    async def test_random_non_admin_cannot_mark_paid(self):
        await self._setup_game_with_collector()
        # Enable admin-rights enforcement so non-admins/non-collectors are blocked
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().send_message.reset_mock()

        bot = get_mock_bot()
        bot.get_chat_member.return_value.status = "member"
        # USERS[2] is neither admin nor collector
        await self.dues_mark_paid(
            self.msg(f"/mark_paid {USERS[1]['first_name']}", user=USERS[2])
        )

        texts = self.sent_texts()
        error_msg = next(
            (t for t in texts
             if "permission" in t.lower() or "only" in t.lower() or "collector" in t.lower()),
            None,
        )
        self.assertIsNotNone(
            error_msg,
            f"Non-collector non-admin should get permission error; got: {texts}",
        )
        self.mgr.set_admin_rights(CHAT_ID, False)

    async def test_mark_paid_full_amount_clears_balance(self):
        """After /mark_paid with no amount, balance must be 0."""
        await self._setup_game_with_collector()
        uid = USERS[1]["id"]
        name = USERS[1]["first_name"]

        balance_before = db.get_dues_balance(CHAT_ID, str(uid))
        self.assertGreater(balance_before, 0, "User should owe money before payment")

        await self.dues_mark_paid(self.msg(f"/mark_paid {name}"))

        balance_after = db.get_dues_balance(CHAT_ID, str(uid))
        self.assertEqual(balance_after, 0, "Balance should be 0 after full payment")


# ── /cancel_game_dues then re-close ──────────────────────────────────────────

class TestCancelAndReclose(IntegrationBase):
    """
    Cancelling a game writes compensating entries; re-closing creates a
    fresh closure and fresh share entries.
    """

    async def test_cancel_posts_announcement(self):
        _enable_dues()
        rc = await self.start_rc("Cancel Test")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_settle_dues(self.msg("/settle_dues"))
        get_mock_bot().send_message.reset_mock()

        await self.dues_cancel_game(self.msg("/cancel_game_dues"))

        texts = self.sent_texts()
        self.assertTrue(
            any("cancel" in t.lower() or "reversal" in t.lower() or "credit" in t.lower()
                for t in texts),
            f"Cancel announcement missing; got: {texts}",
        )

    async def test_cancel_writes_compensating_entries(self):
        _enable_dues()
        rc = await self.start_rc("Compensating Entries")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_settle_dues(self.msg("/settle_dues"))

        # Each of 3 users owes ₹100
        uid = USERS[0]["id"]
        self.assertEqual(db.get_dues_balance(CHAT_ID, str(uid)), 100)

        await self.dues_cancel_game(self.msg("/cancel_game_dues"))

        # After cancel, balance must be 0 (original share + cancel_credit = 0)
        self.assertEqual(db.get_dues_balance(CHAT_ID, str(uid)), 0)

    async def test_cancel_does_not_delete_original_entries(self):
        """Append-only invariant: cancel_credit rows added, originals untouched."""
        _enable_dues()
        rc = await self.start_rc("Append Only Cancel")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_settle_dues(self.msg("/settle_dues"))

        entries_before = db.get_dues_entries(CHAT_ID, limit=100)
        share_count = sum(1 for e in entries_before if e["entry_type"] == "share")

        await self.dues_cancel_game(self.msg("/cancel_game_dues"))

        entries_after = db.get_dues_entries(CHAT_ID, limit=100)
        share_count_after = sum(1 for e in entries_after if e["entry_type"] == "share")
        cancel_count = sum(1 for e in entries_after if e["entry_type"] == "cancel_credit")

        self.assertEqual(share_count_after, share_count,
                         "Original share entries must not be deleted")
        self.assertEqual(cancel_count, share_count,
                         "One cancel_credit per share entry")

    async def test_reclose_after_cancel_creates_new_closure(self):
        """
        After cancel, the same rollcall is re-closeable (cancel deletes the
        closure metadata row so the game can be corrected and re-closed).
        Exactly one closure exists after the re-close.
        """
        _enable_dues()
        rc = await self.start_rc("Reclose Test")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_settle_dues(self.msg("/settle_dues"))

        first_closure = db.get_nth_game_closure(CHAT_ID, 0)
        self.assertIsNotNone(first_closure)

        await self.dues_cancel_game(self.msg("/cancel_game_dues"))

        # After cancel, the closure row is DELETED (metadata, not ledger).
        # The same rollcall is now re-closeable.
        self.assertIsNone(db.get_nth_game_closure(CHAT_ID, 0),
                          "Closure should be deleted by cancel so rollcall can be re-closed")

        get_mock_bot().send_message.reset_mock()
        await self.dues_settle_dues(self.msg("/settle_dues"))

        texts = self.sent_texts()
        ann = next((t for t in texts if "₹" in t), None)
        self.assertIsNotNone(ann, f"Re-close should post announcement; got: {texts}")

        # Exactly one closure exists — the fresh re-close of the same game
        new_closure = db.get_nth_game_closure(CHAT_ID, 0)
        self.assertIsNotNone(new_closure, "Re-close should create a new closure row")
        self.assertNotEqual(new_closure["id"], first_closure["id"],
                            "Re-close closure must be a new row, not the original")
        self.assertIsNone(db.get_nth_game_closure(CHAT_ID, 1),
                          "Exactly one closure should exist after cancel+reclose")

    async def test_cancel_no_game_posts_error(self):
        """Calling /cancel_game_dues with no closed game posts an error."""
        _enable_dues()
        await self.dues_cancel_game(self.msg("/cancel_game_dues"))

        texts = self.sent_texts()
        self.assertTrue(
            any("no closed" in t.lower() or "nothing" in t.lower() or "no game" in t.lower()
                for t in texts),
            f"Should get 'no closed game' error; got: {texts}",
        )


# ── /settle_dues — multi-game picker ─────────────────────────────────────────

class TestSettleDuesPicker(IntegrationBase):
    """
    /settle_dues (the renamed /close_game — the old /close_game and /cg command
    triggers no longer exist at all)
    must reach every unsettled game, not just the latest — regression coverage
    for db.get_latest_closeable_rollcall's old LIMIT-1 blind spot.
    """

    async def _end_unsettled_game(self, title, fee="300", n_in=3):
        """Start, fee, populate, and /erc a rollcall — leaving it ended but
        not financially closed."""
        await self.start_rc(title)
        await self.event_fee(self.msg(f"/ef {fee}", ADMIN_USER))
        for u in USERS[:n_in]:
            await self.vote_in(u)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

    async def test_single_unsettled_game_closes_directly_no_picker(self):
        _enable_dues()
        await self._end_unsettled_game("Only Game")
        get_mock_bot().send_message.reset_mock()

        await self.dues_settle_dues(self.msg("/settle_dues", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("game closed" in t.lower() for t in texts), texts)
        self.assertIsNotNone(db.get_nth_game_closure(CHAT_ID, 0))

    async def test_multiple_unsettled_games_shows_picker_not_latest_only(self):
        _enable_dues()
        await self._end_unsettled_game("Older Game")
        await self._end_unsettled_game("Newer Game")
        get_mock_bot().send_message.reset_mock()

        await self.dues_settle_dues(self.msg("/settle_dues", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("2 games waiting" in t for t in texts), texts)
        # Neither game should have been closed yet — this is just the picker.
        self.assertIsNone(db.get_nth_game_closure(CHAT_ID, 0))
        # The picker keyboard must offer both games.
        bot = get_mock_bot()
        _, kwargs = bot.send_message.call_args_list[-1]
        markup = kwargs.get("reply_markup")
        labels = [btn.text for btn in markup.keyboard]
        self.assertTrue(any("Older Game" in l for l in labels), labels)
        self.assertTrue(any("Newer Game" in l for l in labels), labels)

    async def test_picker_settles_the_specific_game_tapped(self):
        _enable_dues()
        await self._end_unsettled_game("Older Game", fee="200", n_in=2)
        await self._end_unsettled_game("Newer Game", fee="300", n_in=3)
        unsettled = db.get_unsettled_rollcalls(CHAT_ID)
        older = next(g for g in unsettled if g["title"] == "Older Game")
        newer = next(g for g in unsettled if g["title"] == "Newer Game")

        get_mock_bot().send_message.reset_mock()
        await self.dues_settle_pick_callback(self.call(f"settle_pick:{older['id']}", ADMIN_USER))

        # The OLDER game got closed, the newer one is still open.
        closure = db.get_game_closure(older["id"])
        self.assertIsNotNone(closure)
        self.assertIsNone(db.get_game_closure(newer["id"]))

        texts = self.sent_texts()
        self.assertTrue(any("Older Game" in t and "₹" in t for t in texts), texts)

    async def test_picker_rejects_non_admin(self):
        _enable_dues()
        await self._end_unsettled_game("Older Game")
        await self._end_unsettled_game("Newer Game")
        unsettled = db.get_unsettled_rollcalls(CHAT_ID)
        target = unsettled[0]

        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"
        try:
            await self.dues_settle_pick_callback(self.call(f"settle_pick:{target['id']}", USERS[0]))
        finally:
            get_mock_bot().get_chat_member.return_value.status = "administrator"
            self.mgr.set_admin_rights(CHAT_ID, False)

        self.assertIsNone(db.get_game_closure(target["id"]))

    async def test_settling_one_reports_remaining_unsettled_count(self):
        _enable_dues()
        await self._end_unsettled_game("Older Game")
        await self._end_unsettled_game("Newer Game")
        unsettled = db.get_unsettled_rollcalls(CHAT_ID)
        target = unsettled[0]

        get_mock_bot().send_message.reset_mock()
        await self.dues_settle_pick_callback(self.call(f"settle_pick:{target['id']}", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("1 more unsettled game" in t for t in texts), texts)
