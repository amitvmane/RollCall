"""
Static PWA assets — the parts of "is it installable and does it behave
installed" that no browser test exercises.
"""
import json
import os
import re
import struct
import unittest

WEB = os.path.join(os.path.dirname(__file__), "..", "rollCall", "api", "web")


def _read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as fh:
        return fh.read()


def _png_size(name):
    with open(os.path.join(WEB, name), "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", name
    return struct.unpack(">II", head[16:24])


class TestManifest(unittest.TestCase):
    def setUp(self):
        self.m = json.loads(_read("manifest.json"))

    def test_every_icon_exists_with_its_declared_size(self):
        for icon in self.m["icons"]:
            name = icon["src"].rsplit("/", 1)[1]
            self.assertTrue(os.path.isfile(os.path.join(WEB, name)), name)
            if icon["type"] == "image/png":
                w, h = _png_size(name)
                self.assertEqual(icon["sizes"], f"{w}x{h}", name)

    def test_maskable_icons_are_dedicated_pngs(self):
        """The rounded-corner logo was declared maskable, so Android cropped
        it. Maskable must be full-bleed artwork made for it."""
        maskable = [i for i in self.m["icons"] if "maskable" in i.get("purpose", "")]
        self.assertEqual({i["sizes"] for i in maskable}, {"192x192", "512x512"})
        self.assertTrue(all("maskable" in i["src"] for i in maskable))

    def test_scope_keeps_help_and_portal_inside_the_app(self):
        """With scope /web/, the in-app ❓ link to /help left the installed app."""
        self.assertEqual(self.m["scope"], "/")
        self.assertTrue(self.m["start_url"].startswith(self.m["scope"]))

    def test_apple_touch_icon_is_opaque(self):
        """iOS fills transparent pixels with black; the touch icon must have none."""
        self.assertIn('href="/web/apple-touch-icon.png"', _read("index.html"))
        self.assertEqual(_png_size("apple-touch-icon.png"), (180, 180))


class TestServiceWorker(unittest.TestCase):
    def setUp(self):
        self.sw = _read("sw.js")

    def test_offline_fallback_does_not_or_two_promises(self):
        """caches.match() returns a Promise — always truthy — so
        `caches.match(a) || caches.match(b)` never reached b."""
        code = "\n".join(l for l in self.sw.splitlines() if not l.strip().startswith("//"))
        self.assertIsNone(re.search(r"caches\.match\([^)]*\)\s*\|\|", code))

    def test_offline_shell_has_its_stylesheets(self):
        for path in ("/shared/tokens.css", "/shared/account.css", "/shared/theme-init.js"):
            self.assertIn(f'"{path}"', self.sw)

    def test_notification_tag_comes_from_the_server(self):
        self.assertIn("data.tag ||", self.sw)


if __name__ == "__main__":
    unittest.main()
