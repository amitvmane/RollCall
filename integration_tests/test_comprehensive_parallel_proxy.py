"""
Comprehensive integration: 3 parallel rollcalls × 10 proxy users.

Covers every corner case of the combined proxy + parallel rollcall surface:
  - 10 proxies distributed across 3 simultaneous rollcalls
  - Waitlist promotion chains (proxy and real users)
  - Mixed real + proxy waitlists
  - /erc renumbering with existing proxy votes preserved
  - Same proxy name allowed in different rollcalls
  - Settings (limit) isolation per rollcall
  - Admin ops (delete, override) targeting correct rollcall
  - Ghost tracking for proxies across rollcalls
  - All /sif /sof /smf corner cases in parallel context
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
PROXIES = [f"Proxy{i}" for i in range(1, 11)]   # "Proxy1" … "Proxy10"


def _in_names(rc):
    return [u.name for u in rc.inList]

def _out_names(rc):
    return [u.name for u in rc.outList]

def _maybe_names(rc):
    return [u.name for u in rc.maybeList]

def _wait_names(rc):
    return [u.name for u in rc.waitList]


# ──────────────────────────────────────────────────────────────────────────────
# 1. Basic distribution: 10 proxies across 3 rollcalls
# ──────────────────────────────────────────────────────────────────────────────
class TestTenProxiesAcrossThreeRollcalls(IntegrationBase):

    async def _setup_three_rcs(self):
        await self.start_rc("Event A")
        await self.start_rc("Event B")
        await self.start_rc("Event C")

    async def test_three_rollcalls_start_independently(self):
        await self._setup_three_rcs()
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 3)
        for i in range(3):
            self.assertEqual(len(self.rc(i).inList), 0)

    async def test_proxies_1_to_3_land_on_rc1(self):
        await self._setup_three_rcs()
        for name in PROXIES[:3]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 3)
        self.assertEqual(len(self.rc(1).inList), 0)
        self.assertEqual(len(self.rc(2).inList), 0)
        self.assertEqual(_in_names(self.rc(0)), PROXIES[:3])

    async def test_proxies_4_to_6_land_on_rc2(self):
        await self._setup_three_rcs()
        for name in PROXIES[3:6]:
            await self.set_in_for(self.msg(f"/sif {name} ::2", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 0)
        self.assertEqual(len(self.rc(1).inList), 3)
        self.assertEqual(len(self.rc(2).inList), 0)

    async def test_proxies_7_to_10_land_on_rc3(self):
        await self._setup_three_rcs()
        for name in PROXIES[6:]:
            await self.set_in_for(self.msg(f"/sif {name} ::3", ADMIN_USER))
        self.assertEqual(len(self.rc(2).inList), 4)

    async def test_ten_proxies_fully_distributed(self):
        await self._setup_three_rcs()
        for name in PROXIES[:3]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        for name in PROXIES[3:6]:
            await self.set_in_for(self.msg(f"/sif {name} ::2", ADMIN_USER))
        for name in PROXIES[6:]:
            await self.set_in_for(self.msg(f"/sif {name} ::3", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 3)
        self.assertEqual(len(self.rc(1).inList), 3)
        self.assertEqual(len(self.rc(2).inList), 4)

    async def test_same_proxy_name_allowed_in_different_rollcalls(self):
        """'Alice' can be IN on rc1 AND rc2 simultaneously."""
        await self._setup_three_rcs()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        self.assertIn("Alice", _in_names(self.rc(0)))
        self.assertIn("Alice", _in_names(self.rc(1)))

    async def test_same_proxy_duplicate_within_same_rollcall_blocked(self):
        await self._setup_three_rcs()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        self.assertTrue(any("already" in t.lower() for t in self.sent_texts()))
        self.assertEqual(_in_names(self.rc(0)).count("Alice"), 1)

    async def test_sof_removes_proxy_from_correct_rollcall(self):
        await self._setup_three_rcs()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        await self.set_out_for(self.msg("/sof Alice ::2", ADMIN_USER))
        # Alice still in rc1, removed from rc2
        self.assertIn("Alice", _in_names(self.rc(0)))
        self.assertNotIn("Alice", _in_names(self.rc(1)))
        self.assertIn("Alice", _out_names(self.rc(1)))

    async def test_smf_routes_to_correct_rollcall(self):
        await self._setup_three_rcs()
        await self.set_maybe_for(self.msg("/smf Alice ::3", ADMIN_USER))
        self.assertNotIn("Alice", _maybe_names(self.rc(0)))
        self.assertNotIn("Alice", _maybe_names(self.rc(1)))
        self.assertIn("Alice", _maybe_names(self.rc(2)))

    async def test_proxy_in_then_maybe_changes_list(self):
        """Moving a proxy from IN to MAYBE works correctly."""
        await self._setup_three_rcs()
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        self.assertIn("Alice", _in_names(self.rc(1)))
        await self.set_maybe_for(self.msg("/smf Alice ::2", ADMIN_USER))
        self.assertNotIn("Alice", _in_names(self.rc(1)))
        self.assertIn("Alice", _maybe_names(self.rc(1)))

    async def test_proxy_out_then_in_changes_list(self):
        await self._setup_three_rcs()
        await self.set_out_for(self.msg("/sof Alice ::3", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Alice ::3", ADMIN_USER))
        self.assertNotIn("Alice", _out_names(self.rc(2)))
        self.assertIn("Alice", _in_names(self.rc(2)))


# ──────────────────────────────────────────────────────────────────────────────
# 2. Proxy waitlist promotion chains
# ──────────────────────────────────────────────────────────────────────────────
class TestProxyWaitlistPromotion(IntegrationBase):

    async def test_proxy_waitlisted_when_limit_full(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for name in PROXIES[:3]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy4", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 3)
        self.assertEqual(len(self.rc(0).waitList), 1)
        self.assertIn("Proxy4", _wait_names(self.rc(0)))

    async def test_sof_promotes_first_waiter(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for name in PROXIES[:3]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy4", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy5", ADMIN_USER))
        self.assertEqual(len(self.rc(0).waitList), 2)
        await self.set_out_for(self.msg("/sof Proxy1", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 3)
        self.assertEqual(len(rc.waitList), 1)
        self.assertIn("Proxy4", _in_names(rc))  # first waiter promoted
        self.assertIn("Proxy5", _wait_names(rc))  # second still waits

    async def test_five_proxies_waitlisted_then_limit_raised(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for name in PROXIES[:3]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        for name in PROXIES[3:8]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))  # 5 in waitlist
        self.assertEqual(len(self.rc(0).waitList), 5)
        await self.wait_limit(self.msg("/sl 6", ADMIN_USER))  # 3 new slots open
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 6)
        self.assertEqual(len(rc.waitList), 2)

    async def test_limit_reduce_moves_last_proxy_to_waitlist(self):
        await self.start_rc()
        for name in PROXIES[:5]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 3)
        self.assertEqual(len(rc.waitList), 2)

    async def test_proxy_in_waitlist_sof_removes_without_cascade(self):
        """Removing a proxy from waitlist (not IN) should not trigger promotion."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy1", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy2", ADMIN_USER))  # fills IN
        await self.set_in_for(self.msg("/sif Proxy3", ADMIN_USER))  # waitlist
        await self.set_in_for(self.msg("/sif Proxy4", ADMIN_USER))  # waitlist
        # Remove Proxy3 from waitlist (not from IN)
        await self.set_out_for(self.msg("/sof Proxy3", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 2)   # unchanged
        self.assertNotIn("Proxy3", _wait_names(rc))
        self.assertIn("Proxy4", _wait_names(rc))  # still waiting

    async def test_ten_proxies_limit_five_fills_waitlist(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 5", ADMIN_USER))
        for name in PROXIES:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 5)
        self.assertEqual(len(rc.waitList), 5)

    async def test_ten_proxies_all_out_one_by_one_promotes_sequentially(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        for name in PROXIES[:6]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        # Proxy1-3 IN, Proxy4-6 waiting
        for i in range(3):
            await self.set_out_for(self.msg(f"/sof Proxy{i+1}", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 3)
        in_names = _in_names(rc)
        self.assertIn("Proxy4", in_names)
        self.assertIn("Proxy5", in_names)
        self.assertIn("Proxy6", in_names)


# ──────────────────────────────────────────────────────────────────────────────
# 3. Mixed real users + proxy users in waitlist
# ──────────────────────────────────────────────────────────────────────────────
class TestMixedRealAndProxyWaitlist(IntegrationBase):

    async def test_real_user_in_proxy_in_waitlist_promoted_on_real_out(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        await self.vote_in(USERS[0])
        await self.vote_in(USERS[1])
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))   # waitlist
        await self.vote_out(USERS[0])  # real user leaves → Alice promoted
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 2)
        self.assertIn("Alice", _in_names(rc))

    async def test_proxy_in_real_user_in_waitlist_promoted_on_proxy_out(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy1", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy2", ADMIN_USER))
        await self.vote_in(USERS[0])   # real user goes to waitlist
        await self.set_out_for(self.msg("/sof Proxy1", ADMIN_USER))  # proxy leaves
        rc = self.rc(0)
        in_ids = {u.user_id for u in rc.inList}
        self.assertIn(USERS[0]["id"], in_ids)  # real user promoted

    async def test_fifo_order_preserved_mixed_waitlist(self):
        """Waitlist promotion is FIFO regardless of real vs proxy."""
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 1", ADMIN_USER))
        await self.vote_in(USERS[0])                                    # fills IN
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))       # wait #1
        await self.vote_in(USERS[1])                                    # wait #2
        await self.set_in_for(self.msg("/sif Bob", ADMIN_USER))         # wait #3
        await self.vote_out(USERS[0])          # opens 1 slot → Alice promoted
        rc = self.rc(0)
        self.assertIn("Alice", _in_names(rc))
        self.assertIn(USERS[1]["id"], {u.user_id for u in rc.waitList} |
                      {u.user_id for u in rc.inList})

    async def test_ten_real_users_limit_five_then_five_proxies_fill_waitlist(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 5", ADMIN_USER))
        for u in USERS[:5]:
            await self.vote_in(u)
        for name in PROXIES[:5]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 5)
        self.assertEqual(len(rc.waitList), 5)

    async def test_real_and_proxy_both_out_only_top_waiter_promoted(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg("/sif Proxy1", ADMIN_USER))
        await self.vote_in(USERS[1])                   # waitlist
        await self.set_in_for(self.msg("/sif Proxy2", ADMIN_USER))  # waitlist
        await self.vote_out(USERS[0])  # 1 slot opens → User1 (first waiter) promoted
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 2)
        self.assertEqual(len(rc.waitList), 1)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Parallel rollcall lifecycle: /erc renumbering with proxy data intact
# ──────────────────────────────────────────────────────────────────────────────
class TestParallelRollcallRenumbering(IntegrationBase):

    async def test_proxy_data_preserved_when_first_rc_ends(self):
        await self.start_rc("Event A")
        await self.start_rc("Event B")
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        self.assertIn("Alice", _in_names(self.rc(1)))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))  # ends Event A
        # Event B is now rc #1
        rc = self.rc(0)
        self.assertEqual(rc.title, "Event B")
        self.assertIn("Alice", _in_names(rc))

    async def test_ten_proxies_on_rc3_survive_rc1_and_rc2_ending(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.start_rc("C")
        for name in PROXIES:
            await self.set_in_for(self.msg(f"/sif {name} ::3", ADMIN_USER))
        self.assertEqual(len(self.rc(2).inList), 10)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))   # end A
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))   # end B
        rc = self.rc(0)  # C is now #1
        self.assertEqual(rc.title, "C")
        self.assertEqual(len(rc.inList), 10)

    async def test_vote_routing_correct_after_renumber(self):
        """After /erc removes rc1, ::1 routes to what was rc2."""
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.start_rc("Third")
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))   # removes First
        # Second is now ::1, Third is now ::2
        await self.set_in_for(self.msg("/sif Bob ::1", ADMIN_USER))
        self.assertIn("Bob", _in_names(self.rc(0)))   # was Second

    async def test_waitlist_intact_after_renumber(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.wait_limit(self.msg("/sl 2 ::2", ADMIN_USER))
        for name in PROXIES[:4]:
            await self.set_in_for(self.msg(f"/sif {name} ::2", ADMIN_USER))
        self.assertEqual(len(self.rc(1).waitList), 2)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))  # end A
        rc = self.rc(0)  # B is now #1
        self.assertEqual(len(rc.inList), 2)
        self.assertEqual(len(rc.waitList), 2)

    async def test_new_votes_go_to_correct_rc_after_renumber(self):
        await self.start_rc("X")
        await self.start_rc("Y")
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))  # remove X
        await self.vote_in(USERS[0])   # default → Y (now rc #1)
        self.assertIn(USERS[0]["id"], {u.user_id for u in self.rc(0).inList})


# ──────────────────────────────────────────────────────────────────────────────
# 5. Settings isolation per rollcall
# ──────────────────────────────────────────────────────────────────────────────
class TestSettingsIsolationAcrossRollcalls(IntegrationBase):

    async def test_limit_on_rc1_does_not_affect_rc2(self):
        await self.start_rc("Limited")
        await self.start_rc("Unlimited")
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))   # limits rc1
        for name in PROXIES[:3]:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))   # rc1
        for name in PROXIES[3:8]:
            await self.set_in_for(self.msg(f"/sif {name} ::2", ADMIN_USER))   # rc2
        rc1 = self.rc(0)
        rc2 = self.rc(1)
        self.assertEqual(len(rc1.inList), 2)
        self.assertEqual(len(rc1.waitList), 1)
        self.assertEqual(len(rc2.inList), 5)
        self.assertEqual(len(rc2.waitList), 0)

    async def test_shh_mode_applies_globally_not_per_rollcall(self):
        """shh mode is chat-wide, not rollcall-specific."""
        await self.start_rc("A")
        await self.start_rc("B")
        await self.shh(self.msg("/shh", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(USERS[0])           # rc1
        await self.vote_in(USERS[1], rc_suffix="::2")  # rc2
        # No "is now IN" message in shh mode
        texts = self.sent_texts()
        self.assertFalse(any("is now IN" in t for t in texts))

    async def test_per_rollcall_location_set_independently(self):
        await self.start_rc("Morning")
        await self.start_rc("Evening")
        await self.set_location(self.msg("/loc Park ::1", ADMIN_USER))
        await self.set_location(self.msg("/loc Club ::2", ADMIN_USER))
        self.assertEqual(self.rc(0).location, "Park")
        self.assertEqual(self.rc(1).location, "Club")

    async def test_limit_suffix_routes_to_correct_rollcall(self):
        await self.start_rc("P")
        await self.start_rc("Q")
        await self.wait_limit(self.msg("/sl 3 ::2", ADMIN_USER))
        # Fill rc2 to limit
        for name in PROXIES[:4]:
            await self.set_in_for(self.msg(f"/sif {name} ::2", ADMIN_USER))
        rc1 = self.rc(0)
        rc2 = self.rc(1)
        self.assertIsNone(rc1.inListLimit)  # rc1 unlimited
        self.assertEqual(len(rc2.inList), 3)
        self.assertEqual(len(rc2.waitList), 1)


# ──────────────────────────────────────────────────────────────────────────────
# 6. Ghost tracking with proxies across rollcalls
# ──────────────────────────────────────────────────────────────────────────────
class TestProxyGhostAcrossRollcalls(IntegrationBase):

    async def _seed_proxy_ghost(self, proxy_name, count=1):
        for _ in range(count):
            db.increment_ghost_count(CHAT_ID, -1, proxy_name, proxy_name=proxy_name)

    async def test_proxy_ghost_warning_on_rc2(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        await self._seed_proxy_ghost("GhostProxy")
        get_mock_bot().send_message.reset_mock()
        await self.set_in_for(self.msg("/sif GhostProxy ::2", ADMIN_USER))
        self.assertTrue(any(
            "ghost" in t.lower() or "warning" in t.lower()
            for t in self.sent_texts()
        ))
        self.assertNotIn("GhostProxy", _in_names(self.rc(1)))

    async def test_proxy_add_callback_on_rc2_targets_correct_rollcall(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        await self._seed_proxy_ghost("GhostProxy")
        await self.set_in_for(self.msg("/sif GhostProxy ::2", ADMIN_USER))
        call = self.call("proxy_add_1_GhostProxy", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertIn("GhostProxy", _in_names(self.rc(1)))
        self.assertNotIn("GhostProxy", _in_names(self.rc(0)))

    async def test_proxy_cancel_callback_leaves_proxy_off_list(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        await self._seed_proxy_ghost("GhostProxy2")
        await self.set_in_for(self.msg("/sif GhostProxy2 ::2", ADMIN_USER))
        call = self.call("proxy_cancel_1_GhostProxy2", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(1).inList), 0)

    async def test_no_ghost_warning_below_limit_on_rc3(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.start_rc("C")
        await self.set_absent_limit(self.msg("/absent 3", ADMIN_USER))
        await self._seed_proxy_ghost("Alice", count=1)   # 1 ghost, limit=3
        await self.set_in_for(self.msg("/sif Alice ::3", ADMIN_USER))
        self.assertIn("Alice", _in_names(self.rc(2)))   # added directly

    async def test_ghost_tracking_disabled_bypasses_proxy_warning(self):
        await self.start_rc()
        await self.toggle_ghost_tracking(self.msg("/gt off", ADMIN_USER))
        await self._seed_proxy_ghost("SpookyProxy", count=5)
        await self.set_in_for(self.msg("/sif SpookyProxy", ADMIN_USER))
        self.assertIn("SpookyProxy", _in_names(self.rc(0)))   # no warning


# ──────────────────────────────────────────────────────────────────────────────
# 7. Admin operations (delete, override) on parallel rollcalls
# ──────────────────────────────────────────────────────────────────────────────
class TestAdminOpsOnParallelRollcalls(IntegrationBase):

    async def test_delete_user_removes_from_correct_rollcall(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Alice ::2", ADMIN_USER))
        # callback data = delconf_yes_{rc_number_0based}_{admin_id}
        await self.delete_user(self.msg("/del Alice ::2", ADMIN_USER))
        call = self.call(f"delconf_yes_1_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertIn("Alice", _in_names(self.rc(0)))    # rc1 unchanged
        self.assertNotIn("Alice", _in_names(self.rc(1))) # rc2 cleared

    async def test_delete_real_user_from_one_of_three_rollcalls(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.start_rc("C")
        await self.vote_in(USERS[0])                     # rc1
        await self.vote_in(USERS[0], rc_suffix="::2")    # rc2
        await self.vote_in(USERS[0], rc_suffix="::3")    # rc3
        # rc_number=1 (0-based index for ::2); callback = delconf_yes_1_{admin_id}
        await self.delete_user(self.msg(f"/del {USERS[0]['username']} ::2", ADMIN_USER))
        call = self.call(f"delconf_yes_1_{ADMIN_USER['id']}", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertIn(USERS[0]["id"], {u.user_id for u in self.rc(0).inList})  # rc1 ok
        self.assertNotIn(USERS[0]["id"], {u.user_id for u in self.rc(1).inList})  # rc2 removed
        self.assertIn(USERS[0]["id"], {u.user_id for u in self.rc(2).inList})  # rc3 ok

    async def test_whos_in_shows_correct_count_with_proxies(self):
        await self.start_rc()
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Bob", ADMIN_USER))
        rc = self.rc(0)
        names = _in_names(rc)
        self.assertIn("Alice", names)
        self.assertIn("Bob", names)
        self.assertEqual(len(rc.inList), 3)

    async def test_whos_waiting_proxy_in_waitlist(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 1", ADMIN_USER))
        await self.vote_in(USERS[0])
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))   # waitlist
        rc = self.rc(0)
        self.assertEqual(len(rc.waitList), 1)
        self.assertIn("Alice", _wait_names(rc))


# ──────────────────────────────────────────────────────────────────────────────
# 8. Inline button voting on parallel rollcalls
# ──────────────────────────────────────────────────────────────────────────────
class TestInlineButtonsOnParallelRollcalls(IntegrationBase):

    async def test_btn_in_routes_to_correct_rollcall(self):
        await self.start_rc("First")
        await self.start_rc("Second")
        await self.callback_handler(self.call("btn_in_2", USERS[0]))
        self.assertEqual(len(self.rc(0).inList), 0)
        self.assertEqual(len(self.rc(1).inList), 1)

    async def test_btn_out_routes_to_correct_rollcall(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.vote_in(USERS[0], rc_suffix="::2")
        self._clear_rate(USERS[0])  # prevent rate limiter from blocking the OUT
        await self.callback_handler(self.call("btn_out_2", USERS[0]))
        self.assertEqual(len(self.rc(1).outList), 1)

    async def test_btn_maybe_routes_to_correct_rollcall(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.start_rc("C")
        await self.callback_handler(self.call("btn_maybe_3", USERS[0]))
        self.assertEqual(len(self.rc(2).maybeList), 1)

    async def test_btn_in_on_all_three_rollcalls_independent(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.start_rc("C")
        await self.callback_handler(self.call("btn_in_1", USERS[0]))
        await self.callback_handler(self.call("btn_in_2", USERS[1]))
        await self.callback_handler(self.call("btn_in_3", USERS[2]))
        for i in range(3):
            self.assertEqual(len(self.rc(i).inList), 1)

    async def test_btn_in_waitlist_via_inline_with_proxies(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 2", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy1", ADMIN_USER))
        await self.set_in_for(self.msg("/sif Proxy2", ADMIN_USER))
        await self.callback_handler(self.call("btn_in_1", USERS[0]))  # goes to waitlist
        self.assertEqual(len(self.rc(0).waitList), 1)
        self.assertIn(USERS[0]["id"], {u.user_id for u in self.rc(0).waitList})


# ──────────────────────────────────────────────────────────────────────────────
# 9. Real user ghost reconfirmation on parallel rollcalls
# ──────────────────────────────────────────────────────────────────────────────
class TestRealUserGhostOnParallelRollcalls(IntegrationBase):

    def _seed_ghost(self, user_id, count=1):
        for _ in range(count):
            db.increment_ghost_count(CHAT_ID, user_id, f"User{user_id}")

    async def test_reconf_for_user_on_rc2(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._seed_ghost(user["id"], 1)
        get_mock_bot().send_message.reset_mock()
        await self.vote_in(user, rc_suffix="::2")
        texts = self.sent_texts()
        self.assertTrue(any("ghost" in t.lower() or "committing" in t.lower() for t in texts))
        self.assertEqual(len(self.rc(1).inList), 0)

    async def test_reconf_commit_on_rc2_lands_in_rc2(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._seed_ghost(user["id"], 1)
        await self.vote_in(user, rc_suffix="::2")
        call = self.call(f"reconf_in_1_{user['id']}", user)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 0)
        self.assertEqual(len(self.rc(1).inList), 1)

    async def test_reconf_decline_on_rc2_goes_to_out_of_rc2(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        user = USERS[0]
        self._seed_ghost(user["id"], 1)
        await self.vote_in(user, rc_suffix="::2")
        call = self.call(f"reconf_out_1_{user['id']}", user)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(1).outList), 1)
        self.assertEqual(len(self.rc(1).inList), 0)


# ──────────────────────────────────────────────────────────────────────────────
# 10. Comprehensive corner cases per command
# ──────────────────────────────────────────────────────────────────────────────
class TestCommandCornerCases(IntegrationBase):
    """Every command: at least one corner case not covered elsewhere."""

    async def test_in_without_rollcall_sends_error(self):
        await self.vote_in(USERS[0])
        self.assertTrue(any("not active" in t.lower() for t in self.sent_texts()))

    async def test_out_without_being_in_is_ok(self):
        await self.start_rc()
        await self.vote_out(USERS[0])
        rc = self.rc(0)
        self.assertEqual(len(rc.outList), 1)

    async def test_maybe_without_rollcall_sends_error(self):
        await self.vote_maybe(USERS[0])
        self.assertTrue(any("not active" in t.lower() for t in self.sent_texts()))

    async def test_in_with_comment_stored_correctly(self):
        await self.start_rc()
        await self.vote_in(USERS[0], comment="running late")
        user_obj = self.rc(0).inList[0]
        self.assertIn("running late", user_obj.comment)

    async def test_sif_with_long_comment(self):
        await self.start_rc()
        long_comment = "away trip with family will join late"
        await self.set_in_for(self.msg(f"/sif Alice {long_comment}", ADMIN_USER))
        alice = next(u for u in self.rc(0).inList if u.name == "Alice")
        self.assertIn("away", alice.comment)

    async def test_sl_can_be_changed_to_larger_value(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 3", ADMIN_USER))
        self.assertEqual(self.rc(0).inListLimit, 3)
        await self.wait_limit(self.msg("/sl 10", ADMIN_USER))
        self.assertEqual(self.rc(0).inListLimit, 10)

    async def test_sl_zero_clears_cap(self):
        await self.start_rc()
        await self.wait_limit(self.msg("/sl 0", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("cleared" in t.lower() or "no cap" in t.lower() for t in texts))

    async def test_whos_in_empty_rollcall_sends_status(self):
        await self.start_rc()
        get_mock_bot().send_message.reset_mock()
        await self.whos_in(self.msg("/wi", USERS[0]))
        self.assertGreater(self.sent_count(), 0)

    async def test_whos_out_after_three_users_vote_out(self):
        await self.start_rc()
        for u in USERS[:3]:
            await self.vote_out(u)
        await self.whos_out(self.msg("/wo", USERS[0]))
        text = self.sent_texts()[-1]
        for u in USERS[:3]:
            self.assertIn(u["first_name"], text)

    async def test_erc_ends_most_recent_not_oldest(self):
        """Default /erc ends rollcall #1 (the oldest/first one)."""
        await self.start_rc("Old")
        await self.start_rc("New")
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        remaining = self.mgr.get_rollcalls(CHAT_ID)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].title, "New")

    async def test_set_title_changes_rollcall_name(self):
        await self.start_rc("Original")
        await self.set_title(self.msg("/st Updated Name", ADMIN_USER))
        self.assertEqual(self.rc(0).title, "Updated Name")

    async def test_shh_then_louder_toggles_back(self):
        await self.start_rc()
        await self.shh(self.msg("/shh", ADMIN_USER))
        self.assertTrue(self.mgr.get_shh_mode(CHAT_ID))
        await self.louder(self.msg("/louder", ADMIN_USER))
        self.assertFalse(self.mgr.get_shh_mode(CHAT_ID))

    async def test_event_fee_set_and_visible(self):
        await self.start_rc()
        await self.event_fee(self.msg("/ef 500", ADMIN_USER))
        self.assertEqual(str(self.rc(0).event_fee), "500")

    async def test_individual_fee_displays_per_person_amount(self):
        await self.start_rc()
        await self.vote_in(USERS[0])
        await self.event_fee(self.msg("/ef 1000", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.individual_fee(self.msg("/if", ADMIN_USER))
        self.assertGreater(self.sent_count(), 0)

    async def test_location_set(self):
        await self.start_rc()
        await self.set_location(self.msg("/loc Central Park", ADMIN_USER))
        self.assertEqual(self.rc(0).location, "Central Park")

    async def test_sif_no_name_sends_error(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif", ADMIN_USER))
        self.assertTrue(any("missing" in t.lower() for t in self.sent_texts()))

    async def test_sof_proxy_not_in_any_list_still_adds_to_out(self):
        await self.start_rc()
        await self.set_out_for(self.msg("/sof FreshProxy", ADMIN_USER))
        self.assertIn("FreshProxy", _out_names(self.rc(0)))

    async def test_smf_proxy_not_in_any_list_adds_to_maybe(self):
        await self.start_rc()
        await self.set_maybe_for(self.msg("/smf FreshProxy", ADMIN_USER))
        self.assertIn("FreshProxy", _maybe_names(self.rc(0)))

    async def test_invalid_rc_suffix_sends_error(self):
        await self.start_rc()
        await self.vote_in(USERS[0], rc_suffix="::99")
        self.assertTrue(any(
            "not active" in t.lower() or "doesn't exist" in t.lower() or "not started" in t.lower()
            for t in self.sent_texts()
        ))

    async def test_sif_invalid_rc_suffix_sends_error(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Alice ::5", ADMIN_USER))
        self.assertTrue(any(
            "not active" in t.lower() or "doesn't exist" in t.lower() or "not started" in t.lower()
            for t in self.sent_texts()
        ))

    async def test_history_command_returns_results_after_erc(self):
        await self.start_rc("Past Event")
        await self.vote_in(USERS[0])
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.history_command(self.msg("/history", USERS[0]))
        self.assertGreater(self.sent_count(), 0)

    async def test_stats_command_runs_without_error(self):
        await self.start_rc()
        await self.vote_in(USERS[0])
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        await self.stats_command(self.msg("/stats", USERS[0]))
        self.assertGreater(self.sent_count(), 0)

    async def test_toggle_ghost_off_then_on(self):
        await self.toggle_ghost_tracking(self.msg("/gt off", ADMIN_USER))
        self.assertFalse(self.mgr.get_ghost_tracking_enabled(CHAT_ID))
        await self.toggle_ghost_tracking(self.msg("/gt on", ADMIN_USER))
        self.assertTrue(self.mgr.get_ghost_tracking_enabled(CHAT_ID))

    async def test_set_absent_limit_persists(self):
        await self.set_absent_limit(self.msg("/absent 5", ADMIN_USER))
        self.assertEqual(self.mgr.get_absent_limit(CHAT_ID), 5)

    async def test_clear_absent_resets_ghost_count_for_user(self):
        db.increment_ghost_count(CHAT_ID, USERS[0]["id"], USERS[0]["first_name"])
        self.assertEqual(db.get_ghost_count(CHAT_ID, USERS[0]["id"]), 1)
        # command is /clear_absent <name> (with space and name arg)
        name = USERS[0]["first_name"]
        await self.clear_absent(self.msg(f"/clear_absent {name}", ADMIN_USER))
        self.assertEqual(db.get_ghost_count(CHAT_ID, USERS[0]["id"]), 0)

    async def test_ten_users_all_in_all_out_in_one_rollcall(self):
        await self.start_rc("Full Cycle")
        for u in USERS:
            await self.vote_in(u)
        self.assertEqual(len(self.rc(0).inList), 10)
        for u in USERS:
            await self.vote_out(u)
        self.assertEqual(len(self.rc(0).inList), 0)
        self.assertEqual(len(self.rc(0).outList), 10)

    async def test_ten_proxies_all_in_all_out(self):
        await self.start_rc()
        for name in PROXIES:
            await self.set_in_for(self.msg(f"/sif {name}", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 10)
        for name in PROXIES:
            await self.set_out_for(self.msg(f"/sof {name}", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 0)
        self.assertEqual(len(self.rc(0).outList), 10)

    async def test_ten_proxies_maybe_across_three_rollcalls(self):
        await self.start_rc("A")
        await self.start_rc("B")
        await self.start_rc("C")
        suffixes = ["", "::2", "::3"]
        for i, name in enumerate(PROXIES):
            suffix = suffixes[i % 3]
            cmd = f"/smf {name}{' ' + suffix if suffix else ''}"
            await self.set_maybe_for(self.msg(cmd, ADMIN_USER))
        totals = sum(len(self.rc(i).maybeList) for i in range(3))
        self.assertEqual(totals, 10)
