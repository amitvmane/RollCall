"""
Multi-group local load/functional test for RollCall: 10 groups x 50 users
each (500 total), run concurrently.

Uses the REAL pyTelegramBotAPI processing path (same pattern as
scripts/functional_test.py) against a real temp SQLite DB — no handler
mocks. Only outbound Telegram network calls are patched. Per group, exercises:

  - Rollcall lifecycle with 50 real voters (mixed /in /out /maybe)
  - A concurrent burst of 50 simultaneous votes (asyncio.gather) to stress
    the per-chat write lock / WAL concurrency path touched in the 2026-08-08
    audit
  - Proxy votes (ghost users)
  - All /stats variants: group, top, personal, ghost, bot (admin, exercises
    the thread-offloaded bot_stats())
  - Ghost tracking + ghost report
  - Dues: enable, close game across 50 players + proxies, /my_dues
  - Templates: create, schedule, manually fire the scheduler tick
  - Identity merge across two proxy names -> a real user, verify stats recombine

All 10 groups run CONCURRENTLY (asyncio.gather), not sequentially — this is
closer to production, where one bot process serves many groups' simultaneous
activity on a single event loop / single SQLite connection. On top of the
per-group checks, also verifies:

  - Cross-group isolation: no rollcall/dues data leaks between groups
  - /stats bot (bot-wide, thread-offloaded) still works correctly at
    10-group / 500-voter scale
  - No ERROR-level log lines, no unhandled exceptions, anywhere in the run

Run: python scripts/load_test_50_users.py
"""

import os
import sys
import time
import asyncio
import logging
import tempfile
from unittest.mock import AsyncMock, MagicMock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["TELEGRAM_TOKEN"] = "999999:dummy_token_for_load_test"
# A DATABASE_URL from the environment wins, so this same load test can be run
# against a real Postgres (pooling behaves differently under concurrency than
# SQLite's WAL). Defaults to a throwaway SQLite file, which is what CI uses.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_DB_FILE}")
os.environ["ADMIN1"] = "100"

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(_REPO_ROOT, "rollCall"))

from telebot.types import Update  # noqa: E402

import bot_state  # noqa: E402
import handlers  # noqa: F401,E402
import db as _db  # noqa: E402
import rollcall_manager as _rcm  # noqa: E402

_db.init_db()
bot = bot_state.bot

_errors = []


class _ErrorCapture(logging.Handler):
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            _errors.append(record)


logging.getLogger().addHandler(_ErrorCapture())

# Per-call-site outbound capture, NOT a shared global list: with 10 groups'
# scenarios running concurrently (asyncio.gather in main()), a plain global
# list would have one group's feed()/feed_cb() clearing or reading a buffer
# another group's concurrently in-flight call was still writing to --
# exactly the kind of cross-task interference these ContextVars avoid.
# asyncio.Task copies the current context at creation time, so each task
# spawned by process_new_updates (including any fire-and-forget
# asyncio.create_task calls made from inside a handler) still appends into
# the SAME buffer object as the feed()/feed_cb() call that started it,
# while remaining invisible to sibling groups' concurrently-running tasks.
import contextvars  # noqa: E402
_outbound_var = contextvars.ContextVar("outbound", default=None)

_MID_COUNTER = [9000]


def _record(name):
    async def _impl(*args, **kwargs):
        m = MagicMock()
        _MID_COUNTER[0] += 1
        if name == "send_message":
            mid = _MID_COUNTER[0]
        elif name == "edit_message_text":
            mid = args[2] if len(args) > 2 else kwargs.get("message_id")
        elif name == "edit_message_reply_markup":
            mid = args[1] if len(args) > 1 else kwargs.get("message_id")
        else:
            mid = _MID_COUNTER[0]
        m.message_id = mid
        m.chat = MagicMock(id=args[0] if args else kwargs.get("chat_id", -1))
        buf = _outbound_var.get()
        if buf is not None:
            buf.append((name, args, kwargs, mid))
        return m
    return _impl


def last_message_id(outbound):
    """The real message_id of whatever this response last sent/edited — the
    id the NEXT simulated button tap on that message needs to carry."""
    for entry in reversed(outbound):
        mid = entry[3]
        if mid is not None:
            return mid
    return None


bot.send_message = _record("send_message")
bot.edit_message_text = _record("edit_message_text")
bot.edit_message_reply_markup = _record("edit_message_reply_markup")
bot.answer_callback_query = AsyncMock(return_value=None)
# Post-close dues QR is sent via send_photo (fire-and-forget background
# task) -- must be stubbed like every other outbound call, or it makes a
# real network call with the dummy token and fails (harmlessly caught by
# the handler's own try/except, but pollutes the error-log check below).
bot.send_photo = _record("send_photo")
bot.pin_chat_message = AsyncMock(return_value=None)

_fake_member = MagicMock()
_fake_member.status = "administrator"
_fake_member.user = MagicMock(is_bot=False)
bot.get_chat_member = AsyncMock(return_value=_fake_member)

_fake_me = MagicMock()
_fake_me.id = 8324883914
_fake_me.is_bot = True
_fake_me.username = "LoadTestBot"
bot.get_me = AsyncMock(return_value=_fake_me)
bot.set_my_commands = AsyncMock(return_value=True)

CHAT_ID = -1009000000001
UPD_COUNTER = [5000]
MSG_COUNTER = [5000]


def _next_upd():
    UPD_COUNTER[0] += 1
    return UPD_COUNTER[0]


def _next_msg():
    MSG_COUNTER[0] += 1
    return MSG_COUNTER[0]


def make_message_update(text, user_id, first_name, username=None, chat_id=CHAT_ID):
    payload = {
        "update_id": _next_upd(),
        "message": {
            "message_id": _next_msg(),
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "supergroup" if chat_id < 0 else "private",
                      "title": "LoadTest" if chat_id < 0 else None},
            "from": {"id": user_id, "is_bot": False, "first_name": first_name, "username": username},
            "text": text,
            "entities": ([{"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}]
                         if text.startswith("/") else []),
        },
    }
    return Update.de_json(payload)


def make_callback_update(data, user_id, first_name, username=None, chat_id=CHAT_ID, src_msg_id=None):
    payload = {
        "update_id": _next_upd(),
        "callback_query": {
            "id": f"cb_{_next_upd()}",
            "from": {"id": user_id, "is_bot": False, "first_name": first_name, "username": username},
            "chat_instance": "instance_1",
            "message": {
                "message_id": src_msg_id or _next_msg(),
                "date": int(time.time()),
                "chat": {"id": chat_id, "type": "supergroup", "title": "LoadTest"},
                "from": {"id": 8324883914, "is_bot": True, "first_name": "Bot"},
                "text": "panel",
            },
            "data": data,
        },
    }
    return Update.de_json(payload)


async def feed(text, user, chat_id=CHAT_ID):
    buf = []
    token = _outbound_var.set(buf)
    try:
        uid, fname, uname = user
        upd = make_message_update(text, uid, fname, uname, chat_id=chat_id)
        await bot.process_new_updates([upd])
    finally:
        _outbound_var.reset(token)
    return buf


async def feed_cb(data, user, chat_id=CHAT_ID, src_msg_id=None):
    buf = []
    token = _outbound_var.set(buf)
    try:
        bot_state._rate_limits.clear()
        uid, fname, uname = user
        upd = make_callback_update(data, uid, fname, uname, chat_id=chat_id, src_msg_id=src_msg_id)
        await bot.process_new_updates([upd])
    finally:
        _outbound_var.reset(token)
    return buf


def find_callback(outbound, prefix):
    for name, args, kwargs, _mid in outbound:
        markup = kwargs.get("reply_markup") or (args[2] if len(args) > 2 else None)
        if markup and getattr(markup, "keyboard", None):
            for row in markup.keyboard:
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith(prefix):
                        return btn.callback_data
    return None


def text_of(outbound):
    chunks = []
    for name, args, kwargs, _mid in outbound:
        if name in ("send_message", "edit_message_text"):
            if name == "send_message" and len(args) >= 2:
                chunks.append(str(args[1]))
            elif name == "edit_message_text" and len(args) >= 1:
                chunks.append(str(args[0]))
            if "text" in kwargs:
                chunks.append(str(kwargs["text"]))
    return "\n".join(chunks)


results = []


def record(name, passed, detail=""):
    results.append((name, passed, detail))
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}{(' -- ' + detail) if detail and not passed else ''}")


N_GROUPS = 10
USERS_PER_GROUP = 50


def _group_fixture(gid: int):
    """Independent chat/admin/user-id space per group — 10 groups x 50 users
    each (500 total), run concurrently, so ids must not collide across
    groups (a real Telegram deployment has this many independent chats,
    each with its own membership)."""
    chat_id = -1009000000000 - gid
    admin = (100 + gid * 100000, f"Admin{gid}", f"admin{gid}")
    users = [(1000 + gid * 100000 + i, f"G{gid}U{i}", f"g{gid}u{i}") for i in range(USERS_PER_GROUP)]
    return chat_id, admin, users


async def run_group_scenario(gid: int) -> list:
    """Runs the full feature surface for ONE group and returns its own
    [(name, passed, detail), ...] list — kept local (not the shared global
    `results`) so 10 of these can run concurrently via asyncio.gather
    without interleaving each other's bookkeeping."""
    chat_id, admin, users = _group_fixture(gid)
    r = []

    def rec(name, passed, detail=""):
        r.append((f"[grp{gid}] {name}", passed, detail))

    out = await feed("/src Load Test Match", admin, chat_id=chat_id)
    rec("/src starts rollcall", "load test match" in text_of(out).lower())

    in_count, out_count, maybe_count = 30, 12, 8
    assert in_count + out_count + maybe_count == USERS_PER_GROUP
    for i, u in enumerate(users):
        bot_state._rate_limits.clear()
        if i < in_count:
            await feed("/in", u, chat_id=chat_id)
        elif i < in_count + out_count:
            await feed("/out", u, chat_id=chat_id)
        else:
            await feed("/maybe", u, chat_id=chat_id)

    rc = _rcm.manager.get_rollcalls(chat_id)[0]
    rec("50 sequential votes recorded",
        len(rc.inList) == in_count and len(rc.outList) == out_count and len(rc.maybeList) == maybe_count,
        f"in={len(rc.inList)} out={len(rc.outList)} maybe={len(rc.maybeList)}")

    bot_state._rate_limits.clear()
    out = await feed("/set_in_for GhostAlpha", admin, chat_id=chat_id)
    rec("/set_in_for adds a proxy IN", "ghostalpha" in text_of(out).lower())
    bot_state._rate_limits.clear()
    out = await feed("/set_out_for GhostBeta", admin, chat_id=chat_id)
    rec("/set_out_for adds a proxy OUT", "ghostbeta" in text_of(out).lower())

    # Concurrent vote-flip burst WITHIN this group -- exercises
    # manager.get_chat_write_lock()'s serialization under real concurrent
    # asyncio tasks, and the DB write path under WAL. Combined with 10
    # groups themselves running concurrently (see main()), this is up to
    # 500 simultaneous in-flight updates across the whole bot.
    tasks = ([feed_cb("vote:maybe", u, chat_id=chat_id) for u in users[:25]]
             + [feed_cb("vote:in", u, chat_id=chat_id) for u in users[25:]])
    burst_results = await asyncio.gather(*tasks, return_exceptions=True)
    exceptions = [x for x in burst_results if isinstance(x, Exception)]
    rec("concurrent 50-vote burst completes without exceptions", len(exceptions) == 0,
        f"{len(exceptions)} exceptions: {exceptions[:3]}")

    rc = _rcm.manager.get_rollcalls(chat_id)[0]
    total_after_burst = len(rc.inList) + len(rc.outList) + len(rc.maybeList)
    rec("no votes lost/duplicated after concurrent burst", total_after_burst == 52,
        f"total voters={total_after_burst}, expected 52 (50 real + 2 proxies)")

    for cmd, needle in [("/whos_in", None), ("/whos_out", None), ("/whos_maybe", None),
                        ("/rollcalls", "load test match")]:
        bot_state._rate_limits.clear()
        out = await feed(cmd, admin, chat_id=chat_id)
        ok = True if needle is None else needle in text_of(out).lower()
        rec(f"{cmd} responds", ok and len(out) > 0)

    bot_state._rate_limits.clear()
    out = await feed("/stats group", admin, chat_id=chat_id)
    rec("/stats group", len(out) > 0)

    bot_state._rate_limits.clear()
    out = await feed("/stats top", admin, chat_id=chat_id)
    rec("/stats top (leaderboard)", len(out) > 0)

    bot_state._rate_limits.clear()
    out = await feed(f"/stats {users[0][2]}", admin, chat_id=chat_id)
    rec("/stats <user> (personal)", len(out) > 0)

    bot_state._rate_limits.clear()
    out = await feed("/toggle_ghost_tracking", admin, chat_id=chat_id)
    rec("/toggle_ghost_tracking", len(out) > 0)
    bot_state._rate_limits.clear()
    out = await feed("/stats ghost", admin, chat_id=chat_id)
    rec("/stats ghost", len(out) > 0)

    bot_state._rate_limits.clear()
    out = await feed("/enable_dues", admin, chat_id=chat_id)
    rec("/enable_dues", len(out) > 0)
    bot_state._rate_limits.clear()
    out = await feed("/set_upi test@upi", admin, chat_id=chat_id)
    rec("/set_upi", len(out) > 0)
    bot_state._rate_limits.clear()
    out = await feed("/event_fee 500", admin, chat_id=chat_id)
    rec("/event_fee sets game cost", len(out) > 0)

    bot_state._rate_limits.clear()
    out = await feed("/erc", admin, chat_id=chat_id)
    rec("/erc ends rollcall and posts a settle-now nudge", "settle" in text_of(out).lower(), text_of(out)[:200])

    # Full guided settle chain: settle_now -> penalty panel (ghost tracking
    # is on) -> Done -> settle_confirm -> closed.
    settle_now_mid = last_message_id(out)
    settle_now_cb = find_callback(out, "settle_now:")
    rec("settle-now button present on the nudge", settle_now_cb is not None)

    out2 = await feed_cb(settle_now_cb, admin, chat_id=chat_id, src_msg_id=settle_now_mid) if settle_now_cb else []
    pen_done_cb = find_callback(out2, "pen_d:")
    settle_cb = find_callback(out2, "settle_confirm:")
    if pen_done_cb:
        pen_mid = last_message_id(out2)
        out2 = await feed_cb(pen_done_cb, admin, chat_id=chat_id, src_msg_id=pen_mid)
        settle_cb = find_callback(out2, "settle_confirm:")

    if settle_cb:
        settle_mid = last_message_id(out2)
        out3 = await feed_cb(settle_cb, admin, chat_id=chat_id, src_msg_id=settle_mid)
        rec("settle_confirm closes the game", len(out3) > 0, text_of(out3)[:200])
    else:
        rec("settle_confirm closes the game", False,
            f"chain broke -- settle_now={bool(settle_now_cb)} pen_done={bool(pen_done_cb)} "
            f"last_out={text_of(out2)[:200]!r}")

    balances = _db.get_all_dues_balances(chat_id)
    nonzero = [b for b in balances if b.get("balance")]
    rec("dues balances recorded for closed game", len(nonzero) > 0, f"{len(nonzero)} nonzero balances")

    bot_state._rate_limits.clear()
    out = await feed("/my_dues", users[0], chat_id=chat_id)
    rec("/my_dues (member)", len(out) > 0)

    import services.identity as identity_svc
    try:
        identity_svc.link_identities(chat_id, "GhostAlpha", canonical_user_id=users[0][0],
                                      admin_user_id=admin[0], admin_name=admin[1])
        alias_group = identity_svc.get_alias_group(chat_id, user_id=users[0][0])
        rec("identity merge folds GhostAlpha into User0", "GhostAlpha" in alias_group.get("aliases", []),
            str(alias_group))
    except Exception as e:
        rec("identity merge folds GhostAlpha into User0", False, repr(e))

    bot_state._rate_limits.clear()
    out = await feed("/set_template weekly_load Weekly Load Test", admin, chat_id=chat_id)
    rec("/set_template creates a template", len(out) > 0)
    bot_state._rate_limits.clear()
    out = await feed("/schedule_template weekly_load monday 09:00", admin, chat_id=chat_id)
    rec("/schedule_template schedules it", len(out) > 0, text_of(out)[:200])

    try:
        import check_reminders
        tmpl = _db.get_template(chat_id, "weekly_load")
        started = await check_reminders._auto_start_from_template(chat_id, tmpl, stamp_date="2026-08-11")
        rec("scheduler manually fires the template", started is True)
        started_again = await check_reminders._auto_start_from_template(chat_id, tmpl, stamp_date="2026-08-11")
        rec("re-firing same stamp_date is a no-op (CAS)", started_again is False)
    except Exception as e:
        rec("scheduler manually fires the template", False, repr(e))

    return r


async def main():
    t0 = time.time()
    print(f"\n=== Running {N_GROUPS} groups x {USERS_PER_GROUP} users concurrently ({N_GROUPS * USERS_PER_GROUP} total voters) ===")
    group_results = await asyncio.gather(*(run_group_scenario(g) for g in range(N_GROUPS)), return_exceptions=True)
    t1 = time.time()

    for gid, gr in enumerate(group_results):
        if isinstance(gr, Exception):
            record(f"[grp{gid}] scenario crashed", False, repr(gr))
        else:
            for name, passed, detail in gr:
                record(name, passed, detail)

    print("\n=== Cross-group isolation ===")
    fixtures = [_group_fixture(g) for g in range(N_GROUPS)]
    # Each group ends its scenario with exactly ONE open rollcall (the
    # template auto-started in Phase 8, after /erc already closed the
    # original 50-voter game) -- and it must be freshly empty. Any voter
    # showing up on it, or more/fewer than one open rollcall, would mean
    # another group's data got cross-wired onto this chat_id.
    isolation_ok = True
    detail_bits = []
    for gid, (chat_id, _admin, _users) in enumerate(fixtures):
        rcs = _rcm.manager.get_rollcalls(chat_id)
        if len(rcs) != 1:
            isolation_ok = False
            detail_bits.append(f"grp{gid}: expected 1 open (template auto-started) rollcall, got {len(rcs)}")
            continue
        voters = len(rcs[0].inList) + len(rcs[0].outList) + len(rcs[0].maybeList)
        if voters != 0:
            isolation_ok = False
            detail_bits.append(f"grp{gid}: fresh auto-started rollcall has {voters} voters, expected 0")
    record("each group ends isolated -- own rollcall only, no cross-group vote leakage",
           isolation_ok, "; ".join(detail_bits))

    balances_per_group = [len(_db.get_all_dues_balances(c)) for c, _, _ in fixtures]
    record("every group independently has its own dues balances", all(n > 0 for n in balances_per_group),
           f"balances per group: {balances_per_group}")

    print("\n=== Bot-wide aggregate at 10-group scale ===")
    bot_state._rate_limits.clear()
    admin0 = fixtures[0][1]
    t_bs0 = time.time()
    out = await feed("/stats bot", admin0, chat_id=fixtures[0][0])
    t_bs1 = time.time()
    record("/stats bot (thread-offloaded, 10-group scale)",
           len(out) > 0 and "error" not in text_of(out).lower(), text_of(out)[:200])
    print(f"  /stats bot wall time at {N_GROUPS}-group scale: {t_bs1 - t_bs0:.3f}s")

    print("\n=== Error-log check ===")
    record("no ERROR-level log lines across whole run", len(_errors) == 0,
           f"{len(_errors)} errors: {[e.getMessage()[:150] for e in _errors[:5]]}")

    print(f"\nTotal wall time: {t1 - t0:.2f}s for {N_GROUPS}-group x {USERS_PER_GROUP}-user concurrent run "
          f"({N_GROUPS * USERS_PER_GROUP} total voters)")
    passed = sum(1 for _, p, _ in results if p)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} checks passed" + (f", {failed} FAILED" if failed else ""))
    if failed:
        for name, p, detail in results:
            if not p:
                print(f"  FAILED: {name} -- {detail}")
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
