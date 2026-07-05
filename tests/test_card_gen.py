"""
Card generation tests — glyph sanitization and layout-safety helpers.

The Docker image only ships DejaVu fonts (no emoji, no Indic scripts); PIL
draws missing glyphs as tofu boxes. _sanitize must strip unrenderable chars
and fall back when nothing remains. These tests force the matplotlib-bundled
DejaVu so behavior matches production regardless of host system fonts.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from utils import card_gen
from utils.card_gen import (
    _sanitize, _ellipsize, matchday_card, close_receipt_card, qr_png,
)


def setUpModule():
    # Force matplotlib-bundled DejaVu — same family as the Docker image —
    # so results don't depend on host system fonts (e.g. Arial Unicode on mac).
    card_gen._FONT_PATHS[:] = []
    card_gen._FONT_BOLD_PATHS[:] = []
    card_gen._font_cache.clear()
    card_gen._cmap_cache.clear()
    assert "DejaVu" in (card_gen._font_path() or "")


class TestSanitize(unittest.TestCase):
    def test_plain_ascii_untouched(self):
        self.assertEqual(_sanitize("Ravi Kumar"), "Ravi Kumar")

    def test_latin_accents_kept(self):
        self.assertEqual(_sanitize("José García"), "José García")

    def test_emoji_stripped(self):
        self.assertEqual(_sanitize("Rahul 🏸"), "Rahul")
        self.assertEqual(_sanitize("Priya ⭐✨"), "Priya")

    def test_emoji_only_name_falls_back(self):
        self.assertEqual(_sanitize("🔥🔥🔥", fallback="Player 5"), "Player 5")

    def test_devanagari_falls_back_on_dejavu(self):
        # DejaVu has no Devanagari — the whole name must fall back, not tofu.
        self.assertEqual(_sanitize("अर्जुन", fallback="Member"), "Member")

    def test_empty_input_falls_back(self):
        self.assertEqual(_sanitize("", fallback="X"), "X")

    def test_gaps_collapsed_after_strip(self):
        self.assertEqual(_sanitize("A 🏸 B"), "A B")


class TestEllipsize(unittest.TestCase):
    def test_short_untouched(self):
        self.assertEqual(_ellipsize("Amit", 22), "Amit")

    def test_long_gets_ellipsis(self):
        out = _ellipsize("Vishwanathan Subramaniam", 22)
        self.assertEqual(len(out), 22)
        self.assertTrue(out.endswith("…"))


class TestCardsRender(unittest.TestCase):
    """Smoke-render each card type with hostile input — must not raise."""

    HOSTILE_NAMES = ["Amit", "Rahul 🏸", "अर्जुन", "🔥🔥🔥",
                     "Vishwanathan Subramaniam", "José García"]

    def test_matchday_card_renders_png(self):
        buf = matchday_card("Game 🏸", "Saturday, 5 Jul 2026",
                            self.HOSTILE_NAMES, venue="Play Arena")
        self.assertEqual(buf.read(8), b"\x89PNG\r\n\x1a\n")

    def test_matchday_card_two_column_path(self):
        names = [f"Player {i}" for i in range(1, 21)]
        buf = matchday_card("Big Game", "Sunday", names)
        self.assertEqual(buf.read(8), b"\x89PNG\r\n\x1a\n")

    def test_receipt_card_renders_png(self):
        balances = [{"member_name": n, "balance": b}
                    for n, b in [("Rahul 🏸", 340), ("अर्जुन", 200),
                                 ("Amit", 0), ("🔥🔥🔥", 0)]]
        buf = close_receipt_card(
            title="Game 🏸", ground_cost=1200, subsidy=200, per_head=90,
            in_count=12, fund_balance=1540, balances=balances,
        )
        self.assertEqual(buf.read(8), b"\x89PNG\r\n\x1a\n")

    def test_qr_renders_png(self):
        buf = qr_png("someone@upi", 90)
        self.assertEqual(buf.read(8), b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
