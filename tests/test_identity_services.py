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


class TestGetCanonicalMap(unittest.TestCase):
    """Batch version of resolve_canonical used by the hot aggregators
    (get_ghost_leaderboard, get_leaderboard_by_attendance,
    get_all_dues_balances) to avoid one get_identity_link query per proxy
    row — see db.py's Phase 1 N+1 fix."""

    def test_empty_chat_returns_empty_map(self):
        with patch("services.identity.db.list_identity_links", return_value=[]):
            m = identity.get_canonical_map(1)
        self.assertEqual(m, {})

    def test_maps_alias_merged_into_real_user(self):
        links = [{"alias_proxy_name": "Rex", "canonical_user_id": 999, "canonical_proxy_name": None}]
        with patch("services.identity.db.list_identity_links", return_value=links):
            m = identity.get_canonical_map(1)
        self.assertEqual(m["rex"], {"kind": "user", "user_id": 999, "proxy_name": None})

    def test_maps_alias_merged_into_another_proxy(self):
        links = [{"alias_proxy_name": "Ajya", "canonical_user_id": None, "canonical_proxy_name": "Ajay"}]
        with patch("services.identity.db.list_identity_links", return_value=links):
            m = identity.get_canonical_map(1)
        self.assertEqual(m["ajya"], {"kind": "proxy", "user_id": None, "proxy_name": "Ajay"})

    def test_key_is_lowercased_for_case_insensitive_lookup(self):
        links = [{"alias_proxy_name": "SB7", "canonical_user_id": 1, "canonical_proxy_name": None}]
        with patch("services.identity.db.list_identity_links", return_value=links):
            m = identity.get_canonical_map(1)
        self.assertIn("sb7", m)
        self.assertNotIn("SB7", m)

    def test_matches_resolve_canonical_for_each_linked_name(self):
        # The whole point of the batch map is that per-name lookups against
        # it agree with what resolve_canonical (the per-row, DB-hitting
        # path) would have returned for the same name.
        links = [
            {"alias_proxy_name": "Rex", "canonical_user_id": 999, "canonical_proxy_name": None},
            {"alias_proxy_name": "Ajya", "canonical_user_id": None, "canonical_proxy_name": "Ajay"},
        ]
        with patch("services.identity.db.list_identity_links", return_value=links):
            m = identity.get_canonical_map(1)
        for link in links:
            with patch("services.identity.db.get_identity_link",
                       return_value={"canonical_user_id": link["canonical_user_id"],
                                     "canonical_proxy_name": link["canonical_proxy_name"]}):
                expected = identity.resolve_canonical(1, proxy_name=link["alias_proxy_name"])
            self.assertEqual(m[link["alias_proxy_name"].lower()], expected)


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


class TestListIdentityGroups(unittest.TestCase):

    def _link_for(self, name):
        links = {
            "rex": {"canonical_user_id": 999, "canonical_proxy_name": None},
            "aju": {"canonical_user_id": None, "canonical_proxy_name": "Ajay"},
        }
        return links.get(name.lower())

    def test_groups_sorted_alphabetically_by_display_name(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Rex", "Aju", "Ajay"]), \
             patch("services.identity.db.list_identity_links", return_value=[]), \
             patch("services.identity.db.get_identity_link", side_effect=lambda chat_id, name: self._link_for(name)), \
             patch("services.identity.db.get_member_display_info",
                   return_value={"first_name": "Zara", "username": None}):
            result = identity.list_identity_groups(1)
        names = [g["display_name"] for g in result]
        self.assertEqual(names, ["Ajay", "Zara"])

    def test_case_variant_alias_with_no_own_row_still_appears(self):
        """"amit" and "Amit" both resolve to the same canonical via
        case-insensitive lookup even though only ONE of them has its own
        identity_links row (the alias-uniqueness index is case-insensitive,
        so a second case variant can't get a separate row) — both must
        still show up as aliases, not silently vanish."""
        def link_for(chat_id, name):
            # Only "Amit" (exact case) has a stored row; "amit" resolves to
            # the same canonical purely via case-insensitive SQL matching,
            # which this mock simulates by keying on the lowercased name.
            if name.lower() == "amit":
                return {"canonical_user_id": None, "canonical_proxy_name": "Zed"}
            return None
        with patch("services.identity.db.get_all_proxy_names", return_value=["amit", "Amit", "Zed"]), \
             patch("services.identity.db.list_identity_links", return_value=[]), \
             patch("services.identity.db.get_identity_link", side_effect=link_for):
            result = identity.list_identity_groups(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["aliases"]), {"amit", "Amit"})
        self.assertEqual(result[0]["proxy_name"], "Zed")


class TestLinkIdentities(unittest.TestCase):

    def _admin(self):
        return {"admin_user_id": 1, "admin_name": "Admin"}

    def test_basic_merge_into_real_user(self):
        with patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links") as mock_repoint, \
             patch("services.identity.db.log_admin_action") as mock_log, \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value={"first_name": "Rex", "username": "rexreal"}):
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
             patch("services.identity.db.get_member_display_info", return_value={"first_name": "Ajay", "username": "ajayreal"}):
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

    def test_results_sorted_alphabetically_by_display_name(self):
        members = [{"user_id": 1, "first_name": "Zara", "username": None}]
        with patch("services.identity.db.get_active_members", return_value=members), \
             patch("services.identity.db.get_all_proxy_names", return_value=["Amit", "Bala"]), \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_all_identities(1)
        names = [r["display_name"] for r in result]
        self.assertEqual(names, ["Amit", "Bala", "Zara"])

    def test_discarded_proxy_excluded(self):
        with patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.get_all_proxy_names", return_value=["Solo", "Garbage2"]), \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: [{"alias_proxy_name": "Garbage2"}]
                   if status == "discarded" else []):
            result = identity.list_all_identities(1)
        names = {r["proxy_name"] for r in result}
        self.assertNotIn("Garbage2", names)
        self.assertIn("Solo", names)

    def test_proxy_activity_attached_from_db(self):
        activity = {"Solo": {"count": 5, "last_seen": "2026-08-01 10:00:00"}}
        with patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.get_all_proxy_names", return_value=["Solo"]), \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_proxy_name_activity", return_value=activity):
            result = identity.list_all_identities(1)
        self.assertEqual(result[0]["proxy_count"], 5)
        self.assertEqual(result[0]["proxy_last_seen"], "2026-08-01 10:00:00")

    def test_real_member_activity_fields_are_none(self):
        members = [{"user_id": 1, "first_name": "Alice", "username": None}]
        with patch("services.identity.db.get_active_members", return_value=members), \
             patch("services.identity.db.get_all_proxy_names", return_value=[]), \
             patch("services.identity.db.get_proxy_name_activity", return_value={}):
            result = identity.list_all_identities(1)
        self.assertIsNone(result[0]["proxy_count"])
        self.assertIsNone(result[0]["proxy_last_seen"])

    def test_proxy_missing_from_activity_defaults_safely(self):
        # The name-list query and the activity query aren't atomic — a name
        # present in one but absent from the other shouldn't crash the picker.
        with patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.get_all_proxy_names", return_value=["Ghost"]), \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_proxy_name_activity", return_value={}):
            result = identity.list_all_identities(1)
        self.assertIsNone(result[0]["proxy_count"])
        self.assertIsNone(result[0]["proxy_last_seen"])


class TestAutoMergeExactDuplicates(unittest.TestCase):
    """Case/whitespace-only variants of the same proxy name (e.g. "Amit" /
    "amit" / " Amit ") are certainly the same person and merge without
    requiring admin confirmation — unlike fuzzy Levenshtein suggestions.
    Deliberately proxy<->proxy only (see module docstring on the function)."""

    def test_case_variants_auto_merge(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["amit", "Amit"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links"), \
             patch("services.identity.db.log_admin_action") as mock_log, \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            identity.list_all_identities(1)
        mock_upsert.assert_called_once()
        # Deterministic: alphabetically-first ("Amit" < "amit") is canonical.
        self.assertEqual(mock_upsert.call_args.args[1], "amit")
        self.assertEqual(mock_upsert.call_args.kwargs["canonical_proxy_name"], "Amit")
        # System actor, not a real admin.
        self.assertEqual(mock_log.call_args.args[1], 0)

    def test_whitespace_variants_auto_merge(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Amit K", "Amit  K"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links"), \
             patch("services.identity.db.log_admin_action"), \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            identity.list_all_identities(1)
        mock_upsert.assert_called_once()

    def test_proxy_matching_real_user_name_not_auto_merged(self):
        """A proxy exactly named like a real member is NOT auto-merged —
        common first names could coincidentally collide; stays a
        human-reviewed suggestion instead."""
        with patch("services.identity.db.get_all_proxy_names", return_value=["Amit"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 1, "first_name": "Amit", "username": None}]), \
             patch("services.identity.db.list_identity_links", return_value=[]), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            identity.list_all_identities(1)
        mock_upsert.assert_not_called()

    def test_already_linked_name_not_reprocessed(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["amit", "Amit"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: [{"alias_proxy_name": "amit"}]
                   if status == "linked" else []), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            identity.list_all_identities(1)
        mock_upsert.assert_not_called()

    def test_discarded_name_not_resurrected(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["amit", "Amit"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: [{"alias_proxy_name": "amit"}]
                   if status == "discarded" else []), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]), \
             patch("services.identity.db.get_member_display_info", return_value=None):
            identity.list_all_identities(1)
        mock_upsert.assert_not_called()

    def test_list_suggestions_also_triggers_auto_merge(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["amit", "Amit"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]), \
             patch("services.identity.db.upsert_identity_link") as mock_upsert, \
             patch("services.identity.db.repoint_links"), \
             patch("services.identity.db.log_admin_action"), \
             patch("services.identity.db.get_identity_link", return_value=None), \
             patch("services.identity.db.get_links_by_canonical", return_value=[]):
            identity.list_suggestions(1)
        mock_upsert.assert_called_once()


class TestDiscardIdentity(unittest.TestCase):

    def test_discard_calls_db_and_logs(self):
        with patch("services.identity.db.discard_identity_name") as mock_discard, \
             patch("services.identity.db.log_admin_action") as mock_log:
            r = identity.discard_identity(1, "Garbage2", admin_user_id=1, admin_name="Admin")
        mock_discard.assert_called_once_with(1, "Garbage2", created_by=1, created_by_name="Admin")
        mock_log.assert_called_once()
        self.assertEqual(r, {"discarded": True})

    def test_undiscard_calls_db_and_logs(self):
        with patch("services.identity.db.undiscard_identity_name", return_value=True), \
             patch("services.identity.db.log_admin_action") as mock_log:
            r = identity.undiscard_identity(1, "Garbage2", admin_user_id=1, admin_name="Admin")
        mock_log.assert_called_once()
        self.assertEqual(r, {"restored": True})

    def test_undiscard_idempotent_when_not_discarded(self):
        with patch("services.identity.db.undiscard_identity_name", return_value=False), \
             patch("services.identity.db.log_admin_action") as mock_log:
            r = identity.undiscard_identity(1, "Solo", admin_user_id=1, admin_name="Admin")
        mock_log.assert_not_called()
        self.assertEqual(r, {"restored": False})

    def test_list_discarded_returns_sorted_names(self):
        rows = [{"alias_proxy_name": "Zulu"}, {"alias_proxy_name": "Alpha"}]
        with patch("services.identity.db.list_identity_links", return_value=rows):
            result = identity.list_discarded(1)
        self.assertEqual(result, ["Alpha", "Zulu"])

    def test_discarded_name_excluded_from_suggestions(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya", "Ajay"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: [{"alias_proxy_name": "Ajay"}]
                   if status == "discarded" else []):
            result = identity.list_suggestions(1)
        self.assertEqual(result, [])


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
        # Real-member candidates (unlike proxy<->proxy ones) don't compete
        # for the greedy per-name cap, so this cleanly isolates "dismissed
        # pair excluded" from the separate per-name capping behavior
        # (covered by its own test below).
        dismissed = [{"alias_proxy_name": "Ajya", "canonical_user_id": None,
                      "canonical_proxy_name": "Ajay", "status": "dismissed"}]
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya", "Ajay"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 5, "first_name": "Aju", "username": None}]), \
             patch("services.identity.db.list_identity_links",
                   side_effect=lambda chat_id, status: dismissed if status == "dismissed" else []):
            result = identity.list_suggestions(1)
        pairs = {(s["alias_proxy_name"], s["candidate_proxy_name"]) for s in result
                 if s["candidate_kind"] == "proxy"}
        self.assertNotIn(("Ajay", "Ajya"), pairs)
        self.assertNotIn(("Ajya", "Ajay"), pairs)
        # Ajya still gets its own (undismissed) suggestion against real user "Aju".
        aliases_suggested = {s["alias_proxy_name"] for s in result}
        self.assertIn("Ajya", aliases_suggested)

    def test_per_name_cap_avoids_redundant_overlapping_suggestions(self):
        """Three mutually-close proxy names must not all pairwise-suggest —
        each name is claimed by at most one suggestion, so the list stays
        bounded instead of showing every pair within threshold."""
        with patch("services.identity.db.get_all_proxy_names",
                   return_value=["Ajya", "Ajay", "Aju"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        claimed_names = set()
        for s in result:
            claimed_names.add(s["alias_proxy_name"].lower())
            if s["candidate_kind"] == "proxy":
                claimed_names.add(s["candidate_proxy_name"].lower())
        # All three names are mutually close, but each may only be claimed once.
        self.assertLessEqual(len(result), 1)

    def test_normalization_matches_whitespace_and_punctuation_variants(self):
        with patch("services.identity.db.get_all_proxy_names",
                   return_value=["Amit K", "AmitK"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 0)

    def test_exact_username_match_is_exact_username_confidence(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["SB7"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 1, "first_name": "Someone Else", "username": "SB7"}]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 0)
        self.assertEqual(result[0]["confidence"], "exact_username")

    def test_exact_first_name_match_without_username_is_exact_first_name_confidence(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ravi"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 1, "first_name": "Ravi", "username": None}]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["score"], 0)
        self.assertEqual(result[0]["confidence"], "exact_first_name")

    def test_username_wins_tie_break_over_first_name_when_both_exact(self):
        # A member whose username AND first_name both exactly match the
        # proxy name — username must win per the argmin's field order.
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ravi"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 1, "first_name": "Ravi", "username": "Ravi"}]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(result[0]["confidence"], "exact_username")

    def test_fuzzy_real_member_match_is_close_confidence(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya"]), \
             patch("services.identity.db.get_active_members",
                   return_value=[{"user_id": 1, "first_name": "Ajay", "username": None}]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0]["score"], 0)
        self.assertEqual(result[0]["confidence"], "close")

    def test_exact_proxy_pair_is_exact_proxy_confidence(self):
        with patch("services.identity.db.get_all_proxy_names",
                   return_value=["Amit K", "AmitK"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        self.assertEqual(result[0]["confidence"], "exact_proxy")

    def test_close_proxy_pair_is_close_confidence(self):
        with patch("services.identity.db.get_all_proxy_names", return_value=["Ajya", "Ajay"]), \
             patch("services.identity.db.get_active_members", return_value=[]), \
             patch("services.identity.db.list_identity_links", return_value=[]):
            result = identity.list_suggestions(1)
        proxy_result = [s for s in result if s["candidate_kind"] == "proxy"]
        self.assertEqual(len(proxy_result), 1)
        self.assertEqual(proxy_result[0]["confidence"], "close")


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
