"""Cancelling a pending one-off cleans up after itself.

A /schedule_once one-off is a scheduled_rollcalls row plus a hidden __once_
template, and the scheduler deletes that template only when the row FIRES. A
cancelled one never fires, so deleting just the row left the template behind
forever (invisible to /templates), and the group was told the row's raw title
— which for a one-off is the internal name.
"""
import db
from services import templates as tmpl_svc

CHAT = -100777001


def _schedule(**kw):
    return tmpl_svc.schedule_once(
        CHAT, fire_at_iso="2099-01-01T09:00:00Z", title="Holiday Friday",
        admin_user_id=1, admin_name="Alice", **kw,
    )


def test_cancelling_a_one_off_removes_its_hidden_template():
    res = _schedule()
    name = res["template_name"]
    assert tmpl_svc.is_reserved_once_template_name(name)
    assert db.get_template(CHAT, name) is not None

    out = tmpl_svc.cancel_pending_once(CHAT, res["id"])

    assert out == {"display_title": "Holiday Friday"}, "announce the real title, not __once_…"
    assert db.get_template(CHAT, name) is None
    assert all(r["id"] != res["id"] for r in db.get_upcoming_scheduled_rollcalls(CHAT))


def test_cancelling_keeps_a_real_named_template():
    res = _schedule(save_as_template="holiday_keep")
    try:
        assert tmpl_svc.cancel_pending_once(CHAT, res["id"]) == {"display_title": "Holiday Friday"}
        assert db.get_template(CHAT, "holiday_keep") is not None, \
            "only the reserved prefix marks a template as disposable"
    finally:
        db.delete_template(CHAT, "holiday_keep")


def test_unknown_or_foreign_id_cancels_nothing():
    res = _schedule()
    try:
        assert tmpl_svc.cancel_pending_once(CHAT, 999_999_999) is None
        assert tmpl_svc.cancel_pending_once(CHAT + 1, res["id"]) is None, \
            "another chat's id must not reach this chat's row"
        assert db.get_template(CHAT, res["template_name"]) is not None
    finally:
        tmpl_svc.cancel_pending_once(CHAT, res["id"])
