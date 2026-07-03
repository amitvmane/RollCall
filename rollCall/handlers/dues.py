"""
Dues & Treasury handlers.

Commands (all admin-only unless noted):
  /close_game /cg [subsidy] [::N]
  /mark_late name minutes
  /mark_ditch name
  /waive name amount [reason]
  /set_collector name [paid] [::N]
  /mark_paid /paid name [amount]      — admin OR designated collector
  /reimburse name amount [reason]
  /add_adhoc name
  /cancel_game_dues [rollcall_id]
  /my_dues /md                        — user-scoped, own balance only
  /dues                               — admin: full group ledger
  /fund                               — all: fund balance
  /fund_history /fh [page]            — all: fund history paginated
  /log_expense /le amount description
  /fund_topup amount [description]
  /remind_dues
  /set_upi vpa@bank
  /set_penalties t1 t2 t3 ditch
  /set_round_step step

Ledger mutation announcements always post, even in shh mode (durability).
"""
import logging

from bot_state import bot, reply_error
from exceptions import (
    duesGameAlreadyClosed, duesNothingToClose,
    incorrectParameter, insufficientPermissions, parameterMissing,
)
from functions import admin_rights
from rollcall_manager import manager
from services import dues as dues_svc
from handlers.lifecycle import _post_end_cleanup


def _cmd(text: str) -> str:
    """Extract the bare command name from a message (strips /cmd@botname prefix)."""
    return (text.split(" ")[0]).split("@")[0].lower()


def _parse_args(text: str) -> list[str]:
    """Split message into args, stripping the command prefix."""
    return text.split(" ")[1:]


def _parse_rc_suffix(args: list[str]) -> tuple[int, list[str]]:
    """Pop optional ::N suffix and return (0-based rc_index, remaining args)."""
    if args and "::" in args[-1]:
        try:
            idx = int(args[-1].replace("::", "")) - 1
            if idx < 0:
                raise ValueError
            return idx, args[:-1]
        except (ValueError, TypeError):
            raise incorrectParameter("The rollcall number must be a positive integer (e.g. ::2).")
    return 0, args


# ── /close_game (/cg) ────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/close_game")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/cg")
async def close_game(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /close_game")
        cid = message.chat.id
        args = _parse_args(message.text)
        rc_idx, args = _parse_rc_suffix(args)

        subsidy = 0
        if args:
            try:
                subsidy = int(args[0])
            except ValueError:
                raise incorrectParameter("Subsidy must be a whole number (₹). Example: /close_game 60")

        async with manager.get_chat_write_lock(cid):
            result = await dues_svc.close_game(
                cid, subsidy=subsidy,
                admin_uid=message.from_user.id,
                admin_name=message.from_user.first_name or message.from_user.username or "Admin",
                rc_number=rc_idx,
            )

        # Always post announcement — financial record
        await bot.send_message(cid, result["announcement"])

        # If an active rollcall was ended, run the standard post-end cleanup
        end_res = result.get("end_result")
        if end_res:
            await _post_end_cleanup(
                cid,
                end_res.get("rc_number_ended_1based", rc_idx + 1),
                end_res,
                rc_title=result.get("title", ""),
            )
    except Exception as e:
        await reply_error(message, e)


# ── /mark_late ───────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_late")
async def mark_late(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /mark_late")
        cid = message.chat.id
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing("Usage: /mark_late <name> <minutes>")
        try:
            minutes = int(args[-1])
        except ValueError:
            raise incorrectParameter("Minutes must be a whole number. Example: /mark_late Alice 20")
        name = " ".join(args[:-1])

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_late(
                cid, name, minutes,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /mark_ditch ───────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_ditch")
async def mark_ditch(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /mark_ditch")
        cid = message.chat.id
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /mark_ditch <name>")
        name = " ".join(args)

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.mark_ditch(
                cid, name,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /waive ────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/waive")
async def waive(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /waive")
        cid = message.chat.id
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing("Usage: /waive <name> <amount> [reason]")
        try:
            amount = int(args[1])
        except ValueError:
            raise incorrectParameter("Amount must be a whole number. Example: /waive Alice 75 injured")
        name = args[0]
        reason = " ".join(args[2:]) if len(args) > 2 else ""

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.waive(
                cid, name, amount, reason,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /set_collector ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/set_collector")
async def set_collector(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /set_collector")
        cid = message.chat.id
        args = _parse_args(message.text)
        rc_idx, args = _parse_rc_suffix(args)
        if not args:
            raise parameterMissing("Usage: /set_collector <name> [paid] [::N]")
        paid_ground = False
        if args[-1].lower() == "paid":
            paid_ground = True
            args = args[:-1]
        name = " ".join(args)
        if not name:
            raise parameterMissing("Usage: /set_collector <name> [paid]")

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.set_collector(
                cid, name, paid_ground,
                message.from_user.id,
                message.from_user.first_name or "Admin",
                rc_number=rc_idx,
            )
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /mark_paid (/paid) ────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/mark_paid")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/paid")
async def mark_paid(message):
    try:
        cid = message.chat.id
        args = _parse_args(message.text)
        if not args:
            raise parameterMissing("Usage: /mark_paid <name> [amount]")

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
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /reimburse ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/reimburse")
async def reimburse(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /reimburse")
        cid = message.chat.id
        args = _parse_args(message.text)
        if len(args) < 2:
            raise parameterMissing("Usage: /reimburse <name> <amount> [reason]")
        try:
            amount = int(args[1])
        except ValueError:
            raise incorrectParameter("Amount must be a whole number.")
        name = args[0]
        reason = " ".join(args[2:]) if len(args) > 2 else ""

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.reimburse(
                cid, name, amount, reason,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /add_adhoc ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/add_adhoc")
async def add_adhoc(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /add_adhoc")
        cid = message.chat.id
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
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /cancel_game_dues ─────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/cancel_game_dues")
async def cancel_game_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /cancel_game_dues")
        cid = message.chat.id
        args = _parse_args(message.text)
        rollcall_id = None
        if args:
            try:
                rollcall_id = int(args[0])
            except ValueError:
                raise incorrectParameter("Rollcall id must be a number. Example: /cancel_game_dues 42")

        if rollcall_id is None:
            from db import get_latest_game_closure
            closure = get_latest_game_closure(cid)
            if closure is None:
                raise duesNothingToClose("No closed game found to cancel.")
            rollcall_id = closure["rollcall_id"]

        async with manager.get_chat_write_lock(cid):
            result = dues_svc.cancel_game_credit(
                cid, rollcall_id,
                message.from_user.id,
                message.from_user.first_name or "Admin",
            )
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /my_dues (/md) — user-scoped ─────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/my_dues")
@bot.message_handler(func=lambda m: _cmd(m.text) == "/md")
async def my_dues(message):
    try:
        cid = message.chat.id
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
                lines.append(f"  {sign}₹{e['amount']} {e['entry_type']} {e.get('memo') or ''}")

        settings = dues_svc.get_dues_settings(cid)
        if balance > 0 and settings.get("upi_vpa"):
            lines.append(f"\n💳 Pay: `{settings['upi_vpa']}`")

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await reply_error(message, e)


# ── /dues — admin full ledger ─────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/dues")
async def dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /dues — use /my_dues for your own balance.")
        cid = message.chat.id
        result = dues_svc.all_dues(cid, nonzero_only=False)
        balances = result["balances"]
        if not balances:
            await bot.send_message(cid, "✅ No dues recorded yet.")
            return

        lines = ["📋 *Dues ledger:*"]
        for b in balances:
            name = b["member_name"]
            bal = b["balance"]
            icon = "🔴" if bal > 0 else ("🟢" if bal < 0 else "⚪")
            lines.append(f"{icon} {name}: ₹{bal}")

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await reply_error(message, e)


# ── /fund ─────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/fund")
async def fund(message):
    try:
        cid = message.chat.id
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
            lines.append(f"  {sign}₹{t['amount']} {t['txn_type']} — {t.get('description', '')} [{ts}]")

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown")
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
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /fund_topup ───────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/fund_topup")
async def fund_topup(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /fund_topup")
        cid = message.chat.id
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
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /remind_dues ──────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/remind_dues")
async def remind_dues(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /remind_dues")
        cid = message.chat.id
        result = dues_svc.remind_dues(cid)
        await bot.send_message(cid, result["announcement"])
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
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)


# ── /set_penalties ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda m: _cmd(m.text) == "/set_penalties")
async def set_penalties(message):
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Admin only: /set_penalties")
        cid = message.chat.id
        args = _parse_args(message.text)
        if len(args) < 4:
            raise parameterMissing(
                "Usage: /set_penalties <t1> <t2> <t3> <ditch>\n"
                "Example: /set_penalties 50 75 100 200"
            )
        try:
            t1, t2, t3, ditch = int(args[0]), int(args[1]), int(args[2]), int(args[3])
        except ValueError:
            raise incorrectParameter("All four penalty values must be whole numbers (₹).")
        result = dues_svc.set_penalty_tiers(
            cid, t1, t2, t3, ditch,
            message.from_user.id,
            message.from_user.first_name or "Admin",
        )
        await bot.send_message(cid, result["announcement"])
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
        await bot.send_message(cid, result["announcement"])
    except Exception as e:
        await reply_error(message, e)
