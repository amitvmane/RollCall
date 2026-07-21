"""
/weblink — grants web-admin status. Must respect the SAME admin_rights()
gate every other admin command uses, since is_web_admin now controls real
mutating power on the group web page (start/end rollcall, silent mode,
proxy votes, recurring-schedule editing). Previously ungated: any member
could self-grant web-admin, bypassing a group's own /set_admins lock.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))


def _msg(text="/weblink", chat_id=-100, user_id=1, first_name="Eve", username="eve"):
    m = MagicMock()
    m.text = text
    m.chat.id = chat_id
    m.from_user.id = user_id
    m.from_user.first_name = first_name
    m.from_user.username = username
    return m


class TestWeblinkAdminGate(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        import bot_state
        self.bot_state = bot_state
        bot_state.bot.send_message = AsyncMock()

    async def test_real_admin_gets_web_admin_status(self):
        from handlers.web import weblink_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web.admin_rights", new=AsyncMock(return_value=True)), \
             patch("handlers.web.manager") as mgr, \
             patch("handlers.web._db.set_web_admin") as grant:
            mgr.get_rollcalls.return_value = []
            await weblink_cmd(_msg())
        grant.assert_called_once_with(-100, 1, "Eve")

    async def test_non_admin_in_locked_group_does_not_get_web_admin(self):
        """The actual fix: a group that has enabled /set_admins must not let
        a random member self-grant web-admin via /weblink."""
        from handlers.web import weblink_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web.admin_rights", new=AsyncMock(return_value=False)), \
             patch("handlers.web.manager") as mgr, \
             patch("handlers.web._db.set_web_admin") as grant:
            mgr.get_rollcalls.return_value = []
            await weblink_cmd(_msg())
        grant.assert_not_called()

    async def test_non_admin_still_receives_the_voting_link(self):
        """/weblink itself must stay open to everyone — only the web-admin
        grant is gated, not the command."""
        from handlers.web import weblink_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web.admin_rights", new=AsyncMock(return_value=False)), \
             patch("handlers.web.manager") as mgr, \
             patch("handlers.web._db.set_web_admin"), \
             patch("handlers.web.get_group_web_token", return_value="grouptok123"):
            mgr.get_rollcalls.return_value = []
            await weblink_cmd(_msg())
        sent = self.bot_state.bot.send_message.call_args[0][1]
        self.assertIn("grouptok123", sent)

    async def test_default_open_group_still_grants_any_member(self):
        """admin_rights() itself returns True for everyone when a group
        hasn't enabled /set_admins — this preserves that default-open
        behavior unchanged; only locked-down groups are affected."""
        from handlers.web import weblink_cmd
        with patch.dict(os.environ, {"WEB_BASE_URL": "https://rc.example"}), \
             patch("handlers.web.admin_rights", new=AsyncMock(return_value=True)), \
             patch("handlers.web.manager") as mgr, \
             patch("handlers.web._db.set_web_admin") as grant:
            mgr.get_rollcalls.return_value = []
            await weblink_cmd(_msg(user_id=999, first_name="RandomMember"))
        grant.assert_called_once_with(-100, 999, "RandomMember")


if __name__ == "__main__":
    unittest.main()
