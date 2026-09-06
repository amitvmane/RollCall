"""
/version renders the changelog for members, not the raw record.

It used to print the version.json row field by field:

    Version: 10.3
    Description: 10.3 — Readable, Tappable, and It Actually Opens 📱
    ...
    Deployed: Y
    Deployed datetime: 06-09-2026 12:58 UTC

which showed the version number twice (descriptions carry their own header),
exposed an internal deployment flag that means nothing to a player, and
labelled the date with a field name. The changelog text is written for
members; this shows it as written.

These tests pin the formatting rules rather than the exact wording, and cover
the ragged inputs the real file contains — 50-odd hand-typed dates going back
to 2022, some with a time and some without.

Added 2026-09-06.
"""
import os
import re
import unittest

_CORE = os.path.join(os.path.dirname(__file__), "..", "rollCall", "handlers", "core.py")


def _load_helpers():
    """Exec the two pure helpers standalone.

    Importing handlers.core pulls in the whole bot stack for two functions
    that touch nothing but a dict, so lift them out instead.
    """
    src = open(_CORE, encoding="utf-8").read()
    ns = {}
    for fn in ("_friendly_release_date", "_format_release"):
        m = re.search(rf"^def {fn}\(.*?(?=^def |^@bot)", src, re.S | re.M)
        if m is None:
            raise AssertionError(f"{fn} not found in handlers/core.py")
        exec(m.group(0), ns)
    return ns["_format_release"], ns["_friendly_release_date"]


class TestVersionFormatting(unittest.TestCase):
    def setUp(self):
        self.format_release, self.friendly_date = _load_helpers()

    # ── what members should no longer see ────────────────────────────────
    def test_internal_field_labels_are_gone(self):
        out = self.format_release({
            "Version": 10.3, "Description": "10.3 — Title\n\nbody",
            "DeployedOnProd": "Y", "DeployedDatetime": "06-09-2026 12:58 UTC",
        })
        for leaked in ("Description:", "Deployed:", "Deployed datetime:", "DeployedOnProd"):
            self.assertNotIn(leaked, out, f"{leaked!r} is an internal field name, not member-facing copy")

    def test_version_number_is_not_duplicated(self):
        """The description already opens with '10.3 — ...', so nothing should add it again."""
        out = self.format_release({
            "Version": 10.3, "Description": "10.3 — Title\n\nbody",
            "DeployedOnProd": "Y", "DeployedDatetime": "06-09-2026 12:58 UTC",
        })
        self.assertTrue(out.startswith("10.3 — Title"))
        self.assertNotIn("RollCall v10.3", out)

    # ── what must survive ────────────────────────────────────────────────
    def test_version_number_added_when_description_lacks_a_header(self):
        """Older entries are bare one-liners; the number must not go missing."""
        out = self.format_release({
            "Version": 4.5, "Description": "Latest",
            "DeployedOnProd": "Y", "DeployedDatetime": "14-04-2026",
        })
        self.assertIn("4.5", out)
        self.assertIn("Latest", out)

    def test_changelog_body_is_reproduced_verbatim(self):
        body = "10.3 — Title\n\n📱 SECTION\nline one\nline two"
        out = self.format_release({
            "Version": 10.3, "Description": body,
            "DeployedOnProd": "Y", "DeployedDatetime": "06-09-2026 12:58 UTC",
        })
        self.assertIn(body, out)

    # ── dates: a convention, not a guarantee ─────────────────────────────
    def test_date_with_time_and_zone(self):
        self.assertEqual(self.friendly_date("06-09-2026 12:58 UTC"), "6 September 2026")

    def test_date_without_time(self):
        self.assertEqual(self.friendly_date("14-04-2026"), "14 April 2026")

    def test_unparseable_date_passes_through(self):
        """Never raise over a changelog — show whatever was typed."""
        self.assertEqual(self.friendly_date("sometime last year"), "sometime last year")

    def test_missing_date_is_omitted_not_rendered_empty(self):
        out = self.format_release({
            "Version": 9.9, "Description": "9.9 — Thing", "DeployedOnProd": "Y",
        })
        self.assertNotIn("Released", out)
        self.assertEqual(out, "9.9 — Thing")

    # ── the real file ────────────────────────────────────────────────────
    def test_live_entry_fits_in_one_telegram_message(self):
        """Telegram hard-caps a message at 4096 chars; over it, send_message fails
        outright and /version silently returns nothing."""
        import json
        vf = os.path.join(os.path.dirname(__file__), "..", "rollCall", "version.json")
        data = json.load(open(vf, encoding="utf-8"))
        live = [v for v in data if v.get("DeployedOnProd") == "Y"]
        self.assertTrue(live, "no version marked deployed — /version would say nothing")
        for entry in live:
            out = self.format_release(entry)
            self.assertLessEqual(
                len(out), 4096,
                f"v{entry['Version']} renders to {len(out)} chars — over Telegram's limit",
            )


if __name__ == "__main__":
    unittest.main()
