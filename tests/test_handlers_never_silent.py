"""Ratchet: every Telegram handler answers even when it fails.

A handler with no try/except of its own lets an exception escape to
telebot, which logs it — and the person who typed the command hears
nothing. Wrap the body in try/except → reply_error, or decorate it with
bot_state.guarded (CLAUDE.md, "Error replies")."""
import ast
import glob
import os
import unittest

_HANDLERS = os.path.join(os.path.dirname(__file__), "..", "rollCall", "handlers")
# Bodies that are one call to a helper which has its own try/except.
_DELEGATES = {"remind_before_close_command", "remind_after_open_command"}


class TestHandlersNeverSilent(unittest.TestCase):
    def test_every_handler_catches_or_is_guarded(self):
        bad = []
        for path in sorted(glob.glob(os.path.join(_HANDLERS, "*.py"))):
            tree = ast.parse(open(path, encoding="utf-8").read(), path)
            for fn in ast.walk(tree):
                if not isinstance(fn, ast.AsyncFunctionDef):
                    continue
                decos = [ast.unparse(d) for d in fn.decorator_list]
                if not any("message_handler" in d or "callback_query_handler" in d for d in decos):
                    continue
                if "guarded" in decos or fn.name in _DELEGATES:
                    continue
                if not any(isinstance(n, ast.Try) for n in ast.walk(fn)):
                    bad.append(f"{os.path.basename(path)}:{fn.lineno} {fn.name}")
        self.assertEqual(bad, [], "handlers that go silent on an error — add try/except or @guarded")


if __name__ == "__main__":
    unittest.main()
