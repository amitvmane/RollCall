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

    _active_rc_idx: int | None = None  # set when an active RC needs ending post-validation

    if active_rollcalls:
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
            "Run /ef <amount> on the rollcall before /close_game."
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

    parts = [f"⚙️ Penalty tier *{name}*: ₹{amount}"]
    if description:
        parts.append(f"— {description}")
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
    return {"name": name, "announcement": f"🗑 Penalty tier *{name}* removed."}


def list_penalty_tiers(chat_id: int) -> dict:
    """Return all penalty tiers as a formatted announcement."""
    tiers = db.get_penalty_tiers(chat_id)
    if not tiers:
        lines = ["No penalty tiers defined. Use /add_penalty to create one."]
    else:
        lines = ["📋 *Penalty tiers:*"]
        for t in tiers:
            desc = f" — {t['description']}" if t.get("description") else ""
            lines.append(f"  • *{t['name']}*: ₹{t['amount']}{desc}")
        lines.append("\nUse: /mark_penalty <tier> <name>")
    return {"tiers": tiers, "announcement": "\n".join(lines)}


def mark_penalty(
    chat_id: int,
    tier_name: str,
    token: str,
    admin_uid: int,
    admin_name: str,
    rollcall_id: int | None = None,
) -> dict:
    """Assess a named penalty tier against a member.

    Writes a dues entry (entry_type='penalty', memo=tier_name) and a fund
    penalty transaction.
    """
    tier = db.get_penalty_tier(chat_id, tier_name)
    if tier is None:
        raise incorrectParameter(
            f"Penalty tier '{tier_name}' not found. Use /penalties to see defined tiers."
        )
    all_names = [r["member_name"] for r in db.get_all_dues_balances(chat_id, nonzero_only=False)]
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
    return {
        "member_name": member["member_name"],
        "user_id": member["user_id"],
        "tier_name": display_name,
        "amount": amount,
        "announcement": f"⚠️ Penalty ({display_name}): {member['member_name']} → ₹{amount}  _{desc}_",
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
    all_names = [r["member_name"] for r in db.get_all_dues_balances(chat_id, nonzero_only=False)]
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
        "announcement": f"🕊 Waived ₹{amount} for {member['member_name']}: {reason or ''}".strip(),
    }


# ── Payments & collector ──────────────────────────────────────────────────────

def set_collector(
    chat_id: int,
    token: str,
    paid_ground: bool,
    admin_uid: int,
    admin_name: str,
    rc_number: int = 0,
) -> dict:
    """Designate a collector for the current or most-recent game.

    Pre-close (active RC): persists to rollcalls table columns.
    Post-close: updates game_closures collector metadata.
    """
    all_names = [r["member_name"] for r in db.get_all_dues_balances(chat_id, nonzero_only=False)]
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
                               collector_paid_ground=paid_flag)
        # Update in-memory object too so close_game picks it up without DB re-fetch
        try:
            rc.collector_uid = uid
            rc.collector_name = name
            rc.collector_paid_ground = paid_flag
        except Exception:
            pass
        source = "active rollcall"
    else:
        # Post-close: update the latest closure
        closure = db.get_latest_game_closure(chat_id)
        if closure is None:
            raise duesNothingToClose("No active rollcall or closed game to assign a collector to.")
        db.update_game_closure_collector(
            closure["rollcall_id"], uid, name, collector_paid_ground=paid_flag
        )
        source = "last game closure"

    paid_note = " (fronted ground cost)" if paid_ground else ""
    db.log_admin_action(chat_id, admin_uid, admin_name, "set_collector", target_name=name)

    return {
        "collector_uid": uid,
        "collector_name": name,
        "collector_paid_ground": paid_flag,
        "source": source,
        "announcement": f"📦 Collector: {name}{paid_note}",
    }


def mark_paid(
    chat_id: int,
    token: str,
    actor_uid: int,
    actor_name: str,
    amount: int | None = None,
    is_admin: bool = False,
    rollcall_id: int | None = None,
) -> dict:
    """Record a payment from a member.

    Permitted when actor_uid is an admin (is_admin=True) or when they are
    the collector of the most recent game closure.

    amount=None defaults to the member's full outstanding balance.
    Overpayments are allowed and appear as negative balance (credit).
    """
    all_names = [r["member_name"] for r in db.get_all_dues_balances(chat_id, nonzero_only=False)]
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
        "announcement": f"✅ Payment: {member['member_name']} paid ₹{amount} (received by {actor_name})",
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
    all_names = [r["member_name"] for r in db.get_all_dues_balances(chat_id, nonzero_only=False)]
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
        "announcement": f"💸 Reimbursed ₹{amount} to {member['member_name']}: {reason or ''}".strip(),
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
        raise duesNothingToClose("No closed game found. Close a game first with /close_game.")

    all_names = [r["member_name"] for r in db.get_all_dues_balances(chat_id, nonzero_only=False)]
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
        "announcement": f"➕ Ad-hoc: {member['member_name']} joined '{closure['title']}' → ₹{per_head}",
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
            "Use /close_game first, or check the rollcall id."
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
            f"🔁 Cancelled dues for '{closure['title']}': "
            f"{reversed_count} share entries reversed. "
            f"Payments already recorded remain as credits.\n"
            f"Run /ef <amount> then /close_game to re-close with corrected figures."
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
        "announcement": f"🏦 Fund: −₹{amount} — {description}. Balance: ₹{balance}",
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
