"""
Regression: the recurring auto-start must build a rollcall the same way
/start_template does.

`check_reminders._auto_start_from_template` used to hand-roll template→rollcall
setup. That copy handled `event_day`/`event_time` but had never been given the
`offset_days/hours/minutes` fallback, so a recurring template configured with
offsets auto-opened with `finalizeDate=None` — and the reminder loop skips any
rollcall with no close time, so it never auto-closed and sent no reminders.
The one-time scheduled path was always fine; it delegated.

These assert the two paths agree, rather than asserting one hard-coded shape —
a future field added to start_template is then covered for free.
"""
from helpers import IntegrationBase, ADMIN_USER, CHAT_ID


class TestTemplateAutoStartParity(IntegrationBase):

    def _template(self, name, **fields):
        import db
        db.create_or_update_template(CHAT_ID, name, fields.pop("title", "Game"), **fields)
        return db.get_template(CHAT_ID, name)

    async def _finalize_via_manual(self, name):
        from services import templates as tsvc
        await tsvc.start_template(chat_id=CHAT_ID, name=name,
                                  admin_user_id=ADMIN_USER["id"], admin_name="Admin")
        return self.rc(0).finalizeDate

    async def _finalize_via_scheduler(self, tmpl):
        import check_reminders
        await check_reminders._auto_start_from_template(CHAT_ID, tmpl)
        return self.rc(0).finalizeDate

    async def test_offset_only_template_auto_closes(self):
        tmpl = self._template("offsetonly", offsethours=3)
        self.assertIsNotNone(
            await self._finalize_via_scheduler(tmpl),
            "auto-started rollcall has no close time — it will never auto-close",
        )

    async def test_offset_only_matches_manual_start(self):
        tmpl = self._template("offsetonly", offsethours=3)
        sched = await self._finalize_via_scheduler(tmpl)
        await self.end_roll_call(self.msg("/erc", ADMIN_USER))
        manual = await self._finalize_via_manual("offsetonly")
        self.assertIsNotNone(manual)
        self.assertLess(abs((sched - manual).total_seconds()), 120)

    async def test_event_day_template_still_works(self):
        tmpl = self._template("weekly", event_day="friday", event_time="19:00")
        self.assertIsNotNone(await self._finalize_via_scheduler(tmpl))

    async def test_auto_start_carries_limit_location_and_fee(self):
        tmpl = self._template("full", title="Full Game", inlistlimit=12,
                              location="Turf 3", eventfee="200")
        import check_reminders
        await check_reminders._auto_start_from_template(CHAT_ID, tmpl)
        rc = self.rc(0)
        self.assertEqual(rc.title, "Full Game")
        self.assertEqual(rc.inListLimit, 12)
        self.assertEqual(rc.location, "Turf 3")
        self.assertEqual(rc.event_fee, "200")

    async def test_auto_start_is_audit_logged(self):
        import db
        tmpl = self._template("audited", title="Audited Game")
        import check_reminders
        await check_reminders._auto_start_from_template(CHAT_ID, tmpl)
        actions = [r["action_type"] for r in db.get_admin_audit_log(CHAT_ID, limit=20)]
        self.assertIn("start_template", actions)
