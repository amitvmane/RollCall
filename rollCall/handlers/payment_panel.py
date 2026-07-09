"""
Inline payment-marking panel — bare /mark_paid with no arguments.

Lists everyone in the chat with an outstanding balance (group-wide — dues
balances are cumulative across games/penalties/waivers, not scoped to one
rollcall). Tap a name to mark it paid in full (one tap), mark a partial
amount (reply with a number), or view their recent ledger history to see
how the balance accumulated.

The existing `/mark_paid <name> [amount]` command form is untouched — this
panel only appears when /mark_paid is called with no arguments.

Resolution note: full/partial payment both go through the same
dues_svc.mark_paid(token=member_name, ...) the command form uses, which
re-resolves the name via _resolve_member rather than using the already-known
user_id from this panel's snapshot — so the same rare ambiguity the command
form has (two active members sharing a first name) can still surface here.
Not changed, per the decision to keep mark_paid's existing behavior as-is.

Callback data prefixes:
  pay_pick:{idx}     — select a player from the list
  pay_full:{idx}     — mark their full balance as paid
  pay_partial:{idx}  — prompt for a partial amount (reply)
  pay_history:{idx}  — show their recent ledger entries
  pay_back:{idx}     — history view -> back to player-action view
  pay_tolist         — player-action view -> back to the list
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Tuple

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import db
from bot_state import (
    bot, reply_error, safe_edit_text, safe_edit_markup, send_md_fallback,
    _esc_md, _pending_payment_input, _prune_pending,
)
from rollcall_manager import manager
from services import dues as dues_svc

_MAX_SESSIONS = 64
_MAX_LISTED = 20   # no pagination in this round — same scope boundary as the settle_dues picker


# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class _PaymentSession:
    chat_id: int
    balances: List[dict]  # [{"user_id":.., "member_name":.., "balance":..}, ...] index-stable

# keyed by (chat_id, message_id)
_sessions: Dict[tuple, _PaymentSession] = {}


# ── Panel builders ────────────────────────────────────────────────────────────

def _fmt_balance_label(row: dict) -> str:
    return f"{row['member_name']} — ₹{row['balance']}"[:64]


def _list_view(session: "_PaymentSession") -> Tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(row_width=1)
    for idx, row in enumerate(session.balances):
        kb.add(InlineKeyboardButton(_fmt_balance_label(row), callback_data=f"pay_pick:{idx}"))
    if not session.balances:
        return "✅ No outstanding balances.", kb
    return "💰 *Outstanding balances* — tap a name to mark paid:", kb


def _player_action_view(session: "_PaymentSession", idx: int) -> Tuple[str, InlineKeyboardMarkup]:
    row = session.balances[idx]
    text = f"💰 *{_esc_md(row['member_name'])}* owes ₹{row['balance']}\nMark as paid:"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(f"✅ Full ₹{row['balance']}", callback_data=f"pay_full:{idx}"),
        InlineKeyboardButton("✏️ Partial", callback_data=f"pay_partial:{idx}"),
    )
    kb.row(
        InlineKeyboardButton("📜 History", callback_data=f"pay_history:{idx}"),
        InlineKeyboardButton("⬅ Back", callback_data="pay_tolist"),
    )
    return text, kb


def _history_view(session: "_PaymentSession", idx: int) -> Tuple[str, InlineKeyboardMarkup]:
    row = session.balances[idx]
    entries = db.get_dues_entries(
        session.chat_id,
        user_id=row["user_id"],
        member_name=row["member_name"] if row["user_id"] is None else None,
        limit=10,
    )
    lines = [f"📜 *{_esc_md(row['member_name'])}* — recent entries:"]
    if not entries:
        lines.append("_No ledger entries found._")
    for e in entries:
        sign = "+" if e["amount"] > 0 else ""
        ts = str(e.get("created_at", ""))[:10]
        memo = _esc_md(e.get("memo") or "")
        lines.append(f"  {sign}₹{e['amount']} {_esc_md(e['entry_type'])} {memo} [{ts}]")
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("⬅ Back", callback_data=f"pay_back:{idx}"))
    return "\n".join(lines), kb


# ── Public API ────────────────────────────────────────────────────────────────

async def send_payment_panel(chat_id: int) -> None:
    """Send the payment-marking panel — bare /mark_paid entry point."""
    balances = dues_svc.all_dues(chat_id, nonzero_only=True)["balances"][:_MAX_LISTED]
    session = _PaymentSession(chat_id=chat_id, balances=balances)
    text, kb = _list_view(session)
    sent = await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    _sessions[(chat_id, sent.message_id)] = session
    while len(_sessions) > _MAX_SESSIONS:
        _sessions.pop(next(iter(_sessions)))


async def _payment_admin_ok(call) -> bool:
    """Financial write — same admin gate as the penalty panel / settle_dues cards."""
    cid = call.message.chat.id
    if manager.get_admin_rights(cid):
        member = await bot.get_chat_member(cid, call.from_user.id)
        if member.status not in ("administrator", "creator"):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can mark payments", show_alert=True
            )
            return False
    return True


async def _refresh_list(cid: int, mid: int, session: "_PaymentSession") -> None:
    session.balances = dues_svc.all_dues(cid, nonzero_only=True)["balances"][:_MAX_LISTED]
    text, kb = _list_view(session)
    await safe_edit_text(cid, mid, text, parse_mode="Markdown")
    await safe_edit_markup(cid, mid, kb)


# ── Callback handler ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("pay_"))
async def payment_panel_callback(call):
    try:
        cid  = call.message.chat.id
        mid  = call.message.message_id
        data = call.data

        if not await _payment_admin_ok(call):
            return

        session = _sessions.get((cid, mid))
        if session is None:
            await bot.answer_callback_query(call.id, "Panel expired — run /mark_paid again.")
            return

        if data == "pay_tolist":
            # Refresh — someone may have been marked paid since the panel opened.
            await _refresh_list(cid, mid, session)
            await bot.answer_callback_query(call.id)
            return

        try:
            idx = int(data.split(":")[-1])
        except ValueError:
            await bot.answer_callback_query(call.id)
            return
        if idx < 0 or idx >= len(session.balances):
            await bot.answer_callback_query(call.id, "That entry is stale — go back to the list.")
            return

        if data.startswith("pay_pick:"):
            text, kb = _player_action_view(session, idx)
            await safe_edit_text(cid, mid, text, parse_mode="Markdown")
            await safe_edit_markup(cid, mid, kb)
            await bot.answer_callback_query(call.id)

        elif data.startswith("pay_full:"):
            row = session.balances[idx]
            actor = call.from_user
            actor_name = actor.first_name or actor.username or "Admin"
            async with manager.get_chat_write_lock(cid):
                result = dues_svc.mark_paid(
                    cid, row["member_name"], actor.id, actor_name,
                    amount=None, is_admin=True,
                )
            await send_md_fallback(cid, result["announcement"])
            await _refresh_list(cid, mid, session)
            await bot.answer_callback_query(call.id, "✅ Marked paid")

        elif data.startswith("pay_partial:"):
            row = session.balances[idx]
            _prune_pending(_pending_payment_input)
            _pending_payment_input[(cid, call.from_user.id)] = {
                "member_name": row["member_name"], "mid": mid,
                "_ts": datetime.now().timestamp(),
            }
            await bot.answer_callback_query(call.id)
            await safe_edit_text(
                cid, mid,
                f"✏️ Reply with the amount (₹) *{_esc_md(row['member_name'])}* paid.",
                parse_mode="Markdown",
            )
            await safe_edit_markup(cid, mid, InlineKeyboardMarkup())

        elif data.startswith("pay_history:"):
            text, kb = _history_view(session, idx)
            await safe_edit_text(cid, mid, text, parse_mode="Markdown")
            await safe_edit_markup(cid, mid, kb)
            await bot.answer_callback_query(call.id)

        elif data.startswith("pay_back:"):
            text, kb = _player_action_view(session, idx)
            await safe_edit_text(cid, mid, text, parse_mode="Markdown")
            await safe_edit_markup(cid, mid, kb)
            await bot.answer_callback_query(call.id)

        else:
            await bot.answer_callback_query(call.id)

    except Exception as exc:
        logging.exception("payment_panel_callback error")
        try:
            await bot.answer_callback_query(call.id, "Error — try /mark_paid <name> manually.")
        except Exception:
            pass
        await reply_error(call.message, exc)


@bot.message_handler(func=lambda m: (
    m.from_user is not None
    and (m.chat.id, m.from_user.id) in _pending_payment_input
    and m.text.strip().isdigit()
))
async def payment_partial_reply(message):
    try:
        cid = message.chat.id
        uid = message.from_user.id
        pending = _pending_payment_input.pop((cid, uid), None)
        if not pending:
            return
        amount = int(message.text.strip())
        actor_name = message.from_user.first_name or message.from_user.username or "Admin"

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_paid(
                cid, pending["member_name"], uid, actor_name,
                amount=amount, is_admin=True,
            )
        await send_md_fallback(cid, result["announcement"])

        mid = pending.get("mid")
        session = _sessions.get((cid, mid)) if mid is not None else None
        if session is not None:
            await _refresh_list(cid, mid, session)
    except Exception as e:
        await reply_error(message, e)
