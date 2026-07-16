"""
Tests for commands_registry.py menu helpers

Covers:
- menu_entries (scope filtering, category grouping, emoji prefixes)
- CATEGORY_EMOJI covers every category used in COMMANDS
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from commands_registry import (  # noqa: E402
    COMMANDS, CATEGORY_EMOJI, menu_entries,
    USER_CATEGORY_ORDER, ADMIN_CATEGORY_ORDER,
)


class TestCategoryEmoji(unittest.TestCase):

    def test_every_command_category_has_an_emoji(self):
        used = {c["category"] for c in COMMANDS}
        missing = used - set(CATEGORY_EMOJI)
        self.assertEqual(missing, set(),
                         f"categories without a CATEGORY_EMOJI entry: {missing}")


class TestMenuEntries(unittest.TestCase):

    def test_returns_only_requested_scope(self):
        names = {name for name, _ in menu_entries("user", USER_CATEGORY_ORDER)}
        expected = {c["name"] for c in COMMANDS if c["scope"] == "user"}
        self.assertEqual(names, expected)

    def test_summaries_prefixed_with_category_emoji(self):
        by_name = {c["name"]: c for c in COMMANDS}
        for name, summary in menu_entries("admin", ADMIN_CATEGORY_ORDER):
            emoji = CATEGORY_EMOJI[by_name[name]["category"]]
            self.assertTrue(summary.startswith(f"{emoji} "),
                            f"/{name} summary not prefixed with {emoji}: {summary!r}")

    def test_entries_grouped_by_category_order(self):
        by_name = {c["name"]: c for c in COMMANDS}
        rank = {cat: i for i, cat in enumerate(USER_CATEGORY_ORDER)}
        ranks = [rank[by_name[name]["category"]]
                 for name, _ in menu_entries("user", USER_CATEGORY_ORDER)]
        self.assertEqual(ranks, sorted(ranks))

    def test_registry_order_preserved_within_category(self):
        by_name = {c["name"]: c for c in COMMANDS}
        registry_pos = {c["name"]: i for i, c in enumerate(COMMANDS)}
        seen_pos_by_cat = {}
        for name, _ in menu_entries("admin", ADMIN_CATEGORY_ORDER):
            cat = by_name[name]["category"]
            pos = registry_pos[name]
            if cat in seen_pos_by_cat:
                self.assertGreater(pos, seen_pos_by_cat[cat],
                                   f"/{name} out of registry order within {cat!r}")
            seen_pos_by_cat[cat] = pos


if __name__ == "__main__":
    unittest.main()
