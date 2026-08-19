"""Backfilling pre-existing dues — /adjust_dues and /import_dues.

These write to the append-only ledger, so the invariants worth pinning are:
never edit, never touch the fund, and never half-apply a batch.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from exceptions import incorrectParameter, parameterMissing  # noqa: E402
from services import dues as dues_svc  # noqa: E402


class TestCommandMatching(unittest.TestCase):
    """Regression: _cmd used to split on " " rather than any whitespace, so a
    multi-line message whose first line had no space ("/import_dues\\nAlice
    300") produced the token "/import_dues\\nalice" and matched NO handler —
    the bot replied nothing and logged nothing. Silent no-ops are the worst
    failure mode here, so pin it."""

    def test_multiline_command_still_matches(self):
        from handlers.dues import _cmd
        self.assertEqual(_cmd("/import_dues\nAlice 300 June"), "/import_dues")

    def test_plain_and_at_suffixed_forms(self):
        from handlers.dues import _cmd
        self.assertEqual(_cmd("/dues"), "/dues")
        self.assertEqual(_cmd("/dues@RollCallBot extra"), "/dues")
        self.assertEqual(_cmd("/adjust_dues Alice 300"), "/adjust_dues")

    def test_empty_text_does_not_raise(self):
        from handlers.dues import _cmd
        self.assertEqual(_cmd(""), "")
        self.assertEqual(_cmd(None), "")


class TestParseImportLines(unittest.TestCase):

    def test_parses_name_amount_reason(self):
        rows = dues_svc.parse_import_lines("Alice 300 June games")
        self.assertEqual(rows[0]["name"], "Alice")
        self.assertEqual(rows[0]["amount"], 300)
        self.assertEqual(rows[0]["reason"], "June games")

    def test_multi_word_name_uses_last_numeric_token(self):
        """'Ravi Kumar 300 old dues' must not truncate the name at the first
        token — the amount is located from the right."""
        rows = dues_svc.parse_import_lines("Ravi Kumar 300 old dues")
        self.assertEqual(rows[0]["name"], "Ravi Kumar")
        self.assertEqual(rows[0]["amount"], 300)
        self.assertEqual(rows[0]["reason"], "old dues")

    def test_negative_and_signed_amounts(self):
        rows = dues_svc.parse_import_lines("Bob -50 overpaid\nCarol +75")
        self.assertEqual(rows[0]["amount"], -50)
        self.assertEqual(rows[1]["amount"], 75)

    def test_blank_and_comment_lines_skipped(self):
        rows = dues_svc.parse_import_lines("Alice 100\n\n# a note\nBob 200")
        self.assertEqual([r["name"] for r in rows], ["Alice", "Bob"])

    def test_line_without_amount_is_flagged_not_silently_dropped(self):
        rows = dues_svc.parse_import_lines("Alice 100\nBobbb\nCarol 50")
        self.assertIsNone(rows[0]["error"])
        self.assertIsNotNone(rows[1]["error"])
        self.assertEqual(rows[1]["line"], 2)

    def test_amount_only_line_is_an_error(self):
        """A bare number has no name — must not become a nameless entry."""
        rows = dues_svc.parse_import_lines("300")
        self.assertIsNotNone(rows[0]["error"])


class TestAdjustDues(unittest.TestCase):

    def test_writes_adjustment_entry_and_never_touches_fund(self):
        """Money that predates the bot never passed through the fund, so
        crediting it would invent money. mark_paid behaves the same way."""
        with patch("services.dues._resolve_member",
                   return_value={"user_id": 1, "member_name": "Alice"}), \
             patch("services.dues._known_proxy_names", return_value=[]), \
             patch("services.dues.db.add_dues_entry") as add_entry, \
             patch("services.dues.db.add_fund_transaction") as add_fund, \
             patch("services.dues.db.log_admin_action"), \
             patch("services.dues.db.get_dues_balance", return_value=300):
            r = dues_svc.adjust_dues(-100, "Alice", 300, "June games", 7, "Admin")

        add_fund.assert_not_called()
        args = add_entry.call_args.args
        self.assertIsNone(args[1], "rollcall_id must be None — not tied to a game")
        self.assertEqual(args[4], "adjustment")
        self.assertEqual(args[5], 300)
        self.assertEqual(r["balance"], 300)
        self.assertFalse(r["is_new_name"])

    def test_negative_amount_credits(self):
        with patch("services.dues._resolve_member",
                   return_value={"user_id": 1, "member_name": "Alice"}), \
             patch("services.dues._known_proxy_names", return_value=[]), \
             patch("services.dues.db.add_dues_entry") as add_entry, \
             patch("services.dues.db.add_fund_transaction"), \
             patch("services.dues.db.log_admin_action"), \
             patch("services.dues.db.get_dues_balance", return_value=-50):
            dues_svc.adjust_dues(-100, "Alice", -50, "", 7, "Admin")
        self.assertEqual(add_entry.call_args.args[5], -50)

    def test_zero_amount_rejected(self):
        with self.assertRaises(incorrectParameter):
            dues_svc.adjust_dues(-100, "Alice", 0, "", 7, "Admin")

    def test_absurd_amount_rejected(self):
        with self.assertRaises(incorrectParameter):
            dues_svc.adjust_dues(-100, "Alice", 99_999_999, "", 7, "Admin")

    def test_empty_name_rejected(self):
        with self.assertRaises(parameterMissing):
            dues_svc.adjust_dues(-100, "   ", 300, "", 7, "Admin")

    def test_unknown_name_creates_entry_under_that_name(self):
        """Backfill must be able to name people with no ledger history — that
        is the entire point — so a clean 'not found' falls through."""
        with patch("services.dues._resolve_member",
                   side_effect=incorrectParameter("Member not found")), \
             patch("services.dues._known_proxy_names", return_value=[]), \
             patch("services.dues._canonicalize_member",
                   return_value={"user_id": None, "member_name": "Ghostie"}), \
             patch("services.dues.db.add_dues_entry") as add_entry, \
             patch("services.dues.db.log_admin_action"), \
             patch("services.dues.db.get_dues_balance", return_value=300):
            r = dues_svc.adjust_dues(-100, "Ghostie", 300, "", 7, "Admin")
        self.assertTrue(r["is_new_name"])
        self.assertEqual(add_entry.call_args.args[3], "Ghostie")

    def test_ambiguous_name_still_raises(self):
        """Only 'not found' may fall through — two matching people is a real
        error and must not silently create a third identity."""
        with patch("services.dues._resolve_member",
                   side_effect=incorrectParameter("Ambiguous: 2 members match")), \
             patch("services.dues._known_proxy_names", return_value=[]), \
             patch("services.dues.db.add_dues_entry") as add_entry:
            with self.assertRaises(incorrectParameter):
                dues_svc.adjust_dues(-100, "Al", 300, "", 7, "Admin")
        add_entry.assert_not_called()


class TestImportDues(unittest.TestCase):

    def test_malformed_line_aborts_whole_batch(self):
        """A typo halfway down must not leave a half-applied import."""
        with patch("services.dues.adjust_dues") as adj:
            with self.assertRaises(incorrectParameter):
                dues_svc.import_dues(-100, "Alice 100\nBobbb\nCarol 50", 7, "Admin")
        adj.assert_not_called()

    def test_preview_writes_nothing(self):
        with patch("services.dues.adjust_dues") as adj:
            r = dues_svc.import_dues(-100, "Alice 100\nBob 200", 7, "Admin", preview=True)
        adj.assert_not_called()
        self.assertTrue(r["preview"])
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["total"], 300)

    def test_applies_each_line(self):
        with patch("services.dues.adjust_dues",
                   side_effect=lambda c, n, a, r_, u, an: {
                       "member_name": n, "amount": a, "balance": a,
                       "is_new_name": False, "user_id": 1, "reason": r_}) as adj:
            r = dues_svc.import_dues(-100, "Alice 100\nBob 200\nCarol -50", 7, "Admin")
        self.assertEqual(adj.call_count, 3)
        self.assertEqual(r["count"], 3)
        self.assertEqual(r["total"], 250)

    def test_empty_payload_rejected(self):
        with self.assertRaises(parameterMissing):
            dues_svc.import_dues(-100, "   \n\n", 7, "Admin")

    def test_reports_new_names(self):
        with patch("services.dues.adjust_dues",
                   side_effect=lambda c, n, a, r_, u, an: {
                       "member_name": n, "amount": a, "balance": a,
                       "is_new_name": n == "Ghostie", "user_id": None, "reason": r_}):
            r = dues_svc.import_dues(-100, "Alice 100\nGhostie 200", 7, "Admin")
        self.assertEqual(r["new_names"], ["Ghostie"])


if __name__ == "__main__":
    unittest.main()
