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
    remainder = per_head * in_count - net
    return per_head, remainder


# ── Member resolution ────────────────────────────────────────────────────────

def _resolve_member(
    chat_id: int,
    token: str,
    dues_names: list[str] | None = None,
) -> dict:
    """Resolve a name/handle token to a member dict.

    Tries:
      1. @username or first_name match against active chat members (real users)
      2. Case-insensitive match against dues_names (proxy names from ledger history)

    Returns: {'user_id': int|None, 'member_name': str}
    Raises:  incorrectParameter if ambiguous or not found.
    """
    token = token.lstrip("@").strip()
    token_lower = token.lower()

    # Real users from chat_members table
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

    # Proxy / name-keyed members from dues history
    if dues_names:
        proxy_matches = [n for n in dues_names if n.lower() == token_lower]
        if len(proxy_matches) == 1:
            return {"user_id": None, "member_name": proxy_matches[0]}
        if len(proxy_matches) > 1:
            raise incorrectParameter(f"'{token}' is ambiguous in dues history.")

    raise incorrectParameter(
        f"'{token}' not found. Use the exact first name, @username, or proxy name."
    )


# ── Settings ─────────────────────────────────────────────────────────────────

_UPI_RE = re.compile(r"^[a-zA-Z0-9.\-_]{2,}@[a-zA-Z]{2,}$")


def set_upi(chat_id: int, vpa: str, admin_uid: int, admin_name: str) -> dict:
    """Set the group UPI VPA used for payment instructions."""
    vpa = vpa.strip()
    if not _UPI_RE.match(vpa):
        raise incorrectParameter(
            "Invalid UPI VPA. Expected format: yourname@bankname  (e.g. amit@upi, 9876543210@paytm)"
        )
    db.update_chat_settings(chat_id, upi_vpa=vpa)
    db.log_admin_action(chat_id, admin_uid, admin_name, "set_upi", details=vpa)
    return {"upi_vpa": vpa, "announcement": f"💳 UPI VPA set: `{vpa}`"}


def set_penalty_tiers(
    chat_id: int,
    t1: int, t2: int, t3: int, ditch: int,
    admin_uid: int, admin_name: str,
) -> dict:
    """Set late/ditch penalty amounts (₹)."""
    if not (0 < t1 <= t2 <= t3 <= ditch):
        raise incorrectParameter(
            "Penalty tiers must satisfy: 0 < t1 ≤ t2 ≤ t3 ≤ ditch"
        )
    db.update_chat_settings(
        chat_id,
        penalty_late_t1=t1, penalty_late_t2=t2, penalty_late_t3=t3,
        penalty_ditch=ditch,
    )
    db.log_admin_action(
        chat_id, admin_uid, admin_name, "set_penalties",
        details=f"t1={t1} t2={t2} t3={t3} ditch={ditch}",
    )
    return {
        "penalty_late_t1": t1, "penalty_late_t2": t2,
        "penalty_late_t3": t3, "penalty_ditch": ditch,
        "announcement": f"⚙️ Penalty tiers updated: <15min ₹{t1} | 15–29min ₹{t2} | ≥30min ₹{t3} | ditch ₹{ditch}",
    }


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
        "dues_round_step": row.get("dues_round_step") or 10,
        "penalty_late_t1": row.get("penalty_late_t1") or 50,
        "penalty_late_t2": row.get("penalty_late_t2") or 75,
        "penalty_late_t3": row.get("penalty_late_t3") or 100,
        "penalty_ditch": row.get("penalty_ditch") or 200,
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
    members = []
    for u in rc.inList:
        uid = u.user_id if isinstance(u.user_id, int) else None
        name = u.name or u.first_name or str(uid)
        if uid is None:
            # proxy user — look up owner from rc.proxy_owners
            owner_id = None
            if hasattr(rc, "proxy_owners") and rc.proxy_owners:
                owner_id = rc.proxy_owners.get(u.name)
            members.append({"user_id": None, "member_name": name, "proxy_owner_id": owner_id})
        else:
            members.append({"user_id": uid, "member_name": name, "proxy_owner_id": None})
    return members


def _in_list_from_db(rollcall_id: int) -> list[dict]:
    """Build a flat list of IN members from a persisted (ended) rollcall."""
    rows = db.get_rollcall_in_users(rollcall_id)
    members = []
    for r in rows:
        if r.get("proxy_name") is not None:
            members.append({
                "user_id": None,
                "member_name": r["proxy_name"],
                "proxy_owner_id": r.get("proxy_owner_id"),
            })
        else:
            members.append({
                "user_id": r["user_id"],
                "member_name": r.get("first_name") or str(r["user_id"]),
                "proxy_owner_id": None,
            })
    return members


async def close_game(
    chat_id: int,
    subsidy: int,
    admin_uid: int,
    admin_name: str,
    rc_number: int = 0,
) -> dict:
    """Financially close the most recent game for a chat.

    Two modes:
    - Active rollcall present: ends it first (streak/stats preserved), then closes.
    - No active rollcall: closes the latest ended-but-not-closed rollcall from DB.

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
    active_rollcalls = manager.get_rollcalls(chat_id)
    rc_db_id: int | None = None
    title: str = ""
    ground_cost: int = 0
    collector_uid: int | None = None
    collector_name: str | None = None
    collector_paid_ground: int = 0
    end_result: dict | None = None

    if active_rollcalls:
        # Use an active rollcall (close_game ends it)
        idx = rc_number if rc_number < len(active_rollcalls) else 0
        rc = manager.get_rollcall(chat_id, idx)
        if rc is None:
            raise duesNothingToClose("No active rollcall found.")
        rc_db_id = getattr(rc, "id", None) or getattr(rc, "db_id", None)
        title = rc.title or "<Empty>"
        ground_cost = _parse_ground_cost(rc.event_fee)
        collector_uid = getattr(rc, "collector_uid", None)
        collector_name = getattr(rc, "collector_name", None)
        collector_paid_ground = getattr(rc, "collector_paid_ground", 0) or 0
        in_members = _in_list_from_active_rc(rc)

        # End the rollcall so streak/stats are recorded
        from services import rollcalls as _rc_svc
        end_result = await _rc_svc.end_rollcall(
            chat_id, idx, admin_uid, admin_name
        )
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
        in_members = _in_list_from_db(rc_db_id)

    # ── Double-close guard ───────────────────────────────────────────────────
    if rc_db_id and db.get_game_closure(rc_db_id) is not None:
        raise duesGameAlreadyClosed(
            f"'{title}' has already been financially closed. "
            "Use /cancel_game_dues to reverse it first."
        )

    # ── Validate ground_cost ─────────────────────────────────────────────────
    if ground_cost <= 0:
        raise parameterMissing(
            "Ground cost is not set or couldn't be read from the event fee. "
            "Run /ef <amount> on the rollcall before /close_game."
        )

    # ── Validate subsidy ────────────────────────────────────────────────────
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
    )

    # ── Write per-member share entries ───────────────────────────────────────
    for m in in_members:
        uid = m["user_id"]
        name = m["member_name"]
        owner_id = m.get("proxy_owner_id")

        if uid is not None:
            # Real user
            db.add_dues_entry(
                chat_id, rc_db_id, uid, name,
                "share", per_head, None, admin_uid, admin_name,
            )
        elif owner_id is not None:
            # Owned proxy — charge owner, memo records proxy name
            db.add_dues_entry(
                chat_id, rc_db_id, owner_id, name,
                "share", per_head, f"proxy: {name}", admin_uid, admin_name,
            )
        else:
            # Unowned proxy — name-keyed entry
            db.add_dues_entry(
                chat_id, rc_db_id, None, name,
                "share", per_head, None, admin_uid, admin_name,
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

    upi = settings.get("upi_vpa")
    upi_line = f"\n💳 Pay ₹{per_head} to: `{upi}`" if upi else ""
    subsidy_line = f"\n💰 Fund subsidy: ₹{subsidy}" if subsidy > 0 else ""
    remainder_line = f"\n🏦 Rounding → fund: +₹{remainder}" if remainder > 0 else ""
    collector_line = f"\n📦 Collector: {collector_name}" if collector_name else ""

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


def fund_history(chat_id: int, limit: int = 15, offset: int = 0) -> dict:
    """Return paginated fund transaction history."""
    txns = db.get_fund_transactions(chat_id, limit=limit, offset=offset)
    total = db.count_fund_transactions(chat_id)
    return {"transactions": txns, "total": total, "limit": limit, "offset": offset}
