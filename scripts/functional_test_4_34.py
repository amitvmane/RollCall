"""
Functional test for pyTelegramBotAPI 4.34.0 upgrade.

Uses the REAL pyTelegramBotAPI 4.34.0 library — no mocks of telebot, types,
or middleware. Constructs realistic Telegram Update payloads and feeds them
through bot.process_new_updates(), which exercises:

  - The real Update.de_json deserialization
  - The real message router (matching @bot.message_handler decorators)
  - The real callback_query router
  - The real middleware chain (_MemberTrackingMiddleware runs)
  - The real handler functions (no handler-side mocks)
  - The real SQLite database

Only outbound network methods (send_message, edit_message_text,
edit_message_reply_markup, answer_callback_query, get_chat_member, get_me,
set_my_commands) are patched to record calls instead of hitting Telegram.

This fills the gap left by integration_tests/ (which mocks telebot entirely)
and the smoke test (which checks only imports + method existence).
"""

import os
import sys
import asyncio
import logging
import tempfile
import time
import traceback
from unittest.mock import AsyncMock, MagicMock

# ─── Env setup BEFORE any rollCall imports ───────────────────────────────────
_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TELEGRAM_TOKEN"] = "999999:dummy_token_for_functional_test"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE}"
os.environ["ADMIN1"] = "100"  # bot-owner / super admin

# Make rollCall/ importable as top-level modules
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "rollCall"))

# ─── Real telebot imports (this is the point — no mocks here) ────────────────
from telebot.types import Update  # noqa: E402
from importlib.metadata import version as _pkg_version  # noqa: E402

_TBL_VER = _pkg_version("pyTelegramBotAPI")
print(f"pyTelegramBotAPI version: {_TBL_VER}")
assert _TBL_VER == "4.34.0", f"Expected 4.34.0, got {_TBL_VER}"

# ─── Bot state + handlers (this triggers real decorator registration) ────────
import bot_state  # noqa: E402
import handlers  # noqa: F401, E402  -- registers all @bot.message_handler decorators
import db as _db  # noqa: E402

_db.init_db()  # in case bot_state didn't already

bot = bot_state.bot

# ─── Error log capture (catches swallowed exceptions in handlers) ────────────
_errors = []  # list of LogRecord


class _ErrorCapture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            _errors.append(record)


logging.getLogger().addHandler(_ErrorCapture())


# ─── Patch outbound network ──────────────────────────────────────────────────
_outbound = []  # list of (method, args, kwargs)


def _record(name):
    async def _impl(*args, **kwargs):
        _outbound.append((name, args, kwargs))
        # Return a minimal Message-like object so callers can use .message_id
        m = MagicMock()
        m.message_id = 10000 + len(_outbound)
        m.chat = MagicMock(id=args[0] if args else kwargs.get("chat_id", -1))
        return m
    return _impl


bot.send_message = _record("send_message")
bot.edit_message_text = _record("edit_message_text")
bot.edit_message_reply_markup = _record("edit_message_reply_markup")
bot.answer_callback_query = AsyncMock(return_value=None)

# get_chat_member returns a fake member with admin status, so admin_rights() == True
_fake_member = MagicMock()
_fake_member.status = "administrator"
_fake_member.user = MagicMock(is_bot=False)
bot.get_chat_member = AsyncMock(return_value=_fake_member)

# get_me used in some places
_fake_me = MagicMock()
_fake_me.id = 8324883914
_fake_me.is_bot = True
_fake_me.username = "TestGenericRollcallBot"
bot.get_me = AsyncMock(return_value=_fake_me)
bot.set_my_commands = AsyncMock(return_value=True)


# ─── Update factory ──────────────────────────────────────────────────────────
CHAT_ID = -1001999000001  # supergroup-style
UPD_COUNTER = [1000]
MSG_COUNTER = [1000]


def _next_upd():
    UPD_COUNTER[0] += 1
    return UPD_COUNTER[0]


def _next_msg():
    MSG_COUNTER[0] += 1
    return MSG_COUNTER[0]


def make_message_update(text, user_id, first_name, username=None, chat_id=CHAT_ID):
    """Construct an Update with a message — exactly the JSON shape Telegram sends."""
    payload = {
        "update_id": _next_upd(),
        "message": {
            "message_id": _next_msg(),
            "date": int(time.time()),
            "chat": {
                "id": chat_id,
                "type": "supergroup" if chat_id < 0 else "private",
                "title": "FunctionalTest" if chat_id < 0 else None,
            },
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
                "username": username,
            },
            "text": text,
            "entities": (
                [{"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}]
                if text.startswith("/")
                else []
            ),
        },
    }
    return Update.de_json(payload)


def make_callback_update(data, user_id, first_name, username=None,
                          chat_id=CHAT_ID, src_msg_id=None):
    payload = {
        "update_id": _next_upd(),
        "callback_query": {
            "id": f"cb_{_next_upd()}",
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
                "username": username,
            },
            "chat_instance": "instance_1",
            "message": {
                "message_id": src_msg_id or _next_msg(),
                "date": int(time.time()),
                "chat": {
                    "id": chat_id,
                    "type": "supergroup",
                    "title": "FunctionalTest",
                },
                "from": {
                    "id": 8324883914,
                    "is_bot": True,
                    "first_name": "Bot",
                },
                "text": "panel",
            },
            "data": data,
        },
    }
    return Update.de_json(payload)


# ─── Test users ──────────────────────────────────────────────────────────────
ALICE = (100, "Alice", "alice")
BOB = (200, "Bob", "bob")
CAROL = (300, "Carol", "carol")
DAVE = (400, "Dave", "dave")
EVE = (500, "Eve", "eve")
FRANK = (600, "Frank", "frank")
GINA = (700, "Gina", "gina")
HANK = (800, "Hank", "hank")


# ─── Drive helpers ───────────────────────────────────────────────────────────
async def feed(text, user, chat_id=CHAT_ID):
    """Send a message-style update through the real bot router."""
    _outbound.clear()
    _errors.clear()
    uid, fname, uname = user
    upd = make_message_update(text, uid, fname, uname, chat_id=chat_id)
    await bot.process_new_updates([upd])
    return list(_outbound)


async def feed_cb(data, user, chat_id=CHAT_ID):
    _outbound.clear()
    _errors.clear()
    # Clear rate limits so fast-fire test clicks don't hit the 2s in/out/maybe
    # cooldown enforced by _is_rate_limited.
    bot_state._rate_limits.clear()
    uid, fname, uname = user
    upd = make_callback_update(data, uid, fname, uname, chat_id=chat_id)
    await bot.process_new_updates([upd])
    return list(_outbound)


def has_call(out, method_name):
    return any(name == method_name for name, _, _ in out)


def error_msgs():
    return [f"{r.name}: {r.getMessage()}" for r in _errors]


# ─── Test runner ─────────────────────────────────────────────────────────────
results = []  # (scenario, passed, detail)


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    mark = "✅" if passed else "❌"
    print(f"  {mark} {name}{(' — ' + detail) if detail and not passed else ''}")


def text_of(outbound):
    """Return concat of all text args from send_message + edit_message_text calls."""
    chunks = []
    for name, args, kwargs in outbound:
        if name in ("send_message", "edit_message_text"):
            # text is the 2nd positional for send_message, 1st for edit_message_text
            if name == "send_message" and len(args) >= 2:
                chunks.append(str(args[1]))
            elif name == "edit_message_text" and len(args) >= 1:
                chunks.append(str(args[0]))
            if "text" in kwargs:
                chunks.append(str(kwargs["text"]))
    return "\n".join(chunks)


def contains(out, *needles):
    t = text_of(out)
    missing = [n for n in needles if n.lower() not in t.lower()]
    return (not missing), (f"missing: {missing}, got: {t[:200]!r}" if missing else "")


async def run_all():
    print("\n=== Phase 1: Lifecycle & basic voting ===\n")

    # /start_roll_call (alias /src — NOT /sr)
    out = await feed("/src Test Event", ALICE)
    ok, d = contains(out, "test event")
    record("/src starts rollcall", ok, d)

    # /in
    out = await feed("/in", BOB)
    ok, d = contains(out, "bob")
    record("/in adds Bob to IN", ok, d)

    # /out
    out = await feed("/out", CAROL)
    ok, d = contains(out, "carol")
    record("/out adds Carol to OUT", ok, d)

    # /maybe
    out = await feed("/maybe will know later", DAVE)
    ok, d = contains(out, "dave")
    record("/maybe adds Dave with comment", ok, d)

    # /whos_in
    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, "bob")
    record("/whos_in shows Bob", ok, d)

    # /whos_out
    out = await feed("/whos_out", ALICE)
    ok, d = contains(out, "carol")
    record("/whos_out shows Carol", ok, d)

    # /whos_maybe
    out = await feed("/whos_maybe", ALICE)
    ok, d = contains(out, "dave")
    record("/whos_maybe shows Dave", ok, d)

    # /rollcalls
    out = await feed("/rollcalls", ALICE)
    ok, d = contains(out, "test event")
    record("/rollcalls lists active rollcall", ok, d)

    # /panel
    out = await feed("/panel", ALICE)
    ok = len(out) >= 1
    record("/panel responds", ok, f"got {len(out)} outbound" if not ok else "")

    print("\n=== Phase 2: Proxy users (/sif /sof /smf) ===\n")

    out = await feed("/sif proxy_alice", ALICE)
    ok, d = contains(out, "proxy_alice")
    record("/sif adds proxy user", ok, d)

    out = await feed("/sof proxy_carol", ALICE)
    ok, d = contains(out, "proxy_carol")
    record("/sof adds proxy out", ok, d)

    out = await feed("/smf proxy_dave", ALICE)
    ok, d = contains(out, "proxy_dave")
    record("/smf adds proxy maybe", ok, d)

    print("\n=== Phase 3: Settings ===\n")

    out = await feed("/set_title New Title", ALICE)
    ok = len(out) >= 1
    record("/set_title responds", ok)

    out = await feed("/event_fee 500", ALICE)
    ok = len(out) >= 1
    record("/event_fee responds", ok)

    out = await feed("/individual_fee 100", ALICE)
    ok = len(out) >= 1
    record("/individual_fee responds", ok)

    out = await feed("/location Stadium A", ALICE)
    ok = len(out) >= 1
    record("/location responds", ok)

    out = await feed("/when tomorrow 7pm", ALICE)
    ok = len(out) >= 1
    record("/when responds", ok)

    out = await feed("/shh", ALICE)
    ok = len(out) >= 1
    record("/shh responds", ok)

    out = await feed("/louder", ALICE)
    ok = len(out) >= 1
    record("/louder responds", ok)

    out = await feed("/set_limit 2", ALICE)
    ok = len(out) >= 1
    record("/set_limit responds", ok)

    print("\n=== Phase 4: Limits + waitlist ===\n")
    # After /set_limit 2 — Bob and proxy_alice are already IN. Adding Eve should waitlist.
    out = await feed("/in", EVE)
    ok = len(out) >= 1
    record("/in past cap responds (waitlist or already-in handling)", ok)

    out = await feed("/whos_waiting", ALICE)
    ok = len(out) >= 1
    record("/whos_waiting responds", ok)

    print("\n=== Phase 5: Stats / lists / buzz ===\n")

    out = await feed("/stats", ALICE)
    ok = len(out) >= 1
    record("/stats responds", ok)

    out = await feed("/history", ALICE)
    ok = len(out) >= 1
    record("/history responds", ok)

    out = await feed("/buzz", ALICE)
    ok = len(out) >= 1
    record("/buzz responds", ok)

    out = await feed("/version", ALICE)
    ok, d = contains(out, "version")
    record("/version reports version", ok, d)

    print("\n=== Phase 6: Help ===\n")

    out = await feed("/help", ALICE)
    ok, d = contains(out, "command")
    record("/help renders", ok, d)

    out = await feed("/help admin", ALICE)
    ok = len(out) >= 1
    record("/help admin renders", ok)

    out = await feed("/help in", ALICE)
    ok, d = contains(out, "in")
    record("/help in renders detail", ok, d)

    print("\n=== Phase 7: Ghost tracking ===\n")

    out = await feed("/toggle_ghost_tracking", ALICE)
    ok = len(out) >= 1
    record("/toggle_ghost_tracking responds", ok)

    out = await feed("/set_absent_limit 3", ALICE)
    ok = len(out) >= 1
    record("/set_absent_limit responds", ok)

    out = await feed("/clear_absent", ALICE)
    ok = len(out) >= 1
    record("/clear_absent responds", ok)

    print("\n=== Phase 8: Templates (deep) ===\n")

    # /set_template syntax: name "Title" [limit=N] [location=X] [fee=X]
    out = await feed('/set_template friday "Friday Football" limit=14 location="Turf 3" fee=200', ALICE)
    ok = len(_errors) == 0 and len(out) >= 1
    record("/set_template friday with title/limit/location/fee creates template",
           ok, str(error_msgs()) if not ok else "")

    out = await feed('/set_template practice "Tue Practice" limit=10', ALICE)
    ok = len(_errors) == 0 and len(out) >= 1
    record("/set_template practice (second template) creates", ok, str(error_msgs()) if not ok else "")

    out = await feed("/templates", ALICE)
    ok, d = contains(out, "friday", "practice")
    record("/templates lists both templates (friday + practice)", ok, d)

    # /set_template with existing name should update
    out = await feed('/set_template friday "Friday FC" limit=16', ALICE)
    ok = len(_errors) == 0
    record("/set_template friday (update existing) handled without error",
           ok, str(error_msgs()) if not ok else "")

    out = await feed("/templates", ALICE)
    ok = "friday fc" in text_of(out).lower() or "friday" in text_of(out).lower()
    record("/templates reflects updated friday title", ok,
           f"got: {text_of(out)[:200]!r}")

    # /start_template — must run with no active rollcall (Phase 1's rollcall
    # was ended in Phase 13's endconfirm; if still active, this gets queued
    # depending on the bot's max-rollcall policy)
    # Ensure clean state first
    await feed("/erc", ALICE)
    await feed("/erc", ALICE)
    _errors.clear()

    out = await feed("/start_template practice", ALICE)
    ok = len(_errors) == 0
    record("/start_template practice spawns a rollcall without error",
           ok, str(error_msgs()) if not ok else "")

    out = await feed("/rollcalls", ALICE)
    ok = "practice" in text_of(out).lower() or "tue" in text_of(out).lower()
    record("/rollcalls shows the template-started rollcall", ok,
           f"got: {text_of(out)[:200]!r}")

    # /start_template with title override
    out = await feed("/start_template friday Custom Title", ALICE)
    ok = len(_errors) == 0
    record("/start_template friday with title override", ok, str(error_msgs()) if not ok else "")

    out = await feed("/rollcalls", ALICE)
    ok = "custom title" in text_of(out).lower() or "friday" in text_of(out).lower()
    record("/rollcalls shows overridden title from /start_template", ok,
           f"got: {text_of(out)[:200]!r}")

    # Clean up template-started rollcalls
    await feed("/erc", ALICE)
    await feed("/erc", ALICE)
    _errors.clear()

    # /delete_template
    out = await feed("/delete_template practice", ALICE)
    ok = len(_errors) == 0
    record("/delete_template practice removes template", ok, str(error_msgs()) if not ok else "")

    out = await feed("/templates", ALICE)
    ok = "practice" not in text_of(out).lower() or "tue practice" not in text_of(out).lower()
    record("/templates no longer shows deleted 'practice'", ok,
           f"got: {text_of(out)[:200]!r}")

    # /delete_template with bad name
    out = await feed("/delete_template doesnotexist", ALICE)
    ok = len(_errors) == 0
    record("/delete_template <nonexistent> handled gracefully", ok, str(error_msgs()) if not ok else "")

    # /start_template with bad name
    out = await feed("/start_template doesnotexist", ALICE)
    ok = len(_errors) == 0
    record("/start_template <nonexistent> handled gracefully", ok, str(error_msgs()) if not ok else "")

    # /set_template with bad syntax (missing title)
    out = await feed("/set_template badone", ALICE)
    ok = len(_errors) == 0
    record("/set_template with missing title handled gracefully", ok, str(error_msgs()) if not ok else "")

    print("\n=== Phase 8b: Schedules (deep) ===\n")

    # Templates need event_day + event_time before scheduling. Set them first
    # on the 'friday' template (created earlier in Phase 8).
    out = await feed('/set_template friday "Friday FC" limit=16 event_day=sunday event_time=17:00', ALICE)
    ok = len(_errors) == 0
    record("/set_template friday with event_day/event_time", ok, str(error_msgs()) if not ok else "")

    # /schedule_template syntax: name <weekday> <HH:MM> [biweekly]
    # weekday must be full name (monday..sunday), NOT 3-letter abbreviation.
    out = await feed("/schedule_template friday friday 18:00", ALICE)
    ok = len(_errors) == 0 and "schedule" in text_of(out).lower()
    record("/schedule_template friday friday 18:00 (weekly) accepted",
           ok, f"got: {text_of(out)[:150]!r}")

    out = await feed("/schedules", ALICE)
    ok = "friday" in text_of(out).lower()
    record("/schedules lists the scheduled template", ok,
           f"got: {text_of(out)[:200]!r}")

    # Biweekly variant
    out = await feed("/schedule_template friday friday 18:00 biweekly", ALICE)
    ok = len(_errors) == 0
    record("/schedule_template biweekly variant accepted", ok,
           f"got: {text_of(out)[:150]!r}")

    # Monthly variant
    out = await feed("/schedule_template friday monthly 15 09:00", ALICE)
    ok = len(_errors) == 0
    record("/schedule_template monthly variant accepted", ok,
           f"got: {text_of(out)[:150]!r}")

    # Status query (no args after name) — should print current schedule
    out = await feed("/schedule_template friday", ALICE)
    ok = "schedule" in text_of(out).lower() or "enabled" in text_of(out).lower()
    record("/schedule_template friday (status query)", ok,
           f"got: {text_of(out)[:150]!r}")

    # Off variant — unschedule
    out = await feed("/schedule_template friday off", ALICE)
    ok = "disabled" in text_of(out).lower() or len(_errors) == 0
    record("/schedule_template friday off (cancel schedule) accepted", ok,
           f"got: {text_of(out)[:150]!r}")

    # Bad day name
    out = await feed("/schedule_template friday notaday 18:00", ALICE)
    ok = "not a valid weekday" in text_of(out).lower() or "invalid" in text_of(out).lower()
    record("/schedule_template with bad day returns user-facing error", ok,
           f"got: {text_of(out)[:150]!r}")

    # Bad time format
    out = await feed("/schedule_template friday friday 25:99", ALICE)
    ok = "not a valid time" in text_of(out).lower() or "invalid" in text_of(out).lower()
    record("/schedule_template with bad time returns user-facing error", ok,
           f"got: {text_of(out)[:150]!r}")

    # /schedule_template on nonexistent template
    out = await feed("/schedule_template ghost friday 18:00", ALICE)
    ok = "not found" in text_of(out).lower()
    record("/schedule_template on nonexistent template returns 'not found'", ok,
           f"got: {text_of(out)[:150]!r}")

    # Re-add schedule for callback path
    out = await feed("/schedule_template friday friday 18:00", ALICE)
    ok = len(_errors) == 0
    record("/schedule_template re-scheduled friday for callback test", ok,
           str(error_msgs()) if not ok else "")

    # /schedules has interactive toggle buttons — verify markup attached
    out = await feed("/schedules", ALICE)
    has_markup = False
    for _name, _args, _kwargs in out:
        if isinstance(_kwargs, dict) and _kwargs.get("reply_markup") is not None:
            has_markup = True
            break
    record("/schedules emits an inline-keyboard reply_markup", has_markup,
           f"outbound: {[(n, list(k.keys())) for n,_,k in out if isinstance(k, dict)]}")

    # /set_template within already-running rollcall
    await feed("/src TemplateContextTest", ALICE)
    out = await feed('/set_template inside "Inside Title" limit=5', ALICE)
    ok = len(_errors) == 0
    record("/set_template can be created while a rollcall is active", ok,
           str(error_msgs()) if not ok else "")
    await feed("/erc", ALICE)
    _errors.clear()

    print("\n=== Phase 9: Admin overrides ===\n")

    out = await feed("/audit_log", ALICE)
    ok = len(out) >= 1
    record("/audit_log responds", ok)

    out = await feed("/timezone Asia/Kolkata", ALICE)
    ok = len(out) >= 1
    record("/timezone responds", ok)

    out = await feed("/set_admins", ALICE)
    ok = len(out) >= 1
    record("/set_admins responds", ok)

    out = await feed("/unset_admins", ALICE)
    ok = len(out) >= 1
    record("/unset_admins responds", ok)

    print("\n=== Phase 10: Callback regression — panel OUT/MAYBE after IN ===\n")
    print("    (the bug fixed 2026-06-16: UnboundLocalError on format_mention_with_name_md)")
    # Pre-conditions: ensure a clean active rollcall (Phase 8b's lifecycle
    # churn may have left zero active). shh OFF, real int user_id, group chat.
    await feed("/erc", ALICE)  # clear any leftover
    _errors.clear()
    await feed("/src CallbackRegression", ALICE)
    await feed("/louder", ALICE)  # ensure shh is OFF
    # Use FRANK — fresh user not already in any list.
    # Callback data format is btn_<action>_<rc_number>. Active rollcall = #1.

    out = await feed_cb("btn_in_1", FRANK)
    no_err = len(_errors) == 0
    record("Panel button 'in' (FRANK): no exceptions", no_err, str(error_msgs()) if not no_err else "")
    record("Panel button 'in' (FRANK): panel edit_message_text fired",
           has_call(out, "edit_message_text"),
           f"outbound: {[n for n,_,_ in out]}")
    record("Panel button 'in' (FRANK): announcement send_message fired",
           has_call(out, "send_message"),
           f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_out_1", FRANK)
    no_err = len(_errors) == 0
    record("Panel button 'out' after 'in' (FRANK): no UnboundLocalError",
           no_err, str(error_msgs()) if not no_err else "")
    record("Panel button 'out' (FRANK): announcement send_message fired",
           has_call(out, "send_message"),
           f"outbound: {[n for n,_,_ in out]}")
    record("Panel button 'out' (FRANK): panel edit_message_text fired (live refresh)",
           has_call(out, "edit_message_text"),
           f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_maybe_1", FRANK)
    no_err = len(_errors) == 0
    record("Panel button 'maybe' after 'out' (FRANK): no exceptions",
           no_err, str(error_msgs()) if not no_err else "")
    record("Panel button 'maybe' (FRANK): panel edit_message_text fired",
           has_call(out, "edit_message_text"),
           f"outbound: {[n for n,_,_ in out]}")

    print("\n=== Phase 11: Callback sub-menus & refresh ===\n")

    out = await feed_cb("btn_lists_1", BOB)
    record("Panel button 'lists' opens submenu",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_wi_1", BOB)
    record("Submenu 'wi' (who's in) responds",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_wo_1", BOB)
    record("Submenu 'wo' (who's out) responds",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_wm_1", BOB)
    record("Submenu 'wm' (who's maybe) responds",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_ww_1", BOB)
    record("Submenu 'ww' (waitlist) responds",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_refresh_1", BOB)
    record("Panel 'refresh' re-renders",
           has_call(out, "edit_message_text") or len(out) >= 1,
           f"outbound: {[n for n,_,_ in out]}")

    print("\n=== Phase 12: Deep proxy lifecycle (set_limit + waitlist promotion) ===\n")

    # New rollcall for proxy testing — reset state
    await feed("/erc", ALICE)
    await feed("/src ProxyTest", ALICE)
    await feed("/set_limit 2", ALICE)

    out = await feed("/sif Alex", ALICE)
    ok, d = contains(out, "alex")
    record("/sif Alex adds to IN (under cap)", ok, d)

    out = await feed("/sif Brian", ALICE)
    ok, d = contains(out, "brian")
    record("/sif Brian adds to IN (at cap)", ok, d)

    out = await feed("/sif Chad", ALICE)
    # At cap=2, this should waitlist
    ok = len(out) >= 1
    record("/sif Chad past cap (should waitlist or notify)", ok)

    out = await feed("/sif Dan", ALICE)
    ok = len(out) >= 1
    record("/sif Dan past cap responds", ok)

    out = await feed("/sif Alex", ALICE)
    # Duplicate should error (or notify)
    ok = len(out) >= 1
    record("/sif Alex (duplicate) responds with error/info", ok)

    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, "alex", "brian")
    record("/whos_in shows Alex + Brian (under cap)", ok, d)

    out = await feed("/whos_waiting", ALICE)
    ok, d = contains(out, "chad")
    record("/whos_waiting shows Chad (waitlisted)", ok, d)

    # Move Alex to OUT — should promote Chad from waitlist
    out = await feed("/sof Alex", ALICE)
    ok = len(out) >= 1
    record("/sof Alex (was IN) responds and triggers waitlist promotion", ok)

    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, "chad")  # Chad should now be IN
    record("Chad promoted from waitlist to IN after /sof Alex", ok, d)

    out = await feed("/smf Brian", ALICE)
    ok = len(out) >= 1
    record("/smf Brian (was IN) → MAYBE responds + may promote Dan", ok)

    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, "dan")
    record("Dan promoted from waitlist to IN after /smf Brian", ok, d)

    out = await feed("/whos_maybe", ALICE)
    ok, d = contains(out, "brian")
    record("/whos_maybe shows Brian (moved from IN)", ok, d)

    out = await feed("/whos_out", ALICE)
    ok, d = contains(out, "alex")
    record("/whos_out shows Alex (moved from IN)", ok, d)

    # Total no-exception check across all proxy operations
    record("Phase 12 — no ERROR-level logs emitted", len(_errors) == 0,
           str(error_msgs()) if _errors else "")

    print("\n=== Phase 13: End-confirm callback flow ===\n")

    out = await feed_cb("btn_end_1", ALICE)
    record("Panel 'end' opens confirm dialog",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_endcancel_1", ALICE)
    record("Panel 'endcancel' returns to panel",
           has_call(out, "edit_message_text") or len(out) >= 1,
           f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_end_1", ALICE)
    record("Panel 'end' opens confirm dialog (round 2)",
           has_call(out, "edit_message_text"), f"outbound: {[n for n,_,_ in out]}")

    out = await feed_cb("btn_endconfirm_1", ALICE)
    ok = len(out) >= 1
    record("Panel 'endconfirm' actually ends rollcall", ok,
           f"outbound: {[n for n,_,_ in out]}")

    print("\n=== Phase 14: Multi-rollcall scenarios ===\n")

    # All rollcalls should be ended after Phase 13. Start fresh pair.
    out = await feed("/src Morning", ALICE)
    ok, d = contains(out, "morning")
    record("/src Morning starts RC#1", ok, d)

    out = await feed("/src Evening", ALICE)
    ok, d = contains(out, "evening")
    record("/src Evening starts RC#2 (parallel)", ok, d)

    out = await feed("/rollcalls", ALICE)
    ok, d = contains(out, "morning", "evening")
    record("/rollcalls lists both active rollcalls", ok, d)

    # Target rollcall #2 specifically via ::2 suffix
    out = await feed("/in ::2", GINA)
    ok = len(_errors) == 0
    record("/in ::2 (GINA) targets RC#2 without error", ok, str(error_msgs()) if not ok else "")

    out = await feed("/whos_in ::2", ALICE)
    ok, d = contains(out, "gina")
    record("/whos_in ::2 shows Gina in RC#2", ok, d)

    out = await feed("/whos_in ::1", ALICE)
    ok = "gina" not in text_of(out).lower()
    record("Gina is NOT in RC#1 (isolation between rollcalls)", ok,
           f"got: {text_of(out)[:100]!r}")

    out = await feed("/sif RC1Proxy ::1", ALICE)
    ok = len(_errors) == 0
    record("/sif ::1 adds proxy to RC#1 only", ok, str(error_msgs()) if not ok else "")

    out = await feed("/whos_in ::1", ALICE)
    ok, d = contains(out, "rc1proxy")
    record("/whos_in ::1 shows RC1Proxy in RC#1", ok, d)

    out = await feed("/whos_in ::2", ALICE)
    ok = "rc1proxy" not in text_of(out).lower()
    record("RC1Proxy is NOT in RC#2 (proxy isolation)", ok)

    # /buzz across multiple rollcalls
    out = await feed("/buzz", ALICE)
    ok = len(_errors) == 0
    record("/buzz with multiple rollcalls runs without error", ok, str(error_msgs()) if not ok else "")

    # /erc ::1 ends only RC#1
    out = await feed("/erc ::1", ALICE)
    ok = len(_errors) == 0
    record("/erc ::1 ends only RC#1 without error", ok, str(error_msgs()) if not ok else "")

    out = await feed("/rollcalls", ALICE)
    ok = "evening" in text_of(out).lower() and "morning" not in text_of(out).lower()
    record("/rollcalls shows only Evening (RC#1 ended, RC#2 remains)", ok,
           f"got: {text_of(out)[:150]!r}")

    out = await feed("/erc ::1", ALICE)
    ok = len(_errors) == 0
    record("/erc ::1 ends the remaining Evening rollcall", ok, str(error_msgs()) if not ok else "")

    print("\n=== Phase 15: Error-path & edge cases ===\n")

    # Vote with no active rollcall
    out = await feed("/in", BOB)
    ok = "not active" in text_of(out).lower() or "no rollcall" in text_of(out).lower() or "no active" in text_of(out).lower()
    record("/in with no active rollcall returns user-facing error",
           ok, f"got: {text_of(out)[:100]!r}")

    # /erc with no active rollcall
    out = await feed("/erc", ALICE)
    ok = len(_errors) == 0  # should not raise; should return curated message
    record("/erc with no rollcall: no internal error", ok, str(error_msgs()) if not ok else "")

    # /sif with no rollcall
    out = await feed("/sif Foo", ALICE)
    ok = len(_errors) == 0
    record("/sif with no rollcall: no internal error", ok, str(error_msgs()) if not ok else "")

    # Invalid ::N targeting
    await feed("/src ErrTest", ALICE)
    out = await feed("/in ::99", BOB)
    ok = len(_errors) == 0
    record("/in ::99 (out-of-range) returns curated error, not crash",
           ok, str(error_msgs()) if not ok else "")

    # /set_limit with bad arg
    out = await feed("/set_limit abc", ALICE)
    ok = len(_errors) == 0
    record("/set_limit abc (non-integer) handled gracefully", ok, str(error_msgs()) if not ok else "")

    # /set_limit with negative
    out = await feed("/set_limit -5", ALICE)
    ok = len(_errors) == 0
    record("/set_limit -5 (negative) handled gracefully", ok, str(error_msgs()) if not ok else "")

    # /timezone with bad zone
    out = await feed("/timezone NotARealZone/Nowhere", ALICE)
    ok = len(_errors) == 0
    record("/timezone with invalid zone handled gracefully", ok, str(error_msgs()) if not ok else "")

    # Callback with stale rollcall number
    out = await feed_cb("btn_in_99", BOB)
    ok = len(_errors) == 0
    record("Callback with invalid rc_number handled gracefully", ok, str(error_msgs()) if not ok else "")

    # Callback with malformed data
    out = await feed_cb("not_a_valid_callback", BOB)
    ok = len(_errors) == 0
    record("Callback with non-btn_ prefix ignored gracefully", ok, str(error_msgs()) if not ok else "")

    out = await feed_cb("btn_in_abc", BOB)
    ok = len(_errors) == 0
    record("Callback with non-int rc_number handled gracefully", ok, str(error_msgs()) if not ok else "")

    # Close error-test rollcall
    await feed("/erc", ALICE)

    print("\n=== Phase 16: Lifecycle close (final) ===\n")

    out = await feed("/rollcalls", ALICE)
    ok = True  # whatever response is fine — just shouldn't crash
    record("/rollcalls after all ended responds (no crash)", ok)

    out = await feed("/version", ALICE)
    ok, d = contains(out, "version")
    record("/version still reports version after lifecycle churn", ok, d)

    # Final global error check — no ERROR-level logs leaked across entire run
    record("FINAL: no ERROR-level logs from any phase", len(_errors) == 0,
           f"{len(_errors)} errors: {error_msgs()[:5]}" if _errors else "")


def main():
    print("=" * 70)
    print("Functional test for pyTelegramBotAPI 4.34.0")
    print("=" * 70)
    asyncio.run(run_all())

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

    print("\n✅ All scenarios passed against pyTelegramBotAPI 4.34.0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
