"""
Proxy handlers: /set_in_for (/sif), /set_out_for (/sof), /set_maybe_for (/smf)

Thin Telegram adapters over services/proxy.py:
  1. Parse message (name, comment, ::N suffix)
  2. Ghost reconfirmation check (Telegram inline-button UI)
  3. Call proxy service
  4. Format result → send Telegram messages
  5. Update panel
"""
import asyncio
import logging
from datetime import datetime

from bot_state import (
    bot, _log_task_exc, _pending_proxy_add, _prune_pending,
    format_mention_with_name_md, _esc_md,
    _dm_promoted_real_user, reply_error,
)
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
)
from functions import admin_rights, roll_call_not_started
from rollcall_manager import manager
from services import proxy as proxy_svc
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def _parse_proxy_args(text: str) -> tuple[int, str, str]:
    """Return (rc_number_0based, proxy_name, comment)."""
    parts = text.split()
    rc_number = 0
    if len(parts) > 1 and "::" in parts[-1]:
        try:
            rc_number = int(parts[-1].replace("::", "")) - 1
            parts = parts[:-1]
        except ValueError:
            raise incorrectParameter("The rollcall number must be a positive integer")
    if len(parts) < 2:
        raise parameterMissing("Input username is missing")
    proxy_name = parts[1]
    comment = " ".join(parts[2:]) if len(parts) > 2 else ""
    return rc_number, proxy_name, comment


async def _send_promoted_proxy(cid: int, promoted: dict, rc_title: str, rc_number_1based: int):
    """Announce and optionally DM a user promoted waitlist→IN via proxy command."""
    if promoted["is_proxy"]:
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, f"{promoted['name']} → IN")
    else:
        if not manager.get_shh_mode(cid):
            from models import User
            u = User(promoted["name"], promoted.get("username"), promoted["user_id"], [])
            await bot.send_message(
                cid,
                f"{format_mention_with_name_md(u)} → IN",
                parse_mode="Markdown",
            )
        _t = asyncio.create_task(_dm_promoted_real_user(promoted["user_id"], rc_title, rc_number_1based))
        _t.add_done_callback(_log_task_exc)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_in_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sif")
async def set_in_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        cid = message.chat.id
        rc_number, proxy_name, comment = _parse_proxy_args(message.text)

        # Ghost reconfirmation for proxy (Telegram inline-button UI)
        reconf = proxy_svc.check_proxy_ghost_reconfirmation_needed(cid, proxy_name)
        if reconf["needed"]:
            _prune_pending(_pending_proxy_add)
            _pending_proxy_add[(cid, message.from_user.id, proxy_name)] = {
                "comment": comment,
                "_ts": datetime.now().timestamp(),
            }
            rc = manager.get_rollcall(cid, rc_number)
            rc_title = rc.title if rc else ""
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("✅ Yes, add anyway", callback_data=f"proxy_add_{rc_number}_{proxy_name}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"proxy_cancel_{rc_number}_{proxy_name}"),
            )
            await bot.send_message(
                cid,
                f"👻 *Warning:* *{_esc_md(proxy_name)}* has ghosted *{reconf['ghost_count']}* session(s) before.\n"
                f"⚠️ Absent Limit: *{reconf['absent_limit']}*\n\n"
                f"Still add to *{_esc_md(rc_title)}*?",
                parse_mode="Markdown",
                reply_markup=markup,
            )
            return

        result = await proxy_svc.set_in_for(
            cid, message.from_user.id, message.from_user.first_name,
            proxy_name, comment, rc_number
        )

        if result["action"] == "waitlisted":
            if not manager.get_shh_mode(cid):
                await bot.send_message(cid, f"Event max limit is reached, {proxy_name} was added in waitlist")
        elif result["action"] == "added":
            if not manager.get_shh_mode(cid):
                await bot.send_message(cid, f"{proxy_name} is now IN!")

        from handlers.lifecycle import show_panel_for_rollcall
        await show_panel_for_rollcall(cid, result["rc_number_1based"])

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_out_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/sof")
async def set_out_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        cid = message.chat.id
        rc_number, proxy_name, comment = _parse_proxy_args(message.text)

        result = await proxy_svc.set_out_for(
            cid, message.from_user.id, message.from_user.first_name,
            proxy_name, comment, rc_number
        )

        rc_title = result["rollcall"]["title"]
        rc_number_1based = result["rc_number_1based"]

        if result["promoted"]:
            await _send_promoted_proxy(cid, result["promoted"], rc_title, rc_number_1based)
            rc = manager.get_rollcall(cid, rc_number)
            if rc:
                from handlers.lifecycle import notify_proxy_owner_wait_to_in
                from models import User
                promoted = result["promoted"]
                promo_user = User(promoted["name"], promoted.get("username"),
                                  promoted["user_id"], [])
                await notify_proxy_owner_wait_to_in(rc, promo_user, cid, rc_title, rc_number_1based)
        else:
            if not manager.get_shh_mode(cid):
                if result["was_in"]:
                    await bot.send_message(cid, f"{proxy_name} → OUT for '{rc_title}' (#{rc_number_1based})")
                else:
                    await bot.send_message(cid, f"{proxy_name} is now OUT!")

        from handlers.lifecycle import show_panel_for_rollcall
        await show_panel_for_rollcall(cid, rc_number_1based)

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_maybe_for")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/smf")
async def set_maybe_for(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        cid = message.chat.id
        rc_number, proxy_name, comment = _parse_proxy_args(message.text)

        result = await proxy_svc.set_maybe_for(
            cid, message.from_user.id, message.from_user.first_name,
            proxy_name, comment, rc_number
        )

        rc_title = result["rollcall"]["title"]
        rc_number_1based = result["rc_number_1based"]

        if result["promoted"]:
            await _send_promoted_proxy(cid, result["promoted"], rc_title, rc_number_1based)
            rc = manager.get_rollcall(cid, rc_number)
            if rc:
                from handlers.lifecycle import notify_proxy_owner_wait_to_in
                from models import User
                promoted = result["promoted"]
                promo_user = User(promoted["name"], promoted.get("username"),
                                  promoted["user_id"], [])
                await notify_proxy_owner_wait_to_in(rc, promo_user, cid, rc_title, rc_number_1based)
        else:
            if not manager.get_shh_mode(cid):
                await bot.send_message(cid, f"{proxy_name} is now MAYBE!")

        from handlers.lifecycle import show_panel_for_rollcall
        await show_panel_for_rollcall(cid, rc_number_1based)

    except Exception as e:
        await reply_error(message, e)
