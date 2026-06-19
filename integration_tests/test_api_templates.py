"""
Integration tests for template + schedule REST routes.
FastAPI TestClient, real DB, scoped tokens.
"""
import unittest

from fastapi.testclient import TestClient

from mock_helpers import reset_db


def _import():
    import bot_state  # noqa
    import rollcall_manager
    from api.main import app
    return {"app": app, "manager": rollcall_manager.manager}


CHAT_ID = -1001999000101
ADMIN = {"id": 999, "name": "Admin"}


class TemplateAPIBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = _import()
        cls.app = env["app"]
        cls.manager = env["manager"]
        cls.client = TestClient(cls.app)

    def setUp(self):
        reset_db()
        self.manager.clear_cache()
        from api.rate_limit import reset_buckets_for_tests
        reset_buckets_for_tests()
        from db import _hash_token, generate_api_token, insert_api_token
        token = generate_api_token()
        insert_api_token(_hash_token(token), CHAT_ID, "read,vote,admin",
                         label="test", issued_by_user_id=ADMIN["id"])
        self.client.headers["Authorization"] = f"Bearer {token}"

    def _upsert(self, name, **kwargs):
        body = {"admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"], **kwargs}
        return self.client.put(f"/api/v1/chats/{CHAT_ID}/templates/{name}", json=body)

    def _start(self, name, extra=None):
        body = {"admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}
        if extra:
            body["extra_title"] = extra
        return self.client.post(f"/api/v1/chats/{CHAT_ID}/templates/{name}/start", json=body)

    def _admin_body(self):
        return {"admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"]}


class TestTemplateCRUD(TemplateAPIBase):

    def test_list_empty(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/templates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_upsert_creates_template(self):
        r = self._upsert("friday", title="Friday FC", limit=14, location="Field A")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["name"], "friday")
        self.assertEqual(body["title"], "Friday FC")
        self.assertEqual(body["limit"], 14)

    def test_list_returns_created(self):
        self._upsert("friday", title="Fri")
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/templates")
        self.assertEqual(r.status_code, 200)
        names = [t["name"] for t in r.json()]
        self.assertIn("friday", names)

    def test_get_single_template(self):
        self._upsert("friday", title="Fri")
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/templates/friday")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "Fri")

    def test_get_nonexistent_returns_422(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/templates/ghost")
        self.assertEqual(r.status_code, 422)

    def test_upsert_partial_update_preserves_fields(self):
        self._upsert("friday", title="Fri", limit=10)
        r = self._upsert("friday", title="Friday FC")  # don't pass limit
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["limit"], 10)

    def test_delete_template(self):
        self._upsert("friday", title="Fri")
        r = self.client.request("DELETE", f"/api/v1/chats/{CHAT_ID}/templates/friday",
                                json=self._admin_body())
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["deleted"])

    def test_delete_nonexistent_returns_422(self):
        r = self.client.request("DELETE", f"/api/v1/chats/{CHAT_ID}/templates/ghost",
                                json=self._admin_body())
        self.assertEqual(r.status_code, 422)


class TestStartTemplate(TemplateAPIBase):

    def test_start_creates_rollcall(self):
        self._upsert("friday", title="Friday FC", limit=10)
        r = self._start("friday")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertEqual(body["title"], "Friday FC")

    def test_start_with_extra_title(self):
        self._upsert("friday", title="Friday")
        r = self._start("friday", extra="With Guests")
        self.assertEqual(r.status_code, 201)
        self.assertIn("With Guests", r.json()["title"])

    def test_start_nonexistent_returns_422(self):
        r = self._start("ghost")
        self.assertEqual(r.status_code, 422)

    def test_start_requires_admin_scope(self):
        from db import _hash_token, generate_api_token, insert_api_token
        ro = generate_api_token()
        insert_api_token(_hash_token(ro), CHAT_ID, "read", label="ro",
                         issued_by_user_id=ADMIN["id"])
        self._upsert("friday", title="F")
        r = self.client.post(f"/api/v1/chats/{CHAT_ID}/templates/friday/start",
                             json=self._admin_body(),
                             headers={"Authorization": f"Bearer {ro}"})
        self.assertEqual(r.status_code, 403)


class TestScheduleRoutes(TemplateAPIBase):

    def setUp(self):
        super().setUp()
        self._upsert("friday", title="F", event_day="friday", event_time="18:00")

    def _sched_body(self, **kwargs):
        return {"admin_user_id": ADMIN["id"], "admin_name": ADMIN["name"], **kwargs}

    def test_get_schedule_returns_disabled_by_default(self):
        r = self.client.get(f"/api/v1/chats/{CHAT_ID}/templates/friday/schedule")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["schedule_enabled"])

    def test_set_weekly_schedule(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/templates/friday/schedule",
            json=self._sched_body(recurrence_type="weekly",
                                  schedule_day="friday", schedule_time="18:00")
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["schedule_enabled"])
        self.assertEqual(body["schedule_day"], "friday")

    def test_set_monthly_schedule(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/templates/friday/schedule",
            json=self._sched_body(recurrence_type="monthly",
                                  monthly_day=15, schedule_time="09:00")
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["recurrence_type"], "monthly")

    def test_disable_schedule(self):
        self.client.put(
            f"/api/v1/chats/{CHAT_ID}/templates/friday/schedule",
            json=self._sched_body(schedule_day="friday", schedule_time="18:00")
        )
        r = self.client.request(
            "DELETE", f"/api/v1/chats/{CHAT_ID}/templates/friday/schedule",
            json=self._admin_body()
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["schedule_enabled"])

    def test_set_schedule_bad_day_returns_422(self):
        r = self.client.put(
            f"/api/v1/chats/{CHAT_ID}/templates/friday/schedule",
            json=self._sched_body(schedule_day="fri", schedule_time="18:00")
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
