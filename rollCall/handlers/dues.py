"""
Dues & Treasury handlers.

Commands (all admin-only unless noted):
  /settle_dues [subsidy] [::N]
  /mark_penalty tier_name player_name
  /waive name amount [reason]
  /set_collector name [paid] [::N]
  /mark_paid /paid name [amount]      — admin OR designated collector
  /reimburse name amount [reason]
  /add_adhoc name
  /cancel_game_dues [::N]
  /my_dues /md                        — user-scoped, own balance only
  /dues                               — admin: full group ledger
  /fund                               — all: fund balance
  /fund_history /fh [page]            — all: fund history paginated
  /log_expense /le amount description
  /fund_topup amount [description]
  /remind_dues
  /penalties                          — list defined penalty tiers
  /add_penalty name amount [description]
  /remove_penalty name
  /set_upi vpa@bank
  /set_round_step step
  /enable_dues / /disable_dues

Ledger mutation announcements always post, even in shh mode (durability).
"""
import asyncio
import logging
from datetime import datetime

import db as _db
from bot_state import (
    bot, reply_error, _log_task_exc, send_md_fallback, _esc_md,
    safe_edit_markup, safe_edit_text, _pending_subsidy_input, _prune_pending,
    is_chat_admin,
)
from exceptions import (
    duesGameAlreadyClosed, duesNothingToClose,
    incorrectParameter, insufficientPermissions, parameterMissing,
)
from functions import admin_rights
from rollcall_manager import manager
from services import dues as dues_svc
from services import rollcalls as rollcalls_svc
from utils.text import parse_rc_suffix as _parse_rc_suffix
from handlers.lifecycle import _post_end_cleanup


def _require_dues_enabled(cid: int) -> None:
    """Raise if Dues & Treasury is not enabled for this chat."""
    chat = _db.get_or_create_chat(cid)
    if not chat.get("dues_enabled"):
        raise insufficientPermissions(
            "Dues & Treasury is not enabled for this group.\n"
            "An admin can enable it with /enable_dues."
        )


def _cmd(text: str) -> str:
    """Extract the bare command name from a message (strips /cmd@botname prefix)."""
    return (text.split(" ")[0]).split("@")[0].lower()


def _parse_args(text: str) -> list[str]:
    """Split message into args, stripping the command prefix."""
    return text.split(" ")[1:]


# _parse_rc_suffix moved to utils.text.parse_rc_suffix (shared with
# handlers/admin.py and handlers/lists.py) — re-imported under the old name
# below so the rest of this file's call sites are unchanged.


# ── /settle_dues ─────────────────────────────────────────────────────────────
#
# Guided flow (no args, the common case): resolve a target game → a 0-IN
# game offers cancel/skip instead of a financial split → the penalty panel
# opens scoped to that game → tapping "Done" there hands off to the
# confirm/subsidy card below → tapping a subsidy preset (or replying with a
# custom amount) performs the actual close.
#
# The explicit-args form (/settle_dues <subsidy> [::N]) is a deliberate fast
# path that skips all of the above — direct close, no penalty panel, no
# confirm card — unchanged from before this flow existed.

def _fmt_unsettled_label(game: dict) -> str:
    title = game.get("title") or f"game #{game['id']}"
    ended = str(game.get("ended_at") or "")[:10]
    label = f"📋 {title}" + (f" — {ended}" if ended else "")
    return label[:64]


async def _send_unsettled_picker(cid: int, games: list, intro: str) -> None:
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=1)
    for g in games:
        markup.add(InlineKeyboardButton(_fmt_unsettled_label(g), callback_data=f"settle_pick:{g['id']}"))
    await bot.send_message(cid, intro, reply_markup=markup)


# (cid, rollcall_id) → message_id of the pinned settle nudge, so it can be
# unpinned and de-buttoned once the game is actually settled. In-memory only:
# after a restart a stale pin just stays until an admin unpins it by hand —
# harmless, and the nudge's button still works (it re-reads DB state).
_settle_nudge_msgs: dict[tuple[int, int], int] = {}


async def send_settle_nudge(cid: int, rollcall_id: int, title: str = "") -> None:
    """Persistent 'books still open' nudge after a dues-enabled game ends:
    a Settle-now button plus a best-effort silent pin, so the reminder can't
    scroll away like the old one-line text did."""
    total = len(dues_svc.list_unsettled_games(cid)["games"])
    lines = ["💰 Game over — dues to settle." if not title
             else f"💰 *{_esc_md(title)}* is over — dues to settle."]
    if total > 1:
        lines.append(f"⚠️ {total} games are now waiting to be settled.")
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("💰 Settle now", callback_data=f"settle_now:{rollcall_id}"))
    sent = await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)
    _settle_nudge_msgs[(cid, rollcall_id)] = sent.message_id
    try:
        await bot.pin_chat_message(cid, sent.message_id, disable_notification=True)
    except Exception:
        pass  # bot lacks pin rights — the button message alone still stands out


async def _clear_settle_nudge(cid: int, rollcall_id: int) -> None:
    """Unpin and de-button the nudge once its game is settled (best-effort)."""
    mid = _settle_nudge_msgs.pop((cid, rollcall_id), None)
    if mid is None:
        return
    try:
        await bot.unpin_chat_message(cid, mid)
    except Exception:
        pass
    from telebot.types import InlineKeyboardMarkup
    await safe_edit_markup(cid, mid, InlineKeyboardMarkup())


async def _settle_admin_ok(call) -> bool:
    """Shared admin gate for every /settle_dues inline button — financial
    writes, same pattern as the penalty panel / collector picker."""
    cid = call.message.chat.id
    if await is_chat_admin(cid, call.from_user.id):
        return True
    await bot.answer_callback_query(
        call.id, "⛔ Only admins can settle dues", show_alert=True
    )
    return False


async def _send_remaining_unsettled_nudge(cid: int) -> None:
    remaining = dues_svc.list_unsettled_games(cid)["games"]
    if remaining:
        n = len(remaining)
        await send_md_fallback(
            cid,
            f"💰 {n} more unsettled game{'s' if n != 1 else ''} — `/settle_dues` to continue.",
        )


async def _finish_settle_dues(cid: int, result: dict, rc_idx_fallback: int = 0) -> None:
    """Shared post-close side effects: announcement, QR, receipt, post-end
    cleanup, and a nudge listing any games still unsettled."""
    await _clear_settle_nudge(cid, result.get("rollcall_id"))

    # Always post announcement — financial record
    await send_md_fallback(cid, result["announcement"])

    # QR code + VPA (non-blocking best-effort). Prefer the per-game collector
    # UPI (already resolved by dues_svc.close_game — same source as the
    # announcement text) over the group fallback, so the QR always matches
    # what was posted.
    upi = result.get("upi_vpa")
    if upi:
        asyncio.create_task(_send_close_qr(cid, upi, result["per_head"])).add_done_callback(_log_task_exc)

    # Receipt card (non-blocking best-effort)
    asyncio.create_task(_send_close_receipt(cid, result)).add_done_callback(_log_task_exc)

    # If an active rollcall was ended, run the standard post-end cleanup
    end_res = result.get("end_result")
    if end_res:
        await _post_end_cleanup(
            cid,
            end_res.get("rc_number_ended_1based", rc_idx_fallback + 1),
            end_res,
            rc_title=result.get("title", ""),
            settle_nudge=False,
        )

    await _send_remaining_unsettled_nudge(cid)


# ── Zero-IN games: dismiss without a financial split ──────────────────────────

async def _show_empty_game_card(cid: int, rollcall_id: int, title: str) -> None:
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🗑 Cancel this game", callback_data=f"settle_empty:{rollcall_id}"),
        InlineKeyboardButton("⏭ Skip for now", callback_data=f"settle_skip:{rollcall_id}"),
    )
    await bot.send_message(
        cid,
        f"⚠️ *{_esc_md(title)}* has no IN players — nothing to split.",
        parse_mode="Markdown",
        reply_markup=markup,
    )


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_now:"))
async def settle_now_callback(call):
    """Settle-now button on the persistent post-end nudge — same entry as a
    bare /settle_dues, but pre-scoped to the game the nudge was posted for."""
    try:
        cid = call.message.chat.id
        if not await _settle_admin_ok(call):
            return
        rollcall_id = int(call.data.split(":", 1)[1])
        if not _db.get_or_create_chat(cid).get("dues_enabled"):
            # Nudge pinned before /disable_dues — its button must not keep
            # opening the settle flow while dues is off.
            await bot.answer_callback_query(
                call.id, "Dues is disabled for this group (/enable_dues to turn it back on).",
                show_alert=True,
            )
            return
        await bot.answer_callback_query(call.id)
        if _db.get_game_closure(rollcall_id):
            # Someone settled it while the nudge sat pinned.
            await _clear_settle_nudge(cid, rollcall_id)
            from telebot.types import InlineKeyboardMarkup
            await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
            await bot.send_message(cid, "✅ Already settled — books are closed for this game.")
            return
        row = _db.get_rollcall(rollcall_id) or {}
        await _begin_settlement(cid, rollcall_id, row.get("title") or "<Empty>")
    except Exception as exc:
        logging.exception("settle_now_callback error")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_empty:"))
async def settle_empty_callback(call):
    try:
        cid = call.message.chat.id
        if not await _settle_admin_ok(call):
            return
        rollcall_id = int(call.data.split(":", 1)[1])
        await bot.answer_callback_query(call.id, "Cancelling…")

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.close_empty_game(
                cid, rollcall_id,
                call.from_user.id,
                call.from_user.first_name or call.from_user.username or "Admin",
            )

        await _clear_settle_nudge(cid, rollcall_id)
        await safe_edit_text(cid, call.message.message_id, result["announcement"], parse_mode="Markdown")
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
        await _send_remaining_unsettled_nudge(cid)
    except Exception as exc:
        logging.exception("settle_empty_callback error")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_skip:"))
async def settle_skip_callback(call):
    try:
        cid = call.message.chat.id
        await bot.answer_callback_query(call.id, "Skipped")
        await safe_edit_text(
            cid, call.message.message_id,
            "⏭ Skipped — still unsettled, run `/settle_dues` again anytime.",
            parse_mode="Markdown",
        )
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
    except Exception as exc:
        logging.exception("settle_skip_callback error")
        await reply_error(call.message, exc)


# ── Guided resolve → penalty panel handoff ────────────────────────────────────

async def _begin_settlement(cid: int, rollcall_id: int, title: str) -> None:
    """Entry point once a settle target is resolved (freshly ended or already
    sitting unsettled — this function re-reads current state either way, so
    it doesn't need to know which). A 0-IN game gets the cancel/skip card;
    otherwise the penalty panel opens scoped to this game, and its "Done"
    button hands off to the confirm/subsidy card (see handlers.penalty_panel's
    pen_d branch)."""
    in_users = _db.get_rollcall_in_users(rollcall_id)
    if not in_users:
        await _show_empty_game_card(cid, rollcall_id, title)
        return

    row = _db.get_rollcall(rollcall_id) or {}
    ghost_eligible = bool(
        manager.get_ghost_tracking_enabled(cid) and not row.get("absent_marked")
    )
    from handlers.penalty_panel import send_penalty_panel
    opened = await send_penalty_panel(cid, rollcall_id, title, ghost_eligible=ghost_eligible)
    if not opened:
        # No panel (no tiers configured, or nobody to mark) means no Done tap
        # will ever fire — continue straight to the confirm/subsidy card so
        # the guided flow doesn't silently dead-end here.
        await show_settle_confirm(cid, rollcall_id, title)


# ── Confirm / subsidy card ─────────────────────────────────────────────────────

async def show_settle_confirm(cid: int, rollcall_id: int, title: str, mid: int | None = None) -> None:
    """Preview card shown after penalty marking is done for this game.
    Preset subsidy buttons ARE the confirmation — tapping one directly closes
    the game with that amount, no separate extra tap required."""
    row = _db.get_rollcall(rollcall_id) or {}
    ground_cost = dues_svc._parse_ground_cost(row.get("event_fee"))
    in_count = len(_db.get_rollcall_in_users(rollcall_id))
    step = dues_svc.get_dues_settings(cid)["dues_round_step"]
    per_head = 0
    if ground_cost > 0 and in_count > 0:
        per_head, _ = dues_svc.compute_shares(ground_cost, 0, in_count, step)
    fund_balance = dues_svc.fund_summary(cid)["fund_balance"]

    text = (
        f"💰 *{_esc_md(title)}* — ready to close\n"
        f"₹{ground_cost} ÷ {in_count} player{'s' if in_count != 1 else ''} → ₹{per_head}/head (no subsidy)\n"
        f"Fund balance: ₹{fund_balance}"
    )
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=3)
    markup.row(
        InlineKeyboardButton("No subsidy", callback_data=f"settle_confirm:{rollcall_id}:0"),
        InlineKeyboardButton("−₹50", callback_data=f"settle_confirm:{rollcall_id}:50"),
        InlineKeyboardButton("−₹100", callback_data=f"settle_confirm:{rollcall_id}:100"),
    )
    markup.row(
        InlineKeyboardButton("✏️ Custom amount", callback_data=f"settle_custom:{rollcall_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"settle_cancel:{rollcall_id}"),
    )
    if mid is not None:
        await safe_edit_text(cid, mid, text, parse_mode="Markdown")
        await safe_edit_markup(cid, mid, markup)
    else:
        await bot.send_message(cid, text, parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_confirm:"))
async def settle_confirm_callback(call):
    try:
        cid = call.message.chat.id
        if not await _settle_admin_ok(call):
            return
        _, rc_id_s, subsidy_s = call.data.split(":")
        rollcall_id, subsidy = int(rc_id_s), int(subsidy_s)
        await bot.answer_callback_query(call.id, "Closing…")
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())

        async with manager.get_chat_write_lock(cid):
            result = await dues_svc.close_game(
                cid, subsidy=subsidy,
                admin_uid=call.from_user.id,
                admin_name=call.from_user.first_name or call.from_user.username or "Admin",
                target_rollcall_id=rollcall_id,
            )

        await _finish_settle_dues(cid, result)
    except Exception as exc:
        logging.exception("settle_confirm_callback error")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_custom:"))
async def settle_custom_callback(call):
    try:
        cid = call.message.chat.id
        if not await _settle_admin_ok(call):
            return
        rollcall_id = int(call.data.split(":", 1)[1])
        row = _db.get_rollcall(rollcall_id) or {}
        title = row.get("title") or "<Empty>"

        _prune_pending(_pending_subsidy_input)
        _pending_subsidy_input[(cid, call.from_user.id)] = {
            "rollcall_id": rollcall_id, "title": title,
            "_ts": datetime.now().timestamp(),
        }
        await bot.answer_callback_query(call.id)
        await safe_edit_text(
            cid, call.message.message_id,
            f"✏️ Reply with the subsidy amount (₹) for *{_esc_md(title)}*, or 0 for none.",
            parse_mode="Markdown",
        )
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
    except Exception as exc:
        logging.exception("settle_custom_callback error")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_cancel:"))
async def settle_cancel_callback(call):
    try:
        cid = call.message.chat.id
        await bot.answer_callback_query(call.id, "Cancelled")
        await safe_edit_text(
            cid, call.message.message_id,
            "❌ Cancelled — still unsettled, run `/settle_dues` again anytime.",
            parse_mode="Markdown",
        )
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
    except Exception as exc:
        logging.exception("settle_cancel_callback error")
        await reply_error(call.message, exc)


@bot.message_handler(func=lambda m: (
    m.from_user is not None
    and (m.chat.id, m.from_user.id) in _pending_subsidy_input
    and m.text.strip().lstrip("-").isdigit()
))
async def settle_subsidy_reply(message):
    try:
        cid = message.chat.id
        uid = message.from_user.id
        pending = _pending_subsidy_input.pop((cid, uid), None)
        if not pending:
            return
        subsidy = int(message.text.strip())

        async with manager.get_chat_write_lock(cid):
            result = await dues_svc.close_game(
                cid, subsidy=subsidy,
                admin_uid=uid,
                admin_name=message.from_user.first_name or message.from_user.username or "Admin",
                target_rollcall_id=pending["rollcall_id"],
            )

        await _finish_settle_dues(cid, result)
    except Exception as e:
        await reply_error(message, e)


# ── /settle_dues handler ───────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/settle_dues")
async def settle_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /settle_dues")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)

        if not args:
            rollcalls = manager.get_rollcalls(cid)
            if rollcalls:
                # Active rollcall — end it now (same as /erc), then continue
                # the guided flow (zero-IN check → penalty panel → confirm)
                # on the game that was just ended.
                rc = manager.get_rollcall(cid, 0)
                title = rc.title or "<Empty>"
                async with manager.get_chat_write_lock(cid):
                    end_result = await rollcalls_svc.end_rollcall(
                        cid, 0,
                        message.from_user.id, message.from_user.first_name,
                        message.from_user.username,
                    )
                await _post_end_cleanup(
                    cid, end_result["rc_number_ended_1based"], end_result, rc_title=title,
                    settle_nudge=False,
                )
                await _begin_settlement(cid, end_result["rc_db_id"], title)
                return

            unsettled = dues_svc.list_unsettled_games(cid)["games"]
            if not unsettled:
                raise duesNothingToClose(
                    "No game to close. Start and end a rollcall first, or check if it was already closed."
                )
            if len(unsettled) > 1:
                await _send_unsettled_picker(
                    cid, unsettled,
                    f"💰 {len(unsettled)} games waiting to be settled — pick one:",
                )
                return

            game = unsettled[0]
            await _begin_settlement(cid, game["id"], game.get("title") or "<Empty>")
            return

        # Explicit subsidy/::N args — fast path, unchanged: direct close, no
        # penalty panel, no confirm card.
        rc_idx, args = _parse_rc_suffix(args)

        subsidy = 0
        if args:
            try:
                subsidy = int(args[0])
            except ValueError:
                raise incorrectParameter("Subsidy must be a whole number (₹). Example: /settle_dues 60")

        async with manager.get_chat_write_lock(cid):
            result = await dues_svc.close_game(
                cid, subsidy=subsidy,
                admin_uid=message.from_user.id,
                admin_name=message.from_user.first_name or message.from_user.username or "Admin",
                rc_number=rc_idx,
            )

        await _finish_settle_dues(cid, result, rc_idx_fallback=rc_idx)
    except Exception as e:
        await reply_error(message, e)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("settle_pick:"))
async def settle_pick_callback(call):
    try:
        cid = call.message.chat.id
        if not await _settle_admin_ok(call):
            return
        target_id = int(call.data.split(":", 1)[1])
        await bot.answer_callback_query(call.id)
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())

        row = _db.get_rollcall(target_id) or {}
        title = row.get("title") or "<Empty>"
        await _begin_settlement(cid, target_id, title)
    except Exception as exc:
        logging.exception("settle_pick_callback error")
        await reply_error(call.message, exc)


# ── /mark_penalty ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_penalty")
async def mark_penalty(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /mark_penalty")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing(
                "Usage: /mark_penalty <tier_name> <player_name>\n"
                "Example: /mark_penalty late_short Alice\n"
                "Use /penalties to see defined tiers."
            )
        tier_name = args[0]
        player_name = " ".join(args[1:])

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_penalty(
                cid, tier_name, player_name,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /waive ────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/waive")
async def waive(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /waive")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing("Usage: /waive <name> <amount> [reason]")
        # The amount marks where the name ends — scan from index 1 onward (the
        # name always has at least one token) for the first integer-looking
        # token, so multi-word proxy names (e.g. "Team B") resolve correctly
        # instead of the old fixed args[0]/args[1] split truncating the name.
        amount_idx = next(
            (i for i, tok in enumerate(args) if i > 0 and tok.lstrip("-").isdigit()),
            None,
        )
        if amount_idx is None:
            raise incorrectParameter("Amount must be a whole number. Example: /waive Alice 75 injured")
        name = " ".join(args[:amount_idx])
        amount = int(args[amount_idx])
        reason = " ".join(args[amount_idx + 1:])

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.waive(
                cid, name, amount, reason,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /set_collector ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/set_collector")
async def set_collector(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /set_collector")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        rc_idx, args = _parse_rc_suffix(args)
        if not args:
            raise parameterMissing("Usage: /set_collector <name> [paid] [upi@bank] [::N]")

        # Fixed format: <name> [paid] [upi@bank] — only the trailing token(s)
        # are ever inspected for "paid"/UPI, so a name can never be split
        # apart by an @-shaped word in the middle of it.
        import re as _re
        _UPI_PAT = _re.compile(r'^[a-zA-Z0-9.\-_]{2,}@[a-zA-Z]{2,}$')
        collector_upi = None
        if args and _UPI_PAT.match(args[-1]):
            collector_upi = args[-1]
            args = args[:-1]

        paid_ground = False
        if args and args[-1].lower() == "paid":
            paid_ground = True
            args = args[:-1]
        name = " ".join(args)
        if not name:
            raise parameterMissing("Usage: /set_collector <name> [paid] [upi@bank]")

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.set_collector(
                cid, name, paid_ground,
                message.from_user.id,
                message.from_user.first_name or "Admin",
                rc_number=rc_idx,
                collector_upi=collector_upi,
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /pick_collector — inline selection panel ──────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/pick_collector")
async def pick_collector(message):
    """Show the active rollcall's real IN members as buttons — tap to set collector."""
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /pick_collector")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        rc_idx, _ = _parse_rc_suffix(args)

        rollcalls = manager.get_rollcalls(cid)
        if not rollcalls or rc_idx >= len(rollcalls):
            from exceptions import rollCallNotStarted
            raise rollCallNotStarted(
                "No active rollcall. For an already-closed game use /set_collector <name>."
            )
        rc = rollcalls[rc_idx]
        real_in = [u for u in rc.inList if isinstance(u.user_id, int) and u.user_id > 0]
        if not real_in:
            # Nobody real IN yet — jump straight to the all-members panel so a
            # non-playing collector (e.g. venue owner) can still be picked.
            await _show_member_collector_panel(cid, rc_idx, rc.title)
            return

        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        markup = InlineKeyboardMarkup(row_width=2)
        buttons = [
            InlineKeyboardButton(u.name, callback_data=f"pickcol_{rc_idx}_{u.user_id}")
            for u in real_in
        ]
        markup.add(*buttons)
        markup.row(InlineKeyboardButton(
            "👥 Someone not playing…", callback_data=f"pickcol_more_{rc_idx}"))
        await bot.send_message(
            cid,
            f"📦 Who is collecting for *{_esc_md(rc.title or 'this game')}*?",
            parse_mode="Markdown", reply_markup=markup,
        )
    except Exception as e:
        await reply_error(message, e)


# Telegram caps inline keyboards at 100 buttons; well before that the panel
# becomes unusable, so show the most recently active members and point at
# /set_collector <name> for the long tail.
_MEMBER_PANEL_LIMIT = 30


async def _show_member_collector_panel(cid: int, rc_idx: int, rc_title: str = "",
                                       mid: int | None = None) -> None:
    """Collector picker over ALL active tracked members (not just IN voters) —
    covers the non-playing collector, e.g. a venue owner who never votes."""
    rollcalls = manager.get_rollcalls(cid)
    rc = rollcalls[rc_idx] if rc_idx < len(rollcalls) else None
    in_ids = {u.user_id for u in rc.inList} if rc else set()
    members = [m for m in _db.get_active_members(cid) if m["user_id"] not in in_ids]
    if not members:
        raise incorrectParameter(
            "No other known members to pick from — use /set_collector <name>."
        )

    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(*[
        InlineKeyboardButton(
            m.get("first_name") or m.get("username") or str(m["user_id"]),
            callback_data=f"pickcol_{rc_idx}_{m['user_id']}",
        )
        for m in members[:_MEMBER_PANEL_LIMIT]
    ])
    overflow = ""
    if len(members) > _MEMBER_PANEL_LIMIT:
        overflow = f"\nShowing {_MEMBER_PANEL_LIMIT} — for anyone else use /set_collector <name>."
    text = (f"📦 Who is collecting for *{_esc_md(rc_title or 'this game')}*?\n"
            f"(all known members, IN or not){overflow}")
    if mid is not None:
        await safe_edit_text(cid, mid, text, parse_mode="Markdown")
        await safe_edit_markup(cid, mid, markup)
    else:
        await bot.send_message(cid, text, parse_mode="Markdown", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("pickcol_more_"))
async def pick_collector_more_callback(call):
    """'Someone not playing…' — swap the IN-only picker for the all-members one."""
    try:
        cid = call.message.chat.id
        if not await is_chat_admin(cid, call.from_user.id):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can set the collector", show_alert=True
            )
            return
        rc_idx = int(call.data.rsplit("_", 1)[1])
        await bot.answer_callback_query(call.id)
        rollcalls = manager.get_rollcalls(cid)
        title = rollcalls[rc_idx].title if rc_idx < len(rollcalls) else ""
        await _show_member_collector_panel(cid, rc_idx, title, mid=call.message.message_id)
    except Exception as exc:
        logging.exception("pick_collector_more_callback failed")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("pickcol_")
                            and not call.data.startswith("pickcol_more_"))
async def pick_collector_callback(call):
    try:
        cid = call.message.chat.id
        # Financial write — same admin gate as the penalty panel.
        if not await is_chat_admin(cid, call.from_user.id):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can set the collector", show_alert=True
            )
            return

        _, rc_idx_s, uid_s = call.data.split("_")
        rc_idx, uid = int(rc_idx_s), int(uid_s)

        async with manager.get_chat_write_lock(cid):
            rollcalls = manager.get_rollcalls(cid)
            if rc_idx >= len(rollcalls):
                await bot.answer_callback_query(call.id, "That rollcall has ended.", show_alert=True)
                return
            rc = rollcalls[rc_idx]
            target = next(
                (u for u in rc.inList if isinstance(u.user_id, int) and u.user_id == uid),
                None,
            )
            if target is not None:
                target_name = target.name
            else:
                # All-members panel pick: the collector doesn't have to be IN
                # (non-playing venue owner etc.) — as long as they're a known
                # active member. set_collector resolves them by name below.
                member = next(
                    (m for m in _db.get_active_members(cid) if m["user_id"] == uid),
                    None,
                )
                if member is None:
                    await bot.answer_callback_query(
                        call.id, "That member is unknown now — reopen /pick_collector.",
                        show_alert=True,
                    )
                    return
                target_name = (member.get("username")
                               or member.get("first_name") or str(uid))
            result = dues_svc.set_collector(
                cid, target_name, False,
                call.from_user.id,
                call.from_user.first_name or "Admin",
                rc_number=rc_idx,
            )

        await bot.answer_callback_query(call.id, f"📦 {target_name} is collecting")
        try:
            await bot.edit_message_text(
                result["announcement"], cid, call.message.message_id,
            )
        except Exception:
            pass  # collector was already set and confirmed above; the panel text is cosmetic

        # UPI memory: if this member collected before with a personal UPI,
        # offer it back — one tap instead of retyping it every game. The
        # collector is already set either way; this card only decides where
        # the money routes.
        await _offer_collector_upi(cid, rc_idx, uid, target_name)
    except Exception:
        logging.exception("pick_collector_callback failed")
        try:
            await bot.answer_callback_query(call.id, "Could not set collector.", show_alert=True)
        except Exception:
            pass  # already logged above; nothing more useful to do if even the alert fails


# ── Collector UPI memory ──────────────────────────────────────────────────────
# game_closures permanently stores collector_uid + collector_upi, so a
# returning collector's last UPI can be suggested instead of retyped
# (most recent closure wins → correcting it once self-heals future games).

# (cid, card_message_id) → {"rc_idx", "uid", "name", "upi", "_ts"}
_pending_colupi: dict[tuple[int, int], dict] = {}
# (cid, admin_uid) → {"rc_idx", "name", "_ts"} — armed by the ✏️ New UPI button
_pending_colupi_input: dict[tuple[int, int], dict] = {}


async def _offer_collector_upi(cid: int, rc_idx: int, uid: int, target_name: str) -> None:
    last_upi = _db.get_last_collector_upi(cid, uid)
    if not last_upi:
        return  # first-time collector — group UPI fallback, current behavior
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f"✅ Use {last_upi}"[:64], callback_data="colupi_use"),
        InlineKeyboardButton("✏️ New UPI", callback_data="colupi_new"),
        InlineKeyboardButton("🏦 Group UPI", callback_data="colupi_grp"),
    )
    sent = await bot.send_message(
        cid,
        f"💳 Route this game's fees to {target_name}'s usual UPI `{_esc_md(last_upi)}`?",
        parse_mode="Markdown", reply_markup=markup,
    )
    _prune_pending(_pending_colupi)
    _pending_colupi[(cid, sent.message_id)] = {
        "rc_idx": rc_idx, "uid": uid, "name": target_name, "upi": last_upi,
        "_ts": datetime.now().timestamp(),
    }


@bot.callback_query_handler(func=lambda call: call.data in ("colupi_use", "colupi_new", "colupi_grp"))
async def collector_upi_callback(call):
    try:
        cid = call.message.chat.id
        if not await is_chat_admin(cid, call.from_user.id):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can set the collector UPI", show_alert=True)
            return
        pending = _pending_colupi.pop((cid, call.message.message_id), None)
        if pending is None:
            await bot.answer_callback_query(
                call.id, "This prompt has expired — use /set_collector <name> <upi>.",
                show_alert=True)
            return

        from telebot.types import InlineKeyboardMarkup
        if call.data == "colupi_grp":
            await bot.answer_callback_query(call.id, "Using the group UPI")
            await safe_edit_text(cid, call.message.message_id,
                                 "🏦 Group UPI will be used for this game.")
            await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
            return

        if call.data == "colupi_new":
            _prune_pending(_pending_colupi_input)
            _pending_colupi_input[(cid, call.from_user.id)] = {
                "rc_idx": pending["rc_idx"], "name": pending["name"],
                "_ts": datetime.now().timestamp(),
            }
            await bot.answer_callback_query(call.id)
            await safe_edit_text(
                cid, call.message.message_id,
                f"✏️ Reply with {pending['name']}'s new UPI (e.g. `name@okbank`).",
                parse_mode="Markdown")
            await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
            return

        # colupi_use — re-set the collector with the remembered UPI attached
        await bot.answer_callback_query(call.id)
        async with manager.get_chat_write_lock(cid):
            result = dues_svc.set_collector(
                cid, pending["name"], False,
                call.from_user.id,
                call.from_user.first_name or "Admin",
                rc_number=pending["rc_idx"],
                collector_upi=pending["upi"],
            )
        await safe_edit_text(cid, call.message.message_id, result["announcement"],
                             parse_mode="Markdown")
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
    except Exception as exc:
        logging.exception("collector_upi_callback error")
        await reply_error(call.message, exc)


@bot.message_handler(func=lambda m: (
    m.from_user is not None
    and m.text is not None
    and not m.text.startswith("/")
    and (m.chat.id, m.from_user.id) in _pending_colupi_input
    and "@" in m.text
))
async def collector_upi_reply(message):
    try:
        cid = message.chat.id
        uid = message.from_user.id
        pending = _pending_colupi_input.get((cid, uid))
        if not pending:
            return
        vpa = message.text.strip()
        if not dues_svc._UPI_RE.match(vpa):
            # Keep the prompt armed so the next reply retries.
            await reply_error(message, incorrectParameter(
                "That doesn't look like a UPI id (expected name@bank) — try again."))
            return
        _pending_colupi_input.pop((cid, uid), None)
        async with manager.get_chat_write_lock(cid):
            result = dues_svc.set_collector(
                cid, pending["name"], False,
                uid, message.from_user.first_name or "Admin",
                rc_number=pending["rc_idx"],
                collector_upi=vpa,
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /rotate_collector — round-robin auto-assignment ───────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/rotate_collector")
async def rotate_collector(message):
    """Toggle round-robin collector auto-assignment at /settle_dues."""
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /rotate_collector")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)

        if not args:
            on = bool(_db.get_or_create_chat(cid).get("collector_rotation"))
            await bot.send_message(
                cid,
                f"🔄 Collector rotation is {'ON' if on else 'OFF'}.\n"
                "Usage: /rotate_collector on · /rotate_collector off\n"
                "When on: if no collector was set before /settle_dues, the next "
                "IN member (round-robin) is assigned automatically. "
                "/set_collector or /pick_collector always override the rotation.",
            )
            return

        arg = args[0].lower()
        if arg in ("on", "true", "1", "enable"):
            async with manager.get_chat_write_lock(cid):
                result = dues_svc.set_collector_rotation(cid, enabled=True)
            await bot.send_message(cid, result["announcement"])
        elif arg in ("off", "false", "0", "disable"):
            async with manager.get_chat_write_lock(cid):
                result = dues_svc.set_collector_rotation(cid, enabled=False)
            await bot.send_message(cid, result["announcement"])
        else:
            raise incorrectParameter("Usage: /rotate_collector on · /rotate_collector off")
    except Exception as e:
        await reply_error(message, e)


# ── /mark_paid (/paid) ────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_paid")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/paid")
async def mark_paid(message):
    try:
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if not args:
            # No args: admin-only panel listing everyone with an outstanding
            # balance. /mark_paid <name> [amount] below is unchanged — still
            # works for admins AND the designated collector, from a script or
            # muscle memory.
            if await admin_rights(message, manager) is False:
                raise insufficientPermissions(
                    "Admin only: /mark_paid with no arguments. "
                    "The designated collector can still use /mark_paid <name> [amount]."
                )
            from handlers.payment_panel import send_payment_panel
            await send_payment_panel(cid)
            return

        amount = None
        if len(args) >= 2:
            try:
                amount = int(args[-1])
                args = args[:-1]
            except ValueError:
                pass
        name = " ".join(args)

        is_admin = await admin_rights(message, manager) is not False

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_paid(
                cid, name,
                actor_uid=message.from_user.id,
                actor_name=message.from_user.first_name or "someone",
                amount=amount,
                is_admin=is_admin,
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /reimburse ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/reimburse")
async def reimburse(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /reimburse")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing("Usage: /reimburse <name> <amount> [reason]")
        # Same multi-word-name handling as /waive — scan for the amount
        # token instead of assuming the name is always exactly args[0].
        amount_idx = next(
            (i for i, tok in enumerate(args) if i > 0 and tok.lstrip("-").isdigit()),
            None,
        )
        if amount_idx is None:
            raise incorrectParameter("Amount must be a whole number.")
        name = " ".join(args[:amount_idx])
        amount = int(args[amount_idx])
        reason = " ".join(args[amount_idx + 1:])

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.reimburse(
                cid, name, amount, reason,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /add_adhoc ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/add_adhoc")
async def add_adhoc(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /add_adhoc")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /add_adhoc <name>")
        name = " ".join(args)

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.add_adhoc(
                cid, name,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /cancel_game_dues ─────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/cancel_game_dues")
async def cancel_game_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /cancel_game_dues")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        # ::N suffix — 0-based index into closures ordered newest-first.
        # No suffix (or ::1) → latest; ::2 → second most recent.
        n_idx, _ = _parse_rc_suffix(args)

        async with manager.get_chat_write_lock(cid):
            # Fetch the target closure inside the lock so a concurrent /settle_dues
            # cannot shift ordering between fetch and reversal.
            closure = _db.get_nth_game_closure(cid, n_idx)
            if closure is None:
                raise duesNothingToClose(
                    "No closed game found to cancel."
                    + (" Use /cancel_game_dues ::2 for an older game." if n_idx == 0 else "")
                )
            result = dues_svc.cancel_game_credit(
                cid, closure["rollcall_id"],
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /my_dues (/md) — user-scoped ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/my_dues")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/md")
async def my_dues(message):
    try:
        cid = message.chat.id
        _require_dues_enabled(cid)
        uid = message.from_user.id
        result = dues_svc.my_dues(cid, uid)
        balance = result["balance"]
        entries = result["entries"]

        if balance == 0 and not entries:
            await bot.send_message(cid, "✅ You have no outstanding dues.")
            return

        lines = [f"💰 *Your dues:* ₹{balance}" if balance > 0
                 else f"💳 *Your balance:* ₹{abs(balance)} credit"]
        if entries:
            lines.append("\n*Recent entries:*")
            for e in entries[:5]:
                sign = "+" if e["amount"] > 0 else ""
                memo = _esc_md(e.get("memo") or "")
                lines.append(f"  {sign}₹{e['amount']} {_esc_md(e['entry_type'])} {memo}")

        settings = dues_svc.get_dues_settings(cid)
        if balance > 0:
            game_upi = settings.get("upi_vpa")
            treasury_upi = settings.get("treasury_upi")
            if treasury_upi and game_upi and treasury_upi != game_upi:
                lines.append(f"\n💳 Game fees → `{game_upi}`")
                lines.append(f"🏦 Penalties → `{treasury_upi}`")
            elif game_upi:
                lines.append(f"\n💳 Pay: `{game_upi}`")
            elif treasury_upi:
                lines.append(f"\n💳 Pay: `{treasury_upi}`")

        await send_md_fallback(cid, "\n".join(lines))
    except Exception as e:
        await reply_error(message, e)


# ── /dues — admin full ledger ─────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues")
async def dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /dues — use /my_dues for your own balance.")
        cid = message.chat.id
        _require_dues_enabled(cid)
        result = dues_svc.all_dues(cid, nonzero_only=False)
        balances = result["balances"]
        if not balances:
            await bot.send_message(cid, "✅ No dues recorded yet.")
            return

        lines = ["📋 *Dues ledger:*"]
        for b in balances:
            name = _esc_md(b["member_name"])
            bal = b["balance"]
            icon = "🔴" if bal > 0 else ("🟢" if bal < 0 else "⚪")
            lines.append(f"{icon} {name}: ₹{bal}")

        await send_md_fallback(cid, "\n".join(lines))
    except Exception as e:
        await reply_error(message, e)


# ── /fund ─────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/fund")
async def fund(message):
    try:
        cid = message.chat.id
        _require_dues_enabled(cid)
        result = dues_svc.fund_summary(cid)
        bal = result["fund_balance"]
        await bot.send_message(
            cid,
            f"🏦 *Group fund balance:* ₹{bal}\n"
            "_(Balance = booked amounts; not necessarily cash in hand.)_",
            parse_mode="Markdown",
        )
    except Exception as e:
        await reply_error(message, e)


# ── /fund_history (/fh) ───────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/fund_history")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/fh")
async def fund_history(message):
    try:
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        page = 1
        if args:
            try:
                page = max(1, int(args[0]))
            except ValueError:
                pass
        per_page = 10
        offset = (page - 1) * per_page
        result = dues_svc.fund_history(cid, limit=per_page, offset=offset)
        txns = result["transactions"]
        total = result["total"]
        total_pages = max(1, (total + per_page - 1) // per_page)

        if not txns:
            await bot.send_message(cid, "🏦 No fund transactions yet.")
            return

        lines = [f"🏦 *Fund history* (page {page}/{total_pages}):"]
        for t in txns:
            sign = "+" if t["amount"] > 0 else ""
            ts = str(t.get("created_at", ""))[:10]
            desc = _esc_md(t.get("description", ""))
            lines.append(f"  {sign}₹{t['amount']} {_esc_md(t['txn_type'])} — {desc} [{ts}]")

        await send_md_fallback(cid, "\n".join(lines))
    except Exception as e:
        await reply_error(message, e)


# ── /log_expense (/le) ────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/log_expense")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/le")
async def log_expense(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /log_expense")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing("Usage: /log_expense <amount> <description>")
        try:
            amount = int(args[0])
        except ValueError:
            raise incorrectParameter("Amount must be a whole number. Example: /le 150 new balls")
        description = " ".join(args[1:])

        result = dues_svc.log_expense(
            cid, amount, description,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /fund_topup ───────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/fund_topup")
async def fund_topup(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /fund_topup")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /fund_topup <amount> [description]")
        try:
            amount = int(args[0])
        except ValueError:
            raise incorrectParameter("Amount must be a whole number. Example: /fund_topup 500 donations")
        description = " ".join(args[1:]) if len(args) > 1 else "manual top-up"

        result = dues_svc.fund_topup(
            cid, amount, description,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /remind_dues ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/remind_dues")
async def remind_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /remind_dues")
        cid = message.chat.id
        _require_dues_enabled(cid)
        result = dues_svc.remind_dues(cid)
        # Group summary (always)
        await send_md_fallback(cid, result["announcement"])
        # Individual DMs (best-effort, non-blocking)
        asyncio.create_task(_send_dues_dms(cid, result)).add_done_callback(_log_task_exc)
    except Exception as e:
        await reply_error(message, e)


# ── /set_upi ──────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/set_upi")
async def set_upi(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /set_upi")
        cid = message.chat.id
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /set_upi <vpa@bank>  e.g. /set_upi amit@upi")
        vpa = args[0]
        result = dues_svc.set_upi(
            cid, vpa,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /set_treasury_upi ─────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/set_treasury_upi")
async def set_treasury_upi(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /set_treasury_upi")
        cid = message.chat.id
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing(
                "Usage: /set_treasury_upi <vpa@bank>  e.g. /set_treasury_upi treasurer@upi"
            )
        vpa = args[0]
        result = dues_svc.set_treasury_upi(
            cid, vpa,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /penalties ───────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/penalties")
async def penalties(message):
    try:
        # No dues-enabled guard: /penalties is a setup-info command so admins can
        # review tiers before or after enabling dues, consistent with /add_penalty
        # and /remove_penalty which also bypass the guard.
        result = dues_svc.list_penalty_tiers(message.chat.id)
        await send_md_fallback(message.chat.id, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /add_penalty ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/add_penalty")
async def add_penalty(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /add_penalty")
        cid = message.chat.id
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing(
                "Usage: /add_penalty <name> <amount> [mins:<N>] [ditch] [description]\n"
                "  mins:<N>  — auto-select this tier when ≥N minutes late (/mark_late)\n"
                "  ditch     — mark as the no-show tier (/mark_ditch)\n"
                "Examples:\n"
                "  /add_penalty slightly_late 50 mins:1 under 15 min late\n"
                "  /add_penalty very_late 100 mins:20 significantly late\n"
                "  /add_penalty no_show 200 ditch missed the game"
            )
        try:
            amount = int(args[1])
        except ValueError:
            raise incorrectParameter("Amount must be a whole number (₹). Example: /add_penalty ditch 200")

        name = args[0]
        mins_threshold = None
        is_ditch_flag = False
        desc_parts = []
        for part in args[2:]:
            low = part.lower()
            if low.startswith("mins:"):
                try:
                    mins_threshold = int(low[5:])
                except ValueError:
                    raise incorrectParameter("mins: must be followed by a whole number. Example: mins:15")
            elif low == "ditch":
                is_ditch_flag = True
            else:
                desc_parts.append(part)

        # Preserve existing threshold/ditch flag when admin only updates the amount
        existing = _db.get_penalty_tier(cid, name)
        if mins_threshold is None and existing:
            mins_threshold = existing.get("late_minutes_threshold")
        if not is_ditch_flag and existing:
            is_ditch_flag = bool(existing.get("is_ditch", 0))

        result = dues_svc.add_penalty_tier(
            cid, name, amount, " ".join(desc_parts),
            message.from_user.id,
            message.from_user.first_name or "Admin",
            late_minutes_threshold=mins_threshold,
            is_ditch=is_ditch_flag,
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /mark_late ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_late")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/ml")
async def mark_late(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /mark_late")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing(
                "Usage: /mark_late <player_name> <minutes>\n"
                "Example: /mark_late Alice 20\n"
                "The correct tier is chosen automatically from configured thresholds."
            )
        try:
            minutes = int(args[-1])
        except ValueError:
            raise incorrectParameter(
                "Last argument must be the number of minutes late.\n"
                "Example: /mark_late Alice 20"
            )
        player_name = " ".join(args[:-1])
        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_late(
                cid, player_name, minutes,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /mark_ditch ───────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_ditch")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/mdt")
async def mark_ditch(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /mark_ditch")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing(
                "Usage: /mark_ditch <player_name>\n"
                "Example: /mark_ditch Bob"
            )
        player_name = " ".join(args)
        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_ditch(
                cid, player_name,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /remove_penalty ───────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/remove_penalty")
async def remove_penalty(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /remove_penalty")
        cid = message.chat.id
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /remove_penalty <tier_name>")
        name = args[0]
        result = dues_svc.remove_penalty_tier(
            cid, name,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /set_round_step ───────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/set_round_step")
async def set_round_step(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /set_round_step")
        cid = message.chat.id
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /set_round_step <step>  e.g. /set_round_step 5")
        try:
            step = int(args[0])
        except ValueError:
            raise incorrectParameter("Step must be a positive whole number.")
        result = dues_svc.set_round_step(
            cid, step,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await send_md_fallback(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /enable_dues / /disable_dues ─────────────────────────────────────────────

# Guided setup after /enable_dues: a live status card instead of a plain-text
# command dump — the critical miss it prevents is a group with no UPI set,
# which would leave every /settle_dues close without a payment address/QR.

_pending_upi_input: dict[tuple[int, int], dict] = {}


def _dues_setup_card(cid: int):
    """(text, markup) for the setup-status card. Re-rendered by the Re-check
    button so the admin can watch items turn green as they configure."""
    s = dues_svc.get_dues_settings(cid)
    tiers = _db.get_penalty_tiers(cid)
    upi, t_upi = s["upi_vpa"], s["treasury_upi"]

    lines = ["💰 *Dues & Treasury — setup*", ""]
    lines.append(f"✅ Group UPI: `{_esc_md(upi)}`" if upi
                 else "⚠️ Group UPI: *not set* — payment QRs need this")
    lines.append(f"✅ Treasury UPI: `{_esc_md(t_upi)}`" if t_upi
                 else "◻ Treasury UPI: optional (penalties/fund) — falls back to group UPI")
    lines.append(f"✅ Penalty tiers: {len(tiers)} defined (see `/penalties`)" if tiers
                 else "⚠️ Penalty tiers: none — `/add_penalty <name> <amount>`")
    lines.append(f"✅ Rounding: ₹{s['dues_round_step']} per-head steps (`/set_round_step`)")
    lines.append("")
    if upi:
        lines.append("All set — `/src` starts a game, `/settle_dues` closes the books after.")
    else:
        lines.append("Tap the button below and reply with your UPI to finish setup.")

    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []
    if not upi:
        buttons.append(InlineKeyboardButton("💳 Set UPI now", callback_data="dues_setup_upi"))
    buttons.append(InlineKeyboardButton("🔄 Re-check", callback_data="dues_setup_check"))
    markup.add(*buttons)
    return "\n".join(lines), markup


@bot.message_handler(func=lambda m: _cmd(m.text) == "/enable_dues")
async def enable_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /enable_dues")
        cid = message.chat.id
        was_enabled = bool(_db.get_or_create_chat(cid).get("dues_enabled"))
        _db.update_chat_settings(cid, dues_enabled=1)
        if not was_enabled:
            # Disabled → enabled transition: dues history starts NOW. Games
            # ended before this moment (pre-dues history, or games played
            # while dues was off) must not surface as "unsettled" — settling
            # one would retroactively charge members for a game the group
            # handled outside the system. Re-running /enable_dues while
            # already enabled deliberately does NOT restamp.
            dues_svc.stamp_dues_epoch(cid)
        dues_svc.seed_default_penalty_tiers(cid)
        await bot.send_message(
            cid,
            "✅ *Dues & Treasury enabled* for this group.\n\n"
            "Default penalty tiers seeded (edit with `/add_penalty` / `/remove_penalty`):\n"
            "• *late\\_short* ₹50 — under 15 min late\n"
            "• *late\\_long* ₹100 — 15+ min late\n"
            "• *ditch* ₹200 — no-show / absent",
            parse_mode="Markdown",
        )
        text, markup = _dues_setup_card(cid)
        await bot.send_message(cid, text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues_setup")
async def dues_setup(message):
    """Re-open the setup-status card anytime (also linked from /enable_dues)."""
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /dues_setup")
        cid = message.chat.id
        _require_dues_enabled(cid)
        text, markup = _dues_setup_card(cid)
        await bot.send_message(cid, text, parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        await reply_error(message, e)


@bot.callback_query_handler(func=lambda call: call.data == "dues_setup_check")
async def dues_setup_check_callback(call):
    try:
        cid = call.message.chat.id
        await bot.answer_callback_query(call.id)
        text, markup = _dues_setup_card(cid)
        await safe_edit_text(cid, call.message.message_id, text, parse_mode="Markdown")
        await safe_edit_markup(cid, call.message.message_id, markup)
    except Exception as exc:
        logging.exception("dues_setup_check_callback error")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data == "dues_setup_upi")
async def dues_setup_upi_callback(call):
    try:
        cid = call.message.chat.id
        if not await is_chat_admin(cid, call.from_user.id):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can set the UPI", show_alert=True
            )
            return
        _prune_pending(_pending_upi_input)
        _pending_upi_input[(cid, call.from_user.id)] = {
            "card_mid": call.message.message_id,
            "_ts": datetime.now().timestamp(),
        }
        await bot.answer_callback_query(call.id)
        await bot.send_message(
            cid, "✏️ Reply with the group UPI id (e.g. `name@okbank`).",
            parse_mode="Markdown",
        )
    except Exception as exc:
        logging.exception("dues_setup_upi_callback error")
        await reply_error(call.message, exc)


@bot.message_handler(func=lambda m: (
    m.from_user is not None
    and m.text is not None
    and not m.text.startswith("/")
    and (m.chat.id, m.from_user.id) in _pending_upi_input
    and "@" in m.text
))
async def dues_setup_upi_reply(message):
    try:
        cid = message.chat.id
        uid = message.from_user.id
        pending = _pending_upi_input.get((cid, uid))
        if not pending:
            return
        try:
            result = dues_svc.set_upi(
                cid, message.text.strip(),
                uid, message.from_user.first_name or message.from_user.username or "Admin",
            )
        except incorrectParameter as bad:
            # Invalid VPA — keep the pending slot so the next reply retries.
            await reply_error(message, bad)
            return
        _pending_upi_input.pop((cid, uid), None)
        await send_md_fallback(cid, result["announcement"])
        # Refresh the setup card so the UPI line flips to green in place.
        text, markup = _dues_setup_card(cid)
        await safe_edit_text(cid, pending["card_mid"], text, parse_mode="Markdown")
        await safe_edit_markup(cid, pending["card_mid"], markup)
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues_nudges")
async def dues_nudges(message):
    """Toggle the automatic weekly dues reminder (Sunday evening group post + DMs)."""
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /dues_nudges")
        cid = message.chat.id
        _require_dues_enabled(cid)
        args = _parse_args(message.text)

        if not args:
            current = bool(_db.get_or_create_chat(cid).get("dues_weekly_nudge"))
            await bot.send_message(
                cid,
                f"🗓 Weekly dues nudge is {'ON — fires Sunday ~6pm' if current else 'OFF'}.\n"
                "Usage: /dues_nudges on · /dues_nudges off\n"
                "When on: every Sunday evening, members with outstanding dues get a "
                "group summary + individual DM with the UPI details. Silent when "
                "everyone is settled.",
            )
            return

        arg = args[0].lower()
        if arg in ("on", "true", "1", "enable"):
            _db.update_chat_settings(cid, dues_weekly_nudge=1)
            await bot.send_message(
                cid,
                "🗓 Weekly dues nudge ON — outstanding balances will be reminded "
                "every Sunday evening (group summary + DMs). Nothing is sent when "
                "everyone is settled.",
            )
        elif arg in ("off", "false", "0", "disable"):
            _db.update_chat_settings(cid, dues_weekly_nudge=0)
            await bot.send_message(cid, "🗓 Weekly dues nudge OFF.")
        else:
            raise incorrectParameter("Usage: /dues_nudges on · /dues_nudges off")
    except Exception as e:
        await reply_error(message, e)


# ── /new_season — self-serve season reset ─────────────────────────────────────
# Zeroes balances with compensating entries (never deletes) and stamps a fresh
# dues epoch. The confirm card is the only way to execute it, and the fund
# choice (carry vs zero) is made there explicitly — no default to get wrong.

@bot.message_handler(func=lambda m: _cmd(m.text) in ("/new_season", "/dues_reset"))
async def new_season_cmd(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /new_season")
        cid = message.chat.id
        _require_dues_enabled(cid)

        preview = dues_svc.new_season_preview(cid)
        if not preview["members"] and preview["fund_balance"] == 0:
            await bot.send_message(
                cid, "Nothing to reset — all balances and the fund are already at ₹0.")
            return

        lines = [
            "🏁 *Close the season?*",
            "",
            f"This will clear {preview['members']} balance{'s' if preview['members'] != 1 else ''}:",
        ]
        if preview["owed_total"]:
            lines.append(f"• ₹{preview['owed_total']} still owed — will be *forgiven, not collected*")
        if preview["credit_total"]:
            lines.append(f"• ₹{preview['credit_total']} in member credits — will be cleared")
        lines += [
            f"• Group fund: ₹{preview['fund_balance']}",
            "",
            "History is kept (nothing is deleted) and every clearing posts to "
            "this chat. Choose what happens to the fund:",
        ]
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        markup = InlineKeyboardMarkup(row_width=2)
        markup.row(
            InlineKeyboardButton("🏦 Carry fund forward", callback_data="season_go:carry"),
            InlineKeyboardButton("🧹 Zero everything", callback_data="season_go:zero"),
        )
        markup.row(InlineKeyboardButton("❌ Cancel", callback_data="season_cancel"))
        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        await reply_error(message, e)


@bot.callback_query_handler(func=lambda call: call.data in ("season_go:carry", "season_go:zero"))
async def new_season_confirm_callback(call):
    try:
        cid = call.message.chat.id
        if not await is_chat_admin(cid, call.from_user.id):
            await bot.answer_callback_query(
                call.id, "⛔ Only admins can close the season", show_alert=True)
            return
        zero_fund = call.data.endswith(":zero")
        await bot.answer_callback_query(call.id, "Closing the season…")
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.new_season(
                cid, zero_fund,
                call.from_user.id,
                call.from_user.first_name or call.from_user.username or "Admin",
            )
        # Money post — always sends, shh or not (ledger durability rule).
        await send_md_fallback(cid, result["announcement"])
    except Exception as exc:
        logging.exception("new_season_confirm_callback error")
        await reply_error(call.message, exc)


@bot.callback_query_handler(func=lambda call: call.data == "season_cancel")
async def new_season_cancel_callback(call):
    try:
        cid = call.message.chat.id
        await bot.answer_callback_query(call.id, "Cancelled")
        await safe_edit_text(cid, call.message.message_id,
                             "❌ Season reset cancelled — nothing changed.")
        from telebot.types import InlineKeyboardMarkup
        await safe_edit_markup(cid, call.message.message_id, InlineKeyboardMarkup())
    except Exception as exc:
        logging.exception("new_season_cancel_callback error")
        await reply_error(call.message, exc)


@bot.message_handler(func=lambda m: _cmd(m.text) == "/disable_dues")
async def disable_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /disable_dues")
        cid = message.chat.id
        _db.update_chat_settings(cid, dues_enabled=0)
        await bot.send_message(
            cid,
            "⛔ *Dues & Treasury disabled* for this group.\n"
            "Existing ledger data is preserved. Re-enable with `/enable_dues`.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await reply_error(message, e)


# ── /dues_snapshot — post current state to group ─────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues_snapshot")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/ds")
async def dues_snapshot(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /dues_snapshot")
        cid = message.chat.id
        _require_dues_enabled(cid)
        result = dues_svc.dues_snapshot(cid)
        await send_md_fallback(cid, result["text"])
    except Exception as e:
        await reply_error(message, e)


# ── /dues_export — send CSV file to chat ─────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues_export")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/de")
async def dues_export(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /dues_export")
        cid = message.chat.id
        _require_dues_enabled(cid)
        import io, datetime as _dt
        csv_str = dues_svc.dues_export_csv(cid)
        data_rows = [l for l in csv_str.splitlines() if l.strip()][1:]  # skip header
        if not data_rows:
            await bot.send_message(cid, "No dues data yet.")
            return
        date_str = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d")
        filename = f"dues_export_{date_str}.csv"
        buf = io.BytesIO(csv_str.encode("utf-8"))
        buf.name = filename
        await bot.send_document(
            cid, buf,
            caption=f"📊 Dues export — {date_str}",
            visible_file_name=filename,
        )
    except Exception as e:
        await reply_error(message, e)


# ── /card — match-day card ────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/card")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/mc")
async def matchday_card(message):
    """Send a shareable match-day card image showing the current IN list."""
    try:
        from functions import roll_call_not_started
        cid = message.chat.id
        if not roll_call_not_started(message, manager):
            from exceptions import rollCallNotStarted
            raise rollCallNotStarted("No active rollcall. Start one with /rc first.")

        args     = _parse_args(message.text)
        rc_idx, _ = _parse_rc_suffix(args)
        rc       = manager.get_rollcall(cid, rc_idx)
        if rc is None:
            from exceptions import rollCallNotStarted
            raise rollCallNotStarted("Rollcall not found.")

        in_names = [u.name for u in rc.inList]
        if not in_names:
            await bot.send_message(cid, "Nobody is IN yet — card will be more useful once people vote.")
            return

        from utils.card_gen import matchday_card as _gen_card
        date_str = datetime.now().strftime("%A, %-d %b %Y")
        buf      = _gen_card(rc.title or "Game Day", date_str, in_names)
        caption  = f"📋 *{rc.title or 'Game Day'}* — {len(in_names)} players IN"
        await bot.send_photo(cid, buf, caption=caption, parse_mode="Markdown")
    except Exception as e:
        await reply_error(message, e)


# ── /dues_report ─────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues_report")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/dr")
async def dues_report(message):
    """Toggle or query the weekly auto-posted dues snapshot (Sunday >= 20:00)."""
    try:
        if not await admin_rights(message, manager):
            return
        cid  = message.chat.id
        args = _parse_args(message.text)
        if not args:
            chat = _db.get_or_create_chat(cid)
            status = "on" if chat.get("dues_report_enabled") else "off"
            await bot.send_message(
                cid,
                f"📋 Weekly dues report is currently *{status}*.\n"
                f"Use /dues\\_report weekly to enable or /dues\\_report off to disable.",
                parse_mode="Markdown",
            )
            return
        sub = args[0].lower()
        if sub in ("weekly", "on"):
            _db.update_chat_settings(cid, dues_report_enabled=1)
            await bot.send_message(
                cid,
                "✅ Weekly dues report *enabled*. A snapshot will be posted every "
                "Sunday evening (≥ 20:00 local time).",
                parse_mode="Markdown",
            )
        elif sub == "off":
            _db.update_chat_settings(cid, dues_report_enabled=0)
            await bot.send_message(cid, "🔕 Weekly dues report *disabled*.", parse_mode="Markdown")
        else:
            await bot.send_message(
                cid,
                "Usage: /dues\\_report weekly  — enable\n"
                "       /dues\\_report off     — disable\n"
                "       /dues\\_report         — show current status",
                parse_mode="Markdown",
            )
    except Exception as e:
        await reply_error(message, e)


# ── Background helpers ────────────────────────────────────────────────────────

async def _send_close_qr(cid: int, upi: str, per_head: int) -> None:
    """Send QR code + VPA text for the per-head amount after /settle_dues."""
    try:
        from utils.card_gen import qr_png
        buf     = qr_png(upi, per_head)
        caption = f"💳 Pay *₹{per_head}* to: `{upi}`\n_Scan QR or copy the VPA above._"
        await bot.send_photo(cid, buf, caption=caption, parse_mode="Markdown")
    except Exception:
        logging.exception("_send_close_qr failed")


async def _send_close_receipt(cid: int, result: dict) -> None:
    """Send a receipt card image summarising the just-closed game."""
    try:
        from utils.card_gen import close_receipt_card
        balances = _db.get_all_dues_balances(cid, nonzero_only=False)
        buf = close_receipt_card(
            title        = result.get("title", "Game"),
            ground_cost  = result.get("ground_cost", 0),
            subsidy      = result.get("subsidy", 0),
            per_head     = result.get("per_head", 0),
            in_count     = result.get("in_count", 0),
            fund_balance = result.get("fund_balance_after", 0),
            balances     = balances,
        )
        await bot.send_photo(cid, buf, caption="📊 Balance sheet after close")
    except Exception:
        logging.exception("_send_close_receipt failed")


async def _send_dues_dms(cid: int, result: dict) -> None:
    """DM each debtor individually after /remind_dues.

    Real users get a direct DM. Owned proxies get a DM to their owner with
    clear context of which proxy the amount is for. Unowned proxies are
    already noted in the group announcement.
    """
    upi     = result.get("upi_vpa")
    targets = result.get("dm_targets", [])
    sent    = 0
    failed  = []

    for t in targets:
        uid      = t["user_id"]
        name     = t["member_name"]
        balance  = t["balance"]
        is_proxy = t["is_proxy"]

        if is_proxy:
            body = (
                f"📢 *Dues reminder*\n"
                f"Your proxy *{_esc_md(name)}* owes *₹{balance}* to the group."
            )
        else:
            body = f"📢 *Dues reminder*\nYou owe *₹{balance}* to the group."

        if upi:
            body += f"\n💳 Pay ₹{balance} to: `{upi}`"

        try:
            await send_md_fallback(uid, body)
            sent += 1
        except Exception:
            failed.append(name)

    if failed or sent:
        summary = f"✅ Reminders sent to {sent} member(s)."
        if failed:
            summary += f"\n⚠️ Could not DM: {', '.join(failed)} (they may not have started the bot)."
        try:
            await bot.send_message(cid, summary)
        except Exception:
            logging.exception("_send_dues_dms summary failed")
