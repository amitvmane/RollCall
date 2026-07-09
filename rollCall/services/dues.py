"""
Dues & Treasury services — fee splitting, ledger mutations, fund management.

Framework-agnostic: primitives in, dicts out, no telebot, no Markdown.
Every mutating function returns an 'announcement' key with the plain-text
group-post string so handlers can post it without re-building the message.

Append-only ledger invariant: never UPDATE/DELETE dues_entries or
fund_transactions. Corrections are compensating entries (negative amounts).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import db
from bot_state import _esc_md
from exceptions import (
    duesGameAlreadyClosed,
    duesNothingToClose,
    incorrectParameter,
    insufficientPermissions,
    parameterMissing,
)
from rollcall_manager import manager


# ── Pure math ────────────────────────────────────────────────────────────────

def compute_shares(
    ground_cost: int,
    subsidy: int,
    in_count: int,
    step: int = 10,
) -> tuple[int, int]:
    """Return (per_head, remainder) for a game.

    per_head is rounded UP to the nearest `step`.
    remainder = per_head * in_count - net  (always ≥ 0, credited to fund).
    """
    if in_count <= 0:
        raise parameterMissing("No players IN — cannot split costs.")
    if step <= 0:
        step = 1
    net = ground_cost - subsidy
    raw = -(-net // in_count)           # ceiling division, integer-only
    per_head = -(-raw // step) * step   # round raw up to next step
    # Defensive floor — callers are expected to validate subsidy <= ground_cost
    # before calling (close_game does), but a negative per_head should never
    # be possible to produce even if a future caller skips that check.
    per_head = max(per_head, 0)
    remainder = per_head * in_count - net
    return per_head, remainder


# ── Member resolution ────────────────────────────────────────────────────────

def _resolve_member(
    chat_id: int,
    token: str,
    dues_names: list[str] | None = None,
) -> dict:
    """Resolve a name/handle token to a member dict.

    Tries, in order:
      1. @username or first_name match against currently-active chat members
         (real users who've voted at least once and haven't been detected as
         having left the group).
      2. Match against anyone who already has dues ledger history
         (get_all_dues_balances) — covers proxies with prior entries AND real
         users no longer "active" (e.g. left the group after running up a
         balance). Returns their actual historical user_id either way, so a
         departed real user resolves correctly instead of being silently
         downgraded to a proxy-style user_id=None match that then can't find
         their balance (their entries have a real user_id, not None).
      3. Case-insensitive match against `dues_names` — an *additional* name
         list the caller supplies for identities not yet in ledger history,
         e.g. a proxy just /sif'd in for the current game and never charged
         anything before. Callers should pass `_known_proxy_names(chat_id)`
         here, not their own get_all_dues_balances scan (step 2 already
         covers that, with the correct user_id).

    Returns: {'user_id': int|None, 'member_name': str}
    Raises:  incorrectParameter if ambiguous or not found.
    """
    token = token.lstrip("@").strip()
    token_lower = token.lower()

    # 1. Currently-active real users
    active = db.get_active_members(chat_id)
    real_matches = [
        m for m in active
        if (m.get("username") or "").lower() == token_lower
        or (m.get("first_name") or "").lower() == token_lower
    ]
    if len(real_matches) == 1:
        m = real_matches[0]
        return {"user_id": m["user_id"], "member_name": m.get("first_name") or token}
    if len(real_matches) > 1:
        names = ", ".join(m.get("first_name", str(m["user_id"])) for m in real_matches)
        raise incorrectParameter(f"'{token}' matches multiple members: {names}. Use a more specific name.")

    # 2. Ledger history — proxies with prior entries, or real users no longer active
    history = db.get_all_dues_balances(chat_id, nonzero_only=False)
    hist_matches = [r for r in history if (r.get("member_name") or "").lower() == token_lower]
    if len(hist_matches) == 1:
        r = hist_matches[0]
        return {"user_id": r.get("user_id"), "member_name": r["member_name"]}
    if len(hist_matches) > 1:
        names = ", ".join(r["member_name"] for r in hist_matches)
        raise incorrectParameter(f"'{token}' is ambiguous in dues history: {names}.")

    # 3. Extra known names not yet in ledger history
    if dues_names:
        proxy_matches = [n for n in dues_names if n.lower() == token_lower]
        if len(proxy_matches) == 1:
            return {"user_id": None, "member_name": proxy_matches[0]}
        if len(proxy_matches) > 1:
            raise incorrectParameter(f"'{token}' is ambiguous in dues history.")

    raise incorrectParameter(
        f"'{token}' not found. Use the exact first name, @username, or proxy name."
    )


def _known_proxy_names(chat_id: int) -> list[str]:
    """Proxy names resolvable by name even with zero prior ledger history —
    anyone currently IN (or OUT/MAYBE/waitlisted) on an active rollcall, or IN
    on an ended-but-unsettled one. Without this, a first-time proxy (e.g. just
    /sif'd in) can't be targeted by /waive, /add_adhoc, /reimburse,
    /mark_paid, /set_collector, /mark_penalty, or their web-UI equivalents
    until they already have a dues_entries row."""
    names: set[str] = set()
    try:
        for rc in manager.get_rollcalls(chat_id):
            for lst in (rc.inList, rc.outList, rc.maybeList, getattr(rc, "waitList", [])):
                for u in lst:
                    if not isinstance(u.user_id, int):
                        names.add(u.name)
    except Exception:
        logging.exception("_known_proxy_names: active rollcall scan failed")
    try:
        for row in db.get_unsettled_rollcalls(chat_id):
            for r in db.get_rollcall_in_users(row["id"]):
                proxy_name = r.get("proxy_name")
                if proxy_name:
                    names.add(proxy_name)
    except Exception:
        logging.exception("_known_proxy_names: unsettled rollcall scan failed")
    return list(names)


# ── Settings ─────────────────────────────────────────────────────────────────

_UPI_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,}@[a-zA-Z]{2,}$")


def set_upi(chat_id: int, vpa: str, admin_uid: int, admin_name: str) -> dict:
    """Set the fallback group UPI VPA (shown when no collector UPI is set)."""
    vpa = vpa.strip()
    if not _UPI_RE.match(vpa):
        raise incorrectParameter(
            "Invalid UPI VPA. Expected format: yourname@bankname  (e.g. amit@upi, 9876543210@paytm)"
        )
    db.update_chat_settings(chat_id, upi_vpa=vpa)
    db.log_admin_action(chat_id, admin_uid, admin_name, "set_upi", details=vpa)
    return {"upi_vpa": vpa, "announcement": f"💳 Group UPI set: `{vpa}`"}


def set_treasury_upi(chat_id: int, vpa: str, admin_uid: int, admin_name: str) -> dict:
    """Set the treasury UPI — shown on penalty/fund announcements."""
    vpa = vpa.strip()
    if not _UPI_RE.match(vpa):
        raise incorrectParameter(
            "Invalid UPI VPA. Expected format: yourname@bankname  (e.g. treasurer@upi)"
        )
    db.update_chat_settings(chat_id, treasury_upi=vpa)
    db.log_admin_action(chat_id, admin_uid, admin_name, "set_treasury_upi", details=vpa)
    return {"treasury_upi": vpa, "announcement": f"🏦 Treasury UPI set: `{vpa}`"}




def set_round_step(chat_id: int, step: int, admin_uid: int, admin_name: str) -> dict:
    """Set the rounding step for per-head fee calculation."""
    if step <= 0:
        raise incorrectParameter("Round step must be a positive integer.")
    db.update_chat_settings(chat_id, dues_round_step=step)
    db.log_admin_action(chat_id, admin_uid, admin_name, "set_round_step", details=str(step))
    return {"dues_round_step": step, "announcement": f"⚙️ Round step set to ₹{step}"}


def get_dues_settings(chat_id: int) -> dict:
    """Return current dues-related chat settings."""
    row = db.get_or_create_chat(chat_id)
    return {
        "upi_vpa": row.get("upi_vpa"),
        "treasury_upi": row.get("treasury_upi"),
        "dues_round_step": row.get("dues_round_step") or 10,
        "dues_self_paid_mode": row.get("dues_self_paid_mode") or "auto",
    }


def close_preview(chat_id: int) -> dict:
    """Return per-head math for the next closeable game without writing anything.

    Checks the active rollcall first; falls back to the latest ended-but-not-closed
    rollcall. Returns {'available': False} when nothing is closeable.
    """
    settings = get_dues_settings(chat_id)
    step = settings["dues_round_step"]

    active_rcs = manager.get_rollcalls(chat_id)
    if active_rcs:
        rc = active_rcs[0]
        ground_cost = _parse_ground_cost(rc.event_fee)
        in_count = len(rc.inList)
        if ground_cost > 0 and in_count > 0:
            fund = fund_summary(chat_id)
            per_head, remainder = compute_shares(ground_cost, 0, in_count, step)
            return {
                "title": rc.title,
                "ground_cost": ground_cost,
                "in_count": in_count,
                "per_head": per_head,
                "remainder": remainder,
                "fund_balance": fund["fund_balance"],
                "has_active": True,
                "available": True,
            }

    row = db.get_latest_closeable_rollcall(chat_id)
    if not row:
        return {"available": False, "title": "", "ground_cost": 0, "in_count": 0,
                "per_head": 0, "remainder": 0, "fund_balance": 0, "has_active": False}
    ground_cost = _parse_ground_cost(row.get("event_fee"))
    in_members = db.get_rollcall_in_users(row["id"])
    in_count = len(in_members)
    if ground_cost <= 0 or in_count <= 0:
        return {"available": False, "title": row.get("title", ""), "ground_cost": ground_cost,
                "in_count": in_count, "per_head": 0, "remainder": 0,
                "fund_balance": 0, "has_active": False}
    fund = fund_summary(chat_id)
    per_head, remainder = compute_shares(ground_cost, 0, in_count, step)
    return {
        "title": row.get("title", ""),
        "ground_cost": ground_cost,
        "in_count": in_count,
        "per_head": per_head,
        "remainder": remainder,
        "fund_balance": fund["fund_balance"],
        "has_active": False,
        "available": True,
    }


# ── Game close ───────────────────────────────────────────────────────────────

def _parse_ground_cost(event_fee) -> int:
    """Extract the first digit group from event_fee text.

    "600 + shuttles 200" → 600  (not 600200)
    Returns 0 if nothing parseable.
    """
    if not event_fee:
        return 0
    m = re.search(r"\d+", str(event_fee))
    return int(m.group()) if m else 0


def _in_list_from_active_rc(rc) -> list[dict]:
    """Build a flat list of IN members from the in-memory RollCall object."""
    # uid → display name map for owner reference in proxy memos
    uid_to_name = {}
    for u in rc.inList:
        uid = u.user_id if isinstance(u.user_id, int) else None
        if uid is not None:
            uid_to_name[uid] = u.name or u.first_name or str(uid)

    members = []
    for u in rc.inList:
        uid = u.user_id if isinstance(u.user_id, int) else None
        name = u.name or u.first_name or str(uid)
        if uid is None:
            owner_id = None
            if hasattr(rc, "proxy_owners") and rc.proxy_owners:
                owner_id = rc.proxy_owners.get(u.name)
            owner_name = uid_to_name.get(owner_id) if owner_id else None
            members.append({
                "user_id": None, "member_name": name,
                "proxy_owner_id": owner_id, "proxy_owner_name": owner_name,
            })
        else:
            members.append({
                "user_id": uid, "member_name": name,
                "proxy_owner_id": None, "proxy_owner_name": None,
            })
    return members


def _in_list_from_db(rollcall_id: int) -> list[dict]:
    """Build a flat list of IN members from a persisted (ended) rollcall."""
    rows = db.get_rollcall_in_users(rollcall_id)
    uid_to_name = {
        r["user_id"]: (r.get("first_name") or str(r["user_id"]))
        for r in rows if r.get("user_id") is not None
    }
    members = []
    for r in rows:
        if r.get("proxy_name") is not None:
            owner_id = r.get("proxy_owner_id")
            owner_name = uid_to_name.get(owner_id) if owner_id else None
            members.append({
                "user_id": None,
                "member_name": r["proxy_name"],
                "proxy_owner_id": owner_id,
                "proxy_owner_name": owner_name,
            })
        else:
            members.append({
                "user_id": r["user_id"],
                "member_name": r.get("first_name") or str(r["user_id"]),
                "proxy_owner_id": None,
                "proxy_owner_name": None,
            })
    return members


async def close_game(
    chat_id: int,
    subsidy: int,
    admin_uid: int,
    admin_name: str,
    rc_number: int = 0,
    target_rollcall_id: int | None = None,
) -> dict:
    """Financially close a game for a chat.

    Three modes:
    - target_rollcall_id given: close that specific ended-but-unsettled game
      directly (used by /settle_dues's picker, for reaching a game other than
      the latest one).
    - target_rollcall_id not given, active rollcall present: ends it first
      (streak/stats preserved), then closes.
    - target_rollcall_id not given, no active rollcall: closes the latest
      ended-but-not-closed rollcall from DB (back-compat default).

    Returns a dict with: rollcall_id, title, ground_cost, subsidy, per_head,
    remainder, in_count, members (list), fund_balance_after, announcement.

    Raises:
      duesNothingToClose      — nothing to close
      duesGameAlreadyClosed   — already financially closed
      parameterMissing        — event_fee not set / in_count 0
      incorrectParameter      — subsidy out of range
    """
    settings = get_dues_settings(chat_id)
    step = settings["dues_round_step"]

    # ── Resolve rollcall and IN list ─────────────────────────────────────────
    active_rollcalls = [] if target_rollcall_id is not None else manager.get_rollcalls(chat_id)
    rc_db_id: int | None = None
    title: str = ""
    ground_cost: int = 0
    collector_uid: int | None = None
    collector_name: str | None = None
    collector_paid_ground: int = 0
    collector_upi: str | None = None
    end_result: dict | None = None

    _active_rc_idx: int | None = None  # set when an active RC needs ending post-validation

    if target_rollcall_id is not None:
        row = db.get_rollcall(target_rollcall_id)
        if row is None or row.get("chat_id") != chat_id:
            raise duesNothingToClose("That game was not found for this group.")
        rc_db_id = row["id"]
        title = row.get("title") or "<Empty>"
        ground_cost = _parse_ground_cost(row.get("event_fee"))
        collector_uid = row.get("collector_uid")
        collector_name = row.get("collector_name")
        collector_paid_ground = row.get("collector_paid_ground") or 0
        collector_upi = row.get("collector_upi")
        in_members = _in_list_from_db(rc_db_id)
    elif active_rollcalls:
        # Raise early if the requested rollcall slot doesn't exist (fix: was silently
        # falling back to idx=0 and closing the wrong game).
        if rc_number >= len(active_rollcalls):
            raise incorrectParameter(
                f"Rollcall ::{rc_number + 1} does not exist. "
                f"There {'is' if len(active_rollcalls) == 1 else 'are'} "
                f"{len(active_rollcalls)} active rollcall(s)."
            )
        rc = manager.get_rollcall(chat_id, rc_number)
        if rc is None:
            raise duesNothingToClose("No active rollcall found.")
        rc_db_id = getattr(rc, "id", None) or getattr(rc, "db_id", None)
        title = rc.title or "<Empty>"
        ground_cost = _parse_ground_cost(rc.event_fee)
        collector_uid = getattr(rc, "collector_uid", None)
        collector_name = getattr(rc, "collector_name", None)
        collector_paid_ground = getattr(rc, "collector_paid_ground", 0) or 0
        collector_upi = getattr(rc, "collector_upi", None)
        in_members = _in_list_from_active_rc(rc)
        _active_rc_idx = rc_number  # remember for after validation passes
    else:
        # No active rollcall — close latest ended one from DB
        row = db.get_latest_closeable_rollcall(chat_id)
        if row is None:
            raise duesNothingToClose(
                "No game to close. Start and end a rollcall first, or check if it was already closed."
            )
        rc_db_id = row["id"]
        title = row.get("title") or "<Empty>"
        ground_cost = _parse_ground_cost(row.get("event_fee"))
        collector_uid = row.get("collector_uid")
        collector_name = row.get("collector_name")
        collector_paid_ground = row.get("collector_paid_ground") or 0
        collector_upi = row.get("collector_upi")
        in_members = _in_list_from_db(rc_db_id)

    # ── ALL validation before any side effects ───────────────────────────────
    # end_rollcall is deferred until after this block so a validation failure
    # does not permanently end the rollcall with no financial record.

    if rc_db_id and db.get_game_closure(rc_db_id) is not None:
        raise duesGameAlreadyClosed(
            f"'{title}' has already been financially closed. "
            "Use /cancel_game_dues to reverse it first."
        )

    if ground_cost <= 0:
        raise parameterMissing(
            "Ground cost is not set or couldn't be read from the event fee. "
            "Run /ef <amount> on the rollcall before /settle_dues."
        )

    fund_balance = db.get_fund_balance(chat_id)
    if subsidy < 0 or subsidy > min(ground_cost, fund_balance):
        raise incorrectParameter(
            f"Subsidy must be between 0 and min(ground_cost={ground_cost}, fund={fund_balance}) = "
            f"{min(ground_cost, fund_balance)}."
        )

    in_count = len(in_members)
    if in_count == 0:
        raise parameterMissing("No players were IN for this game — cannot split costs.")

    per_head, remainder = compute_shares(ground_cost, subsidy, in_count, step)

    # ── Collector rotation ───────────────────────────────────────────────────
    # When no collector was staged and /rotate_collector is on, auto-assign
    # the next real (Telegram) IN member in uid order, cycling.
    rotation_note = ""
    if not collector_uid and not collector_name:
        chat_row = db.get_or_create_chat(chat_id)
        if chat_row.get("collector_rotation"):
            pick = _next_rotation_collector(in_members, chat_row.get("last_collector_uid"))
            if pick:
                collector_uid, collector_name = pick
                collector_paid_ground = 0
                db.update_chat_settings(chat_id, last_collector_uid=collector_uid)
                rotation_note = " (rotation)"

    # ── End active rollcall NOW — all validation passed ──────────────────────
    end_result: dict | None = None
    if _active_rc_idx is not None:
        from services import rollcalls as _rc_svc
        end_result = await _rc_svc.end_rollcall(
            chat_id, _active_rc_idx, admin_uid, admin_name
        )

    # ── Write closure row ────────────────────────────────────────────────────
    closure_id = db.create_game_closure(
        chat_id=chat_id,
        rollcall_id=rc_db_id,
        title=title,
        ground_cost=ground_cost,
        in_count=in_count,
        subsidy=subsidy,
        per_head=per_head,
        rounding_step=step,
        remainder=remainder,
        closed_by_uid=admin_uid,
        closed_by_name=admin_name,
        collector_uid=collector_uid,
        collector_name=collector_name,
        collector_paid_ground=collector_paid_ground,
        collector_upi=collector_upi,
    )

    # ── Write per-member share entries ───────────────────────────────────────
    for m in in_members:
        uid = m["user_id"]
        name = m["member_name"]
        owner_id = m.get("proxy_owner_id")
        owner_name = m.get("proxy_owner_name")

        if uid is not None:
            # Real user — keyed by user_id
            db.add_dues_entry(
                chat_id, rc_db_id, uid, name,
                "share", per_head, None, admin_uid, admin_name,
            )
        else:
            # All proxies — name-keyed (user_id=None).
            # Owned proxy: memo references the responsible owner so the group
            # knows who to follow up with; they settle privately.
            if owner_id is not None:
                # Format "owner:{uid}:{name}" lets get_proxy_owner_uid parse uid reliably.
                memo = f"owner:{owner_id}:{owner_name or ''}"
            else:
                memo = None
            db.add_dues_entry(
                chat_id, rc_db_id, None, name,
                "share", per_head, memo, admin_uid, admin_name,
            )

    # ── Collector reimbursement (if they fronted ground cost) ────────────────
    if collector_paid_ground and collector_uid:
        # Collector fronted ground_cost; credit them so net = per_head − ground_cost
        db.add_dues_entry(
            chat_id, rc_db_id, collector_uid, collector_name or "Collector",
            "reimbursement", -ground_cost,
            f"fronted ground cost ₹{ground_cost}", admin_uid, admin_name,
        )

    # ── Fund transactions ────────────────────────────────────────────────────
    if subsidy > 0:
        db.add_fund_transaction(
            chat_id, rc_db_id, "subsidy", -subsidy,
            f"subsidy for '{title}'", admin_uid, admin_name,
        )
    if remainder > 0:
        db.add_fund_transaction(
            chat_id, rc_db_id, "rounding", remainder,
            f"rounding remainder from '{title}'", admin_uid, admin_name,
        )

    db.log_admin_action(chat_id, admin_uid, admin_name, "close_game", target_name=title)

    # Payment destination: collector UPI > group fallback UPI
    game_upi = collector_upi or settings.get("upi_vpa")
    treasury_upi = settings.get("treasury_upi")
    upi_line = f"\n💳 Pay ₹{per_head} game fee to: `{game_upi}`" if game_upi else ""
    if treasury_upi and treasury_upi != game_upi:
        upi_line += f"\n🏦 Penalties/fund → `{treasury_upi}`"
    subsidy_line = f"\n💰 Fund subsidy: ₹{subsidy}" if subsidy > 0 else ""
    remainder_line = f"\n🏦 Rounding → fund: +₹{remainder}" if remainder > 0 else ""
    collector_line = f"\n📦 Collector: {collector_name}{rotation_note}" if collector_name else ""

    announcement = (
        f"📊 Game closed: *{title}*\n"
        f"🏟 Ground: ₹{ground_cost}{subsidy_line}\n"
        f"👥 Players: {in_count}  |  Per head: ₹{per_head}"
        f"{remainder_line}{collector_line}{upi_line}"
    )

    fund_balance_after = db.get_fund_balance(chat_id)

    return {
        "rollcall_id": rc_db_id,
        "closure_id": closure_id,
        "title": title,
        "ground_cost": ground_cost,
        "subsidy": subsidy,
        "per_head": per_head,
        "remainder": remainder,
        "in_count": in_count,
        "members": in_members,
        "fund_balance_after": fund_balance_after,
        "end_result": end_result,
        "announcement": announcement,
        "upi_vpa": game_upi,
    }


def close_empty_game(
    chat_id: int,
    rollcall_id: int,
    admin_uid: int,
    admin_name: str,
) -> dict:
    """Dismiss a 0-IN-user game from the unsettled queue without a financial
    split — there's nothing to divide. Writes a zero-value game_closures row
    (no dues_entries, nothing to reverse later) so /settle_dues stops
    surfacing it.

    Raises duesGameAlreadyClosed if it was somehow already closed.
    """
    row = db.get_rollcall(rollcall_id)
    if row is None or row.get("chat_id") != chat_id:
        raise duesNothingToClose("That game was not found for this group.")
    if db.get_game_closure(rollcall_id) is not None:
        raise duesGameAlreadyClosed(f"'{row.get('title') or rollcall_id}' has already been closed.")

    title = row.get("title") or "<Empty>"
    db.create_game_closure(
        chat_id=chat_id,
        rollcall_id=rollcall_id,
        title=title,
        ground_cost=0,
        in_count=0,
        subsidy=0,
        per_head=0,
        rounding_step=get_dues_settings(chat_id)["dues_round_step"],
        remainder=0,
        closed_by_uid=admin_uid,
        closed_by_name=admin_name,
    )
    db.log_admin_action(chat_id, admin_uid, admin_name, "close_empty_game", target_name=title)

    return {
        "rollcall_id": rollcall_id,
        "title": title,
        "announcement": f"🗑 *{_esc_md(title)}* closed with no players — no dues recorded.",
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def my_dues(chat_id: int, user_id: int) -> dict:
    """Return this user's outstanding balance and recent ledger lines."""
    balance = db.get_dues_balance(chat_id, user_id=user_id)
    entries = db.get_dues_entries(chat_id, user_id=user_id, limit=10)
    return {"balance": balance, "entries": entries}


def all_dues(chat_id: int, nonzero_only: bool = True) -> dict:
    """Return per-member balances for the group."""
    balances = db.get_all_dues_balances(chat_id, nonzero_only=nonzero_only)
    return {"balances": balances}


def fund_summary(chat_id: int) -> dict:
    """Return current fund balance."""
    balance = db.get_fund_balance(chat_id)
    return {"fund_balance": balance}


def dues_snapshot(chat_id: int) -> dict:
    """Build a human-readable snapshot of current dues state for the group.

    Returns:
        text        — formatted Markdown message ready to post
        balances    — raw list for programmatic use
        fund_balance — current fund balance
    """
    import datetime as _dt
    balances = db.get_all_dues_balances(chat_id, nonzero_only=False)
    fund_balance = db.get_fund_balance(chat_id)
    last_closure = db.get_nth_game_closure(chat_id, 0)
    tz_label = "UTC"

    owed   = [b for b in balances if b["balance"] > 0]
    credit = [b for b in balances if b["balance"] < 0]
    settled = [b for b in balances if b["balance"] == 0]

    now_str = _dt.datetime.utcnow().strftime("%d %b %Y %H:%M") + " UTC"
    lines = [f"📊 *Dues Snapshot — {now_str}*"]

    if last_closure:
        closed_at = (last_closure.get("created_at") or "")[:10]
        lines.append(
            f"\n🏏 Last game: *{_esc_md(last_closure['title'])}*"
            f" ({closed_at}) · ₹{last_closure['per_head']}/head"
            f" · {last_closure['in_count']} players"
        )

    if owed:
        lines.append("\n💰 *Outstanding:*")
        for b in sorted(owed, key=lambda x: -x["balance"]):
            lines.append(f"  {_esc_md(b['member_name'])}  ₹{b['balance']}")
    else:
        lines.append("\n✅ No outstanding balances")

    if credit:
        lines.append("\n🟢 *Credits:*")
        for b in credit:
            lines.append(f"  {_esc_md(b['member_name'])}  −₹{abs(b['balance'])}")

    if settled:
        names = ", ".join(_esc_md(b["member_name"]) for b in settled[:8])
        extra = f" (+{len(settled) - 8} more)" if len(settled) > 8 else ""
        lines.append(f"\n✔ Settled: {names}{extra}")

    lines.append(f"\n🏦 *Fund balance: ₹{fund_balance}*")

    return {
        "text": "\n".join(lines),
        "balances": balances,
        "fund_balance": fund_balance,
    }


def dues_export_csv(chat_id: int) -> str:
    """Build a CSV string of all member balances — for file export.

    Columns: name, user_id, balance, status
    status = 'owed' | 'credit' | 'settled'
    """
    import io, csv as _csv
    balances = db.get_all_dues_balances(chat_id, nonzero_only=False)
    last_entries = {}
    for b in balances:
        uid = b.get("user_id")
        name = b.get("member_name", "")
        rows = db.get_dues_entries(
            chat_id,
            user_id=uid if uid else None,
            member_name=name if not uid else None,
            limit=1,
        )
        # Keyed by (user_id, name) — a bare name key would collide (and
        # silently overwrite) whenever a real user and a proxy, or two real
        # users, share a display name; get_all_dues_balances itself groups
        # on this same composite identity.
        last_entries[(uid, name)] = rows[0] if rows else {}

    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["name", "user_id", "balance", "status",
                     "last_entry_type", "last_entry_date", "last_amount"])
    for b in sorted(balances, key=lambda x: -x["balance"]):
        bal = b["balance"]
        status = "owed" if bal > 0 else ("credit" if bal < 0 else "settled")
        le = last_entries.get((b.get("user_id"), b["member_name"]), {})
        writer.writerow([
            b["member_name"],
            b.get("user_id") or "",
            bal,
            status,
            le.get("entry_type", ""),
            (le.get("created_at") or "")[:10],
            le.get("amount", ""),
        ])
    return buf.getvalue()


def fund_history(chat_id: int, limit: int = 15, offset: int = 0) -> dict:
    """Return paginated fund transaction history."""
    txns = db.get_fund_transactions(chat_id, limit=limit, offset=offset)
    total = db.count_fund_transactions(chat_id)
    return {"transactions": txns, "total": total, "limit": limit, "offset": offset}


# ── Penalty tiers (user-defined, n tiers) ────────────────────────────────────

def seed_default_penalty_tiers(chat_id: int) -> None:
    """Seed default penalty tiers if none exist yet (called on /enable_dues)."""
    if db.get_penalty_tiers(chat_id):
        return  # already configured — don't overwrite on re-enable
    #  name          amount  description           mins_threshold  is_ditch
    for name, amount, desc, mins, ditch in [
        ("late_short", 50,  "under 15 min late",  1,    False),
        ("late_long",  100, "15+ min late",        15,   False),
        ("ditch",      200, "no-show / absent",    None, True),
    ]:
        db.upsert_penalty_tier(chat_id, name, amount, desc, mins, ditch)


def add_penalty_tier(
    chat_id: int,
    name: str,
    amount: int,
    description: str,
    admin_uid: int,
    admin_name: str,
    late_minutes_threshold: int | None = None,
    is_ditch: bool = False,
) -> dict:
    """Add or update a named penalty tier.

    late_minutes_threshold — minimum minutes late to auto-select this tier
    via /mark_late.  None means the tier is manual-only.
    is_ditch — marks this as the no-show tier used by /mark_ditch.
    """
    name = name.strip().lower()
    if not name:
        raise parameterMissing("Tier name cannot be empty.")
    if len(name) > 40:
        raise incorrectParameter("Tier name must be 40 characters or fewer.")
    if amount <= 0:
        raise incorrectParameter("Penalty amount must be a positive integer (₹).")
    if late_minutes_threshold is not None and late_minutes_threshold < 1:
        raise incorrectParameter("mins threshold must be at least 1.")
    db.upsert_penalty_tier(chat_id, name, amount, description or None,
                           late_minutes_threshold, is_ditch)
    db.log_admin_action(chat_id, admin_uid, admin_name, "add_penalty_tier",
                        details=f"{name}=₹{amount}")

    parts = [f"⚙️ Penalty tier *{_esc_md(name)}*: ₹{amount}"]
    if description:
        parts.append(f"— {_esc_md(description)}")
    if late_minutes_threshold is not None:
        parts.append(f"(auto: ≥{late_minutes_threshold} min late)")
    if is_ditch:
        parts.append("(ditch tier)")
    return {"name": name, "amount": amount, "announcement": " ".join(parts)}


def remove_penalty_tier(
    chat_id: int,
    name: str,
    admin_uid: int,
    admin_name: str,
) -> dict:
    """Remove a penalty tier by name."""
    name = name.strip().lower()
    deleted = db.delete_penalty_tier(chat_id, name)
    if not deleted:
        raise incorrectParameter(
            f"Tier '{name}' not found. Use /penalties to see defined tiers."
        )
    db.log_admin_action(chat_id, admin_uid, admin_name, "remove_penalty_tier", details=name)
    return {"name": name, "announcement": f"🗑 Penalty tier *{_esc_md(name)}* removed."}


def list_unsettled_games(chat_id: int) -> dict:
    """All ended-but-not-financially-closed games for a chat, newest first.

    Used by /settle_dues to show a picker instead of only ever reaching the
    single latest one (db.get_latest_closeable_rollcall's old LIMIT-1 blind
    spot for older missed games)."""
    games = db.get_unsettled_rollcalls(chat_id)
    return {"games": games}


def list_penalty_tiers(chat_id: int) -> dict:
    """Return all penalty tiers as a formatted announcement."""
    tiers = db.get_penalty_tiers(chat_id)
    if not tiers:
        lines = ["No penalty tiers defined. Use /add_penalty to create one."]
    else:
        lines = ["📋 *Penalty tiers:*"]
        for t in tiers:
            desc = f" — {_esc_md(t['description'])}" if t.get("description") else ""
            lines.append(f"  • *{_esc_md(t['name'])}*: ₹{t['amount']}{desc}")
        lines.append("\nUse: /mark_penalty <tier> <name>")
    return {"tiers": tiers, "announcement": "\n".join(lines)}


def mark_penalty(
    chat_id: int,
    tier_name: str,
    token: str,
    admin_uid: int,
    admin_name: str,
    rollcall_id: int | None = None,
    known_identity: int | str | None = None,
) -> dict:
    """Assess a named penalty tier against a member.

    Writes a dues entry (entry_type='penalty', memo=tier_name) and a fund
    penalty transaction.

    known_identity — bypasses name resolution when the caller already knows
    the concrete identity (int user_id or str proxy_name), e.g. the penalty
    panel picks players straight from a rollcall's IN list. Without this,
    _resolve_member can't find a proxy being penalized for the first time —
    it only matches proxy names that already have prior ledger history, and
    penalty marking now happens *before* that first game-share entry exists
    (it's applied ahead of the financial close in the /settle_dues flow).
    """
    tier = db.get_penalty_tier(chat_id, tier_name)
    if tier is None:
        raise incorrectParameter(
            f"Penalty tier '{tier_name}' not found. Use /penalties to see defined tiers."
        )
    if known_identity is not None:
        if isinstance(known_identity, int):
            member = {"user_id": known_identity, "member_name": token}
        else:
            member = {"user_id": None, "member_name": known_identity}
    else:
        all_names = _known_proxy_names(chat_id)
        member = _resolve_member(chat_id, token, dues_names=all_names)
    amount = tier["amount"]
    display_name = tier["name"]

    db.add_dues_entry(
        chat_id, rollcall_id, member["user_id"], member["member_name"],
        "penalty", amount,
        display_name, admin_uid, admin_name,
    )
    db.add_fund_transaction(
        chat_id, rollcall_id, "penalty", amount,
        f"{member['member_name']} — {display_name}", admin_uid, admin_name,
    )
    db.log_admin_action(chat_id, admin_uid, admin_name, "mark_penalty",
                        target_name=member["member_name"], details=f"{display_name} ₹{amount}")

    desc = tier.get("description") or display_name
    _chat_row = db.get_or_create_chat(chat_id)
    treasury_upi = _chat_row.get("treasury_upi") or _chat_row.get("upi_vpa")
    upi_line = f"\n💳 Pay to: `{treasury_upi}`" if treasury_upi else ""
    return {
        "member_name": member["member_name"],
        "user_id": member["user_id"],
        "tier_name": display_name,
        "amount": amount,
        "announcement": f"⚠️ Penalty ({_esc_md(display_name)}): {_esc_md(member['member_name'])} → ₹{amount}  _{_esc_md(desc)}_{upi_line}",
    }


def mark_late(
    chat_id: int,
    token: str,
    minutes: int,
    admin_uid: int,
    admin_name: str,
    rollcall_id: int | None = None,
) -> dict:
    """Assess a late penalty by how many minutes late the player was.

    Picks the tier with the highest late_minutes_threshold that is ≤ minutes.
    The group configures tiers and thresholds via /add_penalty mins:<N>.
    """
    if minutes < 1:
        raise incorrectParameter("Minutes must be at least 1.")
    tier = db.get_tier_for_minutes(chat_id, minutes)
    if tier is None:
        raise incorrectParameter(
            f"No late tier covers {minutes} min late. "
            "Add one with: /add_penalty <name> <amount> mins:<threshold>"
        )
    return mark_penalty(chat_id, tier["name"], token, admin_uid, admin_name, rollcall_id)


def mark_ditch(
    chat_id: int,
    token: str,
    admin_uid: int,
    admin_name: str,
    rollcall_id: int | None = None,
) -> dict:
    """Assess the ditch (no-show) penalty for a player.

    Uses whichever tier is flagged is_ditch=1 for this group.
    Configure with: /add_penalty <name> <amount> ditch <description>
    """
    tier = db.get_ditch_tier(chat_id)
    if tier is None:
        raise incorrectParameter(
            "No ditch tier configured. "
            "Add one with: /add_penalty <name> <amount> ditch <description>"
        )
    return mark_penalty(chat_id, tier["name"], token, admin_uid, admin_name, rollcall_id)


def waive(
    chat_id: int,
    token: str,
    amount: int,
    reason: str,
    admin_uid: int,
    admin_name: str,
    rollcall_id: int | None = None,
) -> dict:
    """Waive (forgive) part or all of a member's dues.

    Writes compensating negative dues entry and fund adjustment.
    Originals are never modified — append-only invariant.
    """
    if amount <= 0:
        raise incorrectParameter("Waive amount must be positive.")
    all_names = _known_proxy_names(chat_id)
    member = _resolve_member(chat_id, token, dues_names=all_names)

    db.add_dues_entry(
        chat_id, rollcall_id, member["user_id"], member["member_name"],
        "waiver", -amount,
        reason or "admin waiver", admin_uid, admin_name,
    )
    db.add_fund_transaction(
        chat_id, rollcall_id, "adjustment", -amount,
        f"waiver: {member['member_name']} — {reason or 'admin'}", admin_uid, admin_name,
    )
    db.log_admin_action(chat_id, admin_uid, admin_name, "waive",
                        target_name=member["member_name"], details=f"₹{amount}")

    return {
        "member_name": member["member_name"],
        "user_id": member["user_id"],
        "amount": amount,
        "reason": reason,
        "announcement": f"🕊 Waived ₹{amount} for {_esc_md(member['member_name'])}: {_esc_md(reason or '')}".strip(),
    }


# ── Payments & collector ──────────────────────────────────────────────────────

def _next_rotation_collector(in_members: list, last_uid) -> tuple | None:
    """Pick the next real-user collector in ascending-uid order, cycling.

    Deterministic regardless of vote order; proxies are never picked (they
    can't receive payments). Returns (uid, name) or None if no real users.
    """
    real = sorted(
        ((m["user_id"], m["member_name"]) for m in in_members if m.get("user_id")),
        key=lambda x: x[0],
    )
    if not real:
        return None
    if last_uid:
        for uid, name in real:
            if uid > last_uid:
                return uid, name
    return real[0]


def set_collector(
    chat_id: int,
    token: str,
    paid_ground: bool,
    admin_uid: int,
    admin_name: str,
    rc_number: int = 0,
    collector_upi: str | None = None,
) -> dict:
    """Designate a collector for the current or most-recent game.

    Pre-close (active RC): persists to rollcalls table columns.
    Post-close: updates game_closures collector metadata.
    collector_upi is optional — if provided it overrides the group UPI in the
    close announcement so game fees are routed to this collector directly.
    """
    all_names = _known_proxy_names(chat_id)
    member = _resolve_member(chat_id, token, dues_names=all_names)
    uid = member["user_id"]
    name = member["member_name"]
    if uid is None:
        raise incorrectParameter("Collector must be a real (Telegram) user, not a proxy.")

    paid_flag = 1 if paid_ground else 0

    # Try active rollcall first
    active = manager.get_rollcalls(chat_id)
    if active:
        idx = rc_number if rc_number < len(active) else 0
        rc = manager.get_rollcalls(chat_id)[idx]
        rc_db_id = getattr(rc, "id", None) or getattr(rc, "db_id", None)
        if rc_db_id:
            db.update_rollcall(rc_db_id,
                               collector_uid=uid, collector_name=name,
                               collector_paid_ground=paid_flag,
                               **({'collector_upi': collector_upi} if collector_upi else {}))
        # Update in-memory object too so close_game picks it up without DB re-fetch
        try:
            rc.collector_uid = uid
            rc.collector_name = name
            rc.collector_paid_ground = paid_flag
            rc.collector_upi = collector_upi
        except Exception:
            pass
        source = "active rollcall"
    else:
        # Post-close: update the latest closure
        closure = db.get_latest_game_closure(chat_id)
        if closure is None:
            raise duesNothingToClose("No active rollcall or closed game to assign a collector to.")
        db.update_game_closure_collector(
            closure["rollcall_id"], uid, name,
            collector_paid_ground=paid_flag,
            collector_upi=collector_upi,
        )
        source = "last game closure"

    paid_note = " (fronted ground cost)" if paid_ground else ""
    upi_note = f" · `{collector_upi}`" if collector_upi else ""
    db.log_admin_action(chat_id, admin_uid, admin_name, "set_collector", target_name=name)

    return {
        "collector_uid": uid,
        "collector_name": name,
        "collector_paid_ground": paid_flag,
        "collector_upi": collector_upi,
        "source": source,
        "announcement": f"📦 Collector: {_esc_md(name)}{paid_note}{upi_note}",
    }


def mark_paid(
    chat_id: int,
    token: str,
    actor_uid: int,
    actor_name: str,
    amount: int | None = None,
    is_admin: bool = False,
    rollcall_id: int | None = None,
    known_identity: int | str | None = None,
) -> dict:
    """Record a payment from a member.

    Permitted when actor_uid is an admin (is_admin=True) or when they are
    the collector of the most recent game closure.

    amount=None defaults to the member's full outstanding balance.
    Overpayments are allowed and appear as negative balance (credit).

    known_identity — bypasses name resolution when the caller already knows
    the concrete identity (int user_id or str proxy_name), e.g. the payment
    panel picks a row straight from its own balances snapshot. Without this,
    re-resolving by name can raise on an ambiguous shared first name, or
    (before the ledger-history lookup in _resolve_member) fail entirely for
    a real user no longer in the active-members table.
    """
    if known_identity is not None:
        if isinstance(known_identity, int):
            member = {"user_id": known_identity, "member_name": token}
        else:
            member = {"user_id": None, "member_name": known_identity}
    else:
        all_names = _known_proxy_names(chat_id)
        member = _resolve_member(chat_id, token, dues_names=all_names)

    # Permission check: admin OR current collector
    if not is_admin:
        closure = db.get_latest_game_closure(chat_id)
        collector_uid = closure.get("collector_uid") if closure else None
        if collector_uid != actor_uid:
            raise insufficientPermissions(
                "Only admins or the designated collector can record payments."
            )

    # Default amount = full outstanding balance
    if amount is None:
        if member["user_id"] is not None:
            amount = db.get_dues_balance(chat_id, user_id=member["user_id"])
        else:
            amount = db.get_dues_balance(chat_id, member_name=member["member_name"])
        if amount <= 0:
            raise incorrectParameter(
                f"{member['member_name']} has no outstanding balance to mark as paid."
            )

    if amount <= 0:
        raise incorrectParameter("Payment amount must be positive.")

    db.add_dues_entry(
        chat_id, rollcall_id, member["user_id"], member["member_name"],
        "payment", -amount,
        f"received by {actor_name}", actor_uid, actor_name,
    )
    db.log_admin_action(chat_id, actor_uid, actor_name, "mark_paid",
                        target_name=member["member_name"], details=f"₹{amount}")

    return {
        "member_name": member["member_name"],
        "user_id": member["user_id"],
        "amount": amount,
        "announcement": f"✅ Payment: {_esc_md(member['member_name'])} paid ₹{amount} (received by {_esc_md(actor_name)})",
    }


def self_paid(
    chat_id: int,
    user_id: int,
    member_name: str,
    amount: int | None = None,
) -> dict:
    """Member self-reports a payment. No admin/collector check; capped at outstanding."""
    outstanding = db.get_dues_balance(chat_id, user_id=user_id)
    if outstanding <= 0:
        raise incorrectParameter("You have no outstanding dues.")

    if amount is None:
        amount = outstanding
    if amount > outstanding:
        amount = outstanding  # cap — no credit via self-report

    if amount <= 0:
        raise incorrectParameter("Payment amount must be positive.")

    db.add_dues_entry(
        chat_id, None, user_id, member_name,
        "payment", -amount,
        "self-reported (web)", user_id, member_name,
    )
    return {
        "member_name": member_name,
        "user_id": user_id,
        "amount": amount,
        "announcement": f"✅ {_esc_md(member_name)} marked ₹{amount} as paid (self-reported)",
    }


def reimburse(
    chat_id: int,
    token: str,
    amount: int,
    reason: str,
    admin_uid: int,
    admin_name: str,
    rollcall_id: int | None = None,
) -> dict:
    """Issue a reimbursement credit to a member (admin only)."""
    if amount <= 0:
        raise incorrectParameter("Reimbursement amount must be positive.")
    all_names = _known_proxy_names(chat_id)
    member = _resolve_member(chat_id, token, dues_names=all_names)

    db.add_dues_entry(
        chat_id, rollcall_id, member["user_id"], member["member_name"],
        "reimbursement", -amount,
        reason or "admin reimbursement", admin_uid, admin_name,
    )
    db.log_admin_action(chat_id, admin_uid, admin_name, "reimburse",
                        target_name=member["member_name"], details=f"₹{amount}")

    return {
        "member_name": member["member_name"],
        "amount": amount,
        "announcement": f"💸 Reimbursed ₹{amount} to {_esc_md(member['member_name'])}: {_esc_md(reason or '')}".strip(),
    }


# ── Ad-hoc joiner ─────────────────────────────────────────────────────────────

def add_adhoc(
    chat_id: int,
    token: str,
    admin_uid: int,
    admin_name: str,
) -> dict:
    """Charge a late-joining player the most recent closure's per-head fee.

    Also adds a fund 'adjustment' matching per_head so the fund accounts for
    the extra income.
    """
    closure = db.get_latest_game_closure(chat_id)
    if closure is None:
        raise duesNothingToClose("No closed game found. Close a game first with /settle_dues.")

    all_names = _known_proxy_names(chat_id)
    member = _resolve_member(chat_id, token, dues_names=all_names)
    per_head = closure["per_head"]
    rc_id = closure["rollcall_id"]

    db.add_dues_entry(
        chat_id, rc_id, member["user_id"], member["member_name"],
        "adhoc", per_head,
        f"ad-hoc joiner — {closure['title']}", admin_uid, admin_name,
    )
    db.add_fund_transaction(
        chat_id, rc_id, "adjustment", per_head,
        f"ad-hoc: {member['member_name']} — {closure['title']}", admin_uid, admin_name,
    )
    db.log_admin_action(chat_id, admin_uid, admin_name, "add_adhoc",
                        target_name=member["member_name"])

    return {
        "member_name": member["member_name"],
        "per_head": per_head,
        "announcement": f"➕ Ad-hoc: {_esc_md(member['member_name'])} joined '{_esc_md(closure['title'])}' → ₹{per_head}",
    }


# ── Cancel game dues ──────────────────────────────────────────────────────────

def cancel_game_credit(
    chat_id: int,
    rollcall_id: int,
    admin_uid: int,
    admin_name: str,
) -> dict:
    """Reverse all share/adhoc dues entries for a game.

    Writes compensating cancel_credit entries so members' balances return
    to pre-game values. Payments already made become credits (the ledger
    faithfully records what happened). Fund rounding/subsidy txns are also
    reversed.
    """
    closure = db.get_game_closure(rollcall_id)
    if closure is None:
        raise incorrectParameter(
            f"No game closure found for rollcall id={rollcall_id}. "
            "Use /settle_dues first, or check the rollcall id."
        )

    entries = db.get_dues_entries_for_rollcall(rollcall_id)
    reversible_types = {"share", "adhoc"}
    reversed_count = 0

    for e in entries:
        if e["entry_type"] in reversible_types:
            db.add_dues_entry(
                chat_id, rollcall_id, e["user_id"], e["member_name"],
                "cancel_credit", -e["amount"],
                f"cancelled: {closure['title']}", admin_uid, admin_name,
            )
            reversed_count += 1

    # Reverse fund transactions for this rollcall.
    # Uses a targeted rollcall-scoped query (not a chat-wide scan with a limit)
    # so cancellation is correct even on groups with thousands of transactions.
    # "adjustment" covers add_adhoc income; "penalty" entries stand independently.
    all_rc_fund_txns = db.get_fund_transactions_for_rollcall(rollcall_id)
    rc_fund_txns = [t for t in all_rc_fund_txns
                    if t["txn_type"] in ("rounding", "subsidy", "adjustment")]
    fund_net = sum(t["amount"] for t in rc_fund_txns)
    if fund_net != 0:
        db.add_fund_transaction(
            chat_id, rollcall_id, "adjustment", -fund_net,
            f"cancellation reversal: {closure['title']}", admin_uid, admin_name,
        )

    # Remove the closure row so the rollcall is eligible for re-close.
    # game_closures is metadata (not a money table) so deletion is allowed.
    # The compensating dues_entries + fund_transactions above stay as the
    # complete audit trail.
    db.delete_game_closure(rollcall_id)

    db.log_admin_action(chat_id, admin_uid, admin_name, "cancel_game_dues",
                        target_name=closure["title"])

    return {
        "rollcall_id": rollcall_id,
        "title": closure["title"],
        "reversed_count": reversed_count,
        "fund_reversal": -fund_net,
        "announcement": (
            f"🔁 Cancelled dues for '{_esc_md(closure['title'])}': "
            f"{reversed_count} share entries reversed. "
            f"Payments already recorded remain as credits.\n"
            f"Run /ef <amount> then /settle_dues to re-close with corrected figures."
        ),
    }


# ── Fund management ───────────────────────────────────────────────────────────

def log_expense(
    chat_id: int,
    amount: int,
    description: str,
    admin_uid: int,
    admin_name: str,
) -> dict:
    """Log a fund expenditure (e.g. new balls, bibs). Amount is positive (deducted)."""
    if amount <= 0:
        raise incorrectParameter("Expense amount must be positive.")
    db.add_fund_transaction(
        chat_id, None, "expense", -amount,
        description or "expense", admin_uid, admin_name,
    )
    balance = db.get_fund_balance(chat_id)
    db.log_admin_action(chat_id, admin_uid, admin_name, "log_expense", details=f"₹{amount} {description}")

    return {
        "amount": amount,
        "description": description,
        "fund_balance": balance,
        "announcement": f"🏦 Fund: −₹{amount} — {_esc_md(description)}. Balance: ₹{balance}",
    }


def fund_topup(
    chat_id: int,
    amount: int,
    description: str,
    admin_uid: int,
    admin_name: str,
) -> dict:
    """Manually add money to the group fund (e.g. special contribution)."""
    if amount <= 0:
        raise incorrectParameter("Top-up amount must be positive.")
    db.add_fund_transaction(
        chat_id, None, "topup", amount,
        description or "manual top-up", admin_uid, admin_name,
    )
    balance = db.get_fund_balance(chat_id)
    db.log_admin_action(chat_id, admin_uid, admin_name, "fund_topup", details=f"₹{amount}")

    return {
        "amount": amount,
        "description": description,
        "fund_balance": balance,
        "announcement": f"🏦 Fund top-up: +₹{amount}. Balance: ₹{balance}",
    }


def remind_dues(chat_id: int) -> dict:
    """Return members with outstanding (positive) balances.

    Each entry in `dm_targets` is ready for individual DM delivery:
      {user_id, member_name, balance, is_proxy, proxy_owner_uid, proxy_owner_name}

    `no_dm` lists proxy members whose owner uid cannot be resolved.
    """
    balances = db.get_all_dues_balances(chat_id, nonzero_only=True)
    owed     = [b for b in balances if b["balance"] > 0]
    settings = get_dues_settings(chat_id)
    upi      = settings.get("upi_vpa")

    dm_targets = []
    no_dm      = []

    for b in owed:
        uid  = b.get("user_id")
        name = b["member_name"]
        if uid is not None:
            dm_targets.append({
                "user_id":          uid,
                "member_name":      name,
                "balance":          b["balance"],
                "is_proxy":         False,
                "proxy_owner_uid":  None,
                "proxy_owner_name": None,
            })
        else:
            # Unowned proxy or proxy whose owner uid is in memo
            owner_uid = db.get_proxy_owner_uid(chat_id, name)
            if owner_uid is not None:
                dm_targets.append({
                    "user_id":          owner_uid,
                    "member_name":      name,
                    "balance":          b["balance"],
                    "is_proxy":         True,
                    "proxy_owner_uid":  owner_uid,
                    "proxy_owner_name": None,
                })
            else:
                no_dm.append(name)

    lines = []
    for b in owed:
        upi_line = f"  💳 Pay ₹{b['balance']} to: `{upi}`" if upi else ""
        lines.append(f"• {b['member_name']}: ₹{b['balance']}{upi_line}")
    if no_dm:
        lines.append(f"\n_Cannot DM (no linked Telegram account): {', '.join(no_dm)}_")

    announcement = (
        "📢 Outstanding dues:\n" + "\n".join(lines)
        if lines else "✅ Everyone is settled up!"
    )

    return {
        "members_owed": owed,
        "upi_vpa":      upi,
        "dm_targets":   dm_targets,
        "no_dm":        no_dm,
        "announcement": announcement,
    }
