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

    def test_inline_handlers_do_not_hand_roll_quote_escaping(self):
        self.assertNotIn("upi.replace(", self.src)
        self.assertNotIn("joinUrl.replace(", self.src)
        self.assertIn("esc(escJsAttr(upi))", self.src)


if __name__ == "__main__":
    unittest.main()
