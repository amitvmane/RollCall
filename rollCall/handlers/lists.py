"""
List handlers: /whos_in, /whos_out, /whos_maybe, /whos_waiting, /history, /buzz
"""
import asyncio
import logging

from bot_state import (
    bot, _is_buzz_rate_limited, _buzz_cooldowns, _BUZZ_COOLDOWN_SECONDS, _fmt_ended_at, _esc_md,
    reply_error,
)
from exceptions import (
    rollCallNotStarted, incorrectParameter, insufficientPermissions, parameterMissing,
)
from functions import admin_rights, roll_call_not_started
from rollcall_manager import manager
from db import (
    get_rollcall_history, mark_member_inactive, log_admin_action,
    upsert_chat_member,
)
from services import lists as lists_svc
from datetime import datetime


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_in")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/wi")
async def whos_in(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.inListText()}")

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_out")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/wo")
async def whos_out(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.outListText()}")

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_maybe")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/wm")
async def whos_maybe(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.maybeListText()}")

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/whos_waiting")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/ww")
async def whos_waiting(message):
    try:
        if roll_call_not_started(message, manager) == False:
            raise rollCallNotStarted("Roll call is not active")

        cid = message.chat.id
        rc_number = 0
        pmts = message.text.split(" ")[1:]

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            rollcalls = manager.get_rollcalls(cid)
            if rc_number < 0 or len(rollcalls) < rc_number + 1:
                raise incorrectParameter("The rollcall number doesn't exist, check /rollcalls to see all rollcalls")

        rc = manager.get_rollcall(cid, rc_number)
        rollcalls = manager.get_rollcalls(cid)
        await bot.send_message(cid, f"{rc.title if len(rollcalls) > 1 else ''} {rc.waitListText()}")

    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/history")
async def history_command(message):
    cid = message.chat.id
    try:
        parts = message.text.strip().split()
        limit = 10
        page = 1
        if len(parts) > 1:
            try:
                limit = max(1, min(20, int(parts[1])))
            except ValueError:
                pass
        if len(parts) > 2:
            try:
                page = max(1, int(parts[2]))
            except ValueError:
                pass

        offset = (page - 1) * limit
        records = get_rollcall_history(cid, limit, offset)
        if not records:
            msg = "No ended rollcalls found for this chat yet." if page == 1 else f"No rollcalls found on page {page}."
            await bot.send_message(cid, msg)
            return

        start_num = offset + 1
        header = f"*📋 Rollcalls {start_num}–{start_num + len(records) - 1}*" + (f" (page {page})" if page > 1 else "") + ":"
        lines = [header, ""]
        for i, r in enumerate(records, start_num):
            ended = _fmt_ended_at(r.get("ended_at"))
            title = r.get("title") or "Untitled"
            in_count = r.get("in_count", 0)
            ghost_count = r.get("ghost_count", 0)
            ghost_str = f"  👻 {ghost_count}" if ghost_count else ""
            lines.append(f"{i}. *{_esc_md(title)}* — {ended}  ✅ {in_count}{ghost_str}")

        if len(records) == limit:
            lines.append(f"\n_Use /history {limit} {page + 1} to see more_")

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logging.exception("Error in /history")
        await bot.send_message(cid, "Error fetching history, please try again later.")


@bot.message_handler(func=lambda message: message.text.split("@")[0].split(" ")[0].lower() == "/buzz")
async def buzz_command(message):
    cid = message.chat.id
    try:
        if await admin_rights(message, manager) == False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        if _is_buzz_rate_limited(cid):
            remaining = int(_BUZZ_COOLDOWN_SECONDS - (datetime.now().timestamp() - _buzz_cooldowns.get(cid, 0))) + 1
            await bot.send_message(cid, f"⏳ /buzz was used recently. Please wait {remaining}s before buzzing again.")
            return

        msg = message.text.strip()
        parts = msg.split()

        # rc_number is None when ::N is NOT given (default = union across all
        # active rollcalls), or 0-based index when admin explicitly targets one.
        rc_number = None
        custom_msg = None
        filtered = []
        for part in parts[1:]:
            if part.startswith("::"):
                try:
                    rc_number = int(part.replace("::", "")) - 1
                except ValueError:
                    pass
            else:
                filtered.append(part)
        if filtered:
            custom_msg = " ".join(filtered)

        no_rollcall = roll_call_not_started(message, manager) == False

        svc = lists_svc.get_non_responders(cid, rc_number)
        candidates = svc["candidates"]
        rollcall_titles = svc["rollcall_titles"]

        if not no_rollcall and not svc["has_active_rollcall"]:
            no_rollcall = True  # treat as no-rollcall buzz

        if not candidates and not no_rollcall:
            # Service reported no active-member candidates at all
            await bot.send_message(
                cid,
                "No known group members yet. Members are recorded the first time they vote in any rollcall."
            )
            return

        if not candidates and no_rollcall:
            await bot.send_message(
                cid,
                "No known group members yet. Members are recorded the first time they vote in any rollcall."
            )
            return

        rollcalls = manager.get_rollcalls(cid)

        if not no_rollcall and not candidates:
            if rc_number is not None or len(rollcall_titles) == 1:
                title = rollcall_titles[0] if rollcall_titles else "this rollcall"
                msg_text = f"✅ Everyone the bot knows has already voted on *{_esc_md(title)}*!"
            else:
                msg_text = "✅ Everyone the bot knows has already voted on at least one active rollcall!"
            await bot.send_message(cid, msg_text, parse_mode="Markdown")
            return

        async def _check_member(u):
            uid = u['user_id']
            try:
                member = await asyncio.wait_for(bot.get_chat_member(cid, uid), timeout=5.0)
                if member.status in ("left", "kicked"):
                    mark_member_inactive(cid, uid)
                    return None
                return u
            except asyncio.TimeoutError:
                logging.warning(f"[/buzz] Timeout checking member {uid} — keeping in ping list")
                return u
            except Exception:
                return None

        results = await asyncio.gather(*[_check_member(u) for u in candidates])
        # chat_members PK already guarantees one row per (chat_id, user_id),
        # but dedupe defensively here so a corrupted or backfilled DB can't
        # produce duplicate @mentions in a single buzz.
        to_ping = []
        seen_uids = set()
        for u in results:
            if u is None:
                continue
            uid = u.get('user_id')
            if uid in seen_uids:
                continue
            seen_uids.add(uid)
            to_ping.append(u)

        if not to_ping:
            if no_rollcall:
                await bot.send_message(cid, "All known members appear to have left the group.")
            else:
                if rc_number is not None or len(rollcall_titles) == 1:
                    title = rollcall_titles[0] if rollcall_titles else "this rollcall"
                    msg_text = f"✅ Everyone the bot knows has already voted on *{_esc_md(title)}*!"
                else:
                    msg_text = "✅ Everyone the bot knows has already voted on at least one active rollcall!"
                await bot.send_message(cid, msg_text, parse_mode="Markdown")
            return

        mentions = _build_mention_list(to_ping)

        if no_rollcall:
            note = custom_msg or "Just a heads-up from the group! 👋"
            await bot.send_message(cid, f"📣 {note}\n\n{mentions}", parse_mode="Markdown")
        else:
            if custom_msg:
                note = custom_msg
            elif rc_number is not None or len(rollcall_titles) == 1:
                title = rollcall_titles[0] if rollcall_titles else "this rollcall"
                note = f"rollcall *{_esc_md(title)}* is open — have you voted?"
            else:
                titles = ", ".join(f"*{_esc_md(t)}*" for t in rollcall_titles)
                note = f"active rollcalls ({titles}) are open — have you voted?"
            await bot.send_message(cid, f"👋 Hey {mentions}\n\n{note}", parse_mode="Markdown")
        log_admin_action(cid, message.from_user.id, message.from_user.first_name, "buzz",
                         details=f"pinged {len(to_ping)} member(s)")

    except Exception as e:
        await reply_error(cid, e)


def _build_mention_list(users: list) -> str:
    parts = []
    for u in users:
        uid = u.get('user_id')
        username = u.get('username')
        name = (u.get('first_name') or username or str(uid)).strip()
        safe_name = name.replace("[", "\\[").replace("]", "\\]").replace("_", "\\_").replace("*", "\\*")
        if username:
            parts.append(f"@{_esc_md(username)}")
        elif uid:
            parts.append(f"[{safe_name}](tg://user?id={uid})")
    return " ".join(parts)
