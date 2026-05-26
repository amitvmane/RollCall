"""
Root conftest.py — guards against running unit tests and integration tests
together in a single pytest process.

integration_tests/conftest.py patches sys.modules at import time (telebot,
config, aiohttp). Those patches persist for the entire process, so they break
the unit-test mocks in tests/ when both suites are collected together.

Run the suites separately:
    pytest tests/
    pytest integration_tests/
"""
import pytest


def pytest_collection_finish(session):
    paths = [str(item.fspath) for item in session.items]
    has_unit = any("/tests/" in p for p in paths)
    has_integration = any("/integration_tests/" in p for p in paths)
    if has_unit and has_integration:
        pytest.exit(
            "\n\nERROR: unit tests (tests/) and integration tests "
            "(integration_tests/) must not run in the same process.\n"
            "Run them separately:\n"
            "    pytest tests/\n"
            "    pytest integration_tests/\n",
            returncode=3,
        )
