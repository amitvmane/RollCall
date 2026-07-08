"""
Unit tests for dues_snapshot() and dues_export_csv() service functions.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from unittest.mock import patch, MagicMock
import pytest

CHAT = -7001


def _bal(name, balance, uid=None):
    return {"member_name": name, "user_id": uid, "balance": balance}


def _entry(entry_type="share", amount=100, created_at="2026-07-06"):
    return {"entry_type": entry_type, "amount": amount, "created_at": created_at}


def _closure(title="Friday Game", per_head=90, in_count=12,
              created_at="2026-07-06"):
    return {
        "title": title, "per_head": per_head, "in_count": in_count,
        "created_at": created_at,
    }


# ── dues_snapshot ─────────────────────────────────────────────────────────────

class TestDuesSnapshot:
    def _run(self, balances, fund=500, closure=None):
        from services import dues as svc
        with patch("db.get_all_dues_balances", return_value=balances), \
             patch("db.get_fund_balance", return_value=fund), \
             patch("db.get_nth_game_closure", return_value=closure):
            return svc.dues_snapshot(CHAT)

    def test_returns_text_and_raw_fields(self):
        result = self._run([_bal("Amit", 90, 1)])
        assert "text" in result
        assert "fund_balance" in result
        assert "balances" in result

    def test_owed_members_listed(self):
        result = self._run([_bal("Amit", 90, 1), _bal("Ravi", 50, 2)])
        assert "Amit" in result["text"]
        assert "Ravi" in result["text"]
        assert "₹90" in result["text"]

    def test_settled_members_shown(self):
        result = self._run([_bal("Amit", 90, 1), _bal("Priya", 0, 2)])
        assert "Priya" in result["text"]
        assert "Settled" in result["text"]

    def test_credit_members_shown(self):
        result = self._run([_bal("Suresh", -30, 3)])
        assert "Suresh" in result["text"]
        assert "Credit" in result["text"] or "credit" in result["text"].lower()

    def test_no_outstanding_message_when_all_settled(self):
        result = self._run([_bal("Amit", 0, 1), _bal("Priya", 0, 2)])
        assert "No outstanding" in result["text"]

    def test_last_game_shown_when_closure_exists(self):
        result = self._run(
            [_bal("Amit", 90, 1)],
            closure=_closure("Friday Futsal", 90, 12),
        )
        assert "Friday Futsal" in result["text"]
        assert "₹90/head" in result["text"]
        assert "12 players" in result["text"]

    def test_no_last_game_line_when_no_closure(self):
        result = self._run([_bal("Amit", 90, 1)], closure=None)
        assert "Last game" not in result["text"]

    def test_fund_balance_shown(self):
        result = self._run([], fund=1240)
        assert "₹1,240" in result["text"] or "₹1240" in result["text"]

    def test_settled_overflow_shows_count(self):
        # More than 8 settled members — extras shown as (+N more)
        balances = [_bal(f"User{i}", 0, i) for i in range(12)]
        result = self._run(balances)
        assert "more" in result["text"]

    def test_empty_group_no_crash(self):
        result = self._run([])
        assert "text" in result
        assert "No outstanding" in result["text"]


# ── dues_export_csv ───────────────────────────────────────────────────────────

class TestDuesExportCSV:
    def _run(self, balances, last_entry=None):
        from services import dues as svc
        le = last_entry or _entry()
        with patch("db.get_all_dues_balances", return_value=balances), \
             patch("db.get_dues_entries", return_value=[le]):
            return svc.dues_export_csv(CHAT)

    def test_returns_string(self):
        result = self._run([_bal("Amit", 90, 1)])
        assert isinstance(result, str)

    def test_header_row_present(self):
        result = self._run([_bal("Amit", 90, 1)])
        first_line = result.splitlines()[0]
        assert "name" in first_line
        assert "balance" in first_line
        assert "status" in first_line

    def test_owed_status(self):
        result = self._run([_bal("Amit", 90, 1)])
        assert "owed" in result

    def test_credit_status(self):
        result = self._run([_bal("Priya", -30, 2)])
        assert "credit" in result

    def test_settled_status(self):
        result = self._run([_bal("Ravi", 0, 3)])
        assert "settled" in result

    def test_member_name_in_csv(self):
        result = self._run([_bal("Suresh", 120, 4)])
        assert "Suresh" in result

    def test_balance_in_csv(self):
        result = self._run([_bal("Nikhil", 75, 5)])
        assert "75" in result

    def test_last_entry_type_in_csv(self):
        result = self._run(
            [_bal("Amit", 90, 1)],
            last_entry=_entry("payment", -90, "2026-07-07"),
        )
        assert "payment" in result

    def test_empty_no_crash(self):
        from services import dues as svc
        with patch("db.get_all_dues_balances", return_value=[]), \
             patch("db.get_dues_entries", return_value=[]):
            result = svc.dues_export_csv(CHAT)
        assert "name" in result  # header still present

    def test_multiple_members_multiple_rows(self):
        balances = [_bal("Amit", 90, 1), _bal("Ravi", 0, 2), _bal("Priya", -20, 3)]
        result = self._run(balances)
        lines = [l for l in result.splitlines() if l.strip()]
        assert len(lines) == 4  # header + 3 members
