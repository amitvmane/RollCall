"""Ratchet: user-facing docs must keep up with the code.

/help and the Telegram command menu re-render themselves from
commands_registry.py, so those never go stale. Everything a human maintains by
hand does: README's command tables and env-var table, and CLAUDE.md's env-var
table. Two commands and one env var shipped on 2026-08-18 before anyone noticed
the docs hadn't moved — hence this guard.

Same ratchet shape as tests/test_functional_coverage.py and
security/audit_baseline.json: a NEW command or env var fails CI until it is
documented, and a stale allowlist entry also fails, so the allowlist can only
shrink.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import commands_registry  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_README = os.path.join(_ROOT, "README.md")
_CLAUDE_MD = os.path.join(_ROOT, "CLAUDE.md")
_ENV_EXAMPLE = os.path.join(_ROOT, ".env.example")

# Commands intentionally absent from README's tables. Empty — README currently
# documents every registered command, including the super-admin ones. Keep it
# that way: an entry here is a command users cannot discover from the README.
README_EXEMPT = set()

# Env vars read by the code but deliberately undocumented (internal or
# test-only). Everything else must appear in BOTH README.md and CLAUDE.md.
ENV_EXEMPT = {
    "PATH", "HOME", "PYTHONPATH", "TZ",
    # Set by the test harness itself, never by an operator.
    "ADMIN1", "ADMIN2",
    # Documented under their canonical alias in the tables.
    "API_KEY",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _env_vars_used():
    """Env var names the runtime actually reads, scanned from source."""
    names = set()
    pattern = re.compile(r"os\.environ(?:\.get)?[\(\[]\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']")
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "rollCall")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                names |= set(pattern.findall(_read(os.path.join(dirpath, fn))))
    return names


class TestCommandsDocumented(unittest.TestCase):

    def setUp(self):
        self.readme = _read(_README)

    def test_every_command_appears_in_readme(self):
        missing = sorted(
            e["name"] for e in commands_registry.COMMANDS
            if e["name"] not in README_EXEMPT
            and f"/{e['name']}" not in self.readme
        )
        self.assertEqual(
            missing, [],
            "\n\nThese commands are registered but never mentioned in README.md:\n  "
            + "\n  ".join(missing)
            + "\n\nAdd them to the relevant README table. /help and the bot menu "
              "render themselves from commands_registry.py, but README does not.\n"
        )

    def test_readme_exemptions_are_not_stale(self):
        registered = {e["name"] for e in commands_registry.COMMANDS}
        gone = sorted(n for n in README_EXEMPT if n not in registered)
        self.assertEqual(gone, [],
                         f"\n\nREADME_EXEMPT lists unregistered commands: {gone}. Remove them.\n")

        now_documented = sorted(n for n in README_EXEMPT if f"/{n}" in self.readme)
        self.assertEqual(
            now_documented, [],
            f"\n\nThese are exempt but now documented anyway: {now_documented}. "
            "Remove them from README_EXEMPT.\n"
        )


class TestEnvVarsDocumented(unittest.TestCase):

    def setUp(self):
        self.readme = _read(_README)
        self.claude_md = _read(_CLAUDE_MD)
        self.used = _env_vars_used() - ENV_EXEMPT

    def test_env_vars_documented_in_readme(self):
        missing = sorted(v for v in self.used if v not in self.readme)
        self.assertEqual(
            missing, [],
            "\n\nEnv vars read by the code but absent from README.md:\n  "
            + "\n  ".join(missing)
            + "\n\nAdd them to the environment table (or to ENV_EXEMPT if genuinely internal).\n"
        )

    def test_env_vars_documented_in_claude_md(self):
        missing = sorted(v for v in self.used if v not in self.claude_md)
        self.assertEqual(
            missing, [],
            "\n\nEnv vars read by the code but absent from CLAUDE.md's table:\n  "
            + "\n  ".join(missing)
            + "\n\nThat table is what future contributors configure from — keep it complete.\n"
        )

    def test_env_vars_documented_in_env_example(self):
        """README tells operators that .env.example documents EVERY supported
        variable, so a var missing from it makes the README a lie."""
        example = _read(_ENV_EXAMPLE)
        missing = sorted(v for v in self.used if v not in example)
        self.assertEqual(
            missing, [],
            "\n\nEnv vars read by the code but absent from .env.example:\n  "
            + "\n  ".join(missing)
            + "\n\nREADME says .env.example documents every supported variable.\n"
        )

    def test_scanner_actually_finds_vars(self):
        """A scanner that silently matched nothing would make both tests above
        pass vacuously forever."""
        self.assertIn("REST_API_ENABLED", self.used)
        self.assertGreater(len(self.used), 5)


if __name__ == "__main__":
    unittest.main()
