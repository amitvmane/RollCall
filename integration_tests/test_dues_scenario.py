"""
Dues & Treasury — realistic multi-game scenario test.

Simulates a football group over several weeks:
  • 20 real members + proxy variants
  • 4 game rounds including a parallel two-court day
  • Fund accumulation via rounding remainders
  • Penalty tiers, penalty assessment
  • Payments by collector and by admin
  • Fund subsidy on a game, fund topup, fund expense
  • Ad-hoc late joiner
  • Game cancellation and re-close
  • Final ledger integrity check

Runs entirely against a real SQLite database through the service layer.
No Telegram bot, no mocks — this is as close to production as possible
without live Telegram credentials.

Why the "ended path" works here:
  close_game() checks manager.get_rollcalls(chat_id) first.  Because this
  test uses a fresh negative chat_id the in-memory manager has no entry, so
  it falls straight to the DB-backed ended-rollcall path — which is identical
  to the live bot flow after a rollcall is ended with /erc.
"""
import asyncio
import sys
import os
import time
import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import db
from services import dues as dues_svc

# ── Test fixtures ─────────────────────────────────────────────────────────────

# Unique negative chat_id per run so parallel CI runs never collide.
CHAT = -(int(time.time() * 1000) % 10**12) - 2 * 10**15

ADMIN = {"uid": 9001, "name": "TestAdmin"}

# 20 members — realistic Indian football group names
MEMBERS = [
    {"user_id": 3000 + i, "first_name": name, "username": f"user{i:02d}"}
    for i, name in enumerate([
        "Amit", "Ravi", "Suresh", "Priya", "Vikram",
        "Anjali", "Dev", "Kavya", "Rohit", "Sneha",
        "Arjun", "Meera", "Kiran", "Pooja", "Nikhil",
        "Divya", "Sanjay", "Lakshmi", "Rahul", "Deepa",
    ], 1)
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _m(name: str) -> dict:
    """Look up a member by first_name."""
    return next(m for m in MEMBERS if m["first_name"] == name)


def _uid(name: str) -> int:
    return _m(name)["user_id"]


def _seed_chat_members() -> None:
    """Insert all 20 members into chat_members so _resolve_member treats them as real users."""
    conn = db.get_connection()
    cur = conn.cursor()
    for m in MEMBERS:
        cur.execute(
            "INSERT OR REPLACE INTO chat_members"
            " (chat_id, user_id, first_name, username, is_active)"
            " VALUES (?, ?, ?, ?, 1)",
            (CHAT, m["user_id"], m["first_name"], m["username"]),
        )
    conn.commit()
    cur.close()


def _insert_ended_rollcall(
    title: str,
    event_fee: int,
    in_names,
    proxies=None,
    collector_uid=None,
    collector_name=None,
    collector_paid_ground: int = 0,
    chat_id=None,
) -> int:
    """Insert a fully-ended rollcall with IN users into the DB and return its id."""
    if chat_id is None:
        chat_id = CHAT
    ended_at = datetime.datetime.utcnow().isoformat() + "Z"
    conn = db.get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO rollcalls "
        "(chat_id, title, is_active, is_cancelled, ended_at, event_fee,"
        " collector_uid, collector_name, collector_paid_ground)"
        " VALUES (?, ?, 0, 0, ?, ?, ?, ?, ?)",
        (chat_id, title, ended_at, str(event_fee),
         collector_uid, collector_name, collector_paid_ground),
    )
    rc_id = cur.lastrowid
    for pos, name in enumerate(in_names, 1):
        m = _m(name)
        cur.execute(
            "INSERT INTO users "
            "(rollcall_id, user_id, first_name, username, status, in_pos)"
            " VALUES (?, ?, ?, ?, 'in', ?)",
            (rc_id, m["user_id"], m["first_name"], m["username"], pos),
        )
    for pos, proxy in enumerate(proxies or [], len(in_names) + 1):
        cur.execute(
            "INSERT INTO proxy_users "
            "(rollcall_id, name, status, proxy_owner_id, in_pos)"
            " VALUES (?, ?, 'in', ?, ?)",
            (rc_id, proxy["name"], proxy.get("owner_id"), pos),
        )
    conn.commit()
    cur.close()
    return rc_id


def _close(subsidy: int = 0) -> dict:
    """Call close_game against the latest unclosed ended rollcall."""
    return asyncio.run(dues_svc.close_game(
        CHAT, subsidy=subsidy,
        admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
    ))


def _bal(name: str) -> int:
    return db.get_dues_balance(CHAT, user_id=_uid(name))


def _bal_proxy(proxy_name: str) -> int:
    return db.get_dues_balance(CHAT, member_name=proxy_name)


def _fund() -> int:
    return db.get_fund_balance(CHAT)


def _all_nonzero() -> dict[str, int]:
    rows = db.get_all_dues_balances(CHAT, nonzero_only=True)
    return {r["member_name"]: r["balance"] for r in rows}


# ── Scenario ──────────────────────────────────────────────────────────────────

class TestDuesScenario:
    """
    Four-week scenario executed as a single ordered class so each method
    builds on the state left by the previous one.
    """

    @classmethod
    def setup_class(cls):
        """Initialise chat, enable dues, seed tiers."""
        db.get_or_create_chat(CHAT)
        db.update_chat_settings(
            CHAT,
            dues_enabled=1,
            dues_round_step=10,
            upi_vpa="group@upi",
        )
        _seed_chat_members()
        dues_svc.seed_default_penalty_tiers(CHAT)

        # Verify tiers were seeded
        tiers = db.get_penalty_tiers(CHAT)
        assert len(tiers) == 3
        tier_names = {t["name"] for t in tiers}
        assert {"late_short", "late_long", "ditch"} == tier_names

    # ── Week 1: clean 12-player game, exact division ──────────────────────────

    def test_01_week1_close_game_12_players(self):
        """
        12 players, ground ₹600. 600/12 = 50 exactly.
        per_head = 50, remainder = 0, fund unchanged.
        """
        _insert_ended_rollcall(
            "Week 1 — Court A",
            event_fee=600,
            in_names=["Amit", "Ravi", "Suresh", "Priya", "Vikram",
                      "Anjali", "Dev", "Kavya", "Rohit", "Sneha",
                      "Arjun", "Meera"],
        )
        result = _close(subsidy=0)

        assert result["per_head"] == 50
        assert result["remainder"] == 0
        assert result["in_count"] == 12
        assert _fund() == 0

        # Every IN player owes ₹50
        for name in ["Amit", "Ravi", "Suresh", "Priya", "Vikram",
                     "Anjali", "Dev", "Kavya", "Rohit", "Sneha",
                     "Arjun", "Meera"]:
            assert _bal(name) == 50, f"{name} should owe ₹50"

        # Players who did not play have zero balance
        assert _bal("Kiran") == 0
        assert _bal("Pooja") == 0

    # ── Week 1 follow-up: 5 members pay, 1 gets a ditch penalty ─────────────

    def test_02_week1_payments_and_penalty(self):
        """
        Amit, Ravi, Suresh, Priya, Vikram pay ₹50 each → balance 0.
        Anjali gets a ditch penalty (₹200) → balance 50 + 200 = 250.
        Fund gets ₹200 from penalty.
        """
        for name in ["Amit", "Ravi", "Suresh", "Priya", "Vikram"]:
            dues_svc.mark_paid(
                CHAT, name,
                actor_uid=ADMIN["uid"], actor_name=ADMIN["name"],
                amount=50, is_admin=True,
            )

        for name in ["Amit", "Ravi", "Suresh", "Priya", "Vikram"]:
            assert _bal(name) == 0, f"{name} should be settled"

        # Anjali missed next week (penalty for week 1 absence in week 2 context)
        dues_svc.mark_penalty(
            CHAT, "ditch", "Anjali",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _bal("Anjali") == 50 + 200   # share + penalty
        assert _fund() == 200               # penalty credited to fund

    # ── Week 2: rounding remainder goes to fund ───────────────────────────────

    def test_03_week2_rounding_into_fund(self):
        """
        7 players + 1 unowned proxy = 8 total, ground ₹600.
        raw = ceil(600/8) = 75  →  per_head = 80 (next ₹10)
        remainder = 80×8 − 600 = 40  →  fund += 40  (total fund = 200+40 = 240)
        """
        _insert_ended_rollcall(
            "Week 2 — Court A",
            event_fee=600,
            in_names=["Kiran", "Pooja", "Nikhil", "Divya", "Sanjay",
                      "Lakshmi", "Rahul"],
            proxies=[{"name": "Walk-in Sunil", "owner_id": None}],
        )
        result = _close(subsidy=0)

        assert result["per_head"] == 80
        assert result["remainder"] == 40
        assert result["in_count"] == 8
        assert _fund() == 240   # 200 (penalty) + 40 (rounding)

        for name in ["Kiran", "Pooja", "Nikhil", "Divya", "Sanjay",
                     "Lakshmi", "Rahul"]:
            assert _bal(name) == 80

        # Unowned proxy billed by name
        assert _bal_proxy("Walk-in Sunil") == 80

    # ── Week 2 same day: parallel second court ───────────────────────────────

    def test_04_week2_parallel_second_court(self):
        """
        Same day, second court.  10 players, ground ₹500 → 500/10 = 50 exactly.
        This tests the parallel rollcall scenario: two separate closures on the
        same day, sequentially processed by close_game.
        """
        _insert_ended_rollcall(
            "Week 2 — Court B",
            event_fee=500,
            in_names=["Amit", "Ravi", "Suresh", "Priya", "Vikram",
                      "Dev", "Kavya", "Rohit", "Sneha", "Arjun"],
        )
        result = _close(subsidy=0)

        assert result["per_head"] == 50
        assert result["remainder"] == 0
        assert result["in_count"] == 10
        assert _fund() == 240   # unchanged — no rounding

        # These players now owe ₹50 from court B (Amit/Ravi/Suresh/Priya/Vikram
        # settled week 1, so they only owe week 2)
        for name in ["Amit", "Ravi", "Suresh", "Priya", "Vikram"]:
            assert _bal(name) == 50, f"{name} should owe ₹50 (court B)"

    # ── Collector flow ────────────────────────────────────────────────────────

    def test_05_collector_receives_payments(self):
        """
        Designate Rohit as collector for the most recent closure (court B).
        Sneha pays Rohit the court B share (₹50); Rohit records it.
        Non-collector, non-admin (Dev) cannot record a payment.

        Sneha owes week1 (₹50) + court B (₹50) = ₹100 total before this test.
        After paying ₹50, balance drops by 50 (not to zero — week1 is still owed).
        """
        dues_svc.set_collector(
            CHAT, "Rohit", paid_ground=False,
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        sneha_before = _bal("Sneha")   # 100: week1(50) + court B(50)
        kavya_before = _bal("Kavya")   # 100: week1(50) + court B(50)

        # Rohit (collector) records Sneha's court B payment
        dues_svc.mark_paid(
            CHAT, "Sneha",
            actor_uid=_uid("Rohit"), actor_name="Rohit",
            amount=50, is_admin=False,
        )
        assert _bal("Sneha") == sneha_before - 50

        # Dev is not collector and not admin — must be rejected
        from exceptions import insufficientPermissions
        with pytest.raises(insufficientPermissions):
            dues_svc.mark_paid(
                CHAT, "Kavya",
                actor_uid=_uid("Dev"), actor_name="Dev",
                amount=50, is_admin=False,
            )
        assert _bal("Kavya") == kavya_before   # unchanged

    # ── Week 3: fund subsidy + owned proxy + ad-hoc joiner ───────────────────

    def test_06_week3_with_subsidy_and_owned_proxy(self):
        """
        15 players + 1 owned proxy (owner = Deepa) = 16 total.
        Ground ₹640, subsidy ₹40 from fund.

        net = 640 − 40 = 600
        raw = ceil(600/16) = ceil(37.5) = 38
        per_head = ceil(38/10)×10 = 40
        remainder = 40×16 − 600 = 40
        fund: 240 − 40 (subsidy) + 40 (remainder) = 240 (unchanged)

        Owned proxy billed under proxy_name (user_id=None) with memo "owner: Deepa".
        Deepa's own share (₹40) goes to her user_id; proxy share goes to "Deepa's Friend"
        name-keyed entry — the two are separate rows, separate balances.
        """
        _insert_ended_rollcall(
            "Week 3 — Full House",
            event_fee=640,
            in_names=["Amit", "Ravi", "Suresh", "Priya", "Vikram",
                      "Anjali", "Dev", "Kavya", "Rohit", "Sneha",
                      "Arjun", "Meera", "Kiran", "Pooja", "Deepa"],
            proxies=[{"name": "Deepa's Friend", "owner_id": _uid("Deepa")}],
        )
        result = _close(subsidy=40)

        assert result["per_head"] == 40
        assert result["remainder"] == 40
        assert result["in_count"] == 16
        assert _fund() == 240   # 240 - 40 subsidy + 40 remainder = 240

        # Deepa owes her own share only (proxy is name-keyed, not on her user_id)
        # She had 0 balance before (not in week 1 or 2), so now 40
        assert _bal("Deepa") == 40

        # Owned proxy billed by name with owner reference in memo
        proxy_bal = _bal_proxy("Deepa's Friend")
        assert proxy_bal == 40
        proxy_entries = db.get_dues_entries(CHAT, member_name="Deepa's Friend", limit=5)
        assert any("owner:" in (e.get("memo") or "") for e in proxy_entries), \
            "Owned proxy entry should reference owner in memo"

    def test_07_week3_adhoc_late_joiner(self):
        """
        Lakshmi played court A (week 2) but missed the week 3 cutoff.
        She showed up late — admin adds her via add_adhoc at week 3's per_head (₹40).
        Fund gets +₹40 adjustment (tied to week 3 rollcall_id for clean reversal).
        """
        # Lakshmi is a real chat member (in chat_members) so _resolve_member gives her user_id
        lakshmi_before = _bal("Lakshmi")  # owes ₹80 from week 2 court A
        fund_before = _fund()

        dues_svc.add_adhoc(
            CHAT, "Lakshmi",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _bal("Lakshmi") == lakshmi_before + 40
        assert _fund() == fund_before + 40

    # ── Fund management ───────────────────────────────────────────────────────

    def test_08_fund_topup_and_expense(self):
        """
        Admin tops up fund with a donation (₹500).
        Then logs a shuttlecock expense (₹150).
        """
        fund_before = _fund()

        dues_svc.fund_topup(
            CHAT, 500, "sponsor donation",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _fund() == fund_before + 500

        dues_svc.log_expense(
            CHAT, 150, "new shuttlecocks",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _fund() == fund_before + 500 - 150

    # ── Cancel week 3 game and re-close ──────────────────────────────────────

    def test_09_cancel_and_reclose_week3(self):
        """
        Admin closed week 3 with wrong ground cost — cancels and re-closes.

        After cancel:
          • All share/adhoc dues entries reversed (cancel_credit rows written).
          • game_closures row deleted → rollcall is eligible for re-close again.
          • Fund reversal: rounding(+40) + subsidy(−40) + adhoc adj(+40) = +40
            → cancel writes −40 → fund_reversal = −40.

        Re-close via /ef + /close_game on the SAME rollcall (not a fresh insert):
          Corrected cost ₹600 (was ₹640), no subsidy, same 16 players.
          net=600, step=10 → raw=38 → per_head=40, remainder=40.
          (Same per_head coincidentally; different ground cost & remainder.)
        """
        amit_before_cancel = _bal("Amit")
        fund_before_cancel = _fund()

        # Before cancel: closure row must exist
        closure_w3 = db.get_nth_game_closure(CHAT, 0)
        assert closure_w3 is not None
        w3_rc_id = closure_w3["rollcall_id"]

        cancel_result = dues_svc.cancel_game_credit(
            CHAT, w3_rc_id,
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert cancel_result["reversed_count"] > 0

        # After cancel: closure row deleted → get_game_closure returns None
        assert db.get_game_closure(w3_rc_id) is None

        # Amit's week3 share reversed — balance drops by 40
        assert _bal("Amit") == amit_before_cancel - 40

        # Fund net for week3: rounding(+40) + subsidy(−40) + Lakshmi adhoc adj(+40) = +40
        # cancel_game_credit writes −40 reversal → fund_reversal = −40
        assert cancel_result["fund_reversal"] == -40
        assert _fund() == fund_before_cancel + cancel_result["fund_reversal"]

        # The rollcall is now closeable again — simulate /ef (corrected cost)
        # then /close_game on the SAME rollcall row (no fresh insert needed).
        db.update_rollcall(w3_rc_id, event_fee="600")
        reclose_result = _close(subsidy=0)

        assert reclose_result["rollcall_id"] == w3_rc_id   # same game
        assert reclose_result["per_head"] == 40
        assert reclose_result["in_count"] == 16
        assert reclose_result["remainder"] == 40

        # Amit owes ₹40 again from the re-close → balance back to pre-cancel level
        assert _bal("Amit") == amit_before_cancel

    # ── Waive and reimburse ───────────────────────────────────────────────────

    def test_10_waive_and_reimburse(self):
        """
        Anjali has a large outstanding balance (week1 share + ditch penalty).
        Admin waives ₹100 for medical reason.
        Admin reimburses Arjun ₹50 (overpaid previously).
        """
        anjali_before = _bal("Anjali")
        assert anjali_before > 0

        dues_svc.waive(
            CHAT, "Anjali", amount=100, reason="medical",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _bal("Anjali") == anjali_before - 100

        arjun_before = _bal("Arjun")
        dues_svc.reimburse(
            CHAT, "Arjun", amount=50, reason="overpaid week1",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _bal("Arjun") == arjun_before - 50

    # ── Custom penalty tier ───────────────────────────────────────────────────

    def test_11_custom_penalty_tier(self):
        """
        Group defines a new tier: 'no_boots' = ₹25.
        Assesses it against Dev.
        """
        dues_svc.add_penalty_tier(
            CHAT, "no_boots", 25, "forgot football boots",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        tier = db.get_penalty_tier(CHAT, "no_boots")
        assert tier is not None
        assert tier["amount"] == 25

        dev_before = _bal("Dev")
        dues_svc.mark_penalty(
            CHAT, "no_boots", "Dev",
            admin_uid=ADMIN["uid"], admin_name=ADMIN["name"],
        )
        assert _bal("Dev") == dev_before + 25
        assert _fund() > 0   # fund credited with penalty

    # ── Ledger integrity: append-only invariant ───────────────────────────────

    def test_12_ledger_append_only(self):
        """
        The dues ledger must never contain UPDATE or DELETE side-effects.
        Verify by checking that every balance is the mathematical SUM of its
        ledger entries (no row has been mutated, only new rows appended).
        """
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT MAX(user_id) AS uid, SUM(amount) AS total"
            " FROM dues_entries WHERE chat_id = ? AND user_id IS NOT NULL"
            " GROUP BY user_id",
            (CHAT,),
        )
        for row in cur.fetchall():
            uid, expected = row["uid"], row["total"]
            actual = db.get_dues_balance(CHAT, user_id=uid)
            assert actual == expected, \
                f"uid={uid}: get_dues_balance={actual} but SUM(entries)={expected}"
        cur.close()

    def test_13_fund_balance_matches_transactions(self):
        """Fund balance must equal SUM of all fund_transactions entries."""
        conn = db.get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT SUM(amount) FROM fund_transactions WHERE chat_id = ?",
            (CHAT,),
        )
        expected = (cur.fetchone()[0] or 0)
        cur.close()
        assert _fund() == expected

    # ── Full summary ──────────────────────────────────────────────────────────

    def test_14_summary_report(self):
        """
        Print a human-readable summary of the final state — useful for
        manual inspection when the test suite is run with -s.
        """
        balances = db.get_all_dues_balances(CHAT, nonzero_only=False)
        txn_count = db.count_fund_transactions(CHAT)
        entry_count = db.count_dues_entries(CHAT)

        print("\n\n" + "=" * 60)
        print(f"DUES SCENARIO SUMMARY  (chat={CHAT})")
        print("=" * 60)
        print(f"Total ledger entries : {entry_count}")
        print(f"Total fund txns      : {txn_count}")
        print(f"Fund balance         : ₹{_fund()}")
        print("\nMember balances:")
        for b in sorted(balances, key=lambda x: -x["balance"]):
            bar = "🔴" if b["balance"] > 0 else ("🟢" if b["balance"] < 0 else "⚪")
            print(f"  {bar} {b['member_name']:<22} ₹{b['balance']:>6}")
        print("=" * 60)

        # Sanity checks on the summary itself
        assert entry_count > 30, "Expected many ledger entries for a realistic scenario"
        assert txn_count > 5, "Expected multiple fund transactions"
        owed = [b for b in balances if b["balance"] > 0]
        assert len(owed) > 0, "At least some members should still owe dues"


# ── UPI routing end-to-end (separate chat to avoid state pollution) ───────────

CHAT_UPI = -98765  # isolated chat for UPI routing tests


class TestUPIRouting:
    """
    End-to-end tests for per-collector UPI and treasury UPI routing.

    Uses a separate chat ID so state doesn't bleed into the main scenario.
    """

    @classmethod
    def setup_class(cls):
        db.get_or_create_chat(CHAT_UPI)
        db.update_chat_settings(
            CHAT_UPI,
            dues_enabled=1,
            dues_round_step=10,
            upi_vpa="group@fallback",
        )
        dues_svc.seed_default_penalty_tiers(CHAT_UPI)
        db.upsert_chat_member(CHAT_UPI, 201, "Rahul", "rahul")
        db.upsert_chat_member(CHAT_UPI, 202, "Priya", "priya")
        db.upsert_chat_member(CHAT_UPI, 203, "Kiran", "kiran")

    def _insert_rc(self, title="UPI Test Game", fee=600, collector_uid=None,
                   collector_name=None, collector_paid=0, collector_upi=None):
        rc_id = _insert_ended_rollcall(
            title=title, event_fee=fee,
            in_names=["Rahul", "Priya", "Kiran"],
            collector_uid=collector_uid,
            collector_name=collector_name,
            collector_paid_ground=collector_paid,
            chat_id=CHAT_UPI,
        )
        if collector_upi:
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("UPDATE rollcalls SET collector_upi=? WHERE id=?",
                        (collector_upi, rc_id))
            conn.commit()
            cur.close()
        return rc_id

    def test_15_collector_upi_in_close_announcement(self):
        """
        When set_collector provides a UPI, the close announcement shows it
        instead of the group fallback UPI.
        """
        rc_id = self._insert_rc(
            title="UPI Routing Test",
            collector_uid=201, collector_name="Rahul",
            collector_paid=1, collector_upi="rahul@ybl",
        )
        result = asyncio.run(
            dues_svc.close_game(CHAT_UPI, subsidy=0,
                                admin_uid=999, admin_name="TestAdmin")
        )
        ann = result["announcement"]
        assert "rahul@ybl" in ann, f"Expected collector UPI in announcement: {ann}"
        assert "group@fallback" not in ann, \
            f"Fallback group UPI should not appear when collector UPI is set: {ann}"
        assert result["per_head"] == 200  # 600 / 3, step=10

    def test_16_treasury_upi_shown_for_penalties(self):
        """
        When treasury_upi is set, penalty announcements show it.
        Falls back to upi_vpa when treasury_upi is absent.
        """
        db.update_chat_settings(CHAT_UPI, treasury_upi="treasurer@hdfc")

        result = dues_svc.mark_penalty(
            CHAT_UPI, "ditch", "priya", admin_uid=999, admin_name="TestAdmin"
        )
        ann = result["announcement"]
        assert "treasurer@hdfc" in ann, \
            f"Expected treasury UPI in penalty announcement: {ann}"
        assert "group@fallback" not in ann

    def test_17_treasury_upi_fallback_when_not_set(self):
        """When treasury_upi is cleared, penalty announcement falls back to upi_vpa."""
        db.update_chat_settings(CHAT_UPI, treasury_upi=None)

        result = dues_svc.mark_penalty(
            CHAT_UPI, "ditch", "kiran", admin_uid=999, admin_name="TestAdmin"
        )
        ann = result["announcement"]
        assert "group@fallback" in ann, \
            f"Expected group UPI as fallback in penalty: {ann}"

    def test_18_set_treasury_upi_service(self):
        """set_treasury_upi persists to DB and is reflected in get_dues_settings."""
        dues_svc.set_treasury_upi(CHAT_UPI, "newtreas@icici", admin_uid=999, admin_name="Admin")
        settings = dues_svc.get_dues_settings(CHAT_UPI)
        assert settings["treasury_upi"] == "newtreas@icici"

    def test_19_set_collector_with_upi_stored_on_closure(self):
        """
        set_collector with a UPI on a post-close game stores the UPI on the closure
        and it is returned via get_game_closure.
        """
        closure = db.get_latest_game_closure(CHAT_UPI)
        assert closure is not None, "Need a closed game from test_15"
        rc_id = closure["rollcall_id"]

        dues_svc.set_collector(
            CHAT_UPI, "rahul", paid_ground=True,
            admin_uid=999, admin_name="Admin",
            collector_upi="rahul@updated",
        )

        updated = db.get_game_closure(rc_id)
        assert updated["collector_upi"] == "rahul@updated", \
            f"Expected collector_upi stored on closure: {updated}"

    def test_20_no_upi_line_when_neither_set(self):
        """When both upi_vpa and treasury_upi are None, no UPI shown in penalty."""
        db.update_chat_settings(CHAT_UPI, upi_vpa=None, treasury_upi=None)

        result = dues_svc.mark_penalty(
            CHAT_UPI, "late_short", "rahul", admin_uid=999, admin_name="Admin"
        )
        ann = result["announcement"]
        assert "💳" not in ann, f"No UPI line expected when neither is set: {ann}"


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest as _pytest
    _pytest.main([__file__, "-v", "-s"])
