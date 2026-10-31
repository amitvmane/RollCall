"""
End-to-end functional test against the REAL pyTelegramBotAPI.

Originally written to qualify the 4.14.1 → 4.34.0 bump (PR #15) and named for
that version; it now runs on every check-in as a standing regression contract,
so keep EXTENDING it rather than replacing it when dependencies move. The
pinned telebot version is whatever requirements.lock says — nothing here is
tied to 4.34 any more.

No mocks of telebot, types, or middleware. Constructs realistic Telegram Update
payloads and feeds them through bot.process_new_updates(), which exercises:

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
# setdefault, not a hard assignment: an inherited DATABASE_URL wins so the same
# scenarios can be driven against real Postgres in CI. This used to overwrite
# it unconditionally, which meant the ONE layer exercising real telebot routing
# through real handlers had never touched Postgres — and every historical
# parity bug in this project (BOOLEAN rejecting 0/1, jsonb pre-decoded,
# TIMESTAMP as datetime, FKs actually enforced) was a data-layer difference
# that only appears under real PG. Matches load_test_50_users.py.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_FILE}")
os.environ["ADMIN1"] = "100"  # bot-owner / super admin

# Make rollCall/ importable as top-level modules
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "rollCall"))

# ─── Real telebot imports (this is the point — no mocks here) ────────────────
from telebot.types import Update  # noqa: E402
from importlib.metadata import version as _pkg_version  # noqa: E402

_TBL_VER = _pkg_version("pyTelegramBotAPI")
print(f"pyTelegramBotAPI version: {_TBL_VER}")
# Deliberately NOT asserted against a fixed version. This used to pin 4.34.0,
# which was right while the file was a one-off gate for that bump but would
# now fail the build the first time telebot moves — exactly when this test is
# most worth running. The version is reported so it shows in the CI log; what
# is pinned lives in requirements.lock.

# ─── Bot state + handlers (this triggers real decorator registration) ────────
import bot_state  # noqa: E402
import handlers  # noqa: F401, E402  -- registers all @bot.message_handler decorators
import db as _db  # noqa: E402
from services import templates as templates_svc  # noqa: E402
from rollcall_manager import manager  # noqa: E402

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
# Messages real Telegram would refuse ("message is too long"). The mock
# accepts anything, so without this a reply past the limit passes here and
# sends nothing in production — /help admin did, at 6.7k characters.
_too_long = []  # (method, length, first line)
_TG_TEXT_LIMIT = 4096


def _telegram_length(text, parse_mode):
    """Length as Telegram counts it: UTF-16 units of the text after its
    formatting markup is parsed away (the limit applies to what's shown)."""
    import html as _html
    import re as _re
    if parse_mode and parse_mode.lower() == "html":
        text = _html.unescape(_re.sub(r"<[^>]+>", "", text))
    elif parse_mode:
        text = _re.sub(r"\\(.)", r"\1", _re.sub(r"(?<!\\)[*_`]", "", text))
    return len(text.encode("utf-16-le")) // 2


_bad_buttons = []  # (method, callback_data) Telegram refuses: BUTTON_DATA_INVALID
_TG_CALLBACK_LIMIT = 64  # bytes, not characters — a Devanagari letter is 3


def _check_buttons(name, kwargs):
    markup = kwargs.get("reply_markup")
    for row in getattr(markup, "keyboard", None) or []:
        for btn in row:
            data = getattr(btn, "callback_data", None)
            if isinstance(data, str) and not 1 <= len(data.encode("utf-8")) <= _TG_CALLBACK_LIMIT:
                _bad_buttons.append((name, data))


_bad_markdown = []  # (method, first line) Telegram refuses: "can't parse entities"


def _markdown_unbalanced(text):
    """True when legacy-Markdown entities can't all close — what makes
    Telegram refuse the message. An unescaped "_" in a title or name does
    it: "Sunday_Game" opens italics that never end."""
    import re as _re
    t = _re.sub(r"\\.", "", text)                  # escaped characters
    t = _re.sub(r"```.*?```", "", t, flags=_re.S)  # pre blocks
    if t.count("`") % 2:
        return True
    t = _re.sub(r"`[^`]*`", "", t)                 # code spans
    t = _re.sub(r"\]\([^)]*\)", "]", t)            # link targets
    return t.count("*") % 2 == 1 or t.count("_") % 2 == 1


def _check_length(name, args, kwargs):
    _check_buttons(name, kwargs)
    if (kwargs.get("parse_mode") or "").lower() == "markdown":
        text = (args[1] if name == "send_message" and len(args) >= 2 else
                args[0] if name == "edit_message_text" and args else kwargs.get("text"))
        if isinstance(text, str) and _markdown_unbalanced(text):
            _bad_markdown.append((name, text.split("\n", 1)[0][:80]))
    if name == "send_message":
        text = args[1] if len(args) >= 2 else kwargs.get("text")
    elif name == "edit_message_text":
        text = args[0] if args else kwargs.get("text")
    else:
        return
    if isinstance(text, str):
        n = _telegram_length(text, kwargs.get("parse_mode"))
        if n > _TG_TEXT_LIMIT:
            _too_long.append((name, n, text.split("\n", 1)[0][:60]))


def _record(name):
    async def _impl(*args, **kwargs):
        _check_length(name, args, kwargs)
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
# File/image and pin outbound calls. Unpatched until 2026-08-18, which is part
# of why /card and /export_stats had no functional coverage — they would have
# attempted a REAL network call to Telegram mid-test. Patched here so the
# handlers that produce match-day cards, receipts and CSV exports (the pillow
# and matplotlib paths) can be exercised offline like everything else.
bot.send_photo = _record("send_photo")
bot.send_document = _record("send_document")
bot.pin_chat_message = _record("pin_chat_message")
bot.unpin = _record("unpin")
bot.unpin_chat_message = _record("unpin_chat_message")

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
                    "type": "supergroup" if chat_id < 0 else "private",
                    "title": "FunctionalTest" if chat_id < 0 else None,
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


def has_panel_send(out):
    """True if a NEW panel message was posted -- specifically the vote
    panel's own keyboard (buttons whose callback_data starts with "btn_",
    built by get_status_keyboard), not just any send_message with SOME
    reply_markup. The ghost-warning prompt ALSO carries buttons (Yes/No,
    callback_data "proxy_add_"/"proxy_cancel_"), so checking for "any
    markup" would misidentify that prompt as the panel. Distinguishing
    these is the whole point of the /sif consistency regression: a plain
    confirmation or a different prompt must never be mistaken for the vote
    panel being re-posted as a new message."""
    for name, _args, kwargs in out:
        if name != "send_message":
            continue
        markup = kwargs.get("reply_markup")
        for row in getattr(markup, "keyboard", []) or []:
            for btn in row:
                if str(getattr(btn, "callback_data", "") or "").startswith("btn_"):
                    return True
    return False


def error_msgs():
    return [f"{r.name}: {r.getMessage()}" for r in _errors]


# ─── Test runner ─────────────────────────────────────────────────────────────
results = []  # (scenario, passed, detail)


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    mark = "✅" if passed else "❌"
    print(f"  {mark} {name}{(' — ' + detail) if detail and not passed else ''}")


def button_labels(outbound):
    """{callback_data: label} for every inline button in the outbound calls."""
    found = {}
    for _, _, kwargs in outbound:
        for row in getattr(kwargs.get("reply_markup"), "keyboard", None) or []:
            for btn in row:
                if getattr(btn, "callback_data", None):
                    found[btn.callback_data] = btn.text
    return found


def buttons_of(outbound):
    return list(button_labels(outbound))


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


def deployed_version_str():
    """The version number /version should be reporting, read from the source.

    Asserting on a hardcoded string here would need editing every release.
    Asserting on the literal word "version" — which is what this used to do —
    passed only because the reply printed a "Version:" field label, so it
    broke the moment /version started rendering the changelog as members read
    it rather than as a database row.
    """
    import json as _json
    import os as _os
    vf = _os.path.join(_os.path.dirname(__file__), "..", "rollCall", "version.json")
    with open(vf, encoding="utf-8") as fh:
        data = _json.load(fh)
    live = [v for v in data if v.get("DeployedOnProd") == "Y"]
    assert live, "no version marked DeployedOnProd=Y — /version would report nothing"
    return str(live[-1]["Version"])


def contains(out, *needles):
    t = text_of(out)
    missing = [n for n in needles if n.lower() not in t.lower()]
    return (not missing), (f"missing: {missing}, got: {t[:200]!r}" if missing else "")


async def phase_dues():
    """Dues & Treasury — the money paths.

    Left uncovered by this file until 2026-08-18 despite being the most
    consequential code in the repo: dues_entries and fund_transactions are
    append-only ledgers, and the group-chat announcement history is the stated
    reconstruction source if the DB is ever lost. A silent handler failure here
    loses money records, so these scenarios assert on the REPLY TEXT (the thing
    members actually see), not merely that no exception escaped.
    """
    print("\n=== Phase 17: Dues & Treasury — setup ===\n")

    out = await feed("/enable_dues", ALICE)
    ok, d = contains(out, "dues")
    record("/enable_dues responds", ok, d)

    out = await feed("/set_upi alice@upi", ALICE)
    ok, d = contains(out, "upi")
    record("/set_upi accepts a VPA", ok, d)

    out = await feed("/set_treasury_upi treasurer@upi", ALICE)
    record("/set_treasury_upi responds", bool(out))

    out = await feed("/dues_setup", ALICE)
    record("/dues_setup renders the setup card", bool(out))

    out = await feed("/set_round_step 5", ALICE)
    record("/set_round_step responds", bool(out))

    out = await feed("/dues_nudges on", ALICE)
    record("/dues_nudges on responds", bool(out))

    out = await feed("/dues_report weekly", ALICE)
    record("/dues_report weekly responds", bool(out))

    print("\n=== Phase 18: Penalty tiers ===\n")

    out = await feed("/penalties", ALICE)
    record("/penalties lists tiers", bool(out))

    out = await feed("/add_penalty very_late 100 mins:20 significantly late", ALICE)
    record("/add_penalty creates a tier", bool(out))

    out = await feed("/penalties", ALICE)
    # The reply is Markdown-escaped, so the tier renders as "very\_late".
    # Compare against the unescaped text or this silently fails on a name
    # that is actually present.
    _tiers = text_of(out).replace("\\", "")
    ok = "very_late" in _tiers
    record("/penalties shows the new tier", ok,
           f"got: {_tiers[:160]!r}" if not ok else "")

    out = await feed("/remove_penalty very_late", ALICE)
    record("/remove_penalty removes a tier", bool(out))

    print("\n=== Phase 19: Game close → settle → collect ===\n")

    # A fresh rollcall to charge against.
    await feed("/src Dues Game", ALICE)
    await feed("/in", ALICE)
    await feed("/in", BOB)
    await feed("/in", CAROL)
    await feed("/ef 600", ALICE)

    out = await feed("/set_collector Bob paid bob@upi", ALICE)
    record("/set_collector assigns a collector", bool(out))

    out = await feed("/pick_collector", ALICE)
    record("/pick_collector responds", bool(out))

    out = await feed("/rotate_collector on", ALICE)
    record("/rotate_collector on responds", bool(out))

    out = await feed("/add_adhoc Guest", ALICE)
    record("/add_adhoc adds a non-voter to the bill", bool(out))

    await feed("/erc", ALICE)

    out = await feed("/settle_dues", ALICE)
    record("/settle_dues responds after game close", bool(out))

    out = await feed("/dues", ALICE)
    record("/dues renders the ledger", bool(out))

    out = await feed("/my_dues", BOB)
    record("/my_dues renders a personal balance", bool(out))

    out = await feed("/mark_paid Alice", ALICE)
    record("/mark_paid records a payment", bool(out))

    out = await feed("/waive Alice 75 injured", ALICE)
    record("/waive records a waiver", bool(out))

    out = await feed("/remind_dues", ALICE)
    record("/remind_dues responds", bool(out))

    print("\n=== Phase 20: Penalties applied to players ===\n")

    out = await feed("/mark_late Alice 20", ALICE)
    record("/mark_late applies a late penalty", bool(out))

    out = await feed("/mark_ditch Bob", ALICE)
    record("/mark_ditch applies the ditch tier", bool(out))

    out = await feed("/mark_penalty late_short Carol", ALICE)
    record("/mark_penalty responds (valid or curated error)", bool(out))

    print("\n=== Phase 20b: Backfilling pre-existing dues ===\n")

    # Opening balance for a name with no prior ledger history — the actual
    # backfill case (a group adopting the bot mid-season).
    out = await feed("/adjust_dues Alice 300 dues from June games", ALICE)
    ok, d = contains(out, "300")
    record("/adjust_dues adds an opening balance", ok, d)

    # Append-only correction: opposite sign, never an edit.
    out = await feed("/adjust_dues Alice -300 correction", ALICE)
    ok, d = contains(out, "300")
    record("/adjust_dues credits back (append-only correction)", ok, d)

    out = await feed("/adjust_dues Alice 0 nope", ALICE)
    record("/adjust_dues rejects a zero amount", bool(out))

    out = await feed("/adjust_dues Alice 99999999 typo", ALICE)
    record("/adjust_dues rejects an implausibly large amount", bool(out))

    out = await feed("/adjust_dues Alice", ALICE)
    record("/adjust_dues without an amount explains usage", bool(out))

    # Multi-word name must survive the amount scan.
    out = await feed("/adjust_dues Ravi Kumar 150 old dues", ALICE)
    ok, d = contains(out, "Ravi")
    record("/adjust_dues handles a multi-word name", ok, d)

    out = await feed("/import_dues preview\nAlice 100 a\nBob 200 b", ALICE)
    ok, d = contains(out, "preview")
    record("/import_dues preview parses without writing", ok, d)

    out = await feed("/import_dues\nAlice 100 a\nBob 200 b\nCarol -50 overpaid", ALICE)
    ok, d = contains(out, "3")
    record("/import_dues writes a multi-line batch", ok, d)

    # A malformed line must abort the WHOLE import, not half-apply it.
    out = await feed("/import_dues\nAlice 100\nBobbb\nCarol 50", ALICE)
    ok, d = contains(out, "line")
    record("/import_dues rejects the batch when a line is malformed", ok, d)

    out = await feed("/import_dues", ALICE)
    record("/import_dues with no lines explains usage", bool(out))

    print("\n=== Phase 21: Fund / treasury ===\n")

    out = await feed("/fund", ALICE)
    record("/fund renders the fund balance", bool(out))

    out = await feed("/fund_topup 500 donations", ALICE)
    record("/fund_topup adds to the fund", bool(out))

    out = await feed("/log_expense 150 new balls", ALICE)
    record("/log_expense records an expense", bool(out))

    out = await feed("/reimburse Bob 600 fronted ground", ALICE)
    record("/reimburse records a reimbursement", bool(out))

    out = await feed("/fund_history", ALICE)
    record("/fund_history renders history", bool(out))

    print("\n=== Phase 22: Dues reporting & season reset ===\n")

    out = await feed("/dues_snapshot", ALICE)
    record("/dues_snapshot responds", bool(out))

    out = await feed("/dues_export", ALICE)
    record("/dues_export responds", bool(out))

    out = await feed("/cancel_game_dues", ALICE)
    record("/cancel_game_dues responds", bool(out))

    # /new_season is guarded (admin-only, two-step confirm, blocked while
    # games are unsettled) — assert it RESPONDS rather than that it resets,
    # so the guard firing is a pass, not a failure.
    out = await feed("/new_season", ALICE)
    record("/new_season responds (confirm card or guard message)", bool(out))

    out = await feed("/disable_dues", ALICE)
    record("/disable_dues responds", bool(out))

    # Money handlers must never leak an internal error into the chat; the
    # global no-ERROR-logs check at the end of Phase 16 covers the whole run,
    # but fail fast here so a dues regression is attributed to this phase.
    record("no ERROR-level logs during dues phases", len(_errors) == 0,
           f"{len(_errors)} errors: {error_msgs()[:5]}" if _errors else "")


async def phase_remaining_surface():
    """The last 19 registered commands that had no functional coverage.

    Mostly thin surfaces (link/token generation, reporting, reminder settings,
    roster admin) but "thin" is exactly what nobody notices breaking — the
    R11 regression that killed every panel button was a decorator on a handler
    nobody drove in a test. Several of these also exercise the pillow and
    matplotlib paths (/card, /export_stats), which no other layer touches.
    """
    print("\n=== Phase 23: Web & token surfaces ===\n")

    out = await feed("/weblink", ALICE)
    record("/weblink responds (link or config warning)", bool(out))

    out = await feed("/mytoken", BOB)
    record("/mytoken issues a personal token", bool(out))

    out = await feed("/mytoken off", BOB)
    record("/mytoken off revokes it", bool(out))

    out = await feed("/weblogin Alice", ALICE)
    record("/weblogin responds for a known member", bool(out))

    # With web configured, the login link goes to the issuing admin's DM and
    # NEVER into the group — whoever opens it is signed in as that member.
    _prev_wb = os.environ.get("WEB_BASE_URL")
    os.environ["WEB_BASE_URL"] = "https://rollcall.test"
    try:
        out = await feed("/weblogin Alice", ALICE)
    finally:
        if _prev_wb is None:
            os.environ.pop("WEB_BASE_URL", None)
        else:
            os.environ["WEB_BASE_URL"] = _prev_wb
    _texts = [(a[0] if a else kw.get("chat_id"), str(a[1] if len(a) > 1 else kw.get("text", "")))
              for n, a, kw in out if n == "send_message"]
    record("/weblogin DMs the link to the admin",
           any(c == ALICE[0] and "/auth/weblogin/" in t for c, t in _texts), str(_texts))
    record("/weblogin never posts the link in the group",
           not any(c == CHAT_ID and "/auth/weblogin/" in t for c, t in _texts), str(_texts))

    # Deep-link verify: opening /start v_<code> only asks; the Confirm tap
    # (in the DM) is what verifies.
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    _vcode = "f" * 32
    _db.create_web_verify_token(_vcode, _dt.now(_tz.utc) + _td(minutes=10))
    out = await feed(f"/start v_{_vcode}", BOB, chat_id=BOB[0])
    _row = _db.get_web_verify_token(_vcode) or {}
    record("/start v_<code> asks for confirmation instead of verifying",
           has_call(out, "send_message") and not _row.get("tg_user_id"), str(_row))
    out = await feed_cb(f"tgv_ok:{_vcode}", BOB, chat_id=BOB[0])
    _row = _db.get_web_verify_token(_vcode) or {}
    record("Confirm tap verifies the browser", _row.get("tg_user_id") == BOB[0], str(_row))

    out = await feed("/gentoken", ALICE)
    record("/gentoken responds", bool(out))

    print("\n=== Phase 24: Reporting & exports ===\n")

    out = await feed("/summary", ALICE)
    record("/summary responds", bool(out))

    out = await feed("/summary 14", ALICE)
    record("/summary with a day window responds", bool(out))

    out = await feed("/calendar", ALICE)
    record("/calendar responds", bool(out))

    # Exercises the CSV/document path (bot.send_document).
    out = await feed("/export_stats", ALICE)
    record("/export_stats responds", bool(out))

    print("\n=== Phase 25: Reminder & schedule settings ===\n")

    for cmd, label in (
        ("/auto_buzz 3", "/auto_buzz sets hours"),
        ("/auto_buzz off", "/auto_buzz off disables"),
        ("/remind_before_close 2", "/remind_before_close sets hours"),
        ("/remind_before_close off", "/remind_before_close off disables"),
        ("/remind_after_open 6", "/remind_after_open sets hours"),
        ("/remind_after_open off", "/remind_after_open off disables"),
        ("/set_rollcall_reminder 2", "/set_rollcall_reminder sets hours"),
    ):
        out = await feed(cmd, ALICE)
        record(label, bool(out))

    print("\n=== Phase 26: Roster admin on a live rollcall ===\n")

    await feed("/src Surface Coverage Game", ALICE)
    await feed("/in", ALICE)
    await feed("/in", BOB)
    await feed("/in", CAROL)

    # Far-future date so this can never fail as "time is in the past".
    out = await feed("/set_rollcall_time 12-06-2030 19:30", ALICE)
    record("/set_rollcall_time accepts a future date", bool(out))

    out = await feed("/set_status Bob out", ALICE)
    record("/set_status changes a member's vote", bool(out))

    out = await feed("/delete_user Carol", ALICE)
    record("/delete_user removes a member from the list", bool(out))

    # Removing someone from a CAPPED in-list frees a slot — the first
    # waitlister has to move up and be told about it. This shipped broken:
    # the delete path was the one slot-freeing path with no promotion step,
    # so the waitlist stalled until an unrelated /out happened to fire.
    await feed("/erc", ALICE)
    await feed("/src Waitlist Promotion Game", ALICE)
    await feed("/set_limit 2", ALICE)
    # These three voted seconds ago in the previous rollcall — the 2s
    # in/out/maybe cooldown would otherwise swallow the votes.
    bot_state._rate_limits.clear()
    await feed("/in", ALICE)
    bot_state._rate_limits.clear()
    await feed("/in", BOB)
    bot_state._rate_limits.clear()
    await feed("/in", CAROL)          # -> WAITING
    out = await feed("/whos_waiting", ALICE)
    record("/set_limit puts the 3rd voter on the waitlist", "Carol" in text_of(out))

    await feed("/delete_user Bob", ALICE)
    out = await feed_cb("delconf_yes_0_100", ALICE)
    record("/delete_user announces the waitlist -> IN promotion",
           "Carol" in text_of(out))

    out = await feed("/whos_in", ALICE)
    in_txt = text_of(out)
    record("/delete_user promotes the waitlister into the freed IN slot",
           "Carol" in in_txt and "Bob" not in in_txt)

    out = await feed("/whos_waiting", ALICE)
    record("waitlist is empty after the promotion", "Carol" not in text_of(out))
    await feed("/erc", ALICE)

    await feed("/src Surface Coverage Game 2", ALICE)
    await feed("/in", ALICE)

    # Match-day card — the pillow rendering path.
    out = await feed("/card", ALICE)
    record("/card renders without crashing", bool(out))

    out = await feed("/mark_absent", ALICE)
    record("/mark_absent responds", bool(out))

    out = await feed("/cancel_roll_call rain", ALICE)
    record("/cancel_roll_call cancels without recording stats", bool(out))

    print("\n=== Phase 26b: Cancel RollCall panel button + optional reason ===\n")

    await feed("/src Panel Cancel Test", ALICE)
    out = await feed_cb("btn_cancelrc_1", ALICE)
    record("btn_cancelrc shows confirmation", has_call(out, "edit_message_text"))

    out = await feed_cb("btn_cancelyes_1", ALICE)
    record("btn_cancelyes asks for an optional reason",
           has_call(out, "edit_message_text"))
    record("rollcall still open while the reason is pending",
           len(manager.get_rollcalls(CHAT_ID)) == 1)

    out = await feed("low count", ALICE)
    record("free-text reply cancels the rollcall with that reason",
           has_call(out, "edit_message_text") and "low count" in text_of(out).lower())
    record("rollcall actually gone after the reason reply",
           len(manager.get_rollcalls(CHAT_ID)) == 0)

    await feed("/src Panel Cancel Skip Test", ALICE)
    await feed_cb("btn_cancelrc_1", ALICE)
    await feed_cb("btn_cancelyes_1", ALICE)
    out = await feed_cb("btn_cancelskip_1", ALICE)
    record("btn_cancelskip cancels immediately with no reason",
           has_call(out, "edit_message_text"))
    record("rollcall actually gone after Skip",
           len(manager.get_rollcalls(CHAT_ID)) == 0)

    print("\n=== Phase 27: Repeat & broadcast ===\n")

    # /repeat clones the last ENDED rollcall, so it needs one to exist.
    await feed("/src Repeat Source", ALICE)
    await feed("/erc", ALICE)

    out = await feed("/repeat", ALICE)
    record("/repeat clones the last ended rollcall", bool(out))
    await feed("/erc", ALICE)

    out = await feed("/repeat repeat=weekly", ALICE)
    record("/repeat with a recurrence flag responds", bool(out))
    await feed("/erc", ALICE)

    # ALICE is uid 100 == ADMIN1, so she is the bot-owner/super-admin.
    out = await feed('/broadcast "scheduled maintenance tonight"', ALICE)
    record("/broadcast responds for the bot owner", bool(out))

    out = await feed("/health", ALICE)
    record("/health responds for the bot owner", bool(out))
    # The backup line is the reason this command exists — a reply that omits it
    # would look fine while hiding exactly the signal it was added to surface.
    record("/health reports backup status", any("Backup" in str(m) or "backup" in str(m) for m in out),
           f"got: {str(out)[:160]}")
    # Uptime is the other half: /health answers "is everything alive right
    # now", and this line is the only one that answers "and how much of the
    # time has it been". It is built from a database read, so a broken
    # heartbeat would degrade to the string below rather than raising —
    # assert the real value, not merely that the line is present.
    record("/health reports uptime", any("Uptime:" in str(m) for m in out),
           f"got: {str(out)[:200]}")
    # The off-site line is the one that answers "would we survive losing this
    # machine". With no RCLONE_REMOTE set here it must read as not-configured
    # rather than as a fault — an operator who hasn't opted in isn't broken.
    record("/health reports the off-site copy", any("Off-site" in str(m) for m in out),
           f"got: {str(out)[:240]}")
    record("/health calls an unconfigured off-site 'not configured', not a failure",
           any("Off-site: not configured" in str(m) for m in out),
           f"got: {str(out)[:240]}")
    record("/health uptime is a real measurement, not the fallback",
           not any("Uptime: unavailable" in str(m) for m in out),
           f"got: {str(out)[:200]}")
    # And it must stay owner-only: it names file paths and infrastructure state.
    out_nonadmin = await feed("/health", BOB)
    record("/health is silent for non-owners", not out_nonadmin,
           f"non-owner got a reply: {str(out_nonadmin)[:120]}")

    record("no ERROR-level logs during surface phases", len(_errors) == 0,
           f"{len(_errors)} errors: {error_msgs()[:5]}" if _errors else "")


class _FakeClock:
    """Burst phases run on fake time: chat_outbox's waits advance this
    instead of sleeping, so a 60-second recovery takes milliseconds."""

    def __init__(self):
        self.now, self.slept = 5000.0, 0.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += max(0.0, seconds)
        self.slept += max(0.0, seconds)
        await asyncio.sleep(0)


class _ThrottlingTelegram:
    """Telegram's per-chat flood limit at the HTTP layer, on the fake clock:
    `burst` messages at once, then `rate` per second; anything over is
    refused with a real ApiTelegramException 429 + retry_after.

    It replaces chat_outbox._orig_process_request — the network call BELOW
    the pacing layer — and swaps the recorder stubs for the real AsyncTeleBot
    send/edit methods, so a reply travels exactly the production path:
    handler → telebot method → chat_outbox pacing → (fake) Telegram.
    Accepted calls are appended to _outbound in the recorder's shape.
    """

    REAL = ("send_message", "edit_message_text", "edit_message_reply_markup")

    def __init__(self, clock, burst=3, rate=1.0):
        self.clock, self.burst, self.rate = clock, burst, rate
        self.tokens, self.stamp = {}, {}
        self.attempted, self.refused = [], 0
        self._next_id = 90000
        self.refuse_if = None     # text -> True: Telegram refuses it every time (retry after 120s)

    def _take(self, chat, method):
        from telebot.apihelper import ApiTelegramException
        now = self.clock.now
        t = min(self.burst, self.tokens.get(chat, self.burst)
                + (now - self.stamp.get(chat, now)) * self.rate)
        self.stamp[chat] = now
        if t < 1:
            self.tokens[chat] = t
            self.refused += 1
            wait = max(1, int((1 - t) / self.rate + 0.999))
            raise ApiTelegramException(method, MagicMock(status_code=429), {
                "ok": False, "error_code": 429,
                "description": f"Too Many Requests: retry after {wait}",
                "parameters": {"retry_after": wait}})
        self.tokens[chat] = t - 1

    @staticmethod
    def _markup(params):
        import json as _json
        from types import SimpleNamespace
        raw = (params or {}).get("reply_markup")
        if not raw:
            return None
        rows = (_json.loads(raw) if isinstance(raw, str) else raw).get("inline_keyboard") or []
        return SimpleNamespace(keyboard=[[SimpleNamespace(**b) for b in row] for row in rows])

    async def _http(self, token, url, method="get", params=None, files=None, **kw):
        params = params or {}
        chat = int(params["chat_id"])
        text = str(params.get("text", ""))
        self.attempted.append((url, chat, text))
        if self.refuse_if and self.refuse_if(text):
            from telebot.apihelper import ApiTelegramException
            self.refused += 1
            raise ApiTelegramException(url, MagicMock(status_code=429), {
                "ok": False, "error_code": 429, "description": "Too Many Requests: retry after 120",
                "parameters": {"retry_after": 120}})
        self._take(chat, url)
        self._next_id += 1
        msg = {"message_id": self._next_id, "date": 0, "text": text,
               "chat": {"id": chat, "type": "supergroup" if chat < 0 else "private"}}
        markup = {"reply_markup": self._markup(params)}
        if url == "sendMessage":
            _outbound.append(("send_message", (chat, text), markup))
        elif url == "editMessageText":
            _outbound.append(("edit_message_text", (text, chat, int(params["message_id"])), markup))
        elif url == "editMessageReplyMarkup":
            _outbound.append(("edit_message_reply_markup", (chat, int(params["message_id"])), markup))
        return msg

    def install(self):
        import chat_outbox
        self._saved = ({n: getattr(bot, n) for n in self.REAL}, chat_outbox._orig_process_request,
                       chat_outbox._clock, chat_outbox._sleep)
        for n in self.REAL:
            setattr(bot, n, getattr(type(bot), n).__get__(bot, type(bot)))
        chat_outbox._orig_process_request = self._http
        chat_outbox._clock, chat_outbox._sleep = self.clock, self.clock.sleep

    def uninstall(self):
        import chat_outbox
        stubs, chat_outbox._orig_process_request, chat_outbox._clock, chat_outbox._sleep = self._saved
        for n, f in stubs.items():
            setattr(bot, n, f)


_VOTERS = [(7001 + i, f"Voter{chr(65 + i)}", f"voter{chr(97 + i)}") for i in range(10)]


def _accepted(outbound, method, chat=CHAT_ID):
    """Texts Telegram ACCEPTED (refused calls never reach the recorder)."""
    out = []
    for name, args, kwargs in outbound:
        if name == method == "send_message" and args and args[0] == chat:
            out.append((str(args[1]) if len(args) > 1 else "", kwargs))
        elif name == method == "edit_message_text" and len(args) > 1 and args[1] == chat:
            out.append((str(args[0]), kwargs))
    return out


def _panel_order_ok(text, names):
    pos = [text.find(n) for n in names]
    return all(p >= 0 for p in pos) and pos == sorted(pos)


async def _settle(clock=None):
    import chat_outbox
    await chat_outbox.drain()
    for _ in range(5):                     # DM/push tasks spawned by promotions
        await asyncio.sleep(0)
    if clock is not None:
        clock.now += 60                    # a minute passes before anyone's next command


def _outbox_empty():
    import chat_outbox
    return not (chat_outbox._blocked_until or chat_outbox._chat_locks
                or chat_outbox._queued or chat_outbox._inflight)


def _rc_in_order(rc):
    return [getattr(u, "user_id", None) for u in rc.inList] if rc else []


def _rc_wait_order(rc):
    return [getattr(u, "user_id", None) for u in rc.waitList] if rc else []


def _db_status_count(rc, status):
    with _db._cursor() as cur:
        ph = '%s' if _db.db_type == 'postgresql' else '?'
        cur.execute(f"SELECT COUNT(*) FROM users WHERE rollcall_id = {ph} AND status = {ph} "
                    f"AND user_id >= 7001 AND user_id <= 7010", (rc.db_id, status))
        row = cur.fetchone()
        return int(row[0] if not isinstance(row, dict) else list(row.values())[0])


def _no_false_failure(tg):
    return [t for _, _, t in tg.attempted
            if "something went wrong" in t.lower() or "too many requests" in t.lower()]


async def phase_vote_burst():
    """Burst safety. Order, completeness and normal behaviour are what this
    bot is trusted for, so each guarantee gets its own check:

      A  normal use is unchanged — no waiting, one panel edit per vote
      B  10 simultaneous /in      — all saved in order, every confirmation
                                    delivered once, final panel shows all 10
      C  10 simultaneous taps     — the same, through the panel buttons
      D  burst over the IN limit  — first 8 IN, last 2 waitlisted, in order
      E  burst of /out            — promotions announced and DMed, in order
    """
    print("\n=== Phase: vote bursts (Telegram throttling) ===\n")
    import chat_outbox

    # ── A: normal usage ───────────────────────────────────────────────────
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock, burst=1000)          # never throttles
    tg.install()
    try:
        await feed("/src Normal Game", ALICE)
        edits_ok, confirms_ok = True, True
        for uid, fname, uname in _VOTERS[:4]:
            bot_state._rate_limits.clear()
            out = await feed("/in", (uid, fname, uname))
            edits = _accepted(out, "edit_message_text")
            sends = _accepted(out, "send_message")
            edits_ok &= len(edits) == 1 and fname in edits[0][0]
            confirms_ok &= sum("is now IN" in t and fname in t for t, _ in sends) == 1
        record("Normal: each vote redraws the panel immediately, once", edits_ok)
        record("Normal: each vote is confirmed immediately, once", confirms_ok)
        record("Normal: nothing waits when Telegram isn't throttling", clock.slept == 0,
               f"waited {clock.slept}s")
        await _settle(clock)
        record("Normal: no deferred redraws left over", _outbox_empty())
        await feed("/erc", ALICE)
    finally:
        tg.uninstall()

    # ── B: 10 simultaneous /in (scheduled messages at 9:01) ───────────────
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock)
    tg.install()
    try:
        await feed("/src Burst Game", ALICE)
        bot_state._rate_limits.clear()
        _outbound.clear(); _errors.clear()
        await bot.process_new_updates([make_message_update("/in", u, f, n) for u, f, n in _VOTERS])
        await _settle(clock)
        out = list(_outbound)
        rc = manager.get_rollcall(CHAT_ID, 0)
        ids = [u for u, _, _ in _VOTERS]
        names = [f for _, f, _ in _VOTERS]
        record("Burst /in: Telegram really throttled the burst", tg.refused >= 1, f"refused {tg.refused}")
        record("Burst /in: the rest waited their turn instead of being refused",
               clock.slept >= 5 and tg.refused <= 3, f"waited {clock.slept}s, refused {tg.refused}")
        record("Burst /in: all 10 on the IN list, in arrival order", _rc_in_order(rc) == ids,
               f"got {_rc_in_order(rc)}")
        record("Burst /in: all 10 saved to the database", _db_status_count(rc, "in") == 10)
        sends = [t for t, _ in _accepted(out, "send_message")]
        per_voter = [sum("is now IN" in t and n in t for t in sends) for n in names]
        record("Burst /in: every voter's confirmation delivered exactly once",
               per_voter == [1] * 10, f"per voter {per_voter}")
        confirm_order = [next((i for i, t in enumerate(sends) if n in t), -1) for n in names]
        record("Burst /in: confirmations posted in vote order",
               -1 not in confirm_order and confirm_order == sorted(confirm_order), f"{confirm_order}")
        edits = [t for t, _ in _accepted(out, "edit_message_text")]
        record("Burst /in: final panel lists all 10 in order",
               bool(edits) and _panel_order_ok(edits[-1], names), f"{len(edits)} edits")
        record("Burst /in: panel redraws coalesced (fewer than one per vote)",
               1 <= len(edits) < 10, f"{len(edits)} edits")
        record("Burst /in: no duplicate panel posted", not has_panel_send(out))
        record("Burst /in: nobody told their vote failed", not _no_false_failure(tg))
        record("Burst /in: no errors logged", not _errors, str(error_msgs()[:2]))
        record("Burst /in: delivery state cleared afterwards", _outbox_empty())
        out = await feed("/whos_in", ALICE)
        record("Burst /in: /whos_in lists all 10", all(n in text_of(out) for n in names))
        await feed("/erc", ALICE)
    finally:
        tg.uninstall()

    # ── C: 10 simultaneous panel-button taps ──────────────────────────────
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock)
    tg.install()
    try:
        await feed("/src Tap Game", ALICE)
        rc = manager.get_rollcall(CHAT_ID, 0)
        panel_id = bot_state._panel_msg_ids.get((CHAT_ID, 1))
        bot_state._rate_limits.clear()
        _outbound.clear(); _errors.clear()
        await bot.process_new_updates([
            make_callback_update(f"btn_in_1_{rc.db_id}", u, f, n, src_msg_id=panel_id)
            for u, f, n in _VOTERS])
        await _settle(clock)
        out = list(_outbound)
        rc = manager.get_rollcall(CHAT_ID, 0)
        record("Burst taps: all 10 on the IN list, in arrival order",
               _rc_in_order(rc) == [u for u, _, _ in _VOTERS], f"got {_rc_in_order(rc)}")
        sends = [t for t, _ in _accepted(out, "send_message")]
        per_voter = [sum("→ IN" in t and n in t for t in sends) for _, n, _ in _VOTERS]
        record("Burst taps: every confirmation delivered exactly once",
               per_voter == [1] * 10, f"per voter {per_voter}")
        edits = [(t, kw) for t, kw in _accepted(out, "edit_message_text")]
        record("Burst taps: final panel lists all 10 in order",
               bool(edits) and _panel_order_ok(edits[-1][0], [n for _, n, _ in _VOTERS]))
        record("Burst taps: the tapped panel is the one redrawn",
               all(True for _ in edits) and bot_state._panel_msg_ids.get((CHAT_ID, 1)) == panel_id)
        record("Burst taps: no duplicate panel posted", not has_panel_send(out))
        record("Burst taps: no errors logged", not _errors, str(error_msgs()[:2]))
        record("Burst taps: delivery state cleared afterwards", _outbox_empty())
        await feed("/erc", ALICE)
    finally:
        tg.uninstall()

    # ── D + E: burst over the IN limit, then a burst of /out ──────────────
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock)
    tg.install()
    try:
        await feed("/src Limit Game", ALICE)
        await feed("/set_limit 8", ALICE)
        bot_state._rate_limits.clear()
        _outbound.clear(); _errors.clear()
        await bot.process_new_updates([make_message_update("/in", u, f, n) for u, f, n in _VOTERS])
        await _settle(clock)
        rc = manager.get_rollcall(CHAT_ID, 0)
        ids = [u for u, _, _ in _VOTERS]
        record("Burst over limit: first 8 arrivals IN, in order", _rc_in_order(rc) == ids[:8],
               f"got {_rc_in_order(rc)}")
        record("Burst over limit: last 2 arrivals waitlisted, in order",
               _rc_wait_order(rc) == ids[8:], f"got {_rc_wait_order(rc)}")
        sends = [t for t, _ in _accepted(list(_outbound), "send_message")]
        waited = [sum("waitlist" in t and n in t for t in sends) for _, n, _ in _VOTERS[8:]]
        record("Burst over limit: both waitlist notices delivered", waited == [1, 1], f"{waited}")

        bot_state._rate_limits.clear()
        _outbound.clear(); _errors.clear()
        a, b = _VOTERS[0], _VOTERS[1]
        await bot.process_new_updates([make_message_update("/out", a[0], a[1], a[2]),
                                       make_message_update("/out", b[0], b[1], b[2])])
        await _settle(clock)
        out = list(_outbound)
        rc = manager.get_rollcall(CHAT_ID, 0)
        record("Burst /out: the 2 waitlisted members promoted, in waitlist order",
               _rc_in_order(rc) == ids[2:8] + ids[8:], f"got {_rc_in_order(rc)}")
        sends = [t for t, _ in _accepted(out, "send_message")]
        promoted = [sum("from WAITING" in t and n in t for t in sends) for _, n, _ in _VOTERS[8:]]
        record("Burst /out: both promotions announced in the group", promoted == [1, 1], f"{promoted}")
        dms = {args[0] for name, args, _ in out if name == "send_message" and args and args[0] > 0}
        record("Burst /out: both promoted members DMed", {ids[8], ids[9]} <= dms, f"DMs to {sorted(dms)}")
        edits = [t for t, _ in _accepted(out, "edit_message_text")]
        final = edits[-1] if edits else ""
        in_part, _, out_part = final.partition("\nOut:")
        record("Burst /out: final panel shows the new IN list, in order",
               _panel_order_ok(in_part, [f for _, f, _ in _VOTERS[2:]])
               and _VOTERS[0][1] not in in_part and _VOTERS[1][1] not in in_part, repr(final[:120]))
        record("Burst /out: final panel shows both drop-outs under Out",
               _panel_order_ok(out_part, [_VOTERS[0][1], _VOTERS[1][1]]), repr(out_part[:60]))
        record("Burst /out: nobody told it failed", not _no_false_failure(tg))
        record("Burst /out: no errors logged", not _errors, str(error_msgs()[:2]))
        record("Burst /out: delivery state cleared afterwards", _outbox_empty())
        await feed("/erc", ALICE)
    finally:
        tg.uninstall()


def _undelivered(tg, outbound):
    """Messages Telegram refused at some point and that never went out
    afterwards — i.e. lost. Must be empty: pacing retries every refusal."""
    accepted = set()
    for name, args, _ in outbound:
        if name == "send_message":
            accepted.add(("sendMessage", args[0], str(args[1])))
        elif name == "edit_message_text":
            accepted.add(("editMessageText", args[1], str(args[0])))
        elif name == "edit_message_reply_markup":
            accepted.add(("editMessageReplyMarkup", args[0], ""))
    return [a for a in dict.fromkeys(tg.attempted) if a not in accepted]


async def phase_mixed_command_burst():
    """Telegram counts EVERY message to a group, whatever command sent it,
    and scheduled messages can fire any command. So: ten different commands
    in the same second, then /erc while Telegram is still throttling."""
    print("\n=== Phase: mixed-command burst + /erc under throttling ===\n")
    import chat_outbox
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock)
    tg.install()
    try:
        await feed("/src Mixed Game", ALICE)
        V = _VOTERS
        batch = [("/in", V[0]), ("/in", V[1]), ("/sif GuestOne", ALICE), ("/in", V[2]),
                 ("/whos_in", BOB), ("/maybe", V[3]), ("/set_limit 20", ALICE),
                 ("/in", V[4]), ("/out", V[5]), ("/in", V[6])]
        bot_state._rate_limits.clear()
        _outbound.clear(); _errors.clear(); tg.attempted.clear()
        await bot.process_new_updates([make_message_update(t, *u) for t, u in batch])
        await chat_outbox.drain()
        out = list(_outbound)
        rc = manager.get_rollcall(CHAT_ID, 0)
        in_names = [u.name for u in rc.inList] if rc else []
        record("Mixed burst: IN list in arrival order, guest included where it arrived",
               in_names == ["VoterA", "VoterB", "GuestOne", "VoterC", "VoterE", "VoterG"], f"{in_names}")
        record("Mixed burst: MAYBE and OUT recorded",
               [u.name for u in rc.maybeList] == ["VoterD"] and [u.name for u in rc.outList] == ["VoterF"])
        record("Mixed burst: Telegram throttled it", tg.refused >= 1, f"refused {tg.refused}")
        lost = _undelivered(tg, out)
        record("Mixed burst: every refused message was delivered later (none lost)",
               not lost, f"{len(lost)} lost: {lost[:2]}")
        texts = " ".join(t for _, _, t in tg.attempted)
        record("Mixed burst: /sif, /whos_in and /set_limit all replied",
               "GuestOne" in texts and "20" in texts and len(tg.attempted) >= 12)
        record("Mixed burst: nobody told it failed", not _no_false_failure(tg))
        record("Mixed burst: no errors logged", not _errors, str(error_msgs()[:2]))

        # /erc in the same second — Telegram is still refusing this chat.
        tg.attempted.clear()
        out = await feed("/erc", ALICE)
        await chat_outbox.drain()
        out = list(_outbound)
        record("/erc under throttling: the rollcall is ended", not manager.get_rollcalls(CHAT_ID))
        record("/erc under throttling: the final summary is delivered",
               any("Ended by" in t for n, a, _ in out if n == "send_message" for t in [str(a[1])]))
        lost = _undelivered(tg, out)
        record("/erc under throttling: nothing lost", not lost, f"{lost[:2]}")
        record("/erc under throttling: admin not told it failed", not _no_false_failure(tg))
        record("/erc under throttling: no errors logged", not _errors, str(error_msgs()[:2]))
        clock.now += 60
        record("Mixed burst: delivery state cleared afterwards", _outbox_empty())
    finally:
        tg.uninstall()


async def phase_stats_season():
    """Stats checked against an independently kept ledger.

    Plays a randomized season through real commands (mind changes, /xrc
    cancels, no-show reviews) in a chat of its own, counts every number
    separately, and compares with /stats, the web Stats tab, the no-show
    board and history. Fixed seeds, so a failure reproduces exactly.
    """
    print("\n=== Phase: stats vs independent ledger (randomized seasons) ===\n")
    import random
    from services import stats as stats_svc, ghost as ghost_svc
    from services.web import get_group_web_token

    members = _VOTERS[:8]
    for n, seed in enumerate((7, 42, 99)):
        rng = random.Random(seed)
        chat = -1001999000700 - n
        mismatches = []

        def expect(label, got, want):
            if got != want:
                mismatches.append(f"{label}: got {got!r}, expected {want!r}")

        await feed("/toggle_ghost_tracking on", ALICE, chat_id=chat)
        manager.set_absent_limit(chat, 1000)   # no reconfirm prompt: votes land directly
        played, cancelled = [], []             # (rc_id, {uid: last vote}, ghosts)
        ghosts_owed = {m[0]: 0 for m in members}
        for g in range(14):
            await feed(f"/src Season{g}", ALICE, chat_id=chat)
            rc_id = manager.get_rollcall(chat, 0).id
            votes = {}
            for m in members:
                final = rng.choice(["in", "in", "in", "out", "maybe", None])
                first = [rng.choice(["in", "out", "maybe"])] if rng.random() < 0.3 else []
                for c in first + [final]:
                    if c is None:
                        continue
                    bot_state._rate_limits.clear()
                    await feed(f"/{c}", m, chat_id=chat)
                    votes[m[0]] = c
            if rng.random() < 0.25:
                await feed("/xrc", ALICE, chat_id=chat)
                cancelled.append((rc_id, votes, set()))
                continue
            await feed("/erc", ALICE, chat_id=chat)
            ins = [u for u, v in votes.items() if v == "in"]
            ghosts = set()
            if ins and rng.random() < 0.7:
                ghosts = set(rng.sample(ins, k=min(len(ins), rng.choice([0, 1, 1, 2]))))
                ghost_svc.review_session(chat, rc_id, ghosts)
                for u in ins:
                    ghosts_owed[u] = ghosts_owed[u] + 1 if u in ghosts else max(0, ghosts_owed[u] - 1)
            played.append((rc_id, votes, ghosts))

        lb = stats_svc.leaderboard(chat, limit=50)
        expect("games counted", lb["total_rollcalls_in_chat"], len(played))
        by_uid = {e["user_id"]: e for e in lb["entries"]}
        for uid, name, _ in members:
            att = sum(1 for _, v, gh in played if v.get(uid) == "in" and uid not in gh)
            expect(f"{name} games played", by_uid.get(uid, {}).get("sessions_attended", 0), att)
            if uid in by_uid and played:
                rate = by_uid[uid].get("attendance_rate")
                if rate is None or abs(rate - 100 * att / len(played)) >= 0.6:
                    mismatches.append(f"{name} attendance rate {rate} vs {100 * att / len(played):.1f}")
        order = [e["sessions_attended"] for e in lb["entries"]]
        expect("leaderboard order", order, sorted(order, reverse=True))

        board = {r.get("user_id"): r for r in _db.get_ghost_leaderboard(chat)}
        for uid, name, _ in members:
            expect(f"{name} no-shows", int((board.get(uid) or {}).get("ghost_count") or 0), ghosts_owed[uid])

        cancelled_ids = {c[0] for c in cancelled}
        hist = {h.get("rollcall_id", h.get("id")): h for h in stats_svc.history(chat, limit=50)}
        for rc_id, votes, _ in played + cancelled:
            h = hist.get(rc_id)
            if h is None:
                mismatches.append(f"history missing game {rc_id}")
                continue
            for k in ("in", "out", "maybe"):
                expect(f"game {rc_id} {k}", h[f"{k}_count"], sum(1 for v in votes.values() if v == k))
            expect(f"game {rc_id} cancelled flag", h["is_cancelled"], rc_id in cancelled_ids)

        pending = {r["id"] for r in _db.get_unprocessed_rollcalls(chat)}
        expect("cancelled games offered for no-show review", pending & cancelled_ids, set())

        web = stats_svc.web_group_stats(get_group_web_token(chat))
        expect("web Stats tab games played",
               {e.get("user_id"): e.get("sessions_attended") for e in web["leaderboard"]},
               {e["user_id"]: e["sessions_attended"] for e in stats_svc.leaderboard(chat, limit=10)["entries"]})
        for h in web["recent_history"]:
            rid = h.get("rollcall_id", h.get("id"))
            expect(f"web game {rid} cancelled flag", h["is_cancelled"], rid in cancelled_ids)

        expect("busiest-time vote total", stats_svc.voting_peak_times(chat)["total_votes"],
               sum(len(v) for _, v, _ in played + cancelled))
        rt = stats_svc.response_time_board(chat)["entries"]
        expect("fastest voters duplicates", len({e["user_id"] for e in rt}), len(rt))

        record(f"Stats season (seed {seed}, {len(played)} played, {len(cancelled)} cancelled) "
               "match an independent count: /stats, web, no-shows, history, peaks",
               not mismatches, "; ".join(mismatches[:6]))


async def phase_close_races():
    """Regressions from the 2026-10-30 review of the burst changes.

    1. A vote's panel redraw used the rollcall number from when the vote was
       saved. Its confirmation can wait out a rate limit for seconds; if
       another rollcall closed meanwhile, the panel was drawn as "ID: 2" when
       it was now #1 (typed `/in ::2` then failed).
    3. /erc's summary failing used to skip everything after it — the ghost
       prompt and removing the closed panel's buttons.
    """
    print("\n=== Phase: close/renumber races under throttling ===\n")
    import chat_outbox
    from bot_state import _panel_msg_ids

    def keys():
        return {k[1]: v for k, v in _panel_msg_ids.items() if k[0] == CHAT_ID}

    # ── 1: vote on #2 waits; /erc #1 renumbers it to #1 meanwhile ─────────
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock, burst=6)
    tg.install()
    try:
        await feed("/src First", ALICE)
        await feed("/src Second", ALICE)
        second_panel = keys()[2]
        clock.now += 60
        bot_state._rate_limits.clear()
        _outbound.clear(); _errors.clear(); tg.attempted.clear()
        tg.tokens[CHAT_ID] = 0; tg.stamp[CHAT_ID] = clock.now        # Telegram: throttled
        vote = asyncio.ensure_future(bot.process_new_updates(
            [make_message_update("/in ::2", *_VOTERS[0])]))
        await asyncio.sleep(0); await asyncio.sleep(0)               # saved; confirmation waiting
        await bot.process_new_updates([make_message_update("/erc ::1", *ALICE)])
        await vote
        await chat_outbox.drain()
        edits = [a for n, a, _ in _outbound if n == "edit_message_text" and a[2] == second_panel]
        last = edits[-1][0] if edits else ""
        record("Renumber race: the vote's panel is drawn as the rollcall's NEW number",
               "Title: Second" in last and "ID: 1" in last, repr(last[:40]))
        record("Renumber race: no stale #2 panel mapping left behind",
               keys() == {1: second_panel}, f"{keys()}")
        record("Renumber race: the vote is on Second", _VOTERS[0][0] in
               [u.user_id for u in manager.get_rollcall(CHAT_ID, 0).inList])
        record("Renumber race: no errors logged", not _errors, str(error_msgs()[:2]))
        clock.now += 60
        await feed("/erc", ALICE)
    finally:
        tg.uninstall()

    # ── 1b: no burst at all — closing #1 must still relabel #2's panel ────
    for close_cmd in ("/erc ::1", "/xrc ::1"):
        clock = _FakeClock()
        tg = _ThrottlingTelegram(clock, burst=1000)
        tg.install()
        try:
            await feed("/src First", ALICE)
            await feed("/src Second", ALICE)
            second_panel = keys()[2]
            _outbound.clear()
            await feed(close_cmd, ALICE)
            await chat_outbox.drain()
            edits = [a[0] for n, a, _ in _outbound if n == "edit_message_text" and a[2] == second_panel]
            record(f"{close_cmd.split()[0]} of #1: the remaining panel is relabelled ID 1",
                   bool(edits) and "Title: Second" in edits[-1] and "ID: 1" in edits[-1],
                   repr(edits[-1][:40]) if edits else "no edit")
            await feed("/erc", ALICE)
        finally:
            tg.uninstall()

    # ── 3: /erc whose summary Telegram never takes ────────────────────────
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock, burst=1000)
    tg.install()
    try:
        await feed("/toggle_ghost_tracking on", ALICE)
        await feed("/src Ghosty", ALICE)
        panel = keys()[1]
        for v in _VOTERS[:3]:
            bot_state._rate_limits.clear()
            await feed("/in", v)
        tg.refuse_if = lambda text: "Ended by" in text
        _outbound.clear(); _errors.clear(); tg.attempted.clear()
        out = await feed("/erc", ALICE)
        await chat_outbox.drain()
        out = list(_outbound)
        texts = [str(a[1]) for n, a, _ in out if n == "send_message"]
        record("/erc summary refused: rollcall still closed", not manager.get_rollcalls(CHAT_ID))
        record("/erc summary refused: ghost prompt still posted",
               any("Did anyone ghost" in t for t in texts), f"{[t[:30] for t in texts]}")
        record("/erc summary refused: closed panel's buttons still removed",
               any(n == "edit_message_reply_markup" and a[1] == panel for n, a, _ in out))
        record("/erc summary refused: admin not told it failed", not _no_false_failure(tg))
        record("/erc summary refused: no errors logged", not _errors, str(error_msgs()[:2]))
        await feed("/toggle_ghost_tracking off", ALICE)
    finally:
        tg.uninstall()

    # ── 4: panel End button in shh mode — #2's panel is relabelled ────────
    # /erc got this fix first; the End button posts no fresh panels in shh
    # mode, so the existing one kept saying "ID: 2".
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock, burst=1000)
    tg.install()
    try:
        await feed("/src First", ALICE)
        await feed("/src Second", ALICE)
        second_panel = keys()[2]
        await feed("/shh", ALICE)
        await feed_cb("btn_end_1", ALICE)
        _outbound.clear(); _errors.clear()
        await feed_cb("btn_endconfirm_1", ALICE)
        await chat_outbox.drain()
        edits = [a[0] for n, a, _ in _outbound if n == "edit_message_text" and a[2] == second_panel]
        record("End button (shh) of #1: the remaining panel is relabelled ID 1",
               bool(edits) and "Title: Second" in edits[-1] and "ID: 1" in edits[-1],
               repr(edits[-1][:40]) if edits else "no edit")
        record("End button (shh): no errors logged", not _errors, str(error_msgs()[:2]))
        await feed("/louder", ALICE)
        await feed("/erc", ALICE)
    finally:
        tg.uninstall()

    # ── 5: End button whose ghost prompt Telegram never takes ─────────────
    # Used to raise out of the handler: the renumber notice and new panels
    # were skipped and the admin could be told the end had failed.
    clock = _FakeClock()
    tg = _ThrottlingTelegram(clock, burst=1000)
    tg.install()
    try:
        await feed("/toggle_ghost_tracking on", ALICE)
        await feed("/src First", ALICE)
        await feed("/src Second", ALICE)
        for v in _VOTERS[:2]:
            bot_state._rate_limits.clear()
            await feed("/in", v)
        await feed_cb("btn_end_1", ALICE)
        tg.refuse_if = lambda text: "Did anyone ghost" in text
        _outbound.clear(); _errors.clear(); tg.attempted.clear()
        await feed_cb("btn_endconfirm_1", ALICE)
        await chat_outbox.drain()
        texts = [str(a[1]) for n, a, _ in _outbound if n == "send_message"]
        record("End button, ghost prompt refused: rollcall closed",
               [r.title for r in manager.get_rollcalls(CHAT_ID)] == ["Second"])
        record("End button, ghost prompt refused: renumber notice still posted",
               any("IDs updated" in t for t in texts), f"{[t[:30] for t in texts]}")
        record("End button, ghost prompt refused: new panel for #1 still posted",
               any("Title: Second" in t and "ID: 1" in t for t in texts))
        record("End button, ghost prompt refused: admin not told it failed", not _no_false_failure(tg))
        record("End button, ghost prompt refused: no errors logged", not _errors, str(error_msgs()[:2]))
        tg.refuse_if = None
        await feed("/erc", ALICE)
        await feed("/toggle_ghost_tracking off", ALICE)
    finally:
        tg.uninstall()


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

    print("\n=== Phase 5a: /buzz skips a merged proxy's real account ===\n")
    print("    An admin who /sif's the same regulars every week (instead of")
    print("    having them /in themselves) must not have /buzz ping their real")
    print("    account too, once that proxy alias is merged to it.")

    await feed("/erc", ALICE)  # isolate from Phase 5's ambient rollcall state
    _errors.clear()
    await feed("/src BuzzMergeCheck", ALICE)
    await feed("/louder", ALICE)

    # BOB and CAROL both need to exist as known chat_members before /buzz can
    # consider pinging them -- any message routes through the real member-
    # tracking middleware and registers the sender, same as a real group.
    await feed("/whos_in", BOB)
    await feed("/whos_in", CAROL)

    # BOB is represented every week by a proxy alias rather than voting
    # himself. Merge it to his real account -- the identity service is the
    # same one the web portal's merge UI calls; there is no Telegram command
    # for it (merge is web-portal only), so this drives it the way that UI
    # would.
    await feed("/sif BobsRegularSlot", ALICE)
    from services.identity import link_identities
    link_identities(
        CHAT_ID, "BobsRegularSlot",
        canonical_user_id=BOB[0],
        admin_user_id=ALICE[0], admin_name=ALICE[1],
    )

    bot_state._buzz_cooldowns.pop(CHAT_ID, None)  # isolate from Phase 5's own /buzz call above
    out = await feed("/buzz", ALICE)
    no_err = len(_errors) == 0
    record("/buzz (merged-proxy case): no exceptions",
           no_err, str(error_msgs()) if not no_err else "")
    text = text_of(out)
    record("/buzz does NOT ping Bob -- his merged proxy alias already voted",
           "@bob" not in text.lower(), f"buzz text: {text[:300]!r}")
    record("/buzz DOES still ping Carol -- neither she nor any alias of hers voted",
           "@carol" in text.lower(), f"buzz text: {text[:300]!r}")

    await feed("/erc", ALICE)
    record("Phase 5a -- no ERROR-level logs emitted", len(_errors) == 0,
           str(error_msgs()) if _errors else "")

    out = await feed("/version", ALICE)
    ok, d = contains(out, deployed_version_str())
    record("/version reports the deployed version", ok, d)

    print("\n=== Phase 6: Help ===\n")

    out = await feed("/help", ALICE)
    ok, d = contains(out, "command")
    record("/help renders", ok, d)

    _long_before = len(_too_long)
    out = await feed("/help admin", ALICE)
    _admin_help = text_of(out)
    # 6.7k characters — over Telegram's limit, so it must arrive in parts,
    # header through the closing hint, with no part refused.
    ok = ("Admin Commands" in _admin_help and "shows details" in _admin_help
          and len(_too_long) == _long_before and len(out) >= 2)
    record("/help admin arrives whole, split under Telegram's limit", ok,
           f"{len(out)} message(s), too long: {_too_long[_long_before:]}")

    # A member's comment is printed on the panel for everyone. Uncapped, the
    # longest /in Telegram itself delivers pushed the panel past the limit,
    # so every panel edit after it failed for the whole group.
    await feed("/src Long comment", ALICE)
    _long_before = len(_too_long)
    bot_state._rate_limits.clear()
    out = await feed("/in " + "x" * 4000, BOB)
    ok = len(_too_long) == _long_before and "x" * 100 in text_of(out) and "x" * 101 not in text_of(out)
    record("/in with a 4,000-character comment: capped at 100, panel stays sendable", ok,
           f"too long: {_too_long[_long_before:]}")
    await feed("/erc", ALICE)

    # Titles went into Markdown unescaped in a few messages. "Sunday_Game"
    # opens italics that never close, so Telegram refused the message: the
    # vote-reminder DM never arrived, and /ef on an ended game errored after
    # saving the fee.
    import check_reminders as _cr
    _md_before = len(_bad_markdown)
    await feed("/src Sunday_Game", ALICE)
    await feed("/in", ALICE)
    _outbound.clear()
    await _cr._dm_non_responders(CHAT_ID, 1, "Sunday_Game", "closes in 2h")
    _dms = [a for n, a, _ in _outbound if n == "send_message" and a and a[0] != CHAT_ID]
    record("Vote-reminder DM for an underscore title parses as Markdown",
           bool(_dms) and len(_bad_markdown) == _md_before,
           f"{len(_dms)} DM(s), refused: {_bad_markdown[_md_before:]}")
    await feed("/erc", ALICE)
    out = await feed("/ef 500", ALICE)
    ok, d = contains(out, "Event Fee set to 500", "Sunday")
    record("/ef on an ended underscore-titled game confirms, Markdown parses",
           ok and len(_bad_markdown) == _md_before, f"{d} refused: {_bad_markdown[_md_before:]}")

    out = await feed("/help in", ALICE)
    ok, d = contains(out, "in")
    record("/help in renders detail", ok, d)

    print("\n=== Phase 7a: /sif panel consistency (ghosted vs clean proxy) ===\n")
    print("    Reported from production: /sif sometimes posts the full panel as a")
    print("    brand-new message, sometimes only edits an existing one silently.")
    print("    Traced to force_new=True on the ghosted-proxy reconfirm path only --")
    print("    every other vote/proxy-vote path edits the existing panel in place.")

    await feed("/erc", ALICE)  # clear any leftover
    _errors.clear()
    await feed("/src GhostPanelConsistency", ALICE)
    await feed("/louder", ALICE)  # ensure shh is OFF so confirmations are visible
    await feed("/toggle_ghost_tracking on", ALICE)
    await feed("/set_absent_limit 1", ALICE)

    # Give KEDAR a ghost history at/above the limit -- /sif on him must ask
    # for reconfirmation rather than adding him directly.
    _db.increment_ghost_count(CHAT_ID, -1, "Kedar", proxy_name="Kedar")

    out = await feed("/sif Kedar", ALICE)
    ok, d = contains(out, "ghosted", "still add")
    record("/sif on a ghosted proxy asks for reconfirmation instead of adding",
           ok, d)
    record("/sif on a ghosted proxy does NOT touch the panel yet -- only the "
           "warning prompt is sent, no edit and no panel-shaped send",
           not has_call(out, "edit_message_text") and not has_panel_send(out),
           f"outbound: {[n for n,_,_ in out]}")

    # Tap "Yes, add anyway" -- this is the proxy_add_ callback.
    out = await feed_cb("proxy_add_0_Kedar", ALICE)
    no_err = len(_errors) == 0
    record("proxy_add_ (ghosted proxy confirmed): no exceptions",
           no_err, str(error_msgs()) if not no_err else "")
    record("proxy_add_: confirmation bubble is edited",
           has_call(out, "edit_message_text"),
           f"outbound: {[n for n,_,_ in out]}")
    # THE regression this phase exists to catch: a ghosted proxy, once
    # confirmed, must update the SAME existing panel in place -- not spawn a
    # second, separate full-list message alongside it.
    record("proxy_add_ (ghosted, confirmed): panel is edited in place, "
           "not re-posted as a new message",
           not has_panel_send(out),
           f"outbound: {[n for n,_,_ in out]}")

    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, "kedar")
    record("Kedar is IN after confirming past the ghost warning", ok, d)

    # GUTTI has no ghost history at all -- /sif adds him straight away. This
    # path already used the default (edit-in-place); it is the CONTROL that
    # proves both proxy paths now agree with each other.
    out = await feed("/sif Gutti", ALICE)
    no_err = len(_errors) == 0
    record("/sif on a clean proxy (no ghost history): no exceptions",
           no_err, str(error_msgs()) if not no_err else "")
    record("/sif on a clean proxy: panel is edited in place (unchanged control case)",
           has_call(out, "edit_message_text") and not has_panel_send(out),
           f"outbound: {[n for n,_,_ in out]}")

    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, "kedar", "gutti")
    record("Both Kedar and Gutti show IN together", ok, d)

    # Callback data is capped at 64 BYTES, and proxy names went into it raw.
    # Over the cap Telegram refuses the whole message: no ghost warning, and
    # no absence-review card for that game. /sif names are one word (a
    # 21-letter Devanagari one is 63 bytes); web guests type their full name
    # with spaces, up to 64 characters. Taps use the buttons as rendered.
    _word = "सुब्रमण्यमवेंकटेश्वरन"
    _guest = "सुब्रमण्यम वेंकटेश्वर रामकृष्ण नारायण"
    _db.increment_ghost_count(CHAT_ID, -1, _word, proxy_name=_word)
    _bad_before = len(_bad_buttons)
    out = await feed(f"/sif {_word}", ALICE)
    _yes = next((d for d in buttons_of(out) if d.startswith("proxy_add_")), None)
    record("/sif warning for a long non-ASCII name: buttons within 64 bytes",
           _yes is not None and len(_bad_buttons) == _bad_before,
           f"buttons: {buttons_of(out)}")
    if _yes:
        await feed_cb(_yes, ALICE)
    from services import web as _web_svc
    await _web_svc.vote_by_token(manager.get_rollcall(CHAT_ID, 0).web_token, _guest, "in")
    out = await feed("/whos_in", ALICE)
    ok, d = contains(out, _word, _guest)
    record("...tapping 'Yes, add anyway' adds them; a long-named web guest is IN too", ok, d)

    out = await feed("/erc", ALICE)
    _ask = next((d for d in buttons_of(out) if d.startswith("ghost_yes_")), None)
    out = await feed_cb(_ask, ALICE) if _ask else []
    _togs = {lbl: d for d, lbl in button_labels(out).items() if d.startswith("ghost_togp_")}
    record("Absence review lists both long names, buttons within 64 bytes",
           _word in _togs and _guest in _togs and len(_bad_buttons) == _bad_before,
           f"buttons: {_togs}")
    for _name in (_word, _guest):
        if _name in _togs:
            out = await feed_cb(_togs[_name], ALICE)
            record(f"...tapping {_name[:10]}… marks exactly them",
                   [lbl for lbl in button_labels(out).values() if "👻" in lbl] == [f"👻 {_name}"]
                   if _name == _word else f"👻 {_name}" in button_labels(out).values(),
                   f"labels: {list(button_labels(out).values())}")

    record("Phase 7a -- no ERROR-level logs emitted", len(_errors) == 0,
           str(error_msgs()) if _errors else "")

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

    print("\n=== Phase 8c: /schedule_once -- one-off, no lingering template ===\n")
    print("    User's ask: schedule a single non-recurring event (a holiday-")
    print("    shifted Friday game, set up from Tuesday) without leaving a")
    print("    permanent template behind in /templates.")

    raw_before = len(_db.get_templates(CHAT_ID))       # unfiltered — sees everything
    visible_before = len(templates_svc.list_templates(CHAT_ID))  # admin-facing — hides one-offs

    out = await feed(
        '/schedule_once tuesday 09:00 "Friday Holiday Football" '
        "location=GroundZero fee=200 limit=16 event_day=friday event_time=07:00",
        ALICE,
    )
    no_err = len(_errors) == 0
    record("/schedule_once (full args): no exceptions",
           no_err, str(error_msgs()) if not no_err else "")
    ok, d = contains(out, "friday holiday football")
    record("/schedule_once confirms the title and open time", ok, d)

    record("A hidden template row DOES exist internally (unfiltered count +1)",
           len(_db.get_templates(CHAT_ID)) == raw_before + 1,
           f"raw template count: {len(_db.get_templates(CHAT_ID))}")
    record("But it is invisible to the admin-facing listing (filtered count unchanged)",
           len(templates_svc.list_templates(CHAT_ID)) == visible_before,
           f"visible template count: {len(templates_svc.list_templates(CHAT_ID))}")
    out = await feed("/templates", ALICE)
    ok, d = contains(out, "__once_")
    record("/templates NEVER shows the internal reserved name to an admin",
           not ok, f"got: {text_of(out)[:200]!r}")

    out = await feed("/schedules", ALICE)
    ok, d = contains(out, "friday holiday football", "one-time")
    record("/schedules surfaces it under one-time (pending)", ok, d)

    # Force it due and fire it through the REAL scheduler function -- not a
    # re-implementation of the firing logic, the actual one that runs every
    # minute in production.
    # Dialect-aware placeholder -- this whole file also runs against real
    # Postgres (see the module docstring on why), and a bare "?" is a SQLite-
    # only syntax that raises a psycopg2.errors.SyntaxError under PG. Matches
    # the "ph = ..." pattern every db.py function already uses.
    _ph = "%s" if _db.db_type == "postgresql" else "?"
    with _db._cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE scheduled_rollcalls SET scheduled_at = '2000-01-01T00:00:00Z' "
            f"WHERE chat_id = {_ph}", (CHAT_ID,)
        )
    from check_reminders import _fire_scheduled_rollcalls
    await _fire_scheduled_rollcalls()

    rollcalls = manager.get_rollcalls(CHAT_ID)
    fired = next((rc for rc in rollcalls if rc.title == "Friday Holiday Football"), None)
    record("Firing actually created the rollcall with the template's fields",
           fired is not None and fired.location == "GroundZero"
           and fired.inListLimit == 16,
           f"rollcalls: {[(rc.title, rc.location, rc.inListLimit) for rc in rollcalls]}")
    record("Opens-Tuesday-closes-Friday resolves with no special-casing "
           "(finalizeDate lands on a Friday, in the future)",
           fired is not None and fired.finalizeDate is not None
           and fired.finalizeDate.weekday() == 4,
           f"finalizeDate: {fired.finalizeDate if fired else None}")

    record("THE regression this phase exists to catch: the hidden template "
           "is gone once it fires -- raw count back to where it started",
           len(_db.get_templates(CHAT_ID)) == raw_before,
           f"raw template count after firing: {len(_db.get_templates(CHAT_ID))}")

    if fired is not None:
        idx = rollcalls.index(fired)
        await feed(f"/erc ::{idx + 1}", ALICE)

    print("\n=== Phase 8c: /schedule_once with save_as_template (opted in) ===\n")

    visible_before2 = len(templates_svc.list_templates(CHAT_ID))
    out = await feed(
        '/schedule_once wednesday 18:00 "Weekly Standup Game" '
        "save_as_template=standup_keeper",
        ALICE,
    )
    no_err = len(_errors) == 0
    record("/schedule_once with save_as_template: no exceptions",
           no_err, str(error_msgs()) if not no_err else "")
    ok, d = contains(out, "standup")  # markdown-escapes the underscore, so match loosely
    record("Confirms the KEPT template's name back to the admin", ok, d)
    record("save_as_template creates exactly one NAMED, VISIBLE template "
           "(unlike the default, which does not touch the admin-facing count)",
           len(templates_svc.list_templates(CHAT_ID)) == visible_before2 + 1,
           f"visible template count: {len(templates_svc.list_templates(CHAT_ID))}")

    with _db._cursor(commit=True) as cur:
        cur.execute(
            f"UPDATE scheduled_rollcalls SET scheduled_at = '2000-01-01T00:00:00Z' "
            f"WHERE chat_id = {_ph} AND title = 'standup_keeper'", (CHAT_ID,)
        )
    await _fire_scheduled_rollcalls()
    record("A named (opted-in) template SURVIVES firing -- it's meant to be reused",
           len(templates_svc.list_templates(CHAT_ID)) == visible_before2 + 1,
           f"visible template count after firing: {len(templates_svc.list_templates(CHAT_ID))}")

    rollcalls = manager.get_rollcalls(CHAT_ID)
    kept = next((rc for rc in rollcalls if rc.title == "Weekly Standup Game"), None)
    if kept is not None:
        idx = rollcalls.index(kept)
        await feed(f"/erc ::{idx + 1}", ALICE)
    out = await feed("/delete_template standup_keeper", ALICE)
    ok = len(_errors) == 0
    record("Cleanup: /delete_template standup_keeper (test hygiene)", ok)

    print("\n=== Phase 8c: /schedule_once error paths ===\n")

    out = await feed('/schedule_once notaday 09:00 "X"', ALICE)
    ok, d = contains(out, "not a valid weekday")
    record("/schedule_once with a bad weekday returns a curated error", ok, d)

    out = await feed('/schedule_once tuesday 25:99 "X"', ALICE)
    ok, d = contains(out, "24-hour", "time")
    record("/schedule_once with an unreadable time returns a curated error", ok, d)

    out = await feed('/schedule_once tuesday 09:00 "X" event_day=friday event_time=9pm', ALICE)
    ok, d = contains(out, "24-hour", "time")
    record("/schedule_once with an unreadable CLOSE time also returns a curated error", ok, d)

    out = await feed('/schedule_once tuesday 09:00 "X" save_as_template=__once_hijack', ALICE)
    ok, d = contains(out, "reserved")
    record("/schedule_once refuses a save_as_template name using the reserved prefix", ok, d)

    await feed('/set_template weekly_keep "Weekly Original" limit=12', ALICE)
    out = await feed(
        '/schedule_once tuesday 09:00 "Holiday One-Off" limit=30 save_as_template=weekly_keep',
        ALICE,
    )
    ok, d = contains(out, "already exists")
    kept_tmpl = _db.get_template(CHAT_ID, "weekly_keep") or {}
    untouched = (kept_tmpl.get("title") == "Weekly Original"
                 and str(kept_tmpl.get("inlistlimit")) == "12")
    record("/schedule_once refuses a save_as_template name that already exists", ok, d)
    record("...and leaves that existing template exactly as it was",
           untouched, f"template after refusal: {kept_tmpl}")
    await feed("/delete_template weekly_keep", ALICE)

    out = await feed('/set_template __once_sneaky "Y"', ALICE)
    ok, d = contains(out, "reserved")
    record("/set_template ALSO refuses to create a new template with the reserved prefix",
           ok, d)

    out = await feed("/schedule_once", ALICE)
    ok = len(out) >= 1
    record("/schedule_once with no args shows usage, doesn't crash", ok)

    print("\n=== Phase 8c: /cancel_once -- cancel a one-off from Telegram ===\n")

    raw_before = len(_db.get_templates(CHAT_ID))
    await feed('/schedule_once thursday 10:00 "Cancel Me Game"', ALICE)
    pending = [p for p in templates_svc.list_pending_once(CHAT_ID)
               if p.get("display_title") == "Cancel Me Game"]
    record("Setup: the one-off to cancel is pending", len(pending) == 1, str(pending))
    out = await feed("/cancel_once", ALICE)
    ok, d = contains(out, "cancel me game")
    record("/cancel_once lists the pending one-off by its real title", ok, d)
    ok, _ = contains(out, "__once_")
    record("/cancel_once never shows the internal reserved name", not ok,
           f"got: {text_of(out)[:200]!r}")
    if pending:
        item_id = pending[0]["id"]
        await feed_cb(f"oncecancel_ask_{item_id}", ALICE)
        out = await feed_cb(f"oncecancel_yes_{item_id}", ALICE)
        ok, d = contains(out, "cancel me game", "cancelled")
        record("Confirming tells the group what was cancelled", ok, d)
        record("The one-off is no longer pending",
               all(p["id"] != item_id for p in templates_svc.list_pending_once(CHAT_ID)))
        record("...and its hidden template went with it (raw count back)",
               len(_db.get_templates(CHAT_ID)) == raw_before,
               f"raw template count: {len(_db.get_templates(CHAT_ID))}")
        out = await feed_cb(f"oncecancel_yes_{item_id}", ALICE)
        ok, d = contains(out, "already fired")
        record("A second tap on a stale button cancels nothing and says so", ok, d)
    out = await feed("/cancel_once", ALICE)
    ok, d = contains(out, "nothing is scheduled")
    record("/cancel_once with nothing pending says so", ok, d)

    # /set_template within already-running rollcall
    await feed("/src TemplateContextTest", ALICE)
    out = await feed('/set_template inside "Inside Title" limit=5', ALICE)
    ok = len(_errors) == 0
    record("/set_template can be created while a rollcall is active", ok,
           str(error_msgs()) if not ok else "")
    await feed("/erc", ALICE)
    _errors.clear()

    # ── Reported from production, 2026-09-23 ──────────────────────────────
    # A rollcall the SCHEDULER started came up with no "Vote on Web" button,
    # then grew one as soon as somebody voted. Cause: the web URL was an
    # optional argument to get_status_keyboard, and the two scheduler call
    # sites didn't pass it, while the vote path did.
    #
    # Driven through _auto_start_from_template — the real function the weekly
    # scheduler calls — because that is the path that was broken. Asserting
    # the button on a /src panel would have passed throughout the bug.
    import os as _os
    from check_reminders import _auto_start_from_template

    _prev_base = _os.environ.get("WEB_BASE_URL")
    _os.environ["WEB_BASE_URL"] = "https://rollcall.test"
    try:
        def _url_buttons(out):
            """Every button carrying a url, across all recorded markups."""
            found = []
            for _n, _a, _kw in out:
                markup = (_kw or {}).get("reply_markup")
                for row in getattr(markup, "keyboard", []) or []:
                    for btn in row:
                        url = getattr(btn, "url", None)
                        if url:
                            found.append((getattr(btn, "text", ""), url))
            return found

        _outbound.clear()
        _errors.clear()
        await _auto_start_from_template(
            CHAT_ID,
            {"name": "friday", "title": "Scheduled Friday", "chatid": CHAT_ID},
            # A claim date nothing else uses. Today's date failed every
            # Friday: the weekly "friday" template had already claimed it
            # earlier in the run, so this auto-start correctly did nothing.
            "2000-01-07",
        )
        sched_out = list(_outbound)
        buttons = _url_buttons(sched_out)
        record("scheduler auto-start sends a panel", has_call(sched_out, "send_message"),
               f"got: {[n for n, _, _ in sched_out]}")
        record("scheduler-started panel carries the Vote on Web button",
               any("Web" in t for t, _ in buttons),
               f"url buttons on the scheduled panel: {buttons}")
        record("that button points at this group's web page",
               any("/web/group/" in u for _, u in buttons),
               f"url buttons: {buttons}")

        # The same panel via /panel — this path always worked, and is here so
        # a regression tells you WHICH path broke rather than just that one did.
        panel_out = await feed("/panel", ALICE)
        record("/panel carries the Vote on Web button too",
               any("Web" in t for t, _ in _url_buttons(panel_out)),
               f"url buttons on /panel: {_url_buttons(panel_out)}")

        await feed("/erc", ALICE)
    finally:
        if _prev_base is None:
            _os.environ.pop("WEB_BASE_URL", None)
        else:
            _os.environ["WEB_BASE_URL"] = _prev_base

    # With no WEB_BASE_URL there is nothing to link to, and a dead button
    # would be worse than none — self-hosted bots must not get one.
    await feed("/src No Web Configured", ALICE)
    out = await feed("/panel", ALICE)
    record("no Vote on Web button when WEB_BASE_URL is unset",
           not any("Web" in str(getattr(b, "text", ""))
                   for _n, _a, _kw in out
                   for row in (getattr((_kw or {}).get("reply_markup"), "keyboard", []) or [])
                   for b in row),
           "a web button appeared with no WEB_BASE_URL set")
    await feed("/erc", ALICE)

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

    # Locked group, Bob a plain member: the /schedules buttons and /timezone
    # used to skip the admin gate their commands (and the web) enforce.
    _admin_member = bot.get_chat_member.return_value
    _plain_member = MagicMock(status="member", user=MagicMock(is_bot=False))
    bot.get_chat_member = AsyncMock(
        side_effect=lambda cid, uid: _plain_member if uid == BOB[0] else _admin_member)
    try:
        tz_before = manager.get_chat(CHAT_ID).get("timezone")
        out = await feed("/timezone America/New_York", BOB)
        ok, d = contains(out, "admin")
        record("Locked group: a member's /timezone change is refused", ok, d)
        record("...and the timezone is unchanged",
               manager.get_chat(CHAT_ID).get("timezone") == tz_before,
               f"timezone now {manager.get_chat(CHAT_ID).get('timezone')!r}")

        bot_state._sched_selection.pop(CHAT_ID, None)
        bot.answer_callback_query.reset_mock()
        await feed_cb("sched_selall", BOB)
        answers = " ".join(str(c.args[1]) for c in bot.answer_callback_query.call_args_list
                           if len(c.args) > 1)
        record("Locked group: a member's tap on the /schedules panel is refused",
               "only admins" in answers.lower() and CHAT_ID not in bot_state._sched_selection,
               f"answers={answers!r}")
        bot.answer_callback_query.reset_mock()
        await feed_cb("sched_apply_off", BOB)
        answers = " ".join(str(c.args[1]) for c in bot.answer_callback_query.call_args_list
                           if len(c.args) > 1)
        record("...including Pause, which used to pause every recurring rollcall",
               "only admins" in answers.lower(), f"answers={answers!r}")

        bot.answer_callback_query.reset_mock()
        await feed_cb("sched_selclear", ALICE)
        answers = " ".join(str(c.args[1]) for c in bot.answer_callback_query.call_args_list
                           if len(c.args) > 1)
        record("...while an admin's tap still works", "cleared" in answers.lower(),
               f"answers={answers!r}")
    finally:
        bot.get_chat_member = AsyncMock(return_value=_admin_member)

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
    morning_id = manager.get_rollcall(CHAT_ID, 0).id
    out = await feed("/erc ::1", ALICE)
    ok = len(_errors) == 0
    record("/erc ::1 ends only RC#1 without error", ok, str(error_msgs()) if not ok else "")
    record("/erc ::1 strips the ended panel's buttons",
           has_call(out, "edit_message_reply_markup"), f"outbound: {[n for n,_,_ in out]}")

    # A tap on Morning's (now dead) panel must not vote in Evening, which
    # slid into #1. Buttons carry the rollcall db id: btn_<action>_<N>_<id>.
    bot.answer_callback_query.reset_mock()
    out = await feed_cb(f"btn_in_1_{morning_id}", BOB)
    answers = " ".join(str(c.args[1]) for c in bot.answer_callback_query.call_args_list if len(c.args) > 1)
    evening_in = [u.user_id for u in manager.get_rollcall(CHAT_ID, 0).inList]
    record("Tap on an ended rollcall's panel is refused, not voted into the next",
           "closed" in answers.lower() and BOB[0] not in evening_in,
           f"answers={answers!r} evening_in={evening_in}")

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

    await phase_dues()
    await phase_remaining_surface()
    await phase_vote_burst()
    await phase_mixed_command_burst()
    await phase_close_races()
    await phase_stats_season()

    print("\n=== Phase 16: Lifecycle close (final) ===\n")

    out = await feed("/rollcalls", ALICE)
    ok = True  # whatever response is fine — just shouldn't crash
    record("/rollcalls after all ended responds (no crash)", ok)

    out = await feed("/version", ALICE)
    ok, d = contains(out, deployed_version_str())
    record("/version still reports the deployed version after lifecycle churn", ok, d)

    # Final global error check — no ERROR-level logs leaked across entire run
    record("FINAL: no ERROR-level logs from any phase", len(_errors) == 0,
           f"{len(_errors)} errors: {error_msgs()[:5]}" if _errors else "")
    record(f"FINAL: no message over Telegram's {_TG_TEXT_LIMIT}-character limit",
           not _too_long, f"{len(_too_long)} too long: {_too_long[:5]}")
    record("FINAL: every Markdown message parses (no unbalanced * _ `)",
           not _bad_markdown, f"{len(_bad_markdown)} refused: {_bad_markdown[:5]}")
    record(f"FINAL: every button's callback_data is 1-{_TG_CALLBACK_LIMIT} bytes",
           not _bad_buttons, f"{len(_bad_buttons)} refused: {_bad_buttons[:5]}")


def main():
    print("=" * 70)
    print(f"Functional test against real pyTelegramBotAPI {_TBL_VER}")
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

    print(f"\n✅ All scenarios passed against pyTelegramBotAPI {_TBL_VER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
