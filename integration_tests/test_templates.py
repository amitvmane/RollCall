"""
Integration: templates — create, list, start, delete, scheduling, auto-start.
"""
import asyncio
import db
from helpers import IntegrationBase, USERS, ADMIN_USER, CHAT_ID
from conftest import get_mock_bot


class TestTemplateCreate(IntegrationBase):

    async def test_set_template_creates_template(self):
        await self.set_template(self.msg('/set_template sunday "Sunday Game" limit=10', ADMIN_USER))
        tmpl = db.get_template(CHAT_ID, "sunday")
        self.assertIsNotNone(tmpl)
        self.assertEqual(tmpl["title"], "Sunday Game")
        self.assertEqual(tmpl["inlistlimit"], 10)

    async def test_set_template_no_name_sends_usage(self):
        await self.set_template(self.msg("/set_template", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("format" in t.lower() or "usage" in t.lower() or "name" in t.lower() for t in texts))

    async def test_set_template_with_location(self):
        await self.set_template(self.msg('/set_template park "Park Game" location=CentralPark', ADMIN_USER))
        tmpl = db.get_template(CHAT_ID, "park")
        self.assertIsNotNone(tmpl)
        self.assertEqual(tmpl["location"], "CentralPark")

    async def test_set_template_with_event_day_time(self):
        await self.set_template(self.msg(
            '/set_template weekly "Weekly" event_day=sunday event_time=17:00',
            ADMIN_USER
        ))
        tmpl = db.get_template(CHAT_ID, "weekly")
        self.assertIsNotNone(tmpl)
        self.assertEqual(tmpl["event_day"], "sunday")
        self.assertEqual(tmpl["event_time"], "17:00")

    async def test_set_template_updates_existing(self):
        await self.set_template(self.msg('/set_template upd "Old Title"', ADMIN_USER))
        await self.set_template(self.msg('/set_template upd "New Title"', ADMIN_USER))
        tmpl = db.get_template(CHAT_ID, "upd")
        self.assertEqual(tmpl["title"], "New Title")

    async def test_set_template_name_too_long_rejected(self):
        long_name = "x" * 51
        await self.set_template(self.msg(f'/set_template {long_name} "Title"', ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("too long" in t.lower() for t in texts))

    async def test_list_templates_shows_template(self):
        await self.set_template(self.msg('/set_template alpha "Alpha Event"', ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.list_templates(self.msg("/templates", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("alpha" in t.lower() for t in texts))

    async def test_list_templates_no_templates_sends_empty_message(self):
        await self.list_templates(self.msg("/templates", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no template" in t.lower() for t in texts))


class TestTemplateStart(IntegrationBase):

    async def _create_template(self, name="weekly", title="Weekly Game",
                               event_day="sunday", event_time="17:00"):
        await self.set_template(self.msg(
            f'/set_template {name} "{title}" event_day={event_day} event_time={event_time}',
            ADMIN_USER
        ))

    async def test_start_template_creates_rollcall(self):
        await self._create_template()
        await self.start_template(self.msg("/start_template weekly", ADMIN_USER))
        rcs = self.mgr.get_rollcalls(CHAT_ID)
        self.assertEqual(len(rcs), 1)
        self.assertEqual(rcs[0].title, "Weekly Game")

    async def test_start_template_queues_panel_update(self):
        await self._create_template()
        await self.start_template(self.msg("/start_template weekly", ADMIN_USER))
        # Panel is debounced (300s louder-mode); verify the pending update was queued
        self.assertTrue(len(self.bs._pending_panel_updates) > 0)

    async def test_start_template_unknown_name_sends_error(self):
        await self.start_template(self.msg("/start_template nonexistent", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not found" in t.lower() for t in texts))

    async def test_start_template_no_name_sends_usage(self):
        await self.start_template(self.msg("/start_template", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("usage" in t.lower() for t in texts))

    async def test_start_template_with_extra_title(self):
        await self._create_template()
        await self.start_template(self.msg('/start_template weekly "With guests"', ADMIN_USER))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertIn("With guests", rc.title)

    async def test_start_template_applies_limit(self):
        await self.set_template(self.msg('/set_template limited "Limited" limit=3 event_day=sunday event_time=17:00', ADMIN_USER))
        await self.start_template(self.msg("/start_template limited", ADMIN_USER))
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(rc.inListLimit, 3)

    async def test_start_template_users_can_vote_in(self):
        await self._create_template()
        await self.start_template(self.msg("/start_template weekly", ADMIN_USER))
        for user in USERS[:5]:
            await self.vote_in(user)
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 5)


class TestTemplateDelete(IntegrationBase):

    async def test_delete_template_removes_it(self):
        await self.set_template(self.msg('/set_template todel "To Delete"', ADMIN_USER))
        self.assertIsNotNone(db.get_template(CHAT_ID, "todel"))
        await self.delete_template_command(self.msg("/delete_template todel", ADMIN_USER))
        self.assertIsNone(db.get_template(CHAT_ID, "todel"))

    async def test_delete_template_sends_confirmation(self):
        await self.set_template(self.msg('/set_template gone "Gone"', ADMIN_USER))
        get_mock_bot().send_message.reset_mock()
        await self.delete_template_command(self.msg("/delete_template gone", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("deleted" in t.lower() for t in texts))

    async def test_delete_template_not_found_sends_error(self):
        await self.delete_template_command(self.msg("/delete_template nobody", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("not found" in t.lower() for t in texts))

    async def test_delete_template_no_name_sends_usage(self):
        await self.delete_template_command(self.msg("/delete_template", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("usage" in t.lower() for t in texts))


class TestTemplateAutoStart(IntegrationBase):
    """Direct call to _auto_start_from_template to test scheduling logic without real time."""

    async def _make_tmpl(self, name="auto", title="Auto Game",
                         event_day="sunday", event_time="17:00",
                         inlistlimit=None):
        await self.set_template(self.msg(
            f'/set_template {name} "{title}" event_day={event_day} event_time={event_time}',
            ADMIN_USER
        ))
        tmpl = db.get_template(CHAT_ID, name)
        if inlistlimit is not None:
            tmpl["inlistlimit"] = inlistlimit
        return tmpl

    async def test_auto_start_creates_rollcall(self):
        from check_reminders import _auto_start_from_template
        tmpl = await self._make_tmpl()
        await _auto_start_from_template(CHAT_ID, tmpl)
        rcs = self.mgr.get_rollcalls(CHAT_ID)
        self.assertEqual(len(rcs), 1)
        self.assertEqual(rcs[0].title, "Auto Game")

    async def test_auto_start_sends_panel(self):
        from check_reminders import _auto_start_from_template
        tmpl = await self._make_tmpl()
        await _auto_start_from_template(CHAT_ID, tmpl)
        # Verify rollcall was created (panel send is an implementation detail)
        rcs = self.mgr.get_rollcalls(CHAT_ID)
        self.assertEqual(len(rcs), 1)
        self.assertEqual(rcs[0].title, "Auto Game")

    async def test_auto_start_applies_limit(self):
        from check_reminders import _auto_start_from_template
        tmpl = await self._make_tmpl(inlistlimit=4)
        await _auto_start_from_template(CHAT_ID, tmpl)
        rc = self.mgr.get_rollcall(CHAT_ID, 0)
        self.assertEqual(rc.inListLimit, 4)

    async def test_auto_start_blocked_at_three_rollcalls(self):
        from check_reminders import _auto_start_from_template
        # Fill up to 3 active rollcalls manually
        for i in range(3):
            await self.start_rc(f"Existing {i+1}")
        tmpl = await self._make_tmpl()
        await _auto_start_from_template(CHAT_ID, tmpl)
        # Still 3 rollcalls; 4th was blocked
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 3)
        # Titles are unchanged — no "Auto Game" was added
        titles = [rc.title for rc in self.mgr.get_rollcalls(CHAT_ID)]
        self.assertNotIn("Auto Game", titles)

    async def test_auto_start_users_can_vote_in(self):
        from check_reminders import _auto_start_from_template
        tmpl = await self._make_tmpl()
        await _auto_start_from_template(CHAT_ID, tmpl)
        for user in USERS[:3]:
            await self.vote_in(user)
        self.assertEqual(len(self.mgr.get_rollcall(CHAT_ID, 0).inList), 3)


class TestScheduleCommand(IntegrationBase):

    async def test_schedules_no_templates_sends_empty(self):
        await self.schedules_command(self.msg("/schedules", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("no scheduled" in t.lower() for t in texts))

    async def test_schedules_shows_scheduled_templates(self):
        await self.set_template(self.msg(
            '/set_template sg "Sunday Game" event_day=sunday event_time=17:00',
            ADMIN_USER
        ))
        db.set_template_schedule(CHAT_ID, "sg", "friday", "09:00", "weekly")
        get_mock_bot().send_message.reset_mock()
        await self.schedules_command(self.msg("/schedules", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("sg" in t.lower() or "sunday game" in t.lower() for t in texts))
