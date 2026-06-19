"""
Tests for services/templates.py — upsert, list, get, start, delete,
get/set/disable schedule.
"""
import unittest

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa
    import rollcall_manager
    from services import templates as tmpl_svc
    from services import rollcalls as rc_svc
    from exceptions import amountOfRollCallsReached, incorrectParameter, parameterMissing
    return {
        "tmpl": tmpl_svc,
        "rc": rc_svc,
        "manager": rollcall_manager.manager,
        "incorrectParameter": incorrectParameter,
        "parameterMissing": parameterMissing,
        "amountOfRollCallsReached": amountOfRollCallsReached,
    }


CHAT_ID = -1001999000201
ADMIN = {"id": 999, "name": "Admin"}


class TemplateBase(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.tmpl = env["tmpl"]
        cls.rc = env["rc"]
        cls.manager = env["manager"]
        cls.incorrectParameter = env["incorrectParameter"]
        cls.parameterMissing = env["parameterMissing"]
        cls.amountOfRollCallsReached = env["amountOfRollCallsReached"]

    def setUp(self):
        reset_db()
        self.manager.clear_cache()


class TestUpsertAndList(TemplateBase):

    def test_upsert_creates_template(self):
        t = self.tmpl.upsert_template(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            title="Friday Football", limit=14, location="Field A"
        )
        self.assertEqual(t["name"], "friday")
        self.assertEqual(t["title"], "Friday Football")
        self.assertEqual(t["limit"], 14)
        self.assertEqual(t["location"], "Field A")

    def test_list_returns_all_templates(self):
        self.tmpl.upsert_template(CHAT_ID, "monday", ADMIN["id"], ADMIN["name"], title="Mon")
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"], title="Fri")
        listed = self.tmpl.list_templates(CHAT_ID)
        names = [t["name"] for t in listed]
        self.assertIn("monday", names)
        self.assertIn("friday", names)

    def test_list_empty_chat(self):
        self.assertEqual(self.tmpl.list_templates(CHAT_ID), [])

    def test_upsert_updates_existing_preserving_unspecified_fields(self):
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                                  title="Friday Football", limit=14)
        # Update only the title — limit should be preserved
        t = self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                                      title="Friday FC")
        self.assertEqual(t["title"], "Friday FC")
        self.assertEqual(t["limit"], 14)

    def test_upsert_invalid_event_day_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                                      event_day="fri")  # abbreviation not valid

    def test_upsert_empty_name_raises(self):
        with self.assertRaises(self.parameterMissing):
            self.tmpl.upsert_template(CHAT_ID, "  ", ADMIN["id"], ADMIN["name"])


class TestGetAndDelete(TemplateBase):

    def test_get_existing_template(self):
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"], title="Fri")
        t = self.tmpl.get_one_template(CHAT_ID, "friday")
        self.assertEqual(t["title"], "Fri")

    def test_get_nonexistent_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.tmpl.get_one_template(CHAT_ID, "ghost")

    def test_delete_removes_template(self):
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])
        result = self.tmpl.delete_one_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])
        self.assertTrue(result["deleted"])
        self.assertEqual(self.tmpl.list_templates(CHAT_ID), [])

    def test_delete_nonexistent_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.tmpl.delete_one_template(CHAT_ID, "ghost", ADMIN["id"], ADMIN["name"])


class TestStartTemplate(TemplateBase):

    async def test_start_creates_rollcall(self):
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                                  title="Friday FC", limit=10)
        rc = await self.tmpl.start_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])
        self.assertEqual(rc["title"], "Friday FC")
        active = self.manager.get_rollcalls(CHAT_ID)
        self.assertEqual(len(active), 1)

    async def test_start_with_extra_title(self):
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"], title="Friday")
        rc = await self.tmpl.start_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                                            extra_title="With Guests")
        self.assertIn("With Guests", rc["title"])

    async def test_start_nonexistent_template_raises(self):
        with self.assertRaises(self.incorrectParameter):
            await self.tmpl.start_template(CHAT_ID, "ghost", ADMIN["id"], ADMIN["name"])

    async def test_start_raises_at_rollcall_cap(self):
        self.tmpl.upsert_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"], title="F")
        for _ in range(3):
            await self.rc.start_rollcall(CHAT_ID, "X", ADMIN["id"], ADMIN["name"])
        with self.assertRaises(self.amountOfRollCallsReached):
            await self.tmpl.start_template(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])


class TestSchedule(TemplateBase):

    def setUp(self):
        super().setUp()
        # All schedule tests need event_day + event_time on the template
        self.tmpl.upsert_template(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            title="Friday FC", event_day="friday", event_time="18:00"
        )

    def test_set_weekly_schedule(self):
        result = self.tmpl.set_schedule(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            recurrence_type="weekly", schedule_day="friday", schedule_time="18:00"
        )
        self.assertTrue(result["schedule_enabled"])
        self.assertEqual(result["schedule_day"], "friday")
        self.assertEqual(result["schedule_time"], "18:00")
        self.assertEqual(result["recurrence_type"], "weekly")

    def test_set_biweekly_schedule(self):
        result = self.tmpl.set_schedule(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            recurrence_type="biweekly", schedule_day="friday", schedule_time="18:00"
        )
        self.assertEqual(result["recurrence_type"], "biweekly")

    def test_set_monthly_schedule(self):
        result = self.tmpl.set_schedule(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            recurrence_type="monthly", monthly_day=15, schedule_time="09:00"
        )
        self.assertEqual(result["recurrence_type"], "monthly")

    def test_disable_schedule(self):
        self.tmpl.set_schedule(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            schedule_day="friday", schedule_time="18:00"
        )
        result = self.tmpl.disable_schedule(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])
        self.assertFalse(result["schedule_enabled"])

    def test_enable_schedule(self):
        self.tmpl.set_schedule(
            CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
            schedule_day="friday", schedule_time="18:00"
        )
        self.tmpl.disable_schedule(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])
        result = self.tmpl.enable_schedule(CHAT_ID, "friday", ADMIN["id"], ADMIN["name"])
        self.assertTrue(result["schedule_enabled"])

    def test_set_schedule_bad_weekday_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.tmpl.set_schedule(
                CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                schedule_day="fri", schedule_time="18:00"
            )

    def test_set_schedule_bad_time_raises(self):
        with self.assertRaises(self.incorrectParameter):
            self.tmpl.set_schedule(
                CHAT_ID, "friday", ADMIN["id"], ADMIN["name"],
                schedule_day="friday", schedule_time="25:99"
            )


if __name__ == "__main__":
    unittest.main()
