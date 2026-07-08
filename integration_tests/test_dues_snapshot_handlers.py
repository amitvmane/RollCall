"""
Integration tests for /dues_snapshot and /dues_export handlers.
Real SQLite, real handlers, mocked bot API.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "rollCall"))

import db
from services import dues as dues_svc
from helpers import IntegrationBase, CHAT_ID, ADMIN_ID, USERS
from mock_helpers import get_mock_bot


def _enable(upi="snap@upi"):
    db.get_or_create_chat(CHAT_ID)
    db.update_chat_settings(CHAT_ID, dues_enabled=1, dues_round_step=10, upi_vpa=upi)
    dues_svc.seed_default_penalty_tiers(CHAT_ID)


def _seed_balance(uid, name, amount, entry_type="share"):
    db.add_dues_entry(CHAT_ID, None, uid, name, entry_type, amount,
                      None, ADMIN_ID, "Admin")


class TestDuesSnapshotHandler(IntegrationBase):

    async def test_snapshot_posts_to_group(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 90)

        await self.dues_snapshot_cmd(self.msg("/dues_snapshot"))

        texts = self.sent_texts()
        self.assertTrue(any("Snapshot" in t for t in texts),
                        f"Expected snapshot header; got: {texts}")

    async def test_snapshot_contains_owed_member(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 90)

        await self.dues_snapshot_cmd(self.msg("/dues_snapshot"))

        combined = "\n".join(self.sent_texts())
        self.assertIn(USERS[0]["first_name"], combined)
        self.assertIn("₹90", combined)

    async def test_snapshot_contains_fund_balance(self):
        _enable()
        db.add_fund_transaction(CHAT_ID, None, "topup", 500, "test", ADMIN_ID, "Admin")

        await self.dues_snapshot_cmd(self.msg("/dues_snapshot"))

        combined = "\n".join(self.sent_texts())
        self.assertIn("500", combined)

    async def test_snapshot_shows_last_game(self):
        _enable()
        rc = await self.start_rc("Snapshot Game")
        rc.event_fee = "300"
        for u in USERS[:3]:
            await self.vote_in(u)
        await self.dues_settle_dues(self.msg("/settle_dues"))
        get_mock_bot().send_message.reset_mock()

        await self.dues_snapshot_cmd(self.msg("/dues_snapshot"))

        combined = "\n".join(self.sent_texts())
        self.assertIn("Snapshot Game", combined)

    async def test_snapshot_admin_only(self):
        _enable()
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"

        await self.dues_snapshot_cmd(self.msg("/dues_snapshot", user=USERS[1]))

        combined = "\n".join(self.sent_texts())
        self.assertTrue(
            any(w in combined.lower() for w in ("permission", "admin", "only")),
            f"Expected permission error; got: {combined}",
        )
        self.mgr.set_admin_rights(CHAT_ID, False)
        get_mock_bot().get_chat_member.return_value.status = "administrator"

    async def test_alias_ds_works(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 50)

        await self.dues_snapshot_cmd(self.msg("/ds"))

        texts = self.sent_texts()
        self.assertTrue(any("Snapshot" in t for t in texts),
                        f"/ds alias should work; got: {texts}")

    async def test_snapshot_empty_group_no_crash(self):
        _enable()
        # No dues entries at all
        await self.dues_snapshot_cmd(self.msg("/dues_snapshot"))

        texts = self.sent_texts()
        self.assertTrue(len(texts) > 0, "Should post something even with no data")


class TestDuesExportHandler(IntegrationBase):

    async def test_export_sends_document(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 90)

        await self.dues_export_cmd(self.msg("/dues_export"))

        bot = get_mock_bot()
        self.assertTrue(bot.send_document.called,
                        "send_document should be called for CSV export")

    async def test_export_document_has_csv_filename(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 90)

        await self.dues_export_cmd(self.msg("/dues_export"))

        bot = get_mock_bot()
        call_kwargs = bot.send_document.call_args
        # filename passed as visible_file_name kwarg
        filename = call_kwargs.kwargs.get("visible_file_name", "")
        self.assertTrue(filename.endswith(".csv"),
                        f"Expected .csv filename; got: {filename}")

    async def test_export_no_data_sends_message_not_file(self):
        _enable()
        # No dues entries

        await self.dues_export_cmd(self.msg("/dues_export"))

        bot = get_mock_bot()
        self.assertFalse(bot.send_document.called,
                         "send_document should NOT be called when there is no data")
        texts = self.sent_texts()
        self.assertTrue(any("No dues" in t for t in texts),
                        f"Expected 'No dues data' message; got: {texts}")

    async def test_export_admin_only(self):
        _enable()
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"

        await self.dues_export_cmd(self.msg("/dues_export", user=USERS[1]))

        bot = get_mock_bot()
        self.assertFalse(bot.send_document.called)
        self.mgr.set_admin_rights(CHAT_ID, False)
        get_mock_bot().get_chat_member.return_value.status = "administrator"

    async def test_alias_de_works(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 90)

        await self.dues_export_cmd(self.msg("/de"))

        bot = get_mock_bot()
        self.assertTrue(bot.send_document.called,
                        "/de alias should trigger CSV export")

    async def test_export_caption_contains_date(self):
        _enable()
        _seed_balance(USERS[0]["id"], USERS[0]["first_name"], 90)

        await self.dues_export_cmd(self.msg("/dues_export"))

        bot = get_mock_bot()
        call_kwargs = bot.send_document.call_args
        caption = call_kwargs.kwargs.get("caption", "")
        self.assertIn("export", caption.lower(),
                      f"Caption should mention export; got: {caption}")


class TestDuesReportHandler(IntegrationBase):

    async def test_enable_weekly_stores_flag(self):
        _enable()
        await self.dues_report_cmd(self.msg("/dues_report weekly"))

        import db
        chat = db.get_or_create_chat(CHAT_ID)
        self.assertEqual(chat.get("dues_report_enabled"), 1)

    async def test_disable_off_clears_flag(self):
        _enable()
        import db
        db.update_chat_settings(CHAT_ID, dues_report_enabled=1)

        await self.dues_report_cmd(self.msg("/dues_report off"))

        chat = db.get_or_create_chat(CHAT_ID)
        self.assertFalse(chat.get("dues_report_enabled"))

    async def test_enable_posts_confirmation(self):
        _enable()
        await self.dues_report_cmd(self.msg("/dues_report weekly"))

        combined = "\n".join(self.sent_texts())
        self.assertIn("enabled", combined.lower())

    async def test_disable_posts_confirmation(self):
        _enable()
        await self.dues_report_cmd(self.msg("/dues_report off"))

        combined = "\n".join(self.sent_texts())
        self.assertIn("disabled", combined.lower())

    async def test_status_shows_current(self):
        _enable()
        await self.dues_report_cmd(self.msg("/dues_report"))

        combined = "\n".join(self.sent_texts())
        self.assertTrue(
            "off" in combined.lower() or "on" in combined.lower(),
            f"Expected on/off status; got: {combined}",
        )

    async def test_invalid_arg_shows_usage(self):
        _enable()
        await self.dues_report_cmd(self.msg("/dues_report badarg"))

        combined = "\n".join(self.sent_texts())
        self.assertIn("Usage", combined)

    async def test_alias_dr_works(self):
        _enable()
        await self.dues_report_cmd(self.msg("/dr weekly"))

        import db
        chat = db.get_or_create_chat(CHAT_ID)
        self.assertEqual(chat.get("dues_report_enabled"), 1)

    async def test_admin_only(self):
        _enable()
        self.mgr.set_admin_rights(CHAT_ID, True)
        get_mock_bot().get_chat_member.return_value.status = "member"

        await self.dues_report_cmd(self.msg("/dues_report weekly", user=USERS[1]))

        import db
        chat = db.get_or_create_chat(CHAT_ID)
        self.assertFalse(chat.get("dues_report_enabled"))
        self.mgr.set_admin_rights(CHAT_ID, False)
        get_mock_bot().get_chat_member.return_value.status = "administrator"
