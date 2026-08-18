"""Ratchet: every registered bot command must be exercised by the functional test.

scripts/functional_test.py is the only layer that drives real telebot routing,
middleware and handlers (integration_tests/ mocks telebot; the smoke test only
checks imports). Its value depends entirely on it staying current as commands
are added — and it spent two months frozen after the PR that created it, which
is exactly the decay this guard exists to prevent.

Semantics, deliberately the same ratchet as security/audit_baseline.json:

  * a registered command with no functional coverage and NOT in the allowlist
    -> FAIL. A newly added command therefore fails CI until it is covered.
  * an allowlisted command that IS now covered -> FAIL, "remove it from the
    allowlist". Keeps the allowlist from rotting into a list of things that
    are no longer true, and makes the number only ever shrink.

The allowlist is the historical debt from before this guard existed. It is not
an amnesty for new work: shrink it, never extend it. Adding a line here for a
NEW command defeats the entire point of the file.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import commands_registry  # noqa: E402

_FUNCTIONAL_TEST = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "functional_test.py"
)

# Commands not yet driven by scripts/functional_test.py. Pre-existing debt,
# measured 2026-08-18 (40 of 92 commands were covered). Shrink this list as
# coverage is added; a new command must never be appended here.
KNOWN_UNCOVERED = {
    # Web / token surfaces — mostly thin wrappers over URL generation.
    "weblink", "mytoken", "weblogin", "gentoken",
    # Stats / reporting extras.
    "calendar", "summary", "export_stats", "card",
    # Lifecycle & settings extras.
    "repeat", "cancel_roll_call", "set_rollcall_time", "set_rollcall_reminder",
    "delete_user", "set_status", "auto_buzz", "remind_before_close",
    "remind_after_open", "mark_absent", "broadcast",
}


def _covered_commands(source: str) -> set:
    """Commands the functional test actually types as a bot command.

    Matches "/name" followed by a boundary so that "/dues" does not count as
    coverage for "/dues_export" — a substring match would silently inflate the
    number and make this guard worthless.
    """
    return set(re.findall(r"/([a-z_0-9]+)(?=[\s\"'@:]|\\n)", source))


class TestFunctionalCoverage(unittest.TestCase):

    def setUp(self):
        with open(_FUNCTIONAL_TEST) as fh:
            self.typed = _covered_commands(fh.read())

    def _is_covered(self, entry):
        names = [entry["name"]] + list(entry.get("aliases") or [])
        return any(n in self.typed for n in names)

    def test_no_new_uncovered_commands(self):
        uncovered = sorted(
            e["name"] for e in commands_registry.COMMANDS
            if not self._is_covered(e) and e["name"] not in KNOWN_UNCOVERED
        )
        self.assertEqual(
            uncovered, [],
            "\n\nThese commands are registered but never exercised by "
            "scripts/functional_test.py:\n  " + "\n  ".join(uncovered) +
            "\n\nAdd a scenario for each to scripts/functional_test.py. Do NOT "
            "add them to KNOWN_UNCOVERED — that list is pre-existing debt and "
            "must only shrink.\n"
        )

    def test_allowlist_has_no_stale_entries(self):
        registered = {e["name"] for e in commands_registry.COMMANDS}
        by_name = {e["name"]: e for e in commands_registry.COMMANDS}

        now_covered = sorted(
            n for n in KNOWN_UNCOVERED
            if n in by_name and self._is_covered(by_name[n])
        )
        self.assertEqual(
            now_covered, [],
            "\n\nThese are in KNOWN_UNCOVERED but the functional test now "
            "covers them:\n  " + "\n  ".join(now_covered) +
            "\n\nRemove them from KNOWN_UNCOVERED in this file.\n"
        )

        gone = sorted(KNOWN_UNCOVERED - registered)
        self.assertEqual(
            gone, [],
            "\n\nKNOWN_UNCOVERED lists commands that are no longer in the "
            "registry:\n  " + "\n  ".join(gone) + "\n\nRemove them.\n"
        )

    def test_guard_itself_detects_a_gap(self):
        """The guard is worthless if its matcher silently matches everything —
        prove it can still say no."""
        self.assertNotIn("definitely_not_a_real_command", self.typed)
        self.assertFalse(
            self._is_covered({"name": "definitely_not_a_real_command", "aliases": []})
        )


if __name__ == "__main__":
    unittest.main()
