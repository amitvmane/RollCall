"""
Unit tests for services/identity.py — merge/unmerge/resolve/suggest core.

All DB + manager calls are mocked so tests run offline without any database.
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

from exceptions import incorrectParameter, parameterMissing  # noqa: E402
import services.identity as identity  # noqa: E402


class TestResolveCanonical(unittest.TestCase):

    def test_real_user_always_self(self):
        r = identity.resolve_canonical(1, user_id=42)
        self.assertEqual(r, {"kind": "user", "user_id": 42, "proxy_name": None})

    def test_unmerged_proxy_resolves_self(self):
        with patch("services.identity.db.get_identity_link", return_value=None):
            r = identity.resolve_canonical(1, proxy_name="Solo")
        self.assertEqual(r, {"kind": "proxy", "user_id": None, "proxy_name": "Solo"})

    def test_proxy_merged_into_user(self):
        link = {"canonical_user_id": 999, "canonical_proxy_name": None}
        with patch("services.identity.db.get_identity_link", return_value=link):
            r = identity.resolve_canonical(1, proxy_name="Rex")
        self.assertEqual(r, {"kind": "user", "user_id": 999, "proxy_name": None})

    def test_proxy_merged_into_proxy(self):
        link = {"canonical_user_id": None, "canonical_proxy_name": "Ajay"}
        with patch("services.identity.db.get_identity_link", return_value=link):
            r = identity.resolve_canonical(1, proxy_name="Ajya")
        self.assertEqual(r, {"kind": "proxy", "user_id": None, "proxy_name": "Ajay"})


class TestGetAliasGroup(unittest.TestCase):

    def test_unmerged_identity_has_no_aliases(self):
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]):
            g = identity.get_alias_group(1, user_id=42)
        self.assertEqual(g["aliases"], [])
        self.assertEqual(g["kind"], "user")

    def test_canonical_user_collects_all_aliases(self):
        links = [{"alias_proxy_name": "Rex"}, {"alias_proxy_name": "Rexx"}]
        with patch("services.identity.db.get_links_by_canonical", return_value=links), \
             patch("services.identity.db.get_member_display_info",
                   return_value={"first_name": "Real", "username": None}):
            g = identity.get_alias_group(1, user_id=999)
        self.assertEqual(g["aliases"], ["Rex", "Rexx"])
        self.assertEqual(g["display_name"], "Real")

    def test_canonical_proxy_display_name_is_proxy_name(self):
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical",
                   return_value=[{"alias_proxy_name": "Aju"}]):
            g = identity.get_alias_group(1, proxy_name="Ajay")
        self.assertEqual(g["display_name"], "Ajay")
        self.assertEqual(g["aliases"], ["Aju"])


class TestLinkIdentities(unittest.TestCase):

    def _admin(self):
        return {"admin_user_id": 1, "admin_name": "Admin"}

    def test_basic_merge_into_real_user(self):
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links") as mock_repoint, \
             patch("services.identity.db.log_admin_action") as mock_log, \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            g = identity.link_identities(1, "Rex", canonical_user_id=999, **self._admin())
        mock_upsert.assert_called_once()
        self.assertEqual(mock_upsert.call_args.kwargs["canonical_user_id"], 999)
        self.assertIsNone(mock_upsert.call_args.kwargs["canonical_proxy_name"])
        mock_repoint.assert_called_once_with(1, "Rex", to_user_id=999, to_proxy_name=None)
        mock_log.assert_called_once()
        self.assertEqual(g["kind"], "user")
        self.assertEqual(g["user_id"], 999)

    def test_basic_merge_into_another_proxy(self):
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links"), \
             patch("services.identity.db.log_admin_action"), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]):
            identity.link_identities(1, "Aju", canonical_proxy_name="Ajay", **self._admin())
        self.assertIsNone(mock_upsert.call_args.kwargs["canonical_user_id"])
        self.assertEqual(mock_upsert.call_args.kwargs["canonical_proxy_name"], "Ajay")

    def test_flattens_through_existing_alias(self):
        """Merging into a name that's ITSELF already an alias must write
        the new link against the FINAL target, not the intermediate name."""
        # "Ajay" resolves to a real user 555 via an existing link.
        existing_link = {"canonical_user_id": 555, "canonical_proxy_name": None}
        with patch("services.identity.db.get_identity_link", return_value=existing_link), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links") as mock_repoint, \
             patch("services.identity.db.log_admin_action"), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            g = identity.link_identities(1, "Aju", canonical_proxy_name="Ajay", **self._admin())
        # New alias "Aju" must point directly at 555, not at "Ajay".
        self.assertEqual(mock_upsert.call_args.kwargs["canonical_user_id"], 555)
        self.assertIsNone(mock_upsert.call_args.kwargs["canonical_proxy_name"])
        mock_repoint.assert_called_once_with(1, "Aju", to_user_id=555, to_proxy_name=None)
        self.assertEqual(g["user_id"], 555)

    def test_cascade_repoints_existing_aliases_of_the_merged_name(self):
        """Merging "Ajay" (which already has its own aliases) into a real
        user must repoint those existing aliases too, in the same call."""
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.upsert_identity_link"), \
             patch("services.identity.db.repoint_links") as mock_repoint, \
             patch("services.identity.db.log_admin_action"), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            identity.link_identities(1, "Ajay", canonical_user_id=555, **self._admin())
        mock_repoint.assert_called_once_with(1, "Ajay", to_user_id=555, to_proxy_name=None)

    def test_self_merge_rejected(self):
        with self.assertRaises(incorrectParameter):
            identity.link_identities(1, "Ajay", canonical_proxy_name="ajay", **self._admin())

    def test_cycle_rejected(self):
        """Merging "Aju" into "Ajay" when "Ajay" already resolves to "Aju"
        (a direct cycle) must be rejected."""
        existing_link = {"canonical_user_id": None, "canonical_proxy_name": "Aju"}
        with patch("services.identity.db.get_identity_link", return_value=existing_link):
            with self.assertRaises(incorrectParameter):
                identity.link_identities(1, "Aju", canonical_proxy_name="Ajay", **self._admin())

    def test_both_targets_given_rejected(self):
        with self.assertRaises(parameterMissing):
            identity.link_identities(1, "Rex", canonical_user_id=1, canonical_proxy_name="X", **self._admin())

    def test_no_target_given_rejected(self):
        with self.assertRaises(parameterMissing):
            identity.link_identities(1, "Rex", **self._admin())

    def test_empty_alias_name_rejected(self):
        with self.assertRaises(parameterMissing):
            identity.link_identities(1, "  ", canonical_user_id=1, **self._admin())


class TestUnmergeIdentity(unittest.TestCase):

    def test_deletes_and_logs(self):
        with patch("services.identity.db.delete_identity_link", return_value=True) as mock_del, \
             patch("services.identity.db.log_admin_action") as mock_log:
            r = identity.unmerge_identity(1, "Rex", admin_user_id=1, admin_name="Admin")
        mock_del.assert_called_once_with(1, "Rex")
        mock_log.assert_called_once()
        self.assertEqual(r, {"unmerged": True})

    def test_idempotent_when_not_linked(self):
        with patch("services.identity.db.delete_identity_link", return_value=False), \
             patch("services.identity.db.log_admin_action") as mock_log:
            r = identity.unmerge_identity(1, "Rex", admin_user_id=1, admin_name="Admin")
        mock_log.assert_not_called()
        self.assertEqual(r, {"unmerged": False})


class TestListAllIdentities(unittest.TestCase):

    def test_real_members_never_show_merged_into(self):
        members = [{"user_id": 1, "first_name": "Alice", "username": None}]
        with patch("services.identity.db.get_active_members", return_value=members), \
             patch("services.identity.db.get_all_proxy_names", return_value=[]):
            result = identity.list_all_identities(1)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["merged_into"])

    def test_unmerged_proxy_merged_into_is_none(self):
        with patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.get_all_proxy_names", return_value=["Solo"]), \
             patch("services.identity.db.get_identity_link", return_value=None):
            result = identity.list_all_identities(1)
        self.assertEqual(result[0]["merged_into"], None)

    def test_merged_proxy_shows_target(self):
        link = {"canonical_user_id": 999, "canonical_proxy_name": None}
        with patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.get_all_proxy_names", return_value=["Rex"]), \
             patch("services.identity.db.get_identity_link", return_value=link):
            result = identity.list_all_identities(1)
        self.assertEqual(result[0]["merged_into"], {"kind": "user", "user_id": 999, "proxy_name": None})


class TestCombinedGhostCount(unittest.TestCase):

    def test_sums_across_group(self):
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical",
                   return_value=[{"alias_proxy_name": "Rex"}, {"alias_proxy_name": "Rexx"}]), \
             patch("services.identity.db.get_ghost_count", return_value=2), \
             patch("services.identity.db.get_ghost_count_by_proxy_name", side_effect=[3, 1]):
            total = identity.combined_ghost_count(1, user_id=999)
        self.assertEqual(total, 6)  # 2 (own) + 3 (Rex) + 1 (Rexx)

    def test_matches_single_value_when_unmerged(self):
        with patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_ghost_count", return_value=4):
            total = identity.combined_ghost_count(1, user_id=999)
        self.assertEqual(total, 4)


class TestListSuggestions(unittest.TestCase):

    def test_returns_empty_without_levenshtein(self):
        with patch.dict(sys.modules, {"Levenshtein": None}):
            result = identity.list_suggestions(1)
        self.assertEqual(result, [])

    def test_close_proxy_pair_suggested(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya", "Ajay"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        pairs = [(s["alias_proxy_name"], s["candidate_proxy_name"]) for s in result]
        self.assertIn(("Ajay", "Ajya"), pairs)

    def test_distant_proxy_pair_not_suggested(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Alpha", "Zulu"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(result, [])

    def test_proxy_vs_real_member_suggested(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 1, "first_name": "Ajay", "username": None}]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["candidate_kind"], "user")
        self.assertEqual(result[0]["candidate_user_id"], 1)

    def test_real_vs_real_never_suggested(self):
        members = [
            {"user_id": 1, "first_name": "Ajay", "username": None},
            {"user_id": 2, "first_name": "Ajaz", "username": None},
        ]
        with patch("services.identity.db.get_all_proxy_names", return_value=[]), \
             patch("services.identity.db.get_active_members", return_value=members), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(result, [])

    def test_already_linked_alias_excluded(self):
        links = [{"alias_proxy_name": "Ajya", "canonical_user_id": 1,
                  "canonical_proxy_name": None, "status": "linked"}]
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya", "Ajay"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: links if status == "linked" else []):
            result = identity.list_suggestions(1)
        self.assertEqual(result, [])

    def test_dismissed_pair_excluded_but_other_candidates_still_surface(self):
        dismissed = [{"alias_proxy_name": "Ajya", "canonical_user_id": None,
                      "canonical_proxy_name": "Ajay", "status": "dismissed"}]
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya", "Ajay", "Aju"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: dismissed if status == "dismissed" else []):
            result = identity.list_suggestions(1)
        pairs = {(s["alias_proxy_name"], s["candidate_proxy_name"]) for s in result}
        self.assertNotIn(("Ajay", "Ajya"), pairs)
        self.assertIn(("Aju", "Ajya"), pairs)


class TestDismissSuggestion(unittest.TestCase):

    def test_dismisses_and_is_idempotent(self):
        with patch("services.identity.db.insert_dismissed_suggestion") as mock_insert:
            r1 = identity.dismiss_suggestion(1, "Ajya", candidate_proxy_name="Ajay",
                                              admin_user_id=1, admin_name="Admin")
            r2 = identity.dismiss_suggestion(1, "Ajya", candidate_proxy_name="Ajay",
                                              admin_user_id=1, admin_name="Admin")
        self.assertEqual(r1, {"dismissed": True})
        self.assertEqual(r2, {"dismissed": True})
        self.assertEqual(mock_insert.call_count, 2)  # idempotency is enforced in db.py, not here

    def test_requires_exactly_one_candidate(self):
        with self.assertRaises(parameterMissing):
            identity.dismiss_suggestion(1, "Ajya", admin_user_id=1, admin_name="Admin")
        with self.assertRaises(parameterMissing):
            identity.dismiss_suggestion(1, "Ajya", candidate_user_id=1, candidate_proxy_name="X",
                                         admin_user_id=1, admin_name="Admin")


class TestIdentityStats(unittest.TestCase):

    def _patch_common(self, **overrides):
        defaults = dict(
            get_chat_ended_rollcall_count=10,
            get_user_attendance_count=5,
            get_user_stats_row={"total_in": 5, "total_out": 1, "total_maybe": 0,
                                 "total_waiting_to_in": 0, "total_rollcalls": 6,
                                 "best_streak": 3, "current_streak": 2},
            get_ghost_count=1,
        )
        defaults.update(overrides)
        return defaults

    def test_real_user_only_group_matches_single_identity_totals(self):
        with patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_chat_ended_rollcall_count", return_value=10), \
             patch("services.identity.db.get_user_attendance_count", return_value=5), \
             patch("services.identity.db.get_user_stats_row",
                   return_value={"total_in": 5, "total_out": 1, "total_maybe": 0,
                                 "total_waiting_to_in": 0, "total_rollcalls": 6,
                                 "best_streak": 3, "current_streak": 2}), \
             patch("services.identity.db.get_ghost_count", return_value=1), \
             patch("services.identity.db.get_identity_last_activity", return_value="2026-01-01"), \
             patch("services.identity.manager") as mock_mgr:
            mock_mgr.get_absent_limit.return_value = 1
            s = identity.identity_stats(1, user_id=999)
        self.assertEqual(s["sessions_attended"], 5)
        self.assertEqual(s["total_in_votes"], 5)
        self.assertEqual(s["best_streak"], 3)
        self.assertEqual(s["current_streak"], 2)
        self.assertEqual(s["ghost_count"], 1)

    def test_merged_group_sums_counters_across_real_and_proxy(self):
        with patch("services.identity.db.get_links_by_canonical",
                   return_value=[{"alias_proxy_name": "Rex"}]), \
             patch("services.identity.db.get_chat_ended_rollcall_count", return_value=10), \
             patch("services.identity.db.get_user_attendance_count", return_value=5), \
             patch("services.identity.db.get_user_stats_row",
                   return_value={"total_in": 5, "total_out": 1, "total_maybe": 0,
                                 "total_waiting_to_in": 0, "total_rollcalls": 6,
                                 "best_streak": 3, "current_streak": 2}), \
             patch("services.identity.db.get_ghost_count", return_value=1), \
             patch("services.identity.db.get_proxy_attendance_count", return_value=2), \
             patch("services.identity.db.get_proxy_stats",
                   return_value={"total_in": 2, "total_out": 0, "total_maybe": 0, "total_rollcalls": 2}), \
             patch("services.identity.db.get_proxy_streaks",
                   return_value={"best_streak": 5, "current_streak": 1}), \
             patch("services.identity.db.get_ghost_count_by_proxy_name", return_value=3), \
             patch("services.identity.db.get_identity_last_activity", return_value=None), \
             patch("services.identity.manager") as mock_mgr:
            mock_mgr.get_absent_limit.return_value = 1
            s = identity.identity_stats(1, user_id=999)
        self.assertEqual(s["sessions_attended"], 7)  # 5 + 2
        self.assertEqual(s["total_in_votes"], 7)      # 5 + 2
        self.assertEqual(s["ghost_count"], 4)          # 1 + 3
        self.assertEqual(s["best_streak"], 5)           # max(3, 5)

    def test_current_streak_belongs_to_most_recently_active_alias(self):
        """The canonical (real user) is iterated first with an OLDER
        timestamp; the proxy alias is more recently active and must win
        current_streak — despite being listed second."""
        def last_activity(chat_id, user_id=None, proxy_name=None):
            return "2026-01-01" if user_id is not None else "2026-06-01"

        with patch("services.identity.db.get_links_by_canonical",
                   return_value=[{"alias_proxy_name": "Rex"}]), \
             patch("services.identity.db.get_chat_ended_rollcall_count", return_value=10), \
             patch("services.identity.db.get_user_attendance_count", return_value=5), \
             patch("services.identity.db.get_user_stats_row",
                   return_value={"total_in": 5, "total_out": 0, "total_maybe": 0,
                                 "total_waiting_to_in": 0, "total_rollcalls": 5,
                                 "best_streak": 3, "current_streak": 2}), \
             patch("services.identity.db.get_ghost_count", return_value=0), \
             patch("services.identity.db.get_proxy_attendance_count", return_value=1), \
             patch("services.identity.db.get_proxy_stats",
                   return_value={"total_in": 1, "total_out": 0, "total_maybe": 0, "total_rollcalls": 1}), \
             patch("services.identity.db.get_proxy_streaks",
                   return_value={"best_streak": 1, "current_streak": 9}), \
             patch("services.identity.db.get_ghost_count_by_proxy_name", return_value=0), \
             patch("services.identity.db.get_identity_last_activity", side_effect=last_activity), \
             patch("services.identity.manager") as mock_mgr:
            mock_mgr.get_absent_limit.return_value = 1
            s = identity.identity_stats(1, user_id=999)
        self.assertEqual(s["current_streak"], 9)  # the proxy alias's value wins

    def test_tie_break_prefers_canonical_iterated_first(self):
        """Equal last-activity timestamps: the canonical member (iterated
        first) keeps its own current_streak rather than being overwritten."""
        with patch("services.identity.db.get_links_by_canonical",
                   return_value=[{"alias_proxy_name": "Rex"}]), \
             patch("services.identity.db.get_chat_ended_rollcall_count", return_value=10), \
             patch("services.identity.db.get_user_attendance_count", return_value=5), \
             patch("services.identity.db.get_user_stats_row",
                   return_value={"total_in": 5, "total_out": 0, "total_maybe": 0,
                                 "total_waiting_to_in": 0, "total_rollcalls": 5,
                                 "best_streak": 3, "current_streak": 2}), \
             patch("services.identity.db.get_ghost_count", return_value=0), \
             patch("services.identity.db.get_proxy_attendance_count", return_value=1), \
             patch("services.identity.db.get_proxy_stats",
                   return_value={"total_in": 1, "total_out": 0, "total_maybe": 0, "total_rollcalls": 1}), \
             patch("services.identity.db.get_proxy_streaks",
                   return_value={"best_streak": 1, "current_streak": 9}), \
             patch("services.identity.db.get_ghost_count_by_proxy_name", return_value=0), \
             patch("services.identity.db.get_identity_last_activity", return_value="2026-01-01"), \
             patch("services.identity.manager") as mock_mgr:
            mock_mgr.get_absent_limit.return_value = 1
            s = identity.identity_stats(1, user_id=999)
        self.assertEqual(s["current_streak"], 2)  # canonical's own value, not the tied proxy's


if __name__ == "__main__":
    unittest.main()
