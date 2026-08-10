"""
Real-DB integration tests for the repeat= shortcut (/start_roll_call,
/repeat) and /calendar — see rollCall/handlers/lifecycle.py,
rollCall/services/templates.py, rollCall/handlers/templates.py.
"""
import pytz
from datetime import datetime, timedelta

import db
from helpers import IntegrationBase, ADMIN_USER, USERS, CHAT_ID
from mock_helpers import get_mock_bot
import services.templates as templates_svc


class TestRepeatFlag(IntegrationBase):

    async def test_weekly_repeat_creates_and_schedules_template(self):
        await self.start_roll_call(self.msg("/src Friday Football repeat=weekly", ADMIN_USER))

        templates = templates_svc.list_templates(CHAT_ID)
        self.assertEqual(len(templates), 1)
        tmpl = templates[0]
        self.assertEqual(tmpl["name"], "friday-football")
        self.assertEqual(tmpl["title"], "Friday Football")
        self.assertTrue(tmpl["schedule_enabled"])
        self.assertEqual(tmpl["recurrence_type"], "weekly")
        self.assertIsNotNone(tmpl["schedule_day"])
        self.assertIsNotNone(tmpl["schedule_time"])

        texts = self.sent_texts()
        self.assertTrue(any("Repeats" in t and "weekly" in t for t in texts))

        # The rollcall itself must still have started normally.
        rc = self.rc(0)
        self.assertEqual(rc.title, "Friday Football")

    async def test_monthly_repeat_uses_day_of_month(self):
        await self.start_roll_call(self.msg("/src Monthly Meetup repeat=monthly", ADMIN_USER))
        tmpl = templates_svc.list_templates(CHAT_ID)[0]
        self.assertEqual(tmpl["recurrence_type"], "monthly")
        today = datetime.now(pytz.timezone("Asia/Kolkata")).day
        self.assertEqual(tmpl["schedule_day"], str(today))

    async def test_invalid_repeat_type_creates_no_rollcall(self):
        await self.start_roll_call(self.msg("/src Bad Test repeat=fortnightly", ADMIN_USER))
        self.assertEqual(len(self.mgr.get_rollcalls(CHAT_ID)), 0)
        self.assertEqual(templates_svc.list_templates(CHAT_ID), [])
        texts = self.sent_texts()
        self.assertTrue(any("fortnightly" in t for t in texts))

    async def test_repeat_command_clones_last_and_schedules(self):
        await self.start_rc("Friday Football")
        rc = self.rc(0)
        rc.location = "Turf"
        rc.event_fee = "500"
        rc.save()
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))

        await self.repeat_roll_call(self.msg("/repeat repeat=biweekly", ADMIN_USER))

        rc2 = self.rc(0)
        self.assertEqual(rc2.title, "Friday Football")
        self.assertEqual(rc2.location, "Turf")

        tmpl = templates_svc.list_templates(CHAT_ID)[0]
        self.assertEqual(tmpl["recurrence_type"], "biweekly")
        self.assertEqual(tmpl["location"], "Turf")

    async def test_rerunning_repeat_flag_refreshes_not_duplicates(self):
        """Same title, run twice across two weeks -> one template, not two,
        with the schedule refreshed to the latest recurrence type."""
        await self.start_roll_call(self.msg("/src Friday Football repeat=weekly", ADMIN_USER))
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        await self.start_roll_call(self.msg("/src Friday Football repeat=biweekly", ADMIN_USER))

        templates = templates_svc.list_templates(CHAT_ID)
        self.assertEqual(len(templates), 1)
        self.assertEqual(templates[0]["recurrence_type"], "biweekly")


class TestCalendar(IntegrationBase):

    async def test_empty_state(self):
        await self.calendar_command(self.msg("/calendar", ADMIN_USER))
        texts = self.sent_texts()
        self.assertTrue(any("Nothing scheduled" in t for t in texts))

    async def test_shows_active_rollcall_closing(self):
        await self.start_rc("Friday Football")
        tz = pytz.timezone("Asia/Kolkata")
        rc = self.rc(0)
        rc.finalizeDate = datetime.now(tz) + timedelta(hours=2)
        rc.save()

        get_mock_bot().send_message.reset_mock()
        await self.calendar_command(self.msg("/calendar", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("Friday Football" in t and "closes" in t for t in texts))

    async def test_shows_recurring_template_next_occurrence(self):
        templates_svc.upsert_template(CHAT_ID, "weekly-game", ADMIN_USER["id"], "Admin", title="Weekly Game")
        templates_svc.set_schedule(
            CHAT_ID, "weekly-game", ADMIN_USER["id"], "Admin",
            recurrence_type="weekly", schedule_day="monday", schedule_time="18:00",
        )

        await self.calendar_command(self.msg("/calendar", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("Weekly Game" in t and "weekly" in t for t in texts))

    async def test_shows_pending_one_time_schedule_with_resolved_title(self):
        templates_svc.upsert_template(CHAT_ID, "one-off", ADMIN_USER["id"], "Admin", title="One-Off Special")
        future = (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.create_scheduled_rollcall(CHAT_ID, "one-off", future, ADMIN_USER["id"], "Admin")

        await self.calendar_command(self.msg("/calendar", ADMIN_USER))

        texts = self.sent_texts()
        self.assertTrue(any("One-Off Special" in t and "starts" in t for t in texts))

    async def test_mixed_sources_sorted_chronologically(self):
        tz = pytz.timezone("Asia/Kolkata")

        # Closes soonest (in 1 hour).
        await self.start_rc("Closing Soon")
        rc = self.rc(0)
        rc.finalizeDate = datetime.now(tz) + timedelta(hours=1)
        rc.save()

        # Starts in 2 days.
        templates_svc.upsert_template(CHAT_ID, "later-oneoff", ADMIN_USER["id"], "Admin", title="Later One-Off")
        future = (datetime.utcnow() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.create_scheduled_rollcall(CHAT_ID, "later-oneoff", future, ADMIN_USER["id"], "Admin")

        get_mock_bot().send_message.reset_mock()
        await self.calendar_command(self.msg("/calendar", ADMIN_USER))

        text = self.sent_texts()[0]
        closing_pos = text.find("Closing Soon")
        later_pos = text.find("Later One-Off")
        self.assertGreater(closing_pos, -1)
        self.assertGreater(later_pos, -1)
        self.assertLess(closing_pos, later_pos, "earlier event must appear first")

    async def test_visible_without_admin_rights_check(self):
        """/calendar has no admin_rights gate -- any member can run it."""
        member = USERS[0]
        await self.calendar_command(self.msg("/calendar", member))
        # No error/permission message -- just the (possibly empty) listing.
        texts = self.sent_texts()
        self.assertTrue(len(texts) >= 1)
        self.assertFalse(any("permission" in t.lower() for t in texts))
