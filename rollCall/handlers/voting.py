"""
Voting handlers: /in, /out, /maybe

Each handler is now a thin Telegram adapter over services/voting.py:
  1. Parse message text + ::N suffix
  2. Ghost-reconfirmation check (Telegram-specific UI — inline buttons)
  3. Call voting service (business logic lives there)
  4. Format service result → send Telegram message
  5. Update panel
"""
import asyncio
import logging
from datetime import datetime

from bot_state import (
    bot, _log_task_exc, _pending_reconf, _is_rate_limited, _get_display_name,
    format_mention_with_name_md, _esc_md,
    warn_no_username, _dm_promoted_real_user, reply_error,
)
from exceptions import rollCallNotStarted, incorrectParameter, alreadyInList
from functions import roll_call_not_started
from rollcall_manager import manager
from services import voting as vote_svc
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def _parse_rc_and_comment(text: str) -> tuple[int, str]:
    """Return (rc_number_0based, comment) from a command text like '/in hi ::2'."""
    parts = text.split()
    rc_number = 0
    if parts and "::" in parts[-1]:
        try:
            rc_number = int(parts[-1].replace("::", "")) - 1
            parts = parts[:-1]
        except ValueError:
            raise incorrectParameter("The rollcall number must be a positive integer")
    comment = " ".join(parts[1:]) if len(parts) > 1 else ""
    return rc_number, comment


async def _send_promoted(cid: int, promoted: dict, rc_title: str, rc_number_1based: int):
    """Announce a waitlist promotion and optionally DM the promoted user."""
    if promoted["is_proxy"]:
        if not manager.get_shh_mode(cid):
            await bot.send_message(
                cid, f"{promoted['name']} → IN (from WAITING) for '{rc_title}' (#{rc_number_1based})"
            )
    else:
        if not manager.get_shh_mode(cid):
            from models import User
            u = User(promoted["name"], promoted.get("username"), promoted["user_id"], [])
            await bot.send_message(
                cid,
                f"{format_mention_with_name_md(u)} → IN (from WAITING) for '{_esc_md(rc_title)}' (#{rc_number_1based})",
                parse_mode="Markdown",
            )
        _t = asyncio.create_task(_dm_promoted_real_user(promoted["user_id"], rc_title, rc_number_1based))
        _t.add_done_callback(_log_task_exc)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/in")
async def in_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return

        cid = message.chat.id
        rc_number, comment = _parse_rc_and_comment(message.text)
        user_id = message.from_user.id
        display_name = _get_display_name(message.from_user)
        username = message.from_user.username or None

        if not username:
            asyncio.create_task(warn_no_username(cid, display_name)).add_done_callback(_log_task_exc)

        from db import upsert_chat_member as _upsert
        _upsert(cid, user_id, display_name, username)

        # Ghost reconfirmation (Telegram-specific UI — inline buttons)
        reconf = vote_svc.check_ghost_reconfirmation_needed(cid, user_id, rc_number)
        if reconf["needed"]:
            if (cid, user_id) in _pending_reconf:
                return
            _pending_reconf[(cid, user_id)] = {
                "rc_number": rc_number,
                "comment": comment,
                "_ts": datetime.now().timestamp(),
            }
            rc_title = reconf["rollcall_title"]
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=f"reconf_in_{rc_number}_{user_id}"),
                InlineKeyboardButton("❌ I'm out", callback_data=f"reconf_out_{rc_number}_{user_id}"),
            )
            await bot.send_message(
                cid,
                f"👻 *Warning:* [​](tg://user?id={user_id}){_esc_md(display_name)}, "
                f"you've ghosted *{reconf['ghost_count']}* session(s) before.\n"
                f"⚠️ Absent Limit: *{reconf['absent_limit']}*\n\n"
                f"Are you committing to be at *{_esc_md(rc_title)}*?",
                parse_mode="Markdown",
                reply_markup=markup,
            )
            return

        # Business logic via service
        result = await vote_svc.vote_in(cid, user_id, display_name, username, comment, rc_number)

        # Format response
        if result["action"] == "waitlisted":
            if not manager.get_shh_mode(cid):
                await bot.send_message(cid, f"Event max limit is reached, {display_name} was added in waitlist")
        elif result["action"] == "added":
            if not manager.get_shh_mode(cid):
                from models import User
                u = User(display_name, username, user_id, [])
                await bot.send_message(
                    cid,
                    f"{format_mention_with_name_md(u)} is now IN!",
                    parse_mode="Markdown",
                )

        from handlers.lifecycle import _update_panel
        rc_number_1based = result["rc_number_1based"]
        rc = manager.get_rollcall(cid, rc_number)
        if rc:
            await _update_panel(cid, rc_number_1based, rc)

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/out")
async def out_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return

        cid = message.chat.id
        rc_number, comment = _parse_rc_and_comment(message.text)
        user_id = message.from_user.id
        display_name = _get_display_name(message.from_user)
        username = message.from_user.username or None

        if not username:
            asyncio.create_task(warn_no_username(cid, display_name)).add_done_callback(_log_task_exc)

        result = await vote_svc.vote_out(cid, user_id, display_name, username, comment, rc_number)

        rc_title = result["rollcall"]["title"]
        rc_number_1based = result["rc_number_1based"]

        # Announce promotion if someone moved waitlist→IN
        if result["promoted"]:
            await _send_promoted(cid, result["promoted"], rc_title, rc_number_1based)
            # Notify proxy owner if promoted user is a proxy
            rc = manager.get_rollcall(cid, rc_number)
            if rc:
                from handlers.lifecycle import notify_proxy_owner_wait_to_in
                from models import User
                promoted = result["promoted"]
                promo_user = User(promoted["name"], promoted.get("username"),
                                  promoted["user_id"], [])
                await notify_proxy_owner_wait_to_in(rc, promo_user, cid, rc_title, rc_number_1based)
        else:
            if not manager.get_shh_mode(cid) and result["action"] != "already":
                from models import User
                u = User(display_name, username, user_id, [])
                if result["was_in"]:
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(u)} → OUT for '{_esc_md(rc_title)}' (#{rc_number_1based})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(u)} is now OUT!",
                        parse_mode="Markdown",
                    )

        from handlers.lifecycle import _update_panel
        rc = manager.get_rollcall(cid, rc_number)
        if rc:
            await _update_panel(cid, rc_number_1based, rc)

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/maybe")
async def maybe_user(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")
        if _is_rate_limited(message.chat.id, message.from_user.id):
            return

        cid = message.chat.id
        rc_number, comment = _parse_rc_and_comment(message.text)
        user_id = message.from_user.id
        display_name = _get_display_name(message.from_user)
        username = message.from_user.username or None

        if not username:
            asyncio.create_task(warn_no_username(cid, display_name)).add_done_callback(_log_task_exc)

        result = await vote_svc.vote_maybe(cid, user_id, display_name, username, comment, rc_number)

        rc_title = result["rollcall"]["title"]
        rc_number_1based = result["rc_number_1based"]

        if result["promoted"]:
            await _send_promoted(cid, result["promoted"], rc_title, rc_number_1based)
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
                from models import User
                u = User(display_name, username, user_id, [])
                await bot.send_message(
                    cid,
                    f"{format_mention_with_name_md(u)} is now MAYBE!",
                    parse_mode="Markdown",
                )

        from handlers.lifecycle import _update_panel
        rc = manager.get_rollcall(cid, rc_number)
        if rc:
            await _update_panel(cid, rc_number_1based, rc)

    except Exception as e:
        await reply_error(message, e)
