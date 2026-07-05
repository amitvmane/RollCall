"""
Inline penalty-marking panel — sent after /erc when dues is enabled.

Flow:
  1. Bot sends panel listing all configured penalty tiers.
  2. Admin taps a tier → panel switches to player-checkbox view.
  3. Admin taps players to toggle ✅ / ◻.
  4. Tap "Apply" → penalties written, panel resets to tier list.
  5. Tap "Done" → panel dismissed.

Callback data prefixes (all ≤ 64 bytes):
  pen_t:{rc_id}:{tier}   — select a tier
  pen_g:{rc_id}:{idx}    — toggle player at list index
  pen_a:{rc_id}          — apply current selections
  pen_b:{rc_id}          — back to tier list
  pen_d:{rc_id}          — done / dismiss
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import db
from bot_state import bot, reply_error, safe_edit_text, safe_edit_markup
from services import dues as dues_svc


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class _PenaltySession:
    chat_id: int
    rollcall_id: int
    title: str
    members: List[dict]    # [{user_id, member_name}, ...]  — ordered, index-stable
    active_tier: Optional[str] = None
    selections: Dict = field(default_factory=dict)   # tier_name → set of indices
    applied: Dict = field(default_factory=dict)      # tier_name → count applied

# keyed by (chat_id, message_id)
_sessions: Dict[Tuple, _PenaltySession] = {}


# ── Panel builders ────────────────────────────────────────────────────────────

def _tier_view(session: _PenaltySession, tiers: list) -> Tuple[str, InlineKeyboardMarkup]:
    """Top-level panel: one button per tier, then Done."""
    applied_notes = "".join(
        f"\n  ✅ {name}: {cnt} marked"
        for name, cnt in session.applied.items() if cnt
    )
    text = (
        f"⚠️ *Penalty marking* — _{session.title}_\n"
        f"Tap a tier to select players:{applied_notes}"
    )
    kb = InlineKeyboardMarkup(row_width=1)
    rc = session.rollcall_id
    for t in tiers:
        name = t["name"]
        amt  = t["amount"]
        done_cnt = session.applied.get(name, 0)
        badge = f" ✅{done_cnt}" if done_cnt else ""
        label = f"{'🔴' if t.get('is_ditch') else '🟡'} {name} ₹{amt}{badge}"
        kb.add(InlineKeyboardButton(label, callback_data=f"pen_t:{rc}:{name}"))
    kb.add(InlineKeyboardButton("✅ Done", callback_data=f"pen_d:{rc}"))
    return text, kb


def _player_view(
    session: _PenaltySession,
    tier: dict,
) -> Tuple[str, InlineKeyboardMarkup]:
    """Player-select panel: checkbox per member, Apply, Back."""
    tier_name = tier["name"]
    selected  = session.selections.get(tier_name, set())
    rc        = session.rollcall_id
    count     = len(selected)

    text = (
        f"⚠️ *{tier_name}* (₹{tier['amount']}) — tap to select players\n"
        f"_{tier.get('description') or 'Tap Apply when done.'}_"
    )
    kb = InlineKeyboardMarkup(row_width=3)
    buttons = []
    for idx, m in enumerate(session.members):
        check = "✅" if idx in selected else "◻"
        label = f"{check} {m['member_name']}"[:24]   # keep buttons tidy
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
) -> None:
    """Send the penalty-marking panel for a just-ended rollcall."""
    tiers = db.get_penalty_tiers(chat_id)
    if not tiers:
        return   # dues enabled but no tiers configured yet

    members = _members_for_rollcall(rollcall_id)
    if not members:
        return   # nobody was IN — nothing to mark

    session = _PenaltySession(
        chat_id=chat_id,
        rollcall_id=rollcall_id,
        title=title,
        members=members,
    )
    text, kb = _tier_view(session, tiers)
    sent = await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    _sessions[(chat_id, sent.message_id)] = session


# ── Callback handler ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("pen_"))
async def penalty_panel_callback(call):
    try:
        cid  = call.message.chat.id
        mid  = call.message.message_id
        data = call.data

        session = _sessions.get((cid, mid))
        if session is None:
            await bot.answer_callback_query(call.id, "Panel expired — run /erc again.")
            return

        tiers      = db.get_penalty_tiers(cid)
        tier_map   = {t["name"]: t for t in tiers}
        rc         = session.rollcall_id

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
            sel = session.selections.setdefault(session.active_tier, set())
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

            actor = call.from_user
            actor_name = actor.first_name or actor.username or "Admin"
            applied_names = []
            errors = []
            for idx in sorted(indices):
                m = session.members[idx]
                try:
                    dues_svc.mark_penalty(
                        cid, tier_name, m["member_name"],
                        actor.id, actor_name,
                    )
                    applied_names.append(m["member_name"])
                except Exception as exc:
                    errors.append(f"{m['member_name']}: {exc}")

            count = len(applied_names)
            session.applied[tier_name] = session.applied.get(tier_name, 0) + count
            session.selections[tier_name] = set()   # clear after apply
            session.active_tier = None

            # Post the ledger announcement to the group
            if applied_names:
                tier = tier_map[tier_name]
                names_str = ", ".join(applied_names)
                await bot.send_message(
                    cid,
                    f"⚠️ Penalty *{tier_name}* (₹{tier['amount']}) applied to: {names_str}",
                    parse_mode="Markdown",
                )
            if errors:
                await bot.send_message(cid, "⚠️ Some penalties could not be applied:\n" + "\n".join(errors))

            # Back to tier view
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
            total = sum(session.applied.values())
            summary = f"✅ Penalty marking done — {total} penalties recorded." if total else "✅ No penalties marked."
            await safe_edit_text(cid, mid, summary)
            await safe_edit_markup(cid, mid, InlineKeyboardMarkup())
            _sessions.pop((cid, mid), None)
            await bot.answer_callback_query(call.id)

        else:
            await bot.answer_callback_query(call.id)

    except Exception as exc:
        logging.exception("penalty_panel_callback error")
        try:
            await bot.answer_callback_query(call.id, "Error — try /mark_late or /mark_penalty manually.")
        except Exception:
            pass
        await reply_error(call.message, exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _members_for_rollcall(rollcall_id: int) -> list:
    """Return IN members for a persisted rollcall as [{user_id, member_name}]."""
    rows = db.get_rollcall_in_users(rollcall_id)
    members = []
    for r in rows:
        if r.get("proxy_name") is not None:
            members.append({"user_id": None, "member_name": r["proxy_name"]})
        else:
            members.append({
                "user_id": r["user_id"],
                "member_name": r.get("first_name") or str(r["user_id"]),
            })
    return members
