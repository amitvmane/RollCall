"""
Guard: rules that must have exactly ONE implementation.

Every defect this file exists to stop had the same shape — a rule written out
by hand in N places, and the N+1th path that needed it simply never got a copy:

  • waitlist promotion lived in addOut/addMaybe/set_limit but not in the
    delete path, so removing a member left the IN list under its cap
  • template→rollcall setup was copied into the recurring auto-start, which
    never gained the offset_* fallback, so those rollcalls never auto-closed

A source-level check is the only layer that catches this: each copy passes its
own tests: it's the *missing* copy that breaks, and there is no test for code
nobody wrote. So assert the single implementation instead.

Adding a call site is always fine. Adding a second *implementation* fails here.
"""
import os
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..", "rollCall")


def _sources():
    for root, _, files in os.walk(ROOT):
        if "__pycache__" in root:
            continue
        for fn in sorted(files):
            if fn.endswith(".py"):
                path = os.path.join(root, fn)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                yield rel, open(path, encoding="utf-8").read()


class TestSingleImplementation(unittest.TestCase):

    def _files_containing(self, needle):
        return sorted(rel for rel, src in _sources() if needle in src)

    def test_promotion_is_announced_in_one_place(self):
        """The waitlist→IN chat message. Five copies once said five things —
        the proxy one omitted the rollcall title and number entirely."""
        self.assertEqual(
            self._files_containing("→ IN (from WAITING)"),
            ["handlers/promotion.py"],
            "Announce promotions via handlers.promotion.announce_promotions / "
            "announce_one instead of writing the message again.",
        )

    def test_promotion_stats_are_recorded_in_one_place(self):
        """total_waiting_to_in + total_in + rollcall total_in move together;
        six copies meant six chances to bump two of the three."""
        self.assertEqual(
            self._files_containing('increment_user_stat(chat_id, user_id, "total_waiting_to_in")'),
            ["services/common.py"],
            "Call services.common.record_promotion_stats instead.",
        )

    def test_rollcall_lookup_error_lives_in_one_place(self):
        """Resolve-or-raise: same two exceptions, same order, one copy."""
        self.assertEqual(
            self._files_containing("The rollcall number doesn't exist"),
            ["services/common.py"],
            "Use services.common.resolve_rollcall_or_raise instead.",
        )

    def test_chat_write_lock_has_one_name_at_call_sites(self):
        """Two names for one lock is how a codebase stops being greppable for
        'is this path locked?'. get_erc_lock survives only as the shim."""
        self.assertEqual(
            self._files_containing("get_erc_lock"),
            ["rollcall_manager.py"],
            "Use manager.get_chat_write_lock(chat_id) at call sites.",
        )

    def test_template_to_rollcall_setup_lives_in_one_place(self):
        """Whoever starts a rollcall from a template — the command, the
        one-time schedule, the recurring auto-start — goes through
        services.templates.start_template, so they can't disagree about which
        template fields apply."""
        # Fields that only mean anything while BUILDING a rollcall. (event_day
        # is excluded on purpose: handlers/templates.py reads it to render
        # /templates and to sanity-check a schedule, neither of which is a
        # second construction path.)
        for field in ('tmpl["inlistlimit"]', 'tmpl.get("offsetdays")'):
            self.assertEqual(
                self._files_containing(field), ["services/templates.py"],
                f"{field} is read outside start_template — start rollcalls from "
                "templates via services.templates.start_template instead.",
            )


if __name__ == "__main__":
    unittest.main()
