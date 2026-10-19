"""
Regressions for confirmed dues/treasury money defects (real SQLite, real
handlers, mocked Telegram API). One test (or small group) per defect:

  1. second /cancel_game_dues after a re-close gave every player a phantom
     credit (reversed the first, already-cancelled close's shares again)
  2. collector "fronted ground cost" credit was never reversed by cancel and
     was written again on re-close → collector credited twice
  3. a negative fund blocked /settle_dues entirely (even subsidy 0)
  4. cancelling a pre-/new_season closure credited the new season and the
     game could never be re-closed
  5. /set_collector <name> paid AFTER close announced the credit but never
     wrote it (and switching collector never withdrew it)
  6. "1,200" was read as ₹1; "1.2k" was accepted as ₹1
  7. monthly treasury statement window compared mismatched text formats
  8. payment panel paid the wrong member after a re-sort
  9. a duplicated /cancel_game_dues reversed a second, different game
"""
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import db
from exceptions import incorrectParameter
from services import dues as dues_svc

from helpers import IntegrationBase, CHAT_ID, ADMIN_ID, ADMIN_USER, USERS
from mock_helpers import get_mock_bot


def _enable_dues(step=10):
    db.get_or_create_chat(CHAT_ID)
    db.update_chat_settings(CHAT_ID, dues_enabled=1, dues_round_step=step,
                            upi_vpa="test@upi")


def _bal(user) -> int:
    return db.get_dues_balance(CHAT_ID, user_id=user["id"])


class _GameBase(IntegrationBase):

    async def _play(self, title, fee, players, collector_paid=None):
        """Start a rollcall, vote `players` IN, optionally stage a paying
        collector, and settle it (subsidy 0). Returns its rollcall id."""
        rc = await self.start_rc(title)
        await self.event_fee(self.msg(f"/ef {fee}", ADMIN_USER))
        for u in players:
            await self.vote_in(u)
        if collector_paid is not None:
            await self.dues_set_collector(
                self.msg(f"/set_collector {collector_paid['first_name']} paid", ADMIN_USER))
        rc_id = getattr(rc, "id", None) or getattr(rc, "db_id", None)
        await self.dues_settle_dues(self.msg("/settle_dues 0", ADMIN_USER))
        assert db.get_game_closure(rc_id) is not None, self.sent_texts()
        return rc_id

    async def _cancel(self, rc_id):
        await self.dues_cancel_game_confirm(self.call(f"cgd_go:{rc_id}", ADMIN_USER))

    async def _reclose(self, fee):
        await self.event_fee(self.msg(f"/ef {fee}", ADMIN_USER))
        await self.dues_settle_dues(self.msg("/settle_dues 0", ADMIN_USER))


# ── 1 + 2: net-based cancel ───────────────────────────────────────────────────

class TestCancelNetsAcrossCloses(_GameBase):

    async def test_second_cancel_after_reclose_leaves_no_phantom_credit(self):
        _enable_dues()
        players = USERS[:10]
        rc_id = await self._play("Phantom", 600, players)       # ₹60/head
        self.assertEqual(_bal(players[0]), 60)

        await self._cancel(rc_id)
        await self._reclose(700)                                 # ₹70/head
        self.assertEqual(_bal(players[0]), 70)

        await self._cancel(rc_id)
        for u in players:
            self.assertEqual(_bal(u), 0, f"{u['first_name']} left with a phantom credit")

    async def test_collector_credit_reversed_on_cancel_and_not_doubled_on_reclose(self):
        _enable_dues()
        players = USERS[:10]
        collector = players[0]
        rc_id = await self._play("Fronted", 600, players, collector_paid=collector)
        self.assertEqual(_bal(collector), 60 - 600)

        await self._cancel(rc_id)
        self.assertEqual(_bal(collector), 0, "cancel must withdraw the ground-cost credit")

        await self._reclose(600)
        self.assertEqual(_bal(collector), 60 - 600, "re-close must credit the ground once")
        reimb = [e for e in db.get_dues_entries_for_rollcall(rc_id)
                 if e["entry_type"] == "reimbursement"]
        self.assertEqual(sum(e["amount"] for e in reimb), -600)

        await self._cancel(rc_id)
        self.assertEqual(_bal(collector), 0)

    async def test_reclose_absorbs_credit_a_pre_fix_cancel_left_standing(self):
        """Ledgers written before the fix: cancel reversed only shares, so the
        -600 credit is still standing when the game is re-closed. Re-close
        must top up to the target, not append another -600."""
        _enable_dues()
        players = USERS[:10]
        collector = players[0]
        rc_id = await self._play("Legacy", 600, players, collector_paid=collector)
        # Simulate the old cancel: share-only reversal + closure delete.
        for e in db.get_dues_entries_for_rollcall(rc_id):
            if e["entry_type"] == "share":
                db.add_dues_entry(CHAT_ID, rc_id, e["user_id"], e["member_name"],
                                  "cancel_credit", -e["amount"], "old cancel",
                                  ADMIN_ID, "Admin")
        db.delete_game_closure(rc_id)

        await self._reclose(600)
        self.assertEqual(_bal(collector), 60 - 600)


# ── 3: overdrawn fund ─────────────────────────────────────────────────────────

class TestSettleWithNegativeFund(_GameBase):

    async def test_subsidy_zero_settles_when_fund_is_negative(self):
        _enable_dues()
        dues_svc.log_expense(CHAT_ID, 150, "balls", ADMIN_ID, "Admin")
        self.assertEqual(db.get_fund_balance(CHAT_ID), -150)

        rc_id = await self._play("Overdrawn", 600, USERS[:10])
        self.assertEqual(_bal(USERS[0]), 60)
        self.assertIsNotNone(db.get_game_closure(rc_id))

    async def test_positive_subsidy_refused_with_clear_message(self):
        _enable_dues()
        dues_svc.log_expense(CHAT_ID, 150, "balls", ADMIN_ID, "Admin")
        rc = await self.start_rc("No Subsidy")
        rc.event_fee = "600"
        for u in USERS[:3]:
            await self.vote_in(u)
        with self.assertRaises(incorrectParameter) as ctx:
            await dues_svc.close_game(CHAT_ID, 50, ADMIN_ID, "Admin")
        self.assertIn("no subsidy", str(ctx.exception).lower())


# ── 4: pre-epoch closures ─────────────────────────────────────────────────────

class TestCancelRefusesPreSeasonGame(_GameBase):

    async def test_cancel_of_game_before_dues_epoch_is_refused(self):
        _enable_dues()
        rc_id = await self._play("Last Season", 600, USERS[:10])
        # /new_season stamps the epoch "now"; push it past the game's end.
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        db.update_chat_settings(CHAT_ID, dues_epoch=future)
        get_mock_bot().send_message.reset_mock()

        await self.dues_cancel_game(self.msg("/cancel_game_dues", ADMIN_USER))
        self.assertTrue(any("before the current season" in t for t in self.sent_texts()),
                        self.sent_texts())
        with self.assertRaises(incorrectParameter):
            dues_svc.cancel_game_credit(CHAT_ID, rc_id, ADMIN_ID, "Admin")

        self.assertIsNotNone(db.get_game_closure(rc_id))
        self.assertEqual(_bal(USERS[0]), 60)


# ── 5: post-close /set_collector paid ─────────────────────────────────────────

class TestSetCollectorAfterClose(_GameBase):

    async def test_paid_after_close_writes_credit_once_and_moves_with_collector(self):
        _enable_dues()
        players = USERS[:10]
        rc_id = await self._play("Late Collector", 600, players)
        a, b = players[0], players[1]
        self.assertEqual(_bal(a), 60)

        get_mock_bot().send_message.reset_mock()
        await self.dues_set_collector(self.msg(f"/set_collector {a['first_name']} paid", ADMIN_USER))
        self.assertEqual(_bal(a), 60 - 600)
        self.assertTrue(any("credited ₹600" in t for t in self.sent_texts()), self.sent_texts())

        # Idempotent: repeating writes nothing.
        await self.dues_set_collector(self.msg(f"/set_collector {a['first_name']} paid", ADMIN_USER))
        self.assertEqual(_bal(a), 60 - 600)

        # Switching to someone who did NOT front it withdraws A's credit.
        await self.dues_set_collector(self.msg(f"/set_collector {b['first_name']}", ADMIN_USER))
        self.assertEqual(_bal(a), 60)
        self.assertEqual(_bal(b), 60)

        # And B fronting it later moves the credit to B; cancel clears all.
        await self.dues_set_collector(self.msg(f"/set_collector {b['first_name']} paid", ADMIN_USER))
        self.assertEqual(_bal(b), 60 - 600)
        await self._cancel(rc_id)
        for u in players:
            self.assertEqual(_bal(u), 0)

        # The paid flag is on the rollcall row too, so re-close keeps it.
        await self._reclose(600)
        self.assertEqual(_bal(b), 60 - 600)

    async def test_upi_only_reset_keeps_paid_flag(self):
        _enable_dues()
        players = USERS[:10]
        await self._play("UPI later", 600, players, collector_paid=players[0])
        dues_svc.set_collector(CHAT_ID, players[0]["first_name"], None, ADMIN_ID, "Admin",
                               collector_upi="u1@ybl")
        self.assertEqual(_bal(players[0]), 60 - 600)


# ── 6: fee parsing ────────────────────────────────────────────────────────────

class TestFeeParsing(_GameBase):

    def test_parse_fee_amount(self):
        cases = {
            "600": 600, "1,200": 1200, "₹1,500": 1500, "1,20,000": 120000,
            "Rs. 1500.00": 1500, "₹1500": 1500, "600 + shuttles 200": 600,
            "1500 INR": 1500,
            "1.2k": None, "2k": None, "2 lakh": None, "1500.50": None,
            "1,2": None, "free": None, "": None,
        }
        for text, want in cases.items():
            self.assertEqual(dues_svc.parse_fee_amount(text), want, text)

    async def test_comma_fee_settles_at_full_amount(self):
        _enable_dues()
        await self._play("Comma", "1,200", USERS[:10])
        self.assertEqual(_bal(USERS[0]), 120)

    async def test_ef_rejects_shorthand(self):
        _enable_dues()
        rc = await self.start_rc("Shorthand")
        get_mock_bot().send_message.reset_mock()
        await self.event_fee(self.msg("/ef 1.2k", ADMIN_USER))
        self.assertIsNone(rc.event_fee)
        self.assertTrue(any("1.2k" in t for t in self.sent_texts()), self.sent_texts())


# ── 7: statement window format ────────────────────────────────────────────────

class TestFundStatementWindow(_GameBase):

    def test_bounds_in_space_format_compare_correctly_on_boundary_day(self):
        _enable_dues()
        dues_svc.fund_topup(CHAT_ID, 100, "topup", ADMIN_ID, "Admin")
        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%d %H:%M:%S"  # periodic_jobs._utc_str
        inside = db.get_fund_transactions_between(
            CHAT_ID, (now - timedelta(minutes=1)).strftime(fmt),
            (now + timedelta(minutes=1)).strftime(fmt))
        self.assertEqual(len(inside), 1)
        # A window starting later the SAME day must not include it (it did:
        # 'YYYY-MM-DDT…' sorts after every 'YYYY-MM-DD …' bound as text).
        later = db.get_fund_transactions_between(
            CHAT_ID, (now + timedelta(seconds=30)).strftime(fmt),
            (now + timedelta(minutes=2)).strftime(fmt))
        if (now + timedelta(seconds=30)).date() == now.date():
            self.assertEqual(later, [])


# ── 8: payment panel identity ─────────────────────────────────────────────────

class TestPaymentPanelStableIdentity(_GameBase):

    async def test_full_pay_after_resort_pays_the_member_the_button_showed(self):
        _enable_dues()
        a, b = USERS[0], USERS[1]
        db.add_dues_entry(CHAT_ID, None, a["id"], a["first_name"], "share", 100, None, ADMIN_ID, "Admin")
        db.add_dues_entry(CHAT_ID, None, b["id"], b["first_name"], "share", 50, None, ADMIN_ID, "Admin")
        await self.dues_mark_paid(self.msg("/mark_paid", ADMIN_USER))
        (mid,) = [m for (c, m) in self.payment_panel_sessions if c == CHAT_ID]

        # Admin 1 opens A's action view (A is row 0).
        await self.payment_panel_callback(self.call(f"pay_pick:0:u{a['id']}", ADMIN_USER, message_id=mid))
        # B's balance grows and the list refreshes — B is now row 0.
        db.add_dues_entry(CHAT_ID, None, b["id"], b["first_name"], "share", 200, None, ADMIN_ID, "Admin")
        await self.payment_panel_callback(self.call("pay_tolist", ADMIN_USER, message_id=mid))

        # Admin 1 taps "Full" on A's (stale-index) button.
        await self.payment_panel_callback(self.call(f"pay_full:0:u{a['id']}", ADMIN_USER, message_id=mid))
        self.assertEqual(_bal(a), 0)
        self.assertEqual(_bal(b), 250)


# ── 9: duplicated cancel ──────────────────────────────────────────────────────

class TestDuplicateCancel(_GameBase):

    async def test_repeated_cancel_does_not_reverse_a_second_game(self):
        _enable_dues()
        g1 = await self._play("Game One", 600, USERS[:10])
        g2 = await self._play("Game Two", 600, USERS[:10])
        self.assertEqual(_bal(USERS[0]), 120)

        # Two cards for the latest game (command sent twice), both tapped.
        await self.dues_cancel_game(self.msg("/cancel_game_dues", ADMIN_USER))
        await self.dues_cancel_game(self.msg("/cancel_game_dues", ADMIN_USER))
        await self._cancel(g2)
        await self._cancel(g2)

        self.assertIsNone(db.get_game_closure(g2))
        self.assertIsNotNone(db.get_game_closure(g1), "first game must stay settled")
        self.assertEqual(_bal(USERS[0]), 60)
