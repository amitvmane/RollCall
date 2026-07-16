"""
Inline penalty-marking panel — sent after /erc when dues is enabled.

When dues is enabled this panel replaces the separate ghost prompt entirely.
The ditch tier (is_ditch=True) acts as the ghost/no-show marker: applying it
writes the ditch penalty AND records the ghost tracking side-effects
(increment_ghost_count, add_ghost_event, reset_user_streak). Tapping Done
finalises attendance tracking (decrement_attended for those who showed up).

Flow:
  1. Bot sends panel listing all configured penalty tiers.
  2. Admin taps a tier → panel switches to player-checkbox view.
  3. Admin taps players to toggle checked / unchecked.
  4. Tap "Apply" → penalties written (+ ghost records if ditch tier), panel
     resets to tier list for the next tier.
  5. Tap "Done" → attendance tracking finalised, panel dismissed.

Callback data prefixes (all ≤ 64 bytes):
  pen_t:{rc_id}:{tier}   — select a tier
  pen_g:{rc_id}:{idx}    — toggle player at list index
  pen_a:{rc_id}          — apply current selections
  pen_b:{rc_id}          — back to tier list
  pen_d:{rc_id}          — done / dismiss
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import db
from db import (
    get_rollcall_in_users, mark_rollcall_absent_done,
)
from bot_state import (
    bot, reply_error, safe_edit_text, safe_edit_markup,
    send_md_fallback, _esc_md, is_chat_admin,
)
from rollcall_manager import manager
from services import dues as dues_svc

# Cap on concurrently tracked panels; oldest evicted first. A panel that was
# never dismissed just expires — tapping it says "run /erc again".
_MAX_SESSIONS = 64


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class _PenaltySession:
    chat_id: int
    rollcall_id: int
    title: str
    members: List[dict]       # [{user_id, member_name, _identity}, ...]  index-stable
    ghost_eligible: bool      # True → ditch Apply also writes ghost records
    active_tier: Optional[str] = None
    selections: Dict = field(default_factory=dict)    # tier_name → set of indices
    applied: Dict = field(default_factory=dict)       # tier_name → count applied
    applied_indices: Dict = field(default_factory=dict)  # tier_name → set of applied indices
    ghost_marked: Set = field(default_factory=set)    # user_id (int) or proxy_name (str)
    ghost_finalised: bool = False


def _locked_indices(session: "_PenaltySession", exclude_tier: Optional[str]) -> Set[int]:
    """Indices already selected or applied in a tier other than `exclude_tier`.

    A player can only be pending/applied in one tier at a time — this keeps
    the penalty panel from double-marking the same player across tiers.
    """
    locked: Set[int] = set()
    for t, sel in session.selections.items():
        if t != exclude_tier:
            locked |= sel
    for t, idxs in session.applied_indices.items():
        if t != exclude_tier:
            locked |= idxs
    return locked

# keyed by (chat_id, message_id)
_sessions: Dict[Tuple, _PenaltySession] = {}


# ── Panel builders ────────────────────────────────────────────────────────────

def _tier_view(session: "_PenaltySession", tiers: list) -> Tuple[str, InlineKeyboardMarkup]:
    applied_notes = "".join(
        f"\n  ✅ {_esc_md(name)}: {cnt} marked"
        for name, cnt in session.applied.items() if cnt
    )
    has_ditch_tier = any(t.get("is_ditch") for t in tiers)
    if session.ghost_eligible:
        ghost_hint = "\n_Ditch tier also records ghost/no-show._"
    elif has_ditch_tier:
        ghost_hint = (
            "\n_Ghost tracking is off for this group — ditch will still charge the "
            "penalty but won't update ghost stats. Enable with /toggle\\_ghost\\_tracking._"
        )
    else:
        ghost_hint = ""
    text = (
        f"⚠️ *Penalty marking* — _{_esc_md(session.title)}_\n"
        f"Tap a tier to select players:{applied_notes}{ghost_hint}"
    )
    kb = InlineKeyboardMarkup(row_width=1)
    rc = session.rollcall_id
    for t in tiers:
        name     = t["name"]
        amt      = t["amount"]
        is_ditch = bool(t.get("is_ditch"))
        done_cnt = session.applied.get(name, 0)
        badge    = f" ✅{done_cnt}" if done_cnt else ""
        if is_ditch:
            ghost_tag = " (no-show)" if session.ghost_eligible else " (ghost tracking off)"
        else:
            ghost_tag = ""
        label    = f"{'🔴' if is_ditch else '🟡'} {name} ₹{amt}{ghost_tag}{badge}"
        kb.add(InlineKeyboardButton(label, callback_data=f"pen_t:{rc}:{name}"))
    kb.add(InlineKeyboardButton("✅ Done", callback_data=f"pen_d:{rc}"))
    return text, kb


def _player_view(
    session: "_PenaltySession",
    tier: dict,
) -> Tuple[str, InlineKeyboardMarkup]:
    tier_name = tier["name"]
    selected  = session.selections.get(tier_name, set())
    rc        = session.rollcall_id
    count     = len(selected)
    is_ditch  = bool(tier.get("is_ditch"))

    if is_ditch and session.ghost_eligible:
        ghost_note = "\n_Selecting these players also records them as ghosts._"
    elif is_ditch:
        ghost_note = (
            "\n_Ghost tracking is off — this only charges the penalty, no ghost "
            "stats are recorded. Enable with /toggle\\_ghost\\_tracking._"
        )
    else:
        ghost_note = ""
    text = (
        f"⚠️ *{_esc_md(tier_name)}* (₹{tier['amount']}) — tap to select players\n"
        f"_{_esc_md(tier.get('description') or 'Tap Apply when done.')}_"
        f"{ghost_note}"
    )
    kb = InlineKeyboardMarkup(row_width=3)
    buttons = []
    locked = _locked_indices(session, tier_name)
    for idx, m in enumerate(session.members):
        if idx in locked:
            label = f"🔒 {m['member_name']}"[:24]
            buttons.append(
                InlineKeyboardButton(label, callback_data=f"pen_locked:{rc}:{idx}")
            )
            continue
        check = "✅" if idx in selected else "◻"
        label = f"{check} {m['member_name']}"[:24]
        buttons.append(
            InlineKeyboardButton(label, callback_data=f"pen_g:{rc}:{idx}")
        )
    kb.add(*buttons)

    apply_label = f"Apply ({count} selected)" if count else "Apply"
    kb.row(
        InlineKeyboardButton(apply_label, callback_data=f"pen_a:{rc}"),
        InlineKeyboardButton("◀ Back",    callback_data=f"pen_b:{rc}"),
    )
    return text, kb


# ── Public API ────────────────────────────────────────────────────────────────

async def send_penalty_panel(
    chat_id: int,
    rollcall_id: int,
    title: str,
    ghost_eligible: bool = False,
) -> bool:
    """Send the penalty-marking panel for a just-ended rollcall.

    When ghost_eligible=True the ditch tier also writes ghost tracking records
    and Done finalises attendance (replaces the separate ghost prompt entirely).

    Returns True if the panel actually opened. False means no panel (no tiers
    configured, or nobody to mark) — the caller must continue the settle flow
    itself, since there'll be no pen_d Done tap to hand off from.
    """
    tiers = db.get_penalty_tiers(chat_id)
    if not tiers:
        # Dues enabled but no tiers configured. If ghost tracking was expected,
        # fall back to the classic ghost prompt so no data is lost.
        if ghost_eligible:
            await _send_fallback_ghost_prompt(chat_id, rollcall_id, title)
        try:
            await bot.send_message(
                chat_id,
                "ℹ️ No penalty tiers set up — skipping penalty marking.\n"
                "To use it next game: /add_penalty late15 50 mins:15 and "
                "/add_penalty ditch 100 ditch No-show",
            )
        except Exception:
            logging.exception("Failed to send no-tiers hint")
        return False

    members = _members_for_rollcall(rollcall_id)
    if not members:
        if ghost_eligible:
            mark_rollcall_absent_done(rollcall_id)
        return False

    session = _PenaltySession(
        chat_id=chat_id,
        rollcall_id=rollcall_id,
        title=title,
        members=members,
        ghost_eligible=ghost_eligible,
    )
    text, kb = _tier_view(session, tiers)
    sent = await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    _sessions[(chat_id, sent.message_id)] = session
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.pop(next(iter(_sessions)))
    return True


# ── Callback handler ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("pen_"))
async def penalty_panel_callback(call):
    try:
        cid  = call.message.chat.id
        mid  = call.message.message_id
        data = call.data

        # Penalties are financial writes — gate on admin status (same pattern
        # as the end-rollcall button in lifecycle.py). Respects the chat's
        # admin-mode setting like every dues command does.
        if not await is_chat_admin(cid, call.from_user.id):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can mark penalties", show_alert=True
            )
            return

        session = _sessions.get((cid, mid))
        if session is None:
            await bot.answer_callback_query(call.id, "Panel expired — run /erc again.")
            return

        tiers    = db.get_penalty_tiers(cid)
        tier_map = {t["name"]: t for t in tiers}
        rc       = session.rollcall_id

        # ── Select tier ──────────────────────────────────────────────────────
        if data.startswith(f"pen_t:{rc}:"):
            tier_name = data[len(f"pen_t:{rc}:"):]
            if tier_name not in tier_map:
                await bot.answer_callback_query(call.id, "Tier no longer exists.")
                return
            session.active_tier = tier_name
            session.selections.setdefault(tier_name, set())
            text, kb = _player_view(session, tier_map[tier_name])
            await safe_edit_text(cid, mid, text, parse_mode="Markdown")
            await safe_edit_markup(cid, mid, kb)
            await bot.answer_callback_query(call.id)

        # ── Locked player (already selected/applied in another tier) ────────
        elif data.startswith(f"pen_locked:{rc}:"):
            await bot.answer_callback_query(
                call.id, "Already selected in another tier — deselect there first.", show_alert=True
            )

        # ── Toggle player ────────────────────────────────────────────────────
        elif data.startswith(f"pen_g:{rc}:"):
            if not session.active_tier:
                await bot.answer_callback_query(call.id)
                return
            try:
                idx = int(data.split(":")[-1])
            except ValueError:
                await bot.answer_callback_query(call.id)
                return
            if idx < 0 or idx >= len(session.members):
                await bot.answer_callback_query(call.id)
                return
            if idx in _locked_indices(session, session.active_tier):
                await bot.answer_callback_query(
                    call.id, "Already selected in another tier — deselect there first.", show_alert=True
                )
                return
            sel  = session.selections.setdefault(session.active_tier, set())
            name = session.members[idx]["member_name"]
            if idx in sel:
                sel.discard(idx)
                await bot.answer_callback_query(call.id, f"◻ {name}")
            else:
                sel.add(idx)
                await bot.answer_callback_query(call.id, f"✅ {name}")
            _, kb = _player_view(session, tier_map[session.active_tier])
            await safe_edit_markup(cid, mid, kb)

        # ── Apply ────────────────────────────────────────────────────────────
        elif data == f"pen_a:{rc}":
            tier_name = session.active_tier
            if not tier_name or tier_name not in tier_map:
                await bot.answer_callback_query(call.id)
                return
            indices = session.selections.get(tier_name, set())
            if not indices:
                await bot.answer_callback_query(call.id, "No players selected.")
                return

            tier       = tier_map[tier_name]
            is_ditch   = bool(tier.get("is_ditch"))
            actor      = call.from_user
            actor_name = actor.first_name or actor.username or "Admin"

            applied_names = []
            applied_idx   = set()
            errors        = []
            # Serialize with /erc, template auto-close, and manual /mark_*
            # commands — same invariant as every chat mutation (CLAUDE.md).
            async with manager.get_chat_write_lock(cid):
                # This game may have been financially closed via a different
                # path (e.g. another admin ran /settle_dues's fast path on it
                # directly) while this panel sat open. Penalty entries are
                # NOT reversed by /cancel_game_dues ("stand independently"),
                # so applying more against an already-closed game would be
                # an orphaned, unreversible write — refuse instead.
                if db.get_game_closure(rc) is not None:
                    await bot.answer_callback_query(
                        call.id,
                        "This game was already financially closed elsewhere — "
                        "no more penalties can be applied here.",
                        show_alert=True,
                    )
                    return
                for idx in sorted(indices):
                    m = session.members[idx]
                    try:
                        dues_svc.mark_penalty(
                            cid, tier_name, m["member_name"],
                            actor.id, actor_name,
                            known_identity=m["_identity"],
                        )
                        applied_names.append(m["member_name"])
                        applied_idx.add(idx)
                    except Exception as exc:
                        errors.append(f"{m['member_name']}: {exc}")

                # Ghost tracking side-effects for ditch tier
                if is_ditch and session.ghost_eligible:
                    ghost_identities = {
                        session.members[i]["_identity"]
                        for i in indices
                        if session.members[i]["_identity"] is not None
                    }
                    if ghost_identities:
                        in_users = get_rollcall_in_users(rc)
                        from handlers.ghost import apply_ghost_marking
                        apply_ghost_marking(cid, rc, ghost_identities, in_users)
                        session.ghost_marked.update(ghost_identities)

            count = len(applied_names)
            session.applied[tier_name] = session.applied.get(tier_name, 0) + count
            session.applied_indices.setdefault(tier_name, set()).update(applied_idx)
            session.selections[tier_name] = set()
            session.active_tier = None

            if applied_names:
                names_str = ", ".join(_esc_md(n) for n in applied_names)
                ghost_note = " (recorded as ghosts)" if is_ditch and session.ghost_eligible else ""
                await send_md_fallback(
                    cid,
                    f"⚠️ Penalty *{_esc_md(tier_name)}* (₹{tier['amount']}) applied to: {names_str}{ghost_note}",
                )
            if errors:
                await bot.send_message(cid, "⚠️ Some penalties could not be applied:\n" + "\n".join(errors))

            text, kb = _tier_view(session, tiers)
            await safe_edit_text(cid, mid, text, parse_mode="Markdown")
            await safe_edit_markup(cid, mid, kb)
            await bot.answer_callback_query(call.id, f"✅ {count} penalties applied")

        # ── Back to tier list ────────────────────────────────────────────────
        elif data == f"pen_b:{rc}":
            session.active_tier = None
            text, kb = _tier_view(session, tiers)
            await safe_edit_text(cid, mid, text, parse_mode="Markdown")
            await safe_edit_markup(cid, mid, kb)
            await bot.answer_callback_query(call.id)

        # ── Done ─────────────────────────────────────────────────────────────
        elif data == f"pen_d:{rc}":
            if session.ghost_eligible and not session.ghost_finalised:
                session.ghost_finalised = True
                async with manager.get_chat_write_lock(cid):
                    in_users = get_rollcall_in_users(rc)
                    from handlers.ghost import _decrement_attended
                    _decrement_attended(cid, in_users, session.ghost_marked)
                    mark_rollcall_absent_done(rc)

            title = session.title
            _sessions.pop((cid, mid), None)
            await bot.answer_callback_query(call.id)
            # Penalty/ghost marking is done — hand off to the financial
            # confirm/subsidy card in the same message (/settle_dues flow).
            from handlers.dues import show_settle_confirm
            await show_settle_confirm(cid, rc, title, mid=mid)

        else:
            await bot.answer_callback_query(call.id)

    except Exception as exc:
        logging.exception("penalty_panel_callback error")
        try:
            await bot.answer_callback_query(call.id, "Error — try /mark_late or /mark_penalty manually.")
        except Exception:
            pass  # already logged above; nothing more useful to do if even the alert fails
        await reply_error(call.message, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _members_for_rollcall(rollcall_id: int) -> list:
    """Return IN members as [{user_id, member_name, _identity}].

    _identity is the value used for ghost tracking:
      - real user  → user_id (int)
      - proxy      → proxy_name (str)
    """
    rows    = get_rollcall_in_users(rollcall_id)
    members = []
    for r in rows:
        proxy_name = r.get("proxy_name")
        if proxy_name is not None:
            members.append({
                "user_id":     None,
                "member_name": proxy_name,
                "_identity":   proxy_name,
            })
        else:
            uid = r["user_id"]
            members.append({
                "user_id":     uid,
                "member_name": r.get("first_name") or str(uid),
                "_identity":   uid,
            })
    return members


async def _send_fallback_ghost_prompt(chat_id: int, rollcall_id: int, title: str) -> None:
    """Fallback: dues enabled but no tiers configured. Show classic ghost prompt."""
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    ghost_markup = InlineKeyboardMarkup(row_width=2)
    ghost_markup.add(
        InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{rollcall_id}"),
        InlineKeyboardButton("✅ No, all showed up",  callback_data=f"ghost_no_{rollcall_id}"),
    )
    title_hint = f" '{title}'" if title else ""
    await bot.send_message(chat_id, f"👻 Did anyone ghost{title_hint}?", reply_markup=ghost_markup)
