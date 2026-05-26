"""
Integration: proxy commands — /sif, /sof, /smf, ghost warning, duplicate guard.
"""
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from mock_helpers import get_mock_bot


class TestProxyCommands(IntegrationBase):

    async def test_sif_adds_proxy_to_in_list(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        rc = self.rc(0)
        names = [u.name for u in rc.inList]
        self.assertIn("Alice", names)

    async def test_sof_adds_proxy_to_out_list(self):
        await self.start_rc()
        await self.set_out_for(self.msg("/sof Bob", ADMIN_USER))
        rc = self.rc(0)
        names = [u.name for u in rc.outList]
        self.assertIn("Bob", names)

    async def test_smf_adds_proxy_to_maybe_list(self):
        await self.start_rc()
        await self.set_maybe_for(self.msg("/smf Charlie", ADMIN_USER))
        rc = self.rc(0)
        names = [u.name for u in rc.maybeList]
        self.assertIn("Charlie", names)

    async def test_sif_with_comment(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Alice away trip", ADMIN_USER))
        rc = self.rc(0)
        alice = next((u for u in rc.inList if u.name == "Alice"), None)
        self.assertIsNotNone(alice)
        self.assertIn("away trip", alice.comment)

    async def test_sif_duplicate_guard(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("already" in t.lower() for t in texts))
        # Still only one Alice in IN list
        names = [u.name for u in self.rc(0).inList]
        self.assertEqual(names.count("Alice"), 1)

    async def test_sif_too_long_name_rejected(self):
        await self.start_rc()
        long_name = "A" * 41
        await self.set_in_for(self.msg(f"/sif {long_name}", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("too long" in t.lower() for t in texts))
        self.assertEqual(len(self.rc(0).inList), 0)

    async def test_sif_no_rollcall_sends_error(self):
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not active" in t.lower() for t in texts))

    async def test_sif_no_name_sends_error(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("missing" in t.lower() for t in texts))

    async def test_sof_moves_proxy_from_in_to_out(self):
        await self.start_rc()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 1)
        await self.set_out_for(self.msg("/sof Alice", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual(len(rc.inList), 0)
        self.assertEqual(len(rc.outList), 1)

    async def test_sif_ten_proxies(self):
        await self.start_rc()
        for i in range(1, 11):
            await self.set_in_for(self.msg(f"/sif Proxy{i}", ADMIN_USER))
        self.assertEqual(len(self.rc(0).inList), 10)


class TestProxyGhostWarning(IntegrationBase):

    async def test_ghost_warning_shown_for_proxy_with_history(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        # Seed ghost_records for proxy "Alice" (proxy entries use user_id=-1)
        db.increment_ghost_count(CHAT_ID, -1, "Alice", proxy_name="Alice")
        get_mock_bot().send_message.reset_mock()
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("ghost" in t.lower() or "warning" in t.lower() for t in texts))
        # Alice should NOT be in IN list yet
        self.assertEqual(len(self.rc(0).inList), 0)

    async def test_proxy_add_anyway_callback_adds_proxy(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, -1, "Alice", proxy_name="Alice")
        await self.set_in_for(self.msg("/sif Alice", ADMIN_USER))
        # rc_number is 0, proxy_name is Alice
        call = self.call("proxy_add_0_Alice", ADMIN_USER)
        await self.ghost_callback_handler(call)
        names = [u.name for u in self.rc(0).inList]
        self.assertIn("Alice", names)

    async def test_proxy_cancel_callback_does_not_add(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 1", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, -1, "Bob", proxy_name="Bob")
        await self.set_in_for(self.msg("/sif Bob", ADMIN_USER))
        call = self.call("proxy_cancel_0_Bob", ADMIN_USER)
        await self.ghost_callback_handler(call)
        self.assertEqual(len(self.rc(0).inList), 0)

    async def test_no_warning_when_ghost_count_below_limit(self):
        await self.start_rc()
        await self.set_absent_limit(self.msg("/absent 3", ADMIN_USER))
        db.increment_ghost_count(CHAT_ID, -1, "Dave", proxy_name="Dave")  # 1 ghost, limit=3
        await self.set_in_for(self.msg("/sif Dave", ADMIN_USER))
        # Should be added directly (no warning)
        names = [u.name for u in self.rc(0).inList]
        self.assertIn("Dave", names)
