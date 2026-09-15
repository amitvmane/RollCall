"""
Every change to a member's state gets announced, and a dead Telegram never
breaks the change itself.

Two rules, both reported from production:

1. COVERAGE — if a member moves between IN/OUT/MAYBE/WAITING, the group is
   told. This failed whenever a vote freed a capped slot: `/out`, `/maybe`,
   `/sof` and `/smf` announced whoever moved UP off the waitlist and silently
   dropped the vote that caused it, because the two announcements were written
   as an if/else. The panel buttons always sent both, so the same action
   announced differently depending on how you did it.

2. RESILIENCE — when Telegram refuses (bot kicked, chat deleted, 403), the
   state change must still land, nothing may escape as an exception, and the
   failure goes to the logs where it can be debugged. Announcing is a report
   of a change, never a precondition for it.

Identity merges are deliberately NOT announced and have no test here.
"""
from unittest.mock import AsyncMock

from helpers import IntegrationBase, USERS, ADMIN_USER
from mock_helpers import get_mock_bot


class _Banned(Exception):
    """Stands in for ApiTelegramException 403 — bot kicked or blocked."""


class _AnnounceBase(IntegrationBase):

    async def capped_real(self):
        """Cap of 2: User1 + User2 IN, User3 WAITING."""
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 2", ADMIN_USER))
        for u in USERS[:3]:
            await self.vote_in(u)
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User1", "User2"])
        self.assertEqual([u.name for u in rc.waitList], ["User3"])

    async def capped_proxy(self):
        """Cap of 2: Guest1 + Guest2 IN, Guest3 WAITING."""
        await self.start_rc()
        await self.wait_limit(self.msg("/set_limit 2", ADMIN_USER))
        for n in ("Guest1", "Guest2", "Guest3"):
            await self.set_in_for(self.msg(f"/sif {n}", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["Guest1", "Guest2"])
        self.assertEqual([u.name for u in rc.waitList], ["Guest3"])

    def announced(self):
        return " | ".join(self.sent_texts())

    def ban_telegram(self):
        b = get_mock_bot()
        b.send_message = AsyncMock(side_effect=_Banned("Forbidden: bot was kicked"))
        b.edit_message_text = AsyncMock(side_effect=_Banned("Forbidden"))
        b.edit_message_reply_markup = AsyncMock(side_effect=_Banned("Forbidden"))


class TestPromotionDoesNotSwallowTheVote(_AnnounceBase):
    """The change and the promotion it caused are both announced."""

    async def test_out_command_announces_voter_and_promotion(self):
        await self.capped_real()
        get_mock_bot().send_message.reset_mock()
        await self.vote_out(USERS[0])
        t = self.announced()
        self.assertIn("User1", t, f"voter's own OUT was not announced: {t}")
        self.assertIn("OUT", t.upper(), t)
        self.assertIn("User3", t, f"promotion was not announced: {t}")

    async def test_maybe_command_announces_voter_and_promotion(self):
        await self.capped_real()
        get_mock_bot().send_message.reset_mock()
        await self.vote_maybe(USERS[0])
        t = self.announced()
        self.assertIn("MAYBE", t.upper(), f"voter's own MAYBE was not announced: {t}")
        self.assertIn("User3", t, f"promotion was not announced: {t}")

    async def test_sof_announces_proxy_and_promotion(self):
        await self.capped_proxy()
        get_mock_bot().send_message.reset_mock()
        await self.set_out_for(self.msg("/sof Guest1", ADMIN_USER))
        t = self.announced()
        self.assertIn("Guest1", t, f"proxy's own OUT was not announced: {t}")
        self.assertIn("Guest3", t, f"promotion was not announced: {t}")

    async def test_smf_announces_proxy_and_promotion(self):
        await self.capped_proxy()
        get_mock_bot().send_message.reset_mock()
        await self.set_maybe_for(self.msg("/smf Guest1", ADMIN_USER))
        t = self.announced()
        self.assertIn("Guest1", t, f"proxy's own MAYBE was not announced: {t}")
        self.assertIn("Guest3", t, f"promotion was not announced: {t}")

    async def test_panel_button_still_announces_both(self):
        """The path that was already correct stays correct."""
        await self.capped_real()
        self.bs._rate_limits.clear()
        get_mock_bot().send_message.reset_mock()
        await self.callback_handler(self.call("btn_out_1", USERS[0]))
        t = self.announced()
        self.assertIn("User1", t, t)
        self.assertIn("User3", t, t)

    async def test_command_and_button_announce_the_same_thing(self):
        """Same action, two entry points, one outcome — the property that
        actually failed here, rather than either message's wording."""
        await self.capped_real()
        get_mock_bot().send_message.reset_mock()
        await self.vote_out(USERS[0])
        via_command = len(self.sent_texts())

        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        await self.capped_real()
        self.bs._rate_limits.clear()
        get_mock_bot().send_message.reset_mock()
        await self.callback_handler(self.call("btn_out_1", USERS[0]))
        via_button = len(self.sent_texts())

        self.assertEqual(via_command, via_button,
                         f"/out sent {via_command} message(s), the panel button sent {via_button}")


class TestStateChangesSurviveDeadTelegram(_AnnounceBase):
    """A refused send never costs us the state change, and never escapes."""

    async def test_out_vote_lands_when_telegram_is_banned(self):
        await self.capped_real()
        self.ban_telegram()
        await self.vote_out(USERS[0])          # must not raise
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User2", "User3"])
        self.assertEqual([u.name for u in rc.outList], ["User1"])
        self.assertEqual(rc.waitList, [])

    async def test_delete_user_lands_when_telegram_is_banned(self):
        await self.capped_real()
        self.ban_telegram()
        await self.delete_user(self.msg(f"/delete_user {USERS[0]['first_name']}", ADMIN_USER))
        await self.ghost_callback_handler(
            self.call(f"delconf_yes_0_{ADMIN_USER['id']}", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["User2", "User3"])
        self.assertEqual(rc.waitList, [])

    async def test_proxy_vote_lands_when_telegram_is_banned(self):
        await self.capped_proxy()
        self.ban_telegram()
        await self.set_out_for(self.msg("/sof Guest1", ADMIN_USER))
        rc = self.rc(0)
        self.assertEqual([u.name for u in rc.inList], ["Guest2", "Guest3"])
        self.assertEqual(rc.waitList, [])

    async def test_failure_is_logged_not_silent(self):
        """The change is invisible in the chat, so the logs are the only place
        left to find out it happened."""
        import logging
        await self.capped_real()
        self.ban_telegram()
        with self.assertLogs(level=logging.WARNING) as captured:
            await self.vote_out(USERS[0])
        self.assertTrue(captured.output, "a refused send produced no log line at all")
