"""/api/v1/web/install-qr — the QR the install sheet shows on a laptop.

It is unauthenticated, so the property that matters is that it can only ever
encode a URL on this deployment: `group` must stay a bare token, never a path
or a host, or the endpoint becomes a free QR generator for any link.
"""
import os
import sys
import unittest
from io import BytesIO
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

os.environ.setdefault("TELEGRAM_TOKEN", "123456789:TEST_BOT_TOKEN_FOR_UNIT_TESTS_ONLY")

try:
    from fastapi.testclient import TestClient
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def _client():
    from api.main import create_app
    return TestClient(create_app(), raise_server_exceptions=False)


@unittest.skipUnless(FASTAPI_AVAILABLE, "fastapi not installed")
class TestInstallQr(unittest.TestCase):

    def setUp(self):
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        self.encoded = []

        def _fake(url):
            self.encoded.append(url)
            return BytesIO(b"\x89PNG\r\n\x1a\nfake")
        self._p = patch("utils.card_gen.qr_url_png", side_effect=_fake)
        self._p.start()
        self._env = patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example/"})
        self._env.start()

    def tearDown(self):
        self._p.stop()
        self._env.stop()

    def test_group_page_with_install_flag(self):
        r = _client().get("/api/v1/web/install-qr?group=abc123")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/png")
        self.assertEqual(self.encoded, ["https://rc.example/web/group/abc123?install=1"])

    def test_without_group_points_at_home(self):
        r = _client().get("/api/v1/web/install-qr")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.encoded, ["https://rc.example/web/?install=1"])

    def test_group_cannot_smuggle_a_path_or_host(self):
        for bad in ("../../evil", "a/b", "x?y=1", "@evil.com", "a" * 65, ""):
            r = _client().get("/api/v1/web/install-qr", params={"group": bad})
            self.assertEqual(r.status_code, 422, bad)
        self.assertEqual(self.encoded, [])


class TestQrUrlPng(unittest.TestCase):
    def test_real_png(self):
        from utils.card_gen import qr_url_png
        self.assertTrue(qr_url_png("https://rc.example/web/").getvalue().startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
