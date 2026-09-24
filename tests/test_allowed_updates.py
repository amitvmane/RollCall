"""Telegram must be told we want callback_query, on every single start.

Production incident, 2026-09-21 to 2026-09-24: every inline panel button was
dead and completely silent. Typed commands worked perfectly. Nothing appeared
in the logs, /health reported the bot healthy, and the watchdog never fired.

Cause: Telegram stores `allowed_updates` SERVER-SIDE, per bot, and reuses it
for any later call that omits the parameter. This deployment's stored list had
become:

    ["message", "edited_message", "channel_post", "edited_channel_post"]

callback_query absent. So button presses were never delivered to the process
at all — which is why there was nothing to log and nothing to alert on. The
setting survives restarts, redeploys, and deleteWebhook (which does not reset
it), so it would have stayed broken indefinitely.

The code had never set allowed_updates anywhere, which is what allowed a stale
remote value to govern the bot's behaviour invisibly.
"""
import ast
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

_RUNNER = pathlib.Path(__file__).resolve().parent.parent / "rollCall" / "runner.py"


class TestDerivedFromRegisteredHandlers(unittest.TestCase):
    """Derived, not hard-coded: a new handler type must not be able to ship
    without Telegram being told to deliver it."""

    def setUp(self):
        import handlers  # noqa: F401  — registers the decorators
        import runner
        self.runner = runner

    def test_callback_query_is_requested(self):
        """The one that broke. Without it every inline button is dead."""
        self.assertIn("callback_query", self.runner.allowed_updates())

    def test_message_is_requested(self):
        self.assertIn("message", self.runner.allowed_updates())

    def test_every_registered_handler_type_is_requested(self):
        """The actual invariant — the list follows the handlers rather than
        having to be remembered alongside them."""
        from telebot import util as tb_util
        from bot_state import bot

        requested = set(self.runner.allowed_updates())
        for update_type in tb_util.update_types:
            bucket = self.runner._HANDLER_BUCKET_ALIASES.get(
                update_type, f"{update_type}_handlers")
            if getattr(bot, bucket, None):
                self.assertIn(update_type, requested,
                              f"{update_type} handlers exist but it is never requested")

    def test_nothing_useless_is_requested(self):
        """Asking for update types we don't handle is free bandwidth for
        Telegram to waste and noise for us to ignore."""
        from bot_state import bot
        for update_type in self.runner.allowed_updates():
            bucket = self.runner._HANDLER_BUCKET_ALIASES.get(
                update_type, f"{update_type}_handlers")
            has_handlers = bool(getattr(bot, bucket, None))
            self.assertTrue(has_handlers or update_type in self.runner._MINIMUM_UPDATES,
                            f"{update_type} requested but nothing handles it")

    def test_a_broken_introspection_degrades_to_working_not_silent(self):
        """If telebot renames its handler buckets, the failure mode must be
        'still receives the essentials', never 'receives nothing'."""
        self.assertIn("callback_query", self.runner._MINIMUM_UPDATES)
        self.assertIn("message", self.runner._MINIMUM_UPDATES)


class TestItIsActuallySentOnEveryStart(unittest.TestCase):
    """Deriving the right list is worthless if it is never passed."""

    def setUp(self):
        self.src = _RUNNER.read_text(encoding="utf-8")
        self.tree = ast.parse(self.src)

    def _call(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(name):
                return node
        self.fail(f"no call to {name} found in runner.py")

    def test_long_poll_sends_allowed_updates(self):
        kw = {k.arg for k in self._call("infinity_polling").keywords}
        self.assertIn("allowed_updates", kw,
                      "polling without allowed_updates inherits Telegram's stored value")

    def test_webhook_registration_sends_allowed_updates(self):
        kw = {k.arg for k in self._call("set_webhook").keywords}
        self.assertIn("allowed_updates", kw,
                      "set_webhook without allowed_updates rewrites the stored value to a default")

    def test_the_incident_is_recorded_where_someone_would_look(self):
        """This is invisible remote state. Someone removing the parameter as
        redundant needs to find out why it is there before they do."""
        self.assertIn("allowed_updates", self.src)
        fn = self.src.split("def allowed_updates")[1].split("\ndef ")[0]
        self.assertIn("callback_query", fn)
        self.assertIn("server-side", fn.lower())


if __name__ == "__main__":
    unittest.main()
