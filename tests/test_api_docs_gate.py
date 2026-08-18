"""OpenAPI docs exposure gate (adversarial pass finding, 2026-08-17).

/api/docs, /api/redoc and the OpenAPI schema are unauthenticated AND on the
rate limiter's bypass list, so on a publicly-reachable deployment they hand a
visitor a free, unthrottled map of every endpoint. They're now opt-in.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from fastapi.testclient import TestClient


def _client():
    from api.main import create_app
    return TestClient(create_app())


class TestApiDocsGate(unittest.TestCase):

    def test_docs_closed_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_DOCS_ENABLED", None)
            c = _client()
            for path in ("/api/docs", "/api/redoc", "/api/v1/openapi.json"):
                self.assertEqual(c.get(path).status_code, 404,
                                 f"{path} should be closed by default")

    def test_docs_open_when_explicitly_enabled(self):
        with patch.dict(os.environ, {"API_DOCS_ENABLED": "true"}):
            c = _client()
            for path in ("/api/docs", "/api/v1/openapi.json"):
                self.assertEqual(c.get(path).status_code, 200,
                                 f"{path} should be served when opted in")

    def test_only_truthy_values_open_it(self):
        for val in ("false", "0", "no", "", "maybe"):
            with patch.dict(os.environ, {"API_DOCS_ENABLED": val}):
                self.assertEqual(_client().get("/api/docs").status_code, 404,
                                 f"API_DOCS_ENABLED={val!r} must not expose docs")

    def test_real_endpoints_unaffected(self):
        """Closing the docs must not disturb actual routing."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_DOCS_ENABLED", None)
            self.assertEqual(_client().get("/api/v1/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
