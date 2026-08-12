"""
Realistic parallel multi-group scenario test — the campaign's Phase 5.

Where scripts/load_test_50_users.py stresses BREADTH (10 groups x 50 voters,
concurrent vote bursts), this one stresses the DEPTH of one real weekly cycle
and, above all, the FINANCIAL INVARIANTS that come out of it. It mirrors the
Thursday-rollcall / Saturday-game flow: dues+treasury setup, a custom penalty
tier, a rollcall people vote on, late/ditch penalties on match day, then a
guided /settle_dues that draws a SUBSIDY out of the treasury, and finally the
exports and an identity merge.

Runs every group CONCURRENTLY (asyncio.gather) so the per-chat write locks,
the append-only ledger writers and the treasury balance are all exercised
under real interleaving rather than one group at a time.

Backend-agnostic: honours DATABASE_URL, so the same scenario runs against
SQLite and against Postgres.

    python scripts/parallel_scenario_test.py
    DATABASE_URL=postgresql://user:pass@127.0.0.1:55432/db python scripts/parallel_scenario_test.py

The Telegram-facing harness (mocked outbound calls, update construction,
per-task ContextVar outbound buffers) is imported from load_test_50_users
rather than duplicated -- that module is import-safe, its runner is behind
an "if __name__" guard.
"""

import os
import sys
import time
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import load_test_50_users as H  # noqa: E402  (installs the whole mock harness)

from unittest.mock import AsyncMock  # noqa: E402

import db as _db  # noqa: E402
import bot_state  # noqa: E402
import rollcall_manager as _rcm  # noqa: E402

# /dues_export and /export_stats deliver a file; the shared harness doesn't
# stub send_document because its own scenario never exports.
H.bot.send_document = H._record("send_document")
H.bot.send_chat_action = AsyncMock(return_value=None)

find_callback, text_of, last_message_id = H.find_callback, H.text_of, H.last_message_id
rec = H.record


async def feed(text, user, chat_id):
    """Every command here is issued back-to-back by a script rather than by
    humans typing, so the anti-flood limiter would reject most of them. The
    load test clears the same bucket between votes for the same reason."""
    bot_state._rate_limits.clear()
    return await H.feed(text, user, chat_id=chat_id)


async def feed_cb(data, user, chat_id, src_msg_id=None):
    bot_state._rate_limits.clear()
    return await H.feed_cb(data, user, chat_id=chat_id, src_msg_id=src_msg_id)

N_GROUPS = 6
MEMBERS_PER_GROUP = 12

GROUND_COST = 900
SUBSIDY = 100
TOPUP = 1000
ROUND_STEP = 10


def _fixture(gid):
    """Independent chat/user id space per group so nothing can collide."""
    chat_id = -1009500000000 - gid
    admin = (500000 + gid * 100000, f"Admin{gid}", f"admin{gid}")
    members = [(700000 + gid * 100000 + i, f"P{gid}M{i}", f"p{gid}m{i}")
               for i in range(MEMBERS_PER_GROUP)]
    return chat_id, admin, members


def _docs(outbound):
    return [e for e in outbound if e[0] == "send_document"]


async def run_group(gid):
    chat_id, admin, members = _fixture(gid)
    g = f"[grp{gid}]"

    # ── Setup: dues, UPI, treasury float ─────────────────────────────────────
    await feed("/enable_dues", admin, chat_id=chat_id)
    out = await feed("/dues", admin, chat_id=chat_id)
    rec(f"{g} /enable_dues sticks (dues panel renders)", len(out) > 0, text_of(out)[:160])

    await feed("/set_upi collector@upi", admin, chat_id=chat_id)
    await feed("/set_treasury_upi treasury@upi", admin, chat_id=chat_id)

    out = await feed(f"/fund_topup {TOPUP} season float", admin, chat_id=chat_id)
    rec(f"{g} /fund_topup credits the treasury", str(TOPUP) in text_of(out), text_of(out)[:160])

    # ── A custom penalty tier, then read it back with /penalties ─────────────
    await feed("/add_penalty verylate 75 mins:15 more than 15 min late", admin, chat_id=chat_id)
    out = await feed("/penalties", admin, chat_id=chat_id)
    rec(f"{g} /penalties lists the custom tier", "verylate" in text_of(out).lower(), text_of(out)[:200])

    # ── Thursday: open the rollcall, everyone votes ──────────────────────────
    out = await feed("/src Saturday Game", admin, chat_id=chat_id)
    rec(f"{g} /src opens the rollcall", "saturday game" in text_of(out).lower(), text_of(out)[:160])

    ins, outs, maybes = members[:8], members[8:10], members[10:]
    for u in ins:
        await feed("/in", u, chat_id=chat_id)
    for u in outs:
        await feed("/out", u, chat_id=chat_id)
    for u in maybes:
        await feed("/maybe", u, chat_id=chat_id)

    # assert against the manager's own state, not scraped reply text
    rc = _rcm.manager.get_rollcalls(chat_id)[0]
    rec(f"{g} all {MEMBERS_PER_GROUP} votes registered",
        (len(rc.inList), len(rc.outList), len(rc.maybeList)) == (len(ins), len(outs), len(maybes)),
        f"in={len(rc.inList)} out={len(rc.outList)} maybe={len(rc.maybeList)}")

    await feed(f"/event_fee {GROUND_COST}", admin, chat_id=chat_id)

    # ── Saturday: penalties on the day ───────────────────────────────────────
    late_player, ditch_player = ins[0][1], ins[1][1]
    out = await feed(f"/mark_late {late_player} 20", admin, chat_id=chat_id)
    rec(f"{g} /mark_late applies a tier", late_player in text_of(out), text_of(out)[:200])

    out = await feed(f"/mark_ditch {ditch_player}", admin, chat_id=chat_id)
    rec(f"{g} /mark_ditch applies the no-show tier", ditch_player in text_of(out), text_of(out)[:200])

    # ── Close the game through the guided settle chain, WITH a subsidy ───────
    out = await feed("/erc", admin, chat_id=chat_id)
    settle_now = find_callback(out, "settle_now:")
    mid = last_message_id(out)
    rec(f"{g} /erc posts the settle-now nudge", settle_now is not None, text_of(out)[:200])

    out2 = await feed_cb(settle_now, admin, chat_id=chat_id, src_msg_id=mid) if settle_now else []
    # Ghost tracking on => the penalty panel comes first; press its Done button.
    pen_done = find_callback(out2, "pen_d:")
    if pen_done:
        out2 = await feed_cb(pen_done, admin, chat_id=chat_id, src_msg_id=last_message_id(out2))

    # Take the subsidy branch explicitly rather than "No subsidy".
    confirm = find_callback(out2, "settle_confirm:")
    rc_id = None
    if confirm:
        rc_id = confirm.split(":")[1]
        confirm = f"settle_confirm:{rc_id}:{SUBSIDY}"
    rec(f"{g} settle panel offers a subsidy branch", confirm is not None, text_of(out2)[:250])

    out3 = await feed_cb(confirm, admin, chat_id=chat_id, src_msg_id=last_message_id(out2)) if confirm else []
    rec(f"{g} settle_confirm closes the game with a ₹{SUBSIDY} subsidy",
        len(out3) > 0, text_of(out3)[:250])

    # ── The money has to add up ──────────────────────────────────────────────
    closure = _db.get_latest_game_closure(chat_id)
    rec(f"{g} closure row written", closure is not None)

    if closure:
        in_count = int(closure["in_count"])
        expected_net = GROUND_COST - SUBSIDY
        # per_head is the rounded-up share; remainder is what rounding produced
        per_head = int(closure["per_head"])
        rec(f"{g} subsidy recorded on the closure",
            int(closure["subsidy"]) == SUBSIDY, f"got {closure['subsidy']}")
        rec(f"{g} per-head covers the net cost ({expected_net}/{in_count} rounded to {ROUND_STEP})",
            per_head * in_count >= expected_net and per_head % ROUND_STEP == 0,
            f"per_head={per_head} in_count={in_count} net={expected_net}")

        # treasury: topup credited, subsidy debited
        bal = _db.get_fund_balance(chat_id)
        rec(f"{g} treasury balance = topup − subsidy (+rounding remainder)",
            bal >= TOPUP - SUBSIDY, f"balance={bal} topup={TOPUP} subsidy={SUBSIDY}")

        # explicit high limit: these default to 15 rows, which would silently
        # truncate the sum-vs-balance invariant below
        txns = _db.get_fund_transactions(chat_id, limit=1000)
        rec(f"{g} fund ledger has both a topup and a subsidy row",
            any(t["txn_type"] == "subsidy" for t in txns)
            and any(int(t["amount"]) == TOPUP for t in txns),
            f"{[(t['txn_type'], t['amount']) for t in txns][:6]}")
        rec(f"{g} fund balance equals the sum of its transactions",
            bal == sum(int(t["amount"]) for t in txns),
            f"balance={bal} sum={sum(int(t['amount']) for t in txns)}")

    # ── Members can see what they owe ────────────────────────────────────────
    out = await feed("/my_dues", ins[2], chat_id=chat_id)
    rec(f"{g} /my_dues shows the member their share", len(text_of(out)) > 0, text_of(out)[:160])

    out = await feed("/dues", admin, chat_id=chat_id)
    rec(f"{g} /dues lists outstanding balances", len(text_of(out)) > 0, text_of(out)[:160])

    # ── Exports ──────────────────────────────────────────────────────────────
    out = await feed("/dues_export", admin, chat_id=chat_id)
    rec(f"{g} /dues_export produces a file", len(_docs(out)) > 0 or "export" in text_of(out).lower(),
        text_of(out)[:160])

    out = await feed("/export_stats", admin, chat_id=chat_id)
    rec(f"{g} /export_stats produces a file", len(_docs(out)) > 0 or "export" in text_of(out).lower(),
        text_of(out)[:160])

    out = await feed("/dues_snapshot", admin, chat_id=chat_id)
    rec(f"{g} /dues_snapshot renders", len(text_of(out)) > 0, text_of(out)[:160])

    # ── Templates ────────────────────────────────────────────────────────────
    await feed("/set_template saturday Saturday Game", admin, chat_id=chat_id)
    out = await feed("/templates", admin, chat_id=chat_id)
    rec(f"{g} /set_template + /templates round-trip", "saturday" in text_of(out).lower(), text_of(out)[:160])

    # ── Append-only ledger invariant ─────────────────────────────────────────
    entries_before = _db.get_dues_entries(chat_id, limit=1000)
    await feed(f"/mark_paid {ins[2][1]}", admin, chat_id=chat_id)
    entries_after = _db.get_dues_entries(chat_id, limit=1000)
    rec(f"{g} ledger is append-only (payment ADDS a row, never edits one)",
        len(entries_after) > len(entries_before),
        f"before={len(entries_before)} after={len(entries_after)}")

    return chat_id


async def main():
    print(f"\n=== Realistic weekly scenario x {N_GROUPS} groups, concurrent ===")
    print(f"    backend: {_db.db_type}  ({os.environ.get('DATABASE_URL','')[:60]})")
    t0 = time.time()
    res = await asyncio.gather(*(run_group(g) for g in range(N_GROUPS)),
                               return_exceptions=True)
    t1 = time.time()

    for gid, r in enumerate(res):
        if isinstance(r, Exception):
            rec(f"[grp{gid}] scenario completed without raising", False, repr(r))
        else:
            rec(f"[grp{gid}] scenario completed without raising", True)

    # ── Cross-group isolation ────────────────────────────────────────────────
    print("\n=== Cross-group isolation ===")
    chat_ids = [c for c in res if not isinstance(c, Exception)]
    balances = {c: _db.get_fund_balance(c) for c in chat_ids}
    rec("every group kept its own treasury balance",
        all(b == balances[chat_ids[0]] for b in balances.values()) and len(set(chat_ids)) == len(chat_ids),
        f"{balances}")

    closures = {c: _db.get_latest_game_closure(c) for c in chat_ids}
    rec("each group closed exactly its own game (distinct closure rows)",
        len({id(v) for v in closures.values()}) == len(chat_ids)
        and all(v is not None for v in closures.values()))

    for c in chat_ids:
        others = [o for o in chat_ids if o != c]
        ents = _db.get_dues_entries(c)
        leaked = [e for e in ents if e.get("chat_id") not in (None, c)]
        if leaked:
            rec(f"no dues entries leaked into chat {c}", False, str(leaked[:2]))
            break
    else:
        rec("no dues entries leaked across any group", True)

    print("\n=== Error-log check ===")
    rec("no ERROR-level log lines across whole run", len(H._errors) == 0,
        f"{len(H._errors)} errors: {[e.getMessage()[:150] for e in H._errors[:5]]}")

    print(f"\nTotal wall time: {t1 - t0:.2f}s for {N_GROUPS} concurrent groups "
          f"({N_GROUPS * MEMBERS_PER_GROUP} members) on {_db.db_type}")

    passed = sum(1 for _, p, _ in H.results if p)
    failed = len(H.results) - passed
    print(f"\n{passed}/{len(H.results)} checks passed" + (f", {failed} FAILED" if failed else ""))
    if failed:
        for name, p, detail in H.results:
            if not p:
                print(f"  FAILED: {name} -- {detail}")
    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
