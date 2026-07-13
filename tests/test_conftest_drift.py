"""
Guards against conftest.py's db_mock silently drifting from the real db.py.

conftest.py replaces sys.modules["db"] with a MagicMock() before any real
module is imported, so unit tests never exercise the real db.py — a
renamed or removed function keeps "working" in unit tests even though it
would break in production. This doesn't change that mocking architecture
(that would require importing real db.py before the mock is installed,
which every other test file assumes has already happened); it just adds a
static check: every db_mock.<name> attribute explicitly configured in
conftest.py must correspond to an actual top-level function name in
rollCall/db.py.
"""

import os
import re
import unittest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PY = os.path.join(_REPO_ROOT, "rollCall", "db.py")
_CONFTEST_PY = os.path.join(_REPO_ROOT, "tests", "conftest.py")

# Attributes conftest.py sets on db_mock that are NOT real db.py function
# names — mock introspection plumbing or plain module-level attributes,
# not functions to check against.
_NON_FUNCTION_ATTRS = {"db_type"}


def _real_db_function_names() -> set:
    src = open(_DB_PY, encoding="utf-8").read()
    return set(re.findall(r"^def (\w+)\(", src, re.MULTILINE))


def _conftest_db_mock_attrs() -> set:
    src = open(_CONFTEST_PY, encoding="utf-8").read()
    # Matches `db_mock.<name>.<rest> = ...` or `db_mock.<name> = ...` /
    # `db_mock.<name>.return_value = ...` — capture only the first
    # attribute segment after db_mock, which is the function name being
    # configured (or, for the .reset_mock line, mock-internals noise
    # filtered out separately below).
    names = set(re.findall(r"\bdb_mock\.(\w+)", src))
    return names


class TestConftestDbMockDrift(unittest.TestCase):

    def test_every_configured_db_mock_attr_is_a_real_db_function(self):
        real_names = _real_db_function_names()
        self.assertTrue(real_names, "Regex extracted zero function names from db.py — check the pattern")

        configured = _conftest_db_mock_attrs() - _NON_FUNCTION_ATTRS
        # `.reset_mock` is a genuine MagicMock built-in, not a db.py
        # function — conftest.py has one line assigning to it
        # (db_mock.delete_user_by_name.reset_mock = MagicMock()).
        configured.discard("reset_mock")

        unknown = sorted(configured - real_names)
        self.assertEqual(
            unknown, [],
            f"conftest.py configures db_mock attributes with no matching "
            f"function in db.py (renamed/removed?): {unknown}",
        )
