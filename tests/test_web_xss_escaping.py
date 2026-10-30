"""Regression: server/user strings must be escaped before reaching innerHTML.

Static checks on the shipped JS — the unit layer can't run a browser, and
these are exactly the spots a stored-XSS payload (a rollcall title, a
template location, an admin-set UPI id) went straight into the DOM.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "rollCall", "api")


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class TestWebUpcomingEscaped(unittest.TestCase):

    def setUp(self):
        src = _read("web/app.js")
        start = src.index("function renderUpcoming")
        self.body = src[start:src.index("async function loadGroup", start)]

    def test_no_raw_user_fields_in_upcoming_markup(self):
        for raw in ("${title}", "${u.location}", "${u.fee}", "${u.event_time}",
                    "${u.event_day[0]"):
            self.assertNotIn(raw, self.body, f"{raw} reaches innerHTML unescaped")

    def test_upcoming_fields_go_through_esc(self):
        for field in ("esc(title)", "esc(u.location)", "esc(u.fee)", "esc(u.event_time)"):
            self.assertIn(field, self.body)


class TestPortalEscEscapesQuotes(unittest.TestCase):

    def setUp(self):
        self.src = _read("portal/app.js")

    def test_esc_is_not_the_textcontent_trick(self):
        # textContent -> innerHTML leaves " and ' raw, and esc() is used
        # inside title="…" attributes.
        m = re.search(r"function esc\(s\)\{(.*?)\}\n", self.src)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertNotIn("textContent", body)
        self.assertIn("&quot;", body)
        self.assertIn("&#39;", body)

    def test_handler_args_go_through_rcargs(self):
        # Copy buttons pass the UPI id / link as data, never as JS source.
        self.assertNotIn("upi.replace(", self.src)
        self.assertNotIn("joinUrl.replace(", self.src)
        self.assertIn("rcArgs(upi,", self.src)
        self.assertIn("rcArgs(joinUrl,", self.src)


class TestNoEscJsAttrOutsideJsSource(unittest.TestCase):
    """escJsAttr escapes for a JS string literal. With no inline handlers left
    there is no JS source in markup, so using it anywhere now corrupts data:
    a checkbox value of O'Brien came back as O\\'Brien and matched nothing."""

    def test_unused_in_templates(self):
        for rel in ("web/app.js", "portal/app.js", "help/app.js"):
            uses = [l for l in _read(rel).splitlines()
                    if "escJsAttr(" in l and "function escJsAttr" not in l
                    and not l.strip().startswith("//")]
            self.assertEqual(uses, [], f"{rel}: {uses[:2]}")


if __name__ == "__main__":
    unittest.main()
