"""Every vote panel offers the web link — including the ones the scheduler sends.

Reported from production: a rollcall started by the weekly scheduler came up
with no "Vote on Web" button, and then grew one the moment somebody voted.

The cause was an optional argument. `get_status_keyboard(rc_number, web_url="")`
left each of eleven call sites to compute the URL and pass it; nine did, and
the two in check_reminders.py — both scheduler paths — did not. Voting rebuilt
the panel through a call site that DID pass it, which is why the button
appeared late and why this looked like a refresh quirk rather than a miss.

An optional argument that silently degrades a feature is the bug, so the fix
is structural: the keyboard takes the chat id and derives the URL itself.
There is nothing left for a call site to forget, and these tests keep it that
way rather than re-checking the two sites that happened to be wrong.
"""
import ast
import os
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

_ROOT = pathlib.Path(__file__).resolve().parent.parent / "rollCall"


def _keyboard_calls():
    for path in sorted(_ROOT.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        src = path.read_text(encoding="utf-8")
        if "get_status_keyboard" not in src:
            continue
        for node in ast.walk(ast.parse(src, str(path))):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("get_status_keyboard"):
                yield path.relative_to(_ROOT), node


class TestNoCallSiteCanOmitTheLink(unittest.TestCase):
    def test_every_call_site_passes_a_chat_id(self):
        missing = [f"{p}:{n.lineno}" for p, n in _keyboard_calls()
                   if len(n.args) + len(n.keywords) < 2]
        self.assertEqual(missing, [], f"panel keyboard built without a chat id: {missing}")

    def test_there_are_call_sites_to_check(self):
        """Guards the guard: a renamed helper would make the sweep above pass
        by finding nothing at all."""
        self.assertGreaterEqual(len(list(_keyboard_calls())), 10)

    def test_chat_id_is_required_by_the_signature(self):
        """A default would let the old bug back in silently."""
        import inspect
        from handlers.lifecycle import get_status_keyboard
        params = inspect.signature(get_status_keyboard).parameters
        self.assertIn("chat_id", params)
        self.assertIs(params["chat_id"].default, inspect.Parameter.empty)

    def test_no_call_site_computes_the_url_itself(self):
        """Deriving it at the call site is what created eleven chances to get
        it wrong. It belongs in one place.

        Matched on real call nodes, not on the text: core.py mentions
        _group_web_url in a comment describing the same env-var pattern, and
        a prose reference is not a second implementation.
        """
        callers = []
        for path in sorted(_ROOT.rglob("*.py")):
            if "__pycache__" in str(path) or path.name == "lifecycle.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
                if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("_group_web_url"):
                    callers.append(f"{path.relative_to(_ROOT)}:{node.lineno}")
        self.assertEqual(callers, [], f"should let the keyboard derive the URL: {callers}")


class TestTheUrlDecision(unittest.TestCase):
    """The actual decision the keyboard now makes for itself.

    Asserted on _group_web_url rather than on a built markup because
    conftest mocks telebot away at this layer — an InlineKeyboardMarkup here
    has no readable buttons, so a test of one would pass whatever happened.
    The rendered button is asserted in scripts/functional_test.py, against
    real telebot.
    """

    def setUp(self):
        from handlers import lifecycle
        self.lc = lifecycle

    def _url(self, base, token="tok123", boom=False):
        # Patched on db, not on the module: _group_web_url imports
        # get_or_create_chat inside the function body, so a module-level
        # patch never intercepts it.
        import db
        kw = {"side_effect": RuntimeError("db down")} if boom \
            else {"return_value": {"group_web_token": token}}
        with patch.dict(os.environ, {"WEB_BASE_URL": base}), \
             patch.object(db, "get_or_create_chat", **kw):
            return self.lc._group_web_url(-100123)

    def test_url_is_built_when_web_is_configured(self):
        self.assertEqual(self._url("https://rollcall.example"),
                         "https://rollcall.example/web/group/tok123")

    def test_trailing_slash_does_not_double_up(self):
        self.assertEqual(self._url("https://rollcall.example/"),
                         "https://rollcall.example/web/group/tok123")

    def test_empty_when_web_is_not_configured(self):
        """Self-hosted bots with no public URL must not get a dead button."""
        self.assertEqual(self._url(""), "")

    def test_empty_when_the_group_has_no_token(self):
        self.assertEqual(self._url("https://rollcall.example", token=None), "")

    def test_a_db_failure_degrades_to_no_button_not_a_crash(self):
        """The panel matters more than the button on it."""
        self.assertEqual(self._url("https://rollcall.example", boom=True), "")


class TestSchedulerPathsSpecifically(unittest.TestCase):
    """The two sites that were actually wrong. Named so a regression here
    points straight at the reported symptom."""

    def test_both_scheduler_panel_sends_pass_the_chat_id(self):
        src = (_ROOT / "check_reminders.py").read_text(encoding="utf-8")
        calls = [ast.unparse(n) for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call)
                 and ast.unparse(n.func).endswith("get_status_keyboard")]
        self.assertEqual(len(calls), 2, f"expected the two scheduler sends, got {calls}")
        for call in calls:
            self.assertIn("chat_id", call)


if __name__ == "__main__":
    unittest.main()
