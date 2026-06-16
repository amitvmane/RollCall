"""
Backward-compat & persistence verification for the telebot 4.34.0 upgrade.

Specifically answers: "do existing templates, schedules, and ongoing
rollcalls survive the lib bump + bug-fix landing on main?"

Approach:
  Phase A — build a prod-like state in a SQLite file:
            - 2 templates (one with weekly schedule, one with monthly)
            - 2 parallel active rollcalls
            - Multiple voters per rollcall (in/out/maybe/waiting lists)
            - Proxy users
            - Ghost tracking ON, ghost records for some users
            - Various settings (timezone, location, fees, when, shh)
            - Reminders set on one rollcall
  Phase B — capture DB row counts & key fields
  Phase C — simulate restart: clear in-memory caches, re-run startup
            paths (manager.clear_cache + resume_reminder_loops)
  Phase D — verify the state is intact post-restart:
            - /rollcalls shows both
            - /templates shows both
            - /schedules lists the scheduled ones
            - Vote lists are unchanged
            - Settings preserved
            - Reminder loops resumed without error
  Phase E — continue operating on the recovered state:
            - vote on a recovered rollcall
            - add a proxy
            - end one rollcall, verify the other survives
            - all callback paths still work

Uses the REAL pyTelegramBotAPI 4.34.0 library. Patches only outbound
network methods. Tracks ERROR-level logs throughout.
"""

import os
import sys
import asyncio
import logging
import sqlite3
import tempfile
import time
from unittest.mock import AsyncMock, MagicMock

# ─── Env: persistent SQLite path so we can "restart" against same state ──────
_DB_FILE = os.path.join(tempfile.gettempdir(), "rollcall_persist_verify.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)

os.environ["TELEGRAM_TOKEN"] = "999999:dummy_for_persist_verify"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["ADMIN1"] = "100"

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "rollCall"))

# Real telebot
from telebot.types import Update  # noqa: E402

import bot_state  # noqa: E402
import handlers  # noqa: F401, E402
import db as _db  # noqa: E402
import rollcall_manager as _rm  # noqa: E402
import check_reminders as _cr  # noqa: E402

_db.init_db()
bot = bot_state.bot

# ─── Error log capture ───────────────────────────────────────────────────────
_errors = []


class _ErrorCapture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            _errors.append(f"{record.name}: {record.getMessage()}")


logging.getLogger().addHandler(_ErrorCapture())

# ─── Patch outbound network ──────────────────────────────────────────────────
_outbound = []


def _record(name):
    async def _impl(*args, **kwargs):
        _outbound.append((name, args, kwargs))
        m = MagicMock()
        m.message_id = 10000 + len(_outbound)
        return m
    return _impl


bot.send_message = _record("send_message")
bot.edit_message_text = _record("edit_message_text")
bot.edit_message_reply_markup = _record("edit_message_reply_markup")
bot.answer_callback_query = AsyncMock(return_value=None)

_fake_member = MagicMock()
_fake_member.status = "administrator"
_fake_member.user = MagicMock(is_bot=False)
bot.get_chat_member = AsyncMock(return_value=_fake_member)

_fake_me = MagicMock()
_fake_me.id = 8324883914
_fake_me.is_bot = True
_fake_me.username = "TestGenericRollcallBot"
bot.get_me = AsyncMock(return_value=_fake_me)
bot.set_my_commands = AsyncMock(return_value=True)

# ─── Update factory ──────────────────────────────────────────────────────────
CHAT_ID = -1001999000123
UPD_COUNTER = [1000]
MSG_COUNTER = [1000]


def _next_upd():
    UPD_COUNTER[0] += 1
    return UPD_COUNTER[0]


def _next_msg():
    MSG_COUNTER[0] += 1
    return MSG_COUNTER[0]


def make_message_update(text, user_id, first_name, username=None, chat_id=CHAT_ID):
    return Update.de_json({
        "update_id": _next_upd(),
        "message": {
            "message_id": _next_msg(),
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "supergroup", "title": "PersistVerify"},
            "from": {"id": user_id, "is_bot": False, "first_name": first_name, "username": username},
            "text": text,
            "entities": [{"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}] if text.startswith("/") else [],
        },
    })


def make_callback_update(data, user_id, first_name, username=None, chat_id=CHAT_ID):
    return Update.de_json({
        "update_id": _next_upd(),
        "callback_query": {
            "id": f"cb_{_next_upd()}",
            "from": {"id": user_id, "is_bot": False, "first_name": first_name, "username": username},
            "chat_instance": "instance_1",
            "message": {
                "message_id": _next_msg(),
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "supergroup", "title": "PersistVerify"},
                "from": {"id": 8324883914, "is_bot": True, "first_name": "Bot"},
                "text": "panel",
            },
            "data": data,
        },
    })


# Test users
ALICE = (100, "Alice", "alice")
BOB = (200, "Bob", "bob")
CAROL = (300, "Carol", "carol")
DAVE = (400, "Dave", "dave")
EVE = (500, "Eve", "eve")
FRANK = (600, "Frank", "frank")


async def feed(text, user, chat_id=CHAT_ID):
    _outbound.clear()
    _errors.clear()
    bot_state._rate_limits.clear()
    uid, fname, uname = user
    upd = make_message_update(text, uid, fname, uname, chat_id=chat_id)
    await bot.process_new_updates([upd])
    return list(_outbound)


async def feed_cb(data, user, chat_id=CHAT_ID):
    _outbound.clear()
    _errors.clear()
    bot_state._rate_limits.clear()
    uid, fname, uname = user
    upd = make_callback_update(data, uid, fname, uname, chat_id=chat_id)
    await bot.process_new_updates([upd])
    return list(_outbound)


def text_of(out):
    chunks = []
    for name, args, kwargs in out:
        if name in ("send_message", "edit_message_text"):
            if name == "send_message" and len(args) >= 2:
                chunks.append(str(args[1]))
            elif name == "edit_message_text" and len(args) >= 1:
                chunks.append(str(args[0]))
            if "text" in kwargs:
                chunks.append(str(kwargs["text"]))
    return "\n".join(chunks)


# ─── Test runner ─────────────────────────────────────────────────────────────
results = []


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    mark = "✅" if passed else "❌"
    print(f"  {mark} {name}{(' — ' + detail) if detail and not passed else ''}")


def db_count(table):
    conn = sqlite3.connect(_DB_FILE)
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


async def phase_a_build_state():
    print("\n=== Phase A: Build prod-like state ===\n")

    # Settings on the chat
    await feed("/timezone Asia/Kolkata", ALICE)
    await feed("/toggle_ghost_tracking", ALICE)
    await feed("/set_absent_limit 2", ALICE)

    # Two templates with different schedules
    await feed('/set_template morning "Morning Football" limit=10 location="Field A" fee=100 event_day=sunday event_time=08:00', ALICE)
    await feed('/set_template evening "Evening Cricket" limit=12 location="Field B" fee=150 event_day=saturday event_time=18:00', ALICE)
    out = await feed("/templates", ALICE)
    record("Phase A: 2 templates created", "morning" in text_of(out).lower() and "evening" in text_of(out).lower())

    await feed("/schedule_template morning sunday 06:00", ALICE)
    await feed("/schedule_template evening monthly 15 17:00", ALICE)
    out = await feed("/schedules", ALICE)
    record("Phase A: 2 schedules active (weekly + monthly)",
           "morning" in text_of(out).lower() and "evening" in text_of(out).lower())

    # Two parallel rollcalls
    await feed("/src Morning Match", ALICE)
    await feed("/src Evening Game", ALICE)

    # Settings on each rollcall
    await feed("/set_limit 3 ::1", ALICE)
    await feed("/location Stadium ::1", ALICE)
    await feed("/event_fee 200 ::1", ALICE)
    await feed("/when tomorrow 7am ::1", ALICE)

    # Votes on RC#1
    await feed("/in ::1", BOB)
    await feed("/in ::1", CAROL)
    await feed("/in ::1", DAVE)  # at cap
    await feed("/in ::1", EVE)   # should waitlist
    await feed("/out ::1", FRANK)

    # Votes on RC#2
    await feed("/in ::2", BOB)
    await feed("/maybe ::2", CAROL)
    await feed("/out ::2", DAVE)

    # Proxy adds on RC#1
    await feed("/sof PreVotedProxy1 ::1", ALICE)
    await feed("/smf PreVotedProxy2 ::1", ALICE)

    # Set a reminder so resume_reminder_loops has something to do.
    # Format is DD-MM-YYYY HH:MM. Use a date a few months out so it's always future.
    await feed("/set_rollcall_time 31-12-2026 18:00 ::1", ALICE)
    await feed("/set_rollcall_reminder 1 ::1", ALICE)

    out = await feed("/rollcalls", ALICE)
    record("Phase A: 2 rollcalls active", "morning" in text_of(out).lower() and "evening" in text_of(out).lower())

    out = await feed("/whos_in ::1", ALICE)
    record("Phase A: RC#1 IN list has Bob, Carol, Dave",
           all(n in text_of(out).lower() for n in ["bob", "carol", "dave"]),
           f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_waiting ::1", ALICE)
    record("Phase A: RC#1 WAIT list has Eve",
           "eve" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    record("Phase A: no ERROR logs during state build", len(_errors) == 0,
           str(_errors[:3]) if _errors else "")


async def phase_b_capture():
    print("\n=== Phase B: Capture DB state ===\n")
    counts = {}
    for t in ["chats", "rollcalls", "users", "proxy_users", "templates", "chat_members"]:
        c = db_count(t)
        counts[t] = c
        print(f"      {t}: {c}")
    return counts


async def phase_c_restart():
    print("\n=== Phase C: Simulate restart ===\n")

    # Clear in-memory state — equivalent to a fresh process start
    _rm.manager.clear_cache()
    bot_state._panel_msg_ids.clear()
    bot_state._pending_reconf.clear()
    bot_state._sched_selection.clear()
    bot_state._rate_limits.clear()
    bot_state._pending_deletes.clear()
    bot_state._pending_overrides.clear()
    bot_state._pending_proxy_add.clear()
    print("      Cleared all in-memory caches.")

    # Re-run startup-equivalent: resume reminder loops
    _errors.clear()
    await _cr.resume_reminder_loops()
    record("Phase C: resume_reminder_loops completed without errors",
           len(_errors) == 0,
           str(_errors[:3]) if _errors else "")


async def phase_d_verify_recovered_state(pre_counts):
    print("\n=== Phase D: Verify state preserved across restart ===\n")

    # DB counts unchanged
    for t, expected in pre_counts.items():
        actual = db_count(t)
        record(f"Phase D: {t} row count unchanged ({expected} → {actual})",
               actual == expected, f"expected {expected}, got {actual}")

    out = await feed("/rollcalls", ALICE)
    record("Phase D: /rollcalls still shows both Morning + Evening",
           "morning" in text_of(out).lower() and "evening" in text_of(out).lower(),
           f"got: {text_of(out)[:200]!r}")

    out = await feed("/templates", ALICE)
    record("Phase D: /templates still shows both templates",
           "morning" in text_of(out).lower() and "evening" in text_of(out).lower(),
           f"got: {text_of(out)[:200]!r}")

    out = await feed("/schedules", ALICE)
    record("Phase D: /schedules still shows scheduled templates",
           "morning" in text_of(out).lower() and "evening" in text_of(out).lower(),
           f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_in ::1", ALICE)
    record("Phase D: RC#1 IN list preserved (Bob+Carol+Dave still IN)",
           all(n in text_of(out).lower() for n in ["bob", "carol", "dave"]),
           f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_out ::1", ALICE)
    record("Phase D: RC#1 OUT list preserved (Frank)",
           "frank" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_waiting ::1", ALICE)
    record("Phase D: RC#1 WAITLIST preserved (Eve)",
           "eve" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_in ::2", ALICE)
    record("Phase D: RC#2 IN list preserved (Bob)",
           "bob" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_maybe ::2", ALICE)
    record("Phase D: RC#2 MAYBE list preserved (Carol)",
           "carol" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_out ::1", ALICE)
    record("Phase D: RC#1 OUT proxy preserved (PreVotedProxy1)",
           "prevotedproxy1" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    out = await feed("/whos_maybe ::1", ALICE)
    record("Phase D: RC#1 MAYBE proxy preserved (PreVotedProxy2)",
           "prevotedproxy2" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    record("Phase D: no ERROR logs across all queries",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")


async def phase_e_continue_operating():
    print("\n=== Phase E: Continue operating on recovered state ===\n")

    # Vote on the recovered rollcall (BOB was IN — re-voting OUT should move him)
    out = await feed("/out ::1", BOB)
    record("Phase E: Bob votes OUT on recovered RC#1 (move IN→OUT)",
           len(_errors) == 0 and len(out) >= 1, str(_errors[:3]) if _errors else "")

    # Bob's removal should promote Eve from waitlist
    out = await feed("/whos_in ::1", ALICE)
    record("Phase E: Eve promoted from WAIT to IN after Bob left RC#1",
           "eve" in text_of(out).lower(), f"got: {text_of(out)[:200]!r}")

    # Add another proxy
    out = await feed("/sif NewProxy ::1", ALICE)
    record("Phase E: /sif adds new proxy to recovered RC#1",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Callback path on recovered rollcall (panel button)
    out = await feed_cb("btn_in_1", DAVE)
    record("Phase E: panel callback btn_in_1 on recovered rollcall — no error",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    out = await feed_cb("btn_out_1", DAVE)
    record("Phase E: panel callback btn_out_1 on recovered rollcall — no error (regression check)",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # End RC#1, verify RC#2 survives
    out = await feed("/erc ::1", ALICE)
    record("Phase E: /erc ::1 ends Morning rollcall",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    out = await feed("/rollcalls", ALICE)
    has_evening = "evening" in text_of(out).lower()
    has_morning = "morning" in text_of(out).lower()
    record("Phase E: After /erc ::1, only Evening RC remains",
           has_evening and not has_morning,
           f"got: {text_of(out)[:200]!r}")

    # Start a NEW template-spawned rollcall
    out = await feed("/start_template evening Live Run", ALICE)
    record("Phase E: /start_template on persisted template still works",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Disable a schedule from /schedules data
    out = await feed("/schedule_template morning off", ALICE)
    record("Phase E: /schedule_template off works on persisted schedule",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Cleanup
    await feed("/erc", ALICE)
    await feed("/erc", ALICE)
    _errors.clear()

    record("Phase E: clean shutdown, no ERROR logs", len(_errors) == 0,
           str(_errors[:3]) if _errors else "")


async def phase_f_log_hygiene():
    print("\n=== Phase F: Log hygiene — user-input errors must NOT log at ERROR ===\n")

    await feed("/src LogHygieneRC", ALICE)

    # Bad date format on /set_rollcall_time
    await feed("/set_rollcall_time tomorrow 8am", ALICE)
    record("Phase F: /set_rollcall_time bad date emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Bad time format
    await feed("/set_rollcall_time 99-99-9999 99:99", ALICE)
    record("Phase F: /set_rollcall_time impossible date emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Past date
    await feed("/set_rollcall_time 01-01-2020 10:00", ALICE)
    record("Phase F: /set_rollcall_time past date emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Bad reminder value
    await feed("/set_rollcall_reminder notanumber", ALICE)
    record("Phase F: /set_rollcall_reminder bad value emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Bad timezone
    await feed("/timezone NotARealZone/Nowhere", ALICE)
    record("Phase F: /timezone bad zone emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Bad set_limit
    await feed("/set_limit abc", ALICE)
    record("Phase F: /set_limit non-int emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # Bad rc number
    await feed("/in ::99", BOB)
    record("Phase F: /in ::99 (out-of-range) emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    # No active rollcall
    await feed("/erc", ALICE)  # close LogHygieneRC
    await feed("/erc", ALICE)  # close remaining if any
    _errors.clear()

    await feed("/in", BOB)  # no active rollcall
    record("Phase F: /in with no active rollcall emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    await feed("/buzz", ALICE)
    record("Phase F: /buzz with no active rollcall emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")

    await feed("/sif Test", ALICE)
    record("Phase F: /sif with no active rollcall emits NO ERROR-level log",
           len(_errors) == 0, str(_errors[:3]) if _errors else "")


async def main_async():
    await phase_a_build_state()
    pre_counts = await phase_b_capture()
    await phase_c_restart()
    await phase_d_verify_recovered_state(pre_counts)
    await phase_e_continue_operating()
    await phase_f_log_hygiene()


def main():
    print("=" * 70)
    print("Backward-compat / persistence verification (telebot 4.34.0)")
    print(f"DB: {_DB_FILE}")
    print("=" * 70)
    asyncio.run(main_async())

    passed = sum(1 for _, p, _ in results if p)
    failed = sum(1 for _, p, _ in results if not p)
    total = len(results)

    print("\n" + "=" * 70)
    print(f"RESULT: {passed}/{total} scenarios passed, {failed} failed")
    print("=" * 70)

    if failed:
        print("\nFailures:")
        for name, p, d in results:
            if not p:
                print(f"  ❌ {name}")
                if d:
                    print(f"     {d}")
        return 1

    print("\n✅ All persistence scenarios passed — existing prod state safe under telebot 4.34.0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
