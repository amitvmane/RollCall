"""
Lifecycle handlers: /src, /erc, /set_title, /panel, inline keyboards, panel machinery,
notify_proxy_owner_wait_to_in, and the btn_* callback handler.
"""
import asyncio
import logging
import os
import re
from datetime import datetime

import pytz
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot_state import (
    bot, best_effort, _panel_msg_ids, _pending_deletes, _pending_overrides,
    _pending_cancel_reason, _prune_pending,
    _log_task_exc,
    _is_rate_limited, _get_display_name, format_mention_with_name,
    format_mention_with_name_md, _esc_md,
    warn_no_username, _dm_promoted_real_user, get_rc_db_id, reply_error,
    safe_edit_text, safe_edit_markup,
)
from config import ADMINS
from exceptions import (
    rollCallNotStarted, insufficientPermissions, parameterMissing, incorrectParameter,
    duplicateProxy, repeatlyName, amountOfRollCallsReached, alreadyInList,
    rollCallClosed,
)
from functions import admin_rights, roll_call_not_started
from models import User
from rollcall_manager import manager
from services.common import ensure_rc_number, find_rollcall_index, rc_db_id as _rc_db_id
import chat_outbox
import db
from db import update_rollcall
from services import rollcalls as rollcalls_svc
from services import voting as voting_svc


def _ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _build_panel_text(rc, rc_number: int) -> str:
    """Build the panel text. The web link is exposed via the inline keyboard
    button (_group_web_url), so we don't duplicate it in the message body."""
    return rc.allList().replace("__RCID__", str(rc_number))


_NOT_DONE = object()


def _close_panel_bookkeeping(cid: int, ended_number: int, result: dict, rc_id):
    """The STATE half of closing a rollcall — call it under the chat write
    lock, right after the service closed it.

    Forgets the closed rollcall's panel and moves every later rollcall's
    panel id down to its new number, so whatever runs once the lock is
    released (a vote on the renumbered rollcall) edits the right message.
    Returns the closed panel's message id (or None) for _strip_panel.

    Announcing is deliberately NOT done here: a Telegram send under the lock
    can wait out a rate limit, and every vote in the chat would wait with it.
    """
    msg_id = _panel_msg_ids.pop((cid, ended_number), None)
    if msg_id is None and rc_id:
        try:
            msg_id = (db.get_rollcall(rc_id) or {}).get("panel_msg_id")
        except Exception:
            msg_id = None
    for entry in sorted(result["renumbered"], key=lambda x: x["old"]):
        old_key = (cid, entry["old"])
        if old_key in _panel_msg_ids:
            _panel_msg_ids[(cid, entry["new"])] = _panel_msg_ids.pop(old_key)
    return msg_id


async def _strip_panel(cid: int, msg_id) -> None:
    """Take the closed panel's buttons away.

    A panel left with live buttons after /erc or /xrc kept addressing
    "rollcall #N" — and once the next rollcall slid into #N, tapping IN on the
    dead panel voted in the wrong one. Best-effort: the rollcall is closed
    either way, and a message someone deleted must not turn into an error.
    """
    if msg_id:
        await safe_edit_markup(cid, msg_id, reply_markup=None)


async def _post_end_cleanup(cid: int, ended_number: int, result: dict, rc_title: str = "",
                            settle_nudge: bool = True, retired=_NOT_DONE) -> None:
    """Panel-ID cleanup, ghost prompt, and renumber announcements after a rollcall ends.

    Called by /erc and /settle_dues (whenever /settle_dues ends an active rollcall).
    `ended_number` is 1-based. `result` is the dict from rollcalls_svc.end_rollcall.
    `settle_nudge=False` skips the pinned settle reminder — used by the
    /settle_dues paths, where settlement is already in progress or done.

    `retired` — callers that closed the rollcall under the chat write lock
    pass what _close_panel_bookkeeping returned there, and call this AFTER
    releasing the lock. Left out, the bookkeeping happens here (callers that
    don't hold the lock).
    """
    if retired is _NOT_DONE:
        retired = _close_panel_bookkeeping(cid, ended_number, result, result.get("rc_db_id"))
    await _strip_panel(cid, retired)

    # Achievement badges — celebratory, so they respect shh mode.
    badges = result.get("badges") or []
    if badges and not manager.get_shh_mode(cid):
        try:
            await bot.send_message(cid, "🎉 Milestones!\n" + "\n".join(badges))
        except Exception:
            logging.exception("Failed to send badge announcement")

    rc_db_id = result.get("rc_db_id")
    ghost_eligible = bool(result.get("ghost_eligible"))

    chat_row = db.get_or_create_chat(cid)
    dues_enabled = bool(chat_row.get("dues_enabled"))

    if dues_enabled and rc_db_id:
        # Ghost/penalty marking now happens as part of /settle_dues (right
        # before the financial close), not immediately here — attendance
        # ending and money-settling used to drift apart across two separate
        # moments/admins. Persistent nudge (pinned + Settle-now button); the
        # penalty panel itself opens from the settle flow.
        if settle_nudge:
            from handlers.dues import send_settle_nudge
            try:
                await send_settle_nudge(cid, rc_db_id, rc_title)
            except Exception:
                logging.exception("Failed to send settle nudge")
    elif ghost_eligible:
        ghost_markup = InlineKeyboardMarkup(row_width=2)
        ghost_markup.add(
            InlineKeyboardButton(
                "👻 Yes, select ghosts",
                callback_data=f"ghost_yes_{result['ghost_rc_db_id']}"
            ),
            InlineKeyboardButton(
                "✅ No, all showed up",
                callback_data=f"ghost_no_{result['ghost_rc_db_id']}"
            ),
        )
        title_hint = f" '{rc_title}'" if rc_title else ""
        await bot.send_message(cid, f"👻 Did anyone ghost{title_hint}?", reply_markup=ghost_markup)

    updated_rollcalls = manager.get_rollcalls(cid)
    if updated_rollcalls:
        lines = [f"⚠️ Rollcall #{ended_number} ended. IDs updated:"]
        for entry in result["renumbered"]:
            lines.append(f"  #{entry['old']} '{entry['title']}' → #{entry['new']}")
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, "\n".join(lines))
            for idx, rollcall in enumerate(updated_rollcalls):
                new_id = idx + 1
                text = f"Rollcall number {new_id}\n\n" + _build_panel_text(rollcall, new_id)
                await bot.send_message(cid, text)


async def _post_cancel_cleanup(cid: int, ended_number: int, result: dict, retired=_NOT_DONE) -> None:
    """Panel-ID cleanup and renumber announcements after a rollcall is cancelled.

    Mirrors _post_end_cleanup's renumber-announcement shape exactly (same
    plain-text repost, no badges/ghost/settle-nudge step — a cancelled
    session recorded no stats, so there is nothing to settle or ghost-check).
    Shared by /xrc and the panel's Cancel button so the two paths can't drift.
    `retired` as in _post_end_cleanup.
    """
    if retired is _NOT_DONE:
        retired = _close_panel_bookkeeping(cid, ended_number, result,
                                           (result.get("cancelled") or {}).get("id"))
    await _strip_panel(cid, retired)

    updated_rollcalls = manager.get_rollcalls(cid)
    if updated_rollcalls:
        lines = [f"⚠️ Rollcall #{ended_number} cancelled. IDs updated:"]
        for entry in result["renumbered"]:
            lines.append(f"  #{entry['old']} '{entry['title']}' → #{entry['new']}")
        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, "\n".join(lines))
            for idx, rollcall in enumerate(updated_rollcalls):
                new_id = idx + 1
                text = f"Rollcall number {new_id}\n\n" + _build_panel_text(rollcall, new_id)
                await bot.send_message(cid, text)


def _group_web_url(cid: int) -> str:
    """Return the permanent group web URL for the keyboard button, or empty string."""
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    if not base:
        return ""
    try:
        from db import get_or_create_chat
        chat = get_or_create_chat(cid)
        token = chat.get("group_web_token")
        return f"{base}/web/group/{token}" if token else ""
    except Exception:
        return ""


# ── Inline keyboards ──────────────────────────────────────────────────────────

def _btn(action: str, rc_number: int, rc_id) -> str:
    """Callback data for a panel button: btn_<action>_<N>_<rollcall db id>.

    N is only the label the panel was drawn with; the db id is what the tap
    acts on. Positions shift when an earlier rollcall closes, and a button
    that resolved "#2" by position after #1 ended would act on whatever is
    #2 now. The legacy three-part form (no id) is still accepted for panels
    posted before this change — see callback_handler.
    """
    if rc_id:
        return f"btn_{action}_{rc_number}_{rc_id}"
    return f"btn_{action}_{rc_number}"


async def get_status_keyboard(rc_number: int, chat_id: int) -> InlineKeyboardMarkup:
    """The vote panel's keyboard. Always offers the web link when one exists.

    chat_id is required and the URL is derived HERE rather than passed in.
    It used to be an optional `web_url` that each of eleven call sites had to
    remember to compute, and two of them — both in the scheduler — did not.
    The effect was invisible in testing and obvious in use: a rollcall the
    scheduler started came up with no "Vote on Web" button, and then grew one
    the moment somebody voted, because the vote path rebuilt the panel
    through a call site that did pass it.

    An optional argument that silently degrades a feature is the bug. Taking
    the chat id instead means there is nothing left to omit. The rollcall's
    db id (stamped into every button) is derived here for the same reason.
    """
    web_url = _group_web_url(chat_id)
    rc = manager.get_rollcall(chat_id, rc_number - 1)
    rc_id = _rc_db_id(rc) if rc is not None else None
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("✅ IN",       callback_data=_btn("in", rc_number, rc_id)),
        InlineKeyboardButton("❌ OUT",      callback_data=_btn("out", rc_number, rc_id)),
        InlineKeyboardButton("❓ MAYBE",    callback_data=_btn("maybe", rc_number, rc_id)),
    )
    markup.add(
        InlineKeyboardButton("📋 Lists",   callback_data=_btn("lists", rc_number, rc_id)),
        InlineKeyboardButton("🔄 Refresh", callback_data=_btn("refresh", rc_number, rc_id)),
    )
    if web_url:
        markup.add(InlineKeyboardButton("🌐 Vote on Web", url=web_url))
    markup.add(
        InlineKeyboardButton(f"🛑 End RollCall #{rc_number}", callback_data=_btn("end", rc_number, rc_id))
    )
    markup.add(
        InlineKeyboardButton(f"🗑 Cancel RollCall #{rc_number}", callback_data=_btn("cancelrc", rc_number, rc_id))
    )
    return markup


async def get_lists_keyboard(rc_number: int, rc_id) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Who's IN",    callback_data=_btn("wi", rc_number, rc_id)),
        InlineKeyboardButton("❌ Who's OUT",   callback_data=_btn("wo", rc_number, rc_id)),
    )
    markup.add(
        InlineKeyboardButton("❓ Who's Maybe", callback_data=_btn("wm", rc_number, rc_id)),
        InlineKeyboardButton("⏳ Waiting",     callback_data=_btn("ww", rc_number, rc_id)),
    )
    markup.add(InlineKeyboardButton("🔙 Back", callback_data=_btn("status", rc_number, rc_id)))
    return markup


async def get_end_confirm_keyboard(rc_number: int, rc_id) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Yes",    callback_data=_btn("endconfirm", rc_number, rc_id)),
        InlineKeyboardButton("❌ Cancel", callback_data=_btn("endcancel", rc_number, rc_id)),
    )
    return markup


async def get_cancelrc_confirm_keyboard(rc_number: int, rc_id) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Yes, cancel", callback_data=_btn("cancelyes", rc_number, rc_id)),
        InlineKeyboardButton("❌ No",          callback_data=_btn("canceldismiss", rc_number, rc_id)),
    )
    return markup


async def get_cancelrc_skip_keyboard(rc_number: int, rc_id) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("⏭ Skip", callback_data=_btn("cancelskip", rc_number, rc_id)))
    return markup


# ── Panel machinery ───────────────────────────────────────────────────────────

def _persist_panel_msg_id(rc, msg_id: int) -> None:
    rc_db_id = getattr(rc, 'db_id', None) or getattr(rc, 'id', None)
    if rc_db_id:
        rc.panel_msg_id = msg_id
        update_rollcall(rc_db_id, panel_msg_id=msg_id)


async def _update_panel(cid: int, rc_number: int, rc, force_new: bool = False) -> None:
    """Update the status panel for a rollcall.

    Edits the existing panel in place (a new message only if there is none,
    or it was deleted). force_new — always sends a fresh message (/panel).

    Bursts: when refreshes for the same rollcall overlap — ten votes in one
    second — the first is drawn now and the rest are folded into ONE more
    redraw once the burst settles, which reads the state as it is then
    (chat_outbox.coalesce). A refresh that doesn't overlap another is drawn
    immediately, exactly as before. Coalescing is keyed by the rollcall's db
    id and the trailing redraw re-finds it by id, so a renumber or close in
    between can't redraw the wrong panel.
    """
    rc_id = _rc_db_id(rc)
    if force_new or not rc_id:
        await _render_panel(cid, rc_number, rc, force_new=force_new)
        return

    async def _trailing():
        idx = find_rollcall_index(cid, rc_id, manager)
        if idx is None:
            return                      # closed meanwhile: its final panel is not ours to redraw
        await _render_panel(cid, idx + 1, manager.get_rollcall(cid, idx))

    await chat_outbox.coalesce(
        ("panel", cid, rc_id),
        lambda: _render_panel(cid, rc_number, rc),
        _trailing,
    )


async def _render_panel(cid: int, rc_number: int, rc, force_new: bool = False) -> None:
    """Draw the panel from `rc` as it is now (sends are paced below — see
    chat_outbox.install_request_pacing)."""
    key = (cid, rc_number)

    if key not in _panel_msg_ids and getattr(rc, 'panel_msg_id', None):
        _panel_msg_ids[key] = rc.panel_msg_id

    text = _build_panel_text(rc, rc_number)
    markup = await get_status_keyboard(rc_number, cid)

    if not force_new:
        existing_msg_id = _panel_msg_ids.get(key)
        if existing_msg_id:
            try:
                await bot.edit_message_text(text, cid, existing_msg_id, reply_markup=markup)
                return
            except Exception as e:
                err = str(e).lower()
                if "message is not modified" in err:
                    return
                if chat_outbox.is_rate_limited(e):
                    # Telegram is throttling this chat. Posting a whole new
                    # panel instead (the fallback below) would only add to
                    # the flood and leave a duplicate list behind.
                    raise
                logging.debug(f"Panel edit failed for ({cid}, {rc_number}): {e}")

    sent = await bot.send_message(cid, text, reply_markup=markup)
    _panel_msg_ids[key] = sent.message_id
    _persist_panel_msg_id(rc, sent.message_id)


async def show_panel_for_rollcall(chat_id: int, rc_number: int, force_new: bool = False):
    rollcalls = manager.get_rollcalls(chat_id)
    index = rc_number - 1
    if index < 0 or index >= len(rollcalls):
        return
    rc = rollcalls[index]
    await _update_panel(chat_id, rc_number, rc, force_new=force_new)


# ── Proxy owner notification ──────────────────────────────────────────────────

async def notify_proxy_owner_wait_to_in(rc, moved_user: User, cid, title: str, rc_number: int):
    try:
        proxy_owners = getattr(rc, "proxy_owners", None)
        if not proxy_owners:
            return
        user_key = moved_user.user_id
        owner_id = proxy_owners.get(user_key)
        if not owner_id:
            return
        try:
            member = await bot.get_chat_member(cid, owner_id)
            if member.user.username:
                owner_mention = f"@{_esc_md(member.user.username)}"
            else:
                owner_mention = f"[{_esc_md(member.user.first_name)}](tg://user?id={owner_id})"
        except Exception:
            owner_mention = "Proxy owner"

        txt = f"{owner_mention}, your proxy {format_mention_with_name_md(moved_user)} → IN for '{_esc_md(title)}' (#{rc_number})"
        await bot.send_message(cid, txt, parse_mode="Markdown")

        try:
            await bot.send_message(
                owner_id,
                f"🎉 Your proxy *{_esc_md(moved_user.name)}* got promoted from WAITING → IN for *{_esc_md(title)}* (#{rc_number})!",
                parse_mode="Markdown",
            )
        except Exception:
            pass  # DM best-effort (user may have blocked the bot / never started a chat with it) — the group announcement above already happened
    except Exception:
        logging.exception("Failed to notify proxy owner for WAITING→IN")


# ── repeat= flag (shared by /start_roll_call and /repeat) ──────────────────────
# Auto-creates/refreshes a template from a fresh rollcall's own settings and
# schedules it to recur — a one-step shortcut for the common "same game every
# week" case that would otherwise need /set_template + /schedule_template.

_REPEAT_FLAG_RE = re.compile(r"^repeat=(\w+)$", re.IGNORECASE)
_REPEAT_TYPES = {"daily", "weekly", "biweekly", "monthly"}


def _extract_repeat_flag(tokens: list) -> tuple:
    """Scan title tokens for a `repeat=<type>` flag (anywhere, not just
    trailing) and strip it out. Returns (remaining_tokens, repeat_type or
    None) — repeat_type is lowercased and NOT validated here; the caller
    checks it against _REPEAT_TYPES before creating anything, so a typoed
    flag fails cleanly instead of starting an unscheduled rollcall."""
    remaining = []
    repeat_type = None
    for tok in tokens:
        m = _REPEAT_FLAG_RE.match(tok)
        if m and repeat_type is None:
            repeat_type = m.group(1).lower()
        else:
            remaining.append(tok)
    return remaining, repeat_type


async def _apply_repeat_flag(cid: int, rc, repeat_type: str, admin_user_id: int, admin_name: str):
    """Create/refresh a template from `rc`'s settings (title, limit,
    location, fee — same fields /repeat already clones) and schedule it to
    recur on rc's own weekday/day-of-month and time, so it repeats going
    forward with zero further admin action. Returns a confirmation string,
    or None if scheduling failed — best-effort: a scheduling failure must
    not undo the rollcall that's already been created and announced, so
    this never raises past its own try/except.

    Template name is a slug of the title; upsert_template is idempotent
    (services/templates.py), so re-running `repeat=weekly` under the same
    title next week just refreshes the existing template/schedule instead
    of erroring or creating a duplicate.
    """
    try:
        tz = pytz.timezone(rc.timezone)
    except Exception:
        tz = pytz.timezone("Asia/Kolkata")
    now = datetime.now(tz)

    slug = re.sub(r"[^a-z0-9]+", "-", (rc.title or "").lower()).strip("-")[:40] or "repeat"

    from services import templates as templates_svc

    try:
        templates_svc.upsert_template(
            cid, slug, admin_user_id, admin_name,
            title=rc.title, limit=rc.inListLimit or None,
            location=rc.location or None, fee=rc.event_fee or None,
        )
        sched_kwargs = {"recurrence_type": repeat_type, "schedule_time": now.strftime("%H:%M")}
        if repeat_type == "monthly":
            sched_kwargs["monthly_day"] = now.day
        else:
            sched_kwargs["schedule_day"] = now.strftime("%A").lower()
        # The rollcall just started by hand IS today's occurrence — without
        # fired_today the scheduler opened a second one a minute later.
        templates_svc.set_schedule(cid, slug, admin_user_id, admin_name, fired_today=True, **sched_kwargs)
    except Exception:
        logging.exception("[repeat] failed to schedule recurring template for chat %s", cid)
        return None

    when = ("every day" if repeat_type == "daily"
            else f"on day {now.day} of the month" if repeat_type == "monthly"
            else f"every {now.strftime('%A')} ({repeat_type})")
    return f"🔁 Repeats {when} at {now.strftime('%H:%M')} {tz.zone} — manage with /schedules."


# ── /start_roll_call ──────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/start_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/src")
async def start_roll_call(message):
    cid = message.chat.id
    msg = message.text

    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        arr = msg.split(" ")
        title_tokens, repeat_type = _extract_repeat_flag(arr[1:])
        if repeat_type and repeat_type not in _REPEAT_TYPES:
            raise incorrectParameter(
                f"'{repeat_type}' isn't a valid repeat type. Use: " + ", ".join(sorted(_REPEAT_TYPES))
            )
        title = ' '.join(title_tokens) if title_tokens else None

        # Creating a rollcall shifts the numbering every other command
        # addresses lists by, so it has to serialise with votes and /erc
        # like every other mutation (CLAUDE.md).
        async with manager.get_chat_write_lock(cid):
            result = await rollcalls_svc.start_rollcall(
                cid, title,
                message.from_user.id, message.from_user.first_name,
                message.from_user.username,
            )
        rc_number_1based = result["number"]
        rc = manager.get_rollcall(cid, result["rc_index"])
        markup = await get_status_keyboard(rc_number_1based, cid)
        text = _build_panel_text(rc, rc_number_1based)
        sent = await bot.send_message(message.chat.id, text, reply_markup=markup)
        _panel_msg_ids[(cid, rc_number_1based)] = sent.message_id
        _persist_panel_msg_id(rc, sent.message_id)

        if repeat_type:
            repeat_msg = await _apply_repeat_flag(
                cid, rc, repeat_type, message.from_user.id,
                message.from_user.first_name or message.from_user.username or "Admin",
            )
            if repeat_msg:
                await bot.send_message(cid, repeat_msg)

        # Dues reminder: prompt the admin to set the ground cost now so it
        # isn't forgotten before /erc.  Only shown in non-shh mode.
        chat_row = db.get_or_create_chat(cid)
        if chat_row.get("dues_enabled") and not manager.get_shh_mode(cid):
            await bot.send_message(
                cid,
                "💰 *Dues active* — set the ground cost now so it's ready for `/settle_dues`:\n"
                "`/ef <amount>`  e.g. `/ef 600`",
                parse_mode="Markdown",
            )

    except Exception as e:
        await reply_error(cid, e)


# ── /repeat ───────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/repeat")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/rpt")
async def repeat_roll_call(message):
    """Clone the last ended rollcall's settings (title, fee, location, limit) into a new one."""
    cid = message.chat.id
    try:
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        _tokens, repeat_type = _extract_repeat_flag(message.text.split(" ")[1:])
        if repeat_type and repeat_type not in _REPEAT_TYPES:
            raise incorrectParameter(
                f"'{repeat_type}' isn't a valid repeat type. Use: " + ", ".join(sorted(_REPEAT_TYPES))
            )

        last = db.get_latest_ended_rollcall(cid)
        if not last:
            raise incorrectParameter(
                "No previous rollcall found to repeat. Start one with /rc <title>."
            )

        # Creating a rollcall shifts the numbering every other command
        # addresses lists by, so it has to serialise with votes and /erc
        # like every other mutation (CLAUDE.md).
        async with manager.get_chat_write_lock(cid):
            result = await rollcalls_svc.start_rollcall(
                cid, last.get("title"),
                message.from_user.id, message.from_user.first_name,
                message.from_user.username,
            )
        rc_number_1based = result["number"]
        rc = manager.get_rollcall(cid, result["rc_index"])

        # Apply cloned settings before building the panel so it shows them
        cloned = []
        if last.get("in_list_limit"):
            rc.inListLimit = last["in_list_limit"]
            cloned.append(f"limit {last['in_list_limit']}")
        if last.get("location"):
            rc.location = last["location"]
            cloned.append(f"location {last['location']}")
        if last.get("event_fee"):
            rc.event_fee = last["event_fee"]
            cloned.append(f"fee {last['event_fee']}")
        rc.save()

        markup = await get_status_keyboard(rc_number_1based, cid)
        text = _build_panel_text(rc, rc_number_1based)
        sent = await bot.send_message(cid, text, reply_markup=markup)
        _panel_msg_ids[(cid, rc_number_1based)] = sent.message_id
        _persist_panel_msg_id(rc, sent.message_id)

        if cloned and not manager.get_shh_mode(cid):
            await bot.send_message(
                cid,
                f"🔁 Repeated from last game — carried over: {', '.join(cloned)}",
            )

        if repeat_type:
            repeat_msg = await _apply_repeat_flag(
                cid, rc, repeat_type, message.from_user.id,
                message.from_user.first_name or message.from_user.username or "Admin",
            )
            if repeat_msg:
                await bot.send_message(cid, repeat_msg)
    except Exception as e:
        await reply_error(cid, e)


# ── /end_roll_call ────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/end_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/erc")
async def end_roll_call(message):
    try:
        if roll_call_not_started(message, manager) is False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        pmts = message.text.split(" ")[1:]
        rc_number = 0
        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The RollCallnumber must be a positive integer")

        async with manager.get_chat_write_lock(cid):
            ensure_rc_number(cid, rc_number, manager)
            rc = manager.get_rollcall(cid, rc_number)
            ended_number = rc_number + 1
            ended_by = message.from_user.first_name or message.from_user.username or "someone"

            # Capture finish text before the service removes the rollcall
            finish_text = rc.finishList().replace("__RCID__", str(ended_number))
            finish_text = f"{finish_text}\n\n🎉 Ended by {ended_by}"

            # One-line summary appended to finish text
            _in_n = len(rc.inList)
            _out_n = len(rc.outList)
            _maybe_n = len(rc.maybeList)
            _medals = ["🥇", "🥈", "🥉"]
            _top = [u.name for u in rc.inList[:3]]
            _top_str = "  ".join(f"{_medals[i]} {_top[i]}" for i in range(len(_top)))
            finish_text += f"\n\n📊 {_in_n} IN · {_out_n} OUT · {_maybe_n} MAYBE"
            if _top_str:
                finish_text += f"\n{_top_str}"

            result = await rollcalls_svc.end_rollcall(
                cid, rc_number,
                message.from_user.id, message.from_user.first_name,
                message.from_user.username,
            )
            retired = _close_panel_bookkeeping(cid, ended_number, result, result.get("rc_db_id"))

        # Announce after releasing the lock — see _close_panel_bookkeeping.
        await bot.send_message(cid, finish_text)
        await _post_end_cleanup(cid, ended_number, result, rc_title=rc.title, retired=retired)
    except Exception as e:
        await reply_error(message, e)


# ── /cancel_roll_call (/xrc) ──────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/cancel_roll_call")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/xrc")
async def cancel_roll_call(message):
    try:
        if roll_call_not_started(message, manager) is False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        cid = message.chat.id
        pmts = message.text.split(" ")[1:]
        rc_number = 0
        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The RollCallnumber must be a positive integer")
        reason = " ".join(pmts).strip() or None

        async with manager.get_chat_write_lock(cid):
            ensure_rc_number(cid, rc_number, manager)
            rc = manager.get_rollcall(cid, rc_number)
            ended_number = rc_number + 1
            cancelled_by = message.from_user.first_name or message.from_user.username or "someone"

            result = await rollcalls_svc.cancel_rollcall(
                cid, rc_number,
                message.from_user.id, message.from_user.first_name,
                message.from_user.username,
                reason=reason,
            )
            retired = _close_panel_bookkeeping(cid, ended_number, result,
                                               (result.get("cancelled") or {}).get("id"))

        # Announce after releasing the lock — see _close_panel_bookkeeping.
        reason_part = f" — {_esc_md(reason)}" if reason else ""
        await bot.send_message(
            cid,
            f"❌ *{_esc_md(rc.title)}* has been cancelled by {_esc_md(cancelled_by)}{reason_part}.\n"
            "No attendance was recorded.",
            parse_mode="Markdown",
        )
        await _post_cancel_cleanup(cid, ended_number, result, retired=retired)
    except Exception as e:
        await reply_error(message, e)


# ── /set_title ────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/set_title")
@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/st")
async def set_title(message):
    try:
        if roll_call_not_started(message, manager) is False:
            raise rollCallNotStarted("Roll call is not active")
        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")
        elif len(message.text.split(" ")) <= 1:
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        cid = message.chat.id
        msg = message.text
        pmts = msg.split(" ")[1:]
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

            ensure_rc_number(cid, rc_number, manager)

        title = " ".join(pmts)
        if not title.strip():
            await bot.send_message(message.chat.id, "Input title is missing")
            return

        # Renaming rewrites the rollcall row a concurrent vote is also
        # writing to.
        async with manager.get_chat_write_lock(cid):
            result = rollcalls_svc.set_title(
                cid, rc_number, title,
                message.from_user.id, message.from_user.first_name,
            )
        rc = manager.get_rollcall(cid, rc_number)

        if not manager.get_shh_mode(cid):
            await bot.send_message(cid, "The roll call title is set to: " + result["title"])

        await _update_panel(cid, rc_number + 1, rc)

    except Exception as e:
        await reply_error(message, e)


# ── /panel ────────────────────────────────────────────────────────────────────

@bot.message_handler(func=lambda message: (message.text.split(" "))[0].split("@")[0].lower() == "/panel")
async def show_panel(message):
    try:
        cid = message.chat.id

        if await admin_rights(message, manager) is False:
            raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")

        pmts = message.text.split(" ")[1:]
        rc_number = 0

        if len(pmts) > 0 and "::" in pmts[-1]:
            try:
                rc_number = int(pmts[-1].replace("::", "")) - 1
                del pmts[-1]
            except Exception:
                raise incorrectParameter("The rollcall number must be a positive integer")

        ensure_rc_number(cid, rc_number, manager)

        rc = manager.get_rollcall(cid, rc_number)
        text = _build_panel_text(rc, rc_number + 1)
        markup = await get_status_keyboard(rc_number + 1, cid)

        sent = await bot.send_message(cid, text, reply_markup=markup)
        _panel_msg_ids[(cid, rc_number + 1)] = sent.message_id
        _persist_panel_msg_id(rc, sent.message_id)

    except Exception as e:
        await reply_error(message, e)


# ── btn_* callback handler ────────────────────────────────────────────────────

def reconf_callback(choice: str, rc_index: int, user_id: int, rc_id) -> str:
    """reconf_<in|out>_<index>_<uid>[_<rollcall db id>] — the ghost
    re-confirmation buttons. The id is what the confirm acts on (see
    handlers/ghost.py); the index is kept for prompts posted before it."""
    base = f"reconf_{choice}_{rc_index}_{user_id}"
    return f"{base}_{rc_id}" if rc_id else base


async def _resolve_legacy_panel(call, cid: int, rc_number: int):
    """0-based index for a panel posted before buttons carried the rollcall's
    db id (btn_<action>_<N>), or None after answering the tap.

    Position is all such a button knows, and position is exactly what drifts.
    The tapped message's own id is the tie-breaker: if it is the recorded
    panel of an open rollcall, act on THAT one whatever its number is now; if
    it is the recorded panel of a rollcall that has since closed, refuse
    rather than act on whichever rollcall took its number. Only a message we
    have no record of falls back to the old by-position behaviour.
    """
    rollcalls = manager.get_rollcalls(cid)
    msg_id = getattr(call.message, "message_id", None)
    if isinstance(msg_id, int):
        for i, r in enumerate(rollcalls):
            if getattr(r, "panel_msg_id", None) == msg_id:
                return i
        try:
            closed = db.is_closed_rollcall_panel(cid, msg_id)
        except Exception:
            closed = False
        if closed:
            await _refuse_closed_panel(call, cid)
            return None
    if rc_number < 1 or rc_number > len(rollcalls):
        await bot.answer_callback_query(call.id, "Invalid rollcall!")
        return None
    return rc_number - 1


async def _refuse_closed_panel(call, cid: int) -> None:
    """A tap on a panel whose rollcall has closed: say so, and take the dead
    buttons away so nobody else taps them."""
    try:
        await bot.answer_callback_query(call.id, "This rollcall has already closed.", show_alert=True)
    except Exception:
        logging.warning("closed-panel answer failed (chat=%s)", cid, exc_info=True)
    await safe_edit_markup(cid, call.message.message_id, reply_markup=None)

# NOTE: the @callback_query_handler decorator lives on callback_handler (the
# single-arg dispatcher further down), NOT on these _cb_* helpers — telebot
# invokes handlers as fn(call), so registering a multi-arg helper breaks every
# panel button with a TypeError swallowed inside telebot's dispatch.

async def _cb_vote(call, cid: int, rc_number: int, rc, action: str) -> None:
    """btn_{in,out,maybe}_{rc_number}_{rc_id} — vote dispatch, ghost
    reconfirmation gate, waitlist/promotion announcements, and panel refresh.

    `rc` is the rollcall the tapped panel belongs to; every step below goes
    by its db id, never by position, so a vote racing /erc lands in this
    rollcall or nowhere — not in whichever one slid into its number."""
    rc_id = _rc_db_id(rc)
    if _is_rate_limited(cid, call.from_user.id):
        await bot.answer_callback_query(call.id, "You're voting too fast — please wait a moment.")
        return
    _username = call.from_user.username or None
    if not _username:
        asyncio.create_task(warn_no_username(cid, call.from_user.first_name)).add_done_callback(_log_task_exc)
    _first_name = _get_display_name(call.from_user)

    try:
        # Ghost reconfirmation check for IN votes (uses service helper)
        if action == "in":
            from bot_state import _pending_reconf
            reconf = voting_svc.check_ghost_reconfirmation_needed(
                cid, call.from_user.id, rc_number - 1, rollcall_id=rc_id)
            if reconf["needed"]:
                if (cid, call.from_user.id) in _pending_reconf:
                    await bot.answer_callback_query(call.id, "You already have a pending confirmation — please use the earlier buttons.")
                    return
                _pending_reconf[(cid, call.from_user.id)] = {
                    'rc_number': rc_number - 1,
                    'rc_db_id': rc_id,
                    'comment': '',
                    '_ts': datetime.now().timestamp(),
                }
                ghost_markup = InlineKeyboardMarkup(row_width=2)
                ghost_markup.add(
                    InlineKeyboardButton("✅ Yes, I'll be there!", callback_data=reconf_callback("in", rc_number - 1, call.from_user.id, rc_id)),
                    InlineKeyboardButton("❌ I'm out", callback_data=reconf_callback("out", rc_number - 1, call.from_user.id, rc_id)),
                )
                _user_obj = User(_first_name, _username, call.from_user.id, [])
                await bot.send_message(
                    cid,
                    f"👻 *Warning:* {format_mention_with_name_md(_user_obj)}, you've ghosted *{reconf['ghost_count']}* session(s) before.\n"
                    f"⚠️ Absent Limit: *{reconf['absent_limit']}*\n\n"
                    f"Are you committing to be at *{_esc_md(reconf['rollcall_title'])}*?",
                    parse_mode="Markdown",
                    reply_markup=ghost_markup
                )
                return

        if action == "in":
            svc_result = await voting_svc.vote_in(cid, call.from_user.id, _first_name, _username, rc_number=rc_number - 1, rollcall_id=rc_id)
        elif action == "out":
            svc_result = await voting_svc.vote_out(cid, call.from_user.id, _first_name, _username, rc_number=rc_number - 1, rollcall_id=rc_id)
        else:
            svc_result = await voting_svc.vote_maybe(cid, call.from_user.id, _first_name, _username, rc_number=rc_number - 1, rollcall_id=rc_id)
    except alreadyInList as e:
        await bot.answer_callback_query(call.id, str(e) or "You're already in this status!")
        return
    except rollCallClosed:
        await _refuse_closed_panel(call, cid)
        return

    # The service resolved the rollcall by id inside the lock and reports
    # where it is NOW. The lock is released again, so a concurrent /erc
    # could have closed it since — re-find it by id for the panel refresh.
    rc_number = svc_result["rc_number_1based"]
    idx = find_rollcall_index(cid, rc_id, manager) if rc_id else rc_number - 1
    rc = manager.get_rollcall(cid, idx) if idx is not None else None
    if rc is None:
        logging.warning(
            "Rollcall #%s in chat %s vanished between vote and panel refresh",
            rc_number, cid,
        )
        await bot.answer_callback_query(call.id, "Vote recorded, but this rollcall has since ended.")
        return
    rc_number = idx + 1
    user_d = svc_result["user"]
    user_id_v = user_d["user_id"]
    user_name_v = user_d["name"]
    user_uname_v = user_d.get("username")

    # The vote is saved: every Telegram call below is best_effort, so a 429
    # during a burst is logged instead of surfacing as a failed vote.
    if svc_result["action"] == "waitlisted":
        await best_effort(bot.answer_callback_query(call.id, "Event max limit reached, added to waitlist"),
                          "tap answer", cid)
        if not manager.get_shh_mode(cid):
            if isinstance(user_id_v, int):
                _u = User(user_name_v, user_uname_v, user_id_v, [])
                await best_effort(bot.send_message(
                    cid,
                    f"{format_mention_with_name_md(_u)} → WAITING for '{_esc_md(rc.title)}' (#{rc_number})",
                    parse_mode="Markdown",
                ), "waitlist announcement", cid)
            else:
                await best_effort(bot.send_message(cid, f"{user_name_v} → WAITING for '{rc.title}' (#{rc_number})"),
                                  "waitlist announcement", cid)
    else:
        await best_effort(bot.answer_callback_query(call.id, "Status updated"), "tap answer", cid)
        if not manager.get_shh_mode(cid):
            label = {"in": "IN", "out": "OUT", "maybe": "MAYBE"}[action]
            if isinstance(user_id_v, int):
                _u = User(user_name_v, user_uname_v, user_id_v, [])
                await best_effort(bot.send_message(
                    cid,
                    f"{format_mention_with_name_md(_u)} → {label} for '{_esc_md(rc.title)}' (#{rc_number})",
                    parse_mode="Markdown",
                ), "vote announcement", cid)
            else:
                await best_effort(bot.send_message(cid, f"{user_name_v} → {label} for '{rc.title}' (#{rc_number})"),
                                  "vote announcement", cid)

    promoted = svc_result.get("promoted")
    if promoted and action in ("out", "maybe"):
        from handlers.promotion import announce_one
        await best_effort(announce_one(cid, promoted, rc.title, rc_number, rc), "promotion notice", cid)

    # The tapped message is the panel to redraw (as before); going through
    # _update_panel means a burst of taps is coalesced and paced exactly
    # like a burst of /in commands.
    _panel_msg_ids[(cid, rc_number)] = call.message.message_id
    await best_effort(_update_panel(cid, rc_number, rc), "panel refresh", cid)


async def _cb_lists_menu(call, cid: int, rc_number: int, rc) -> None:
    """btn_lists_{rc_number} — show the lists submenu (wi/wo/wm/ww buttons)."""
    await bot.answer_callback_query(call.id)
    markup = await get_lists_keyboard(rc_number, _rc_db_id(rc))
    try:
        await bot.edit_message_text("Select list:", cid, call.message.message_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Lists menu edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_list_view(call, cid: int, rc_number: int, rc, action: str) -> None:
    """btn_{wi,wo,wm,ww}_{rc_number} — render one specific list in place."""
    await bot.answer_callback_query(call.id)
    if action == "wi":
        text = rc.inListText()
    elif action == "wo":
        text = rc.outListText()
    elif action == "wm":
        text = rc.maybeListText()
    else:
        text = rc.waitListText()
    try:
        await bot.edit_message_text(
            text if text.strip() else "List is empty.",
            cid, call.message.message_id,
            reply_markup=await get_lists_keyboard(rc_number, _rc_db_id(rc)),
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("List view edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_status(call, cid: int, rc_number: int, rc) -> None:
    """btn_status_{rc_number} — back to the main panel."""
    await bot.answer_callback_query(call.id)
    text = _build_panel_text(rc, rc_number)
    markup = await get_status_keyboard(rc_number, cid)
    try:
        await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Status edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_refresh(call, cid: int, rc_number: int, rc) -> None:
    """btn_refresh_{rc_number} — re-render the main panel in place."""
    await bot.answer_callback_query(call.id, "Refreshed")
    text = _build_panel_text(rc, rc_number)
    markup = await get_status_keyboard(rc_number, cid)
    try:
        await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
        _panel_msg_ids[(cid, rc_number)] = call.message.message_id
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Refresh edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_end_prompt(call, cid: int, rc_number: int, rc) -> None:
    """btn_end_{rc_number} — show the "are you sure?" end-rollcall confirmation."""
    member = await bot.get_chat_member(cid, call.from_user.id)
    admin_mode = manager.get_admin_rights(cid)
    if admin_mode and member.status not in ["administrator", "creator"]:
        await bot.answer_callback_query(call.id, "⛔ Only admins can end rollcalls", show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    markup = await get_end_confirm_keyboard(rc_number, _rc_db_id(rc))
    # Auto-started rollcalls + /src with no args can leave rc.title empty
    # or "<Empty>". Show a sensible label instead of "''" in the prompt.
    if rc.title and rc.title != "<Empty>":
        rc_label = f"'{rc.title}'"
    else:
        rc_label = "this rollcall"
    try:
        await bot.edit_message_text(
            f"Are you sure you want to end {rc_label} (#{rc_number})?",
            cid, call.message.message_id, reply_markup=markup,
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("End-confirm edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_end_confirm(call, cid: int, rc_number: int, rc) -> None:
    """btn_endconfirm_{rc_number} — actually end the rollcall: finish list,
    stats/renumbering, ghost-check prompt, panel re-posting."""
    admin_mode = manager.get_admin_rights(cid)
    if admin_mode:
        member = await bot.get_chat_member(cid, call.from_user.id)
        if member.status not in ["administrator", "creator"]:
            await bot.answer_callback_query(call.id, "⛔ Only admins can end rollcalls", show_alert=True)
            return

    # Re-find THIS rollcall by db id inside the lock. Re-fetching by
    # position instead (as this did) ended whichever rollcall had slid into
    # #N while the "are you sure?" prompt sat there — the one action here
    # with no undo.
    rc_id = _rc_db_id(rc)
    # Close it under the lock; announce once the lock is released, so no vote
    # in this chat waits on a Telegram send (see _close_panel_bookkeeping).
    async with manager.get_chat_write_lock(cid):
        idx = find_rollcall_index(cid, rc_id, manager) if rc_id else rc_number - 1
        rc = manager.get_rollcall(cid, idx) if idx is not None else None
        if rc is not None:
            rc_number = idx + 1
            ended_by = call.from_user.first_name or call.from_user.username or "someone"
            ended_number = rc_number

            # Capture finish text before the service removes the rollcall.
            try:
                final_text = rc.finishList().replace("__RCID__", str(rc_number))
                final_text = f"{final_text}\n\n🎉 Ended by {ended_by}"
            except Exception:
                logging.exception(f"Failed to build finish list for rollcall #{rc_number}")
                final_text = f"Rollcall #{rc_number} ended by {ended_by}."

            result = await rollcalls_svc.end_rollcall(
                cid, rc_number - 1,
                call.from_user.id, ended_by,
                getattr(call.from_user, "username", None),
            )
            retired = _close_panel_bookkeeping(cid, ended_number, result, result.get("rc_db_id"))

    if rc is None:
        await bot.answer_callback_query(call.id, "Rollcall already ended.")
        await safe_edit_markup(cid, call.message.message_id, reply_markup=None)
        return

    await bot.answer_callback_query(call.id, "Rollcall ended")

    # Replace the "Are you sure?" prompt in-place with the finish list.
    try:
        await bot.edit_message_text(
            final_text, cid, call.message.message_id, reply_markup=None,
        )
    except Exception:
        logging.exception(f"Failed to edit end-confirm message for chat {cid}")
        try:
            await bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)
        except Exception:
            pass  # cosmetic (clear stale buttons); already logged above, and the real finish-list content is sent unconditionally next
        try:
            await bot.send_message(cid, final_text)
        except Exception:
            logging.exception(f"Failed to send finish list for chat {cid}")

    # This message (the panel itself) was just stripped above; any other
    # copy of the panel goes too.
    if retired != call.message.message_id:
        await _strip_panel(cid, retired)

    if result["ghost_eligible"]:
        ghost_markup = InlineKeyboardMarkup(row_width=2)
        ghost_markup.add(
            InlineKeyboardButton("👻 Yes, select ghosts", callback_data=f"ghost_yes_{result['ghost_rc_db_id']}"),
            InlineKeyboardButton("✅ No, all showed up", callback_data=f"ghost_no_{result['ghost_rc_db_id']}")
        )
        await bot.send_message(cid, f"👻 Did anyone ghost '{rc.title}'?", reply_markup=ghost_markup)

    updated_rollcalls = manager.get_rollcalls(cid)
    if updated_rollcalls and not manager.get_shh_mode(cid):
        lines = [f"⚠️ Rollcall #{ended_number} ended. IDs updated:"]
        for entry in result["renumbered"]:
            lines.append(f"  #{entry['old']} '{entry['title']}' → #{entry['new']}")
        await bot.send_message(cid, "\n".join(lines))
        for idx, rollcall in enumerate(updated_rollcalls):
            new_id = idx + 1
            text = _build_panel_text(rollcall, new_id)
            panel_markup = await get_status_keyboard(new_id, cid)
            sent = await bot.send_message(cid, text, reply_markup=panel_markup)
            if sent is not None:
                _panel_msg_ids[(cid, new_id)] = sent.message_id
                _persist_panel_msg_id(rollcall, sent.message_id)


async def _cb_end_cancel(call, cid: int, rc_number: int, rc) -> None:
    """btn_endcancel_{rc_number} — dismiss the end-confirmation, back to panel."""
    await bot.answer_callback_query(call.id, "Cancelled")
    text = _build_panel_text(rc, rc_number)
    markup = await get_status_keyboard(rc_number, cid)
    try:
        await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Endcancel edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_cancelrc_prompt(call, cid: int, rc_number: int, rc) -> None:
    """btn_cancelrc_{rc_number} — show the "are you sure?" cancel confirmation.

    Mirrors _cb_end_prompt, but the wording makes clear this records no
    stats at all — it's for a session that didn't happen, not one ending
    normally.
    """
    member = await bot.get_chat_member(cid, call.from_user.id)
    admin_mode = manager.get_admin_rights(cid)
    if admin_mode and member.status not in ["administrator", "creator"]:
        await bot.answer_callback_query(call.id, "⛔ Only admins can cancel rollcalls", show_alert=True)
        return
    await bot.answer_callback_query(call.id)
    markup = await get_cancelrc_confirm_keyboard(rc_number, _rc_db_id(rc))
    rc_label = f"'{rc.title}'" if rc.title and rc.title != "<Empty>" else "this rollcall"
    try:
        await bot.edit_message_text(
            f"Cancel {rc_label} (#{rc_number})? No stats will be recorded — use this "
            "for a session that didn't happen (rain, too few players, etc.), not one that just ended.",
            cid, call.message.message_id, reply_markup=markup,
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Cancelrc-confirm edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_cancelrc_dismiss(call, cid: int, rc_number: int, rc) -> None:
    """btn_canceldismiss_{rc_number} — dismiss the cancel-confirmation, back to panel."""
    await bot.answer_callback_query(call.id, "Dismissed")
    text = _build_panel_text(rc, rc_number)
    markup = await get_status_keyboard(rc_number, cid)
    try:
        await bot.edit_message_text(text, cid, call.message.message_id, reply_markup=markup)
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Canceldismiss edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _cb_cancelrc_yes(call, cid: int, rc_number: int, rc) -> None:
    """btn_cancelyes_{rc_number} — confirmed; ask for an optional reason
    before actually cancelling. The next free-text reply from this admin, or
    the Skip button, finishes the job via _execute_cancel."""
    admin_mode = manager.get_admin_rights(cid)
    if admin_mode:
        member = await bot.get_chat_member(cid, call.from_user.id)
        if member.status not in ["administrator", "creator"]:
            await bot.answer_callback_query(call.id, "⛔ Only admins can cancel rollcalls", show_alert=True)
            return
    rc_db_id = getattr(rc, "db_id", None) or getattr(rc, "id", None)
    _prune_pending(_pending_cancel_reason)
    _pending_cancel_reason[(cid, call.from_user.id)] = {
        "rc_db_id": rc_db_id, "title": rc.title, "message_id": call.message.message_id,
        "_ts": datetime.now().timestamp(),
    }
    await bot.answer_callback_query(call.id)
    skip_markup = await get_cancelrc_skip_keyboard(rc_number, rc_db_id)
    try:
        await bot.edit_message_text(
            "✏️ Reason for cancelling (optional)? Reply with text, or tap Skip.",
            cid, call.message.message_id, reply_markup=skip_markup,
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logging.warning("Cancel-reason prompt edit failed (chat=%s msg=%s): %s", cid, call.message.message_id, e)


async def _execute_cancel(cid: int, actor, pending: dict, message_id: int, reason: str | None) -> None:
    """Shared tail for both post-confirm paths (typed reason, or Skip tap).

    Re-resolves the rollcall by rc_db_id rather than trusting any rc_number
    captured earlier — time has passed waiting for the reply, and another
    admin could have ended or cancelled a different rollcall in the meantime,
    shifting numbers.
    """
    if not pending:
        return
    rc_db_id = pending.get("rc_db_id")
    actor_name = actor.first_name or actor.username or "someone"

    # Cancel under the lock; announce after releasing it (see
    # _close_panel_bookkeeping) so no vote waits on a Telegram send.
    async with manager.get_chat_write_lock(cid):
        rollcalls = manager.get_rollcalls(cid)
        idx = next(
            (i for i, r in enumerate(rollcalls)
             if (getattr(r, "db_id", None) or getattr(r, "id", None)) == rc_db_id),
            None,
        )
        if idx is not None:
            ended_number = idx + 1
            result = await rollcalls_svc.cancel_rollcall(
                cid, idx, actor.id, actor_name, getattr(actor, "username", None),
                reason=reason,
            )
            retired = _close_panel_bookkeeping(cid, ended_number, result,
                                               (result.get("cancelled") or {}).get("id"))

    if idx is None:
        try:
            await bot.edit_message_text(
                "That rollcall is no longer open — nothing to cancel.",
                cid, message_id, reply_markup=None,
            )
        except Exception:
            pass
        return

    reason_part = f" — {_esc_md(reason)}" if reason else ""
    final_text = (
        f"❌ *{_esc_md(pending.get('title') or 'Rollcall')}* has been cancelled "
        f"by {_esc_md(actor_name)}{reason_part}.\nNo attendance was recorded."
    )
    try:
        await bot.edit_message_text(final_text, cid, message_id, reply_markup=None, parse_mode="Markdown")
    except Exception:
        logging.exception(f"Failed to edit cancel-confirm message for chat {cid}")
        try:
            await bot.send_message(cid, final_text, parse_mode="Markdown")
        except Exception:
            logging.exception(f"Failed to send cancel result for chat {cid}")

    await _post_cancel_cleanup(cid, ended_number, result, retired=retired)


async def _cb_cancelrc_skip(call, cid: int) -> None:
    """btn_cancelskip_{rc_number} — skip the optional reason, cancel now."""
    pending = _pending_cancel_reason.pop((cid, call.from_user.id), None)
    await bot.answer_callback_query(call.id)
    await _execute_cancel(cid, call.from_user, pending, call.message.message_id, reason=None)


@bot.message_handler(func=lambda m: (
    m.from_user is not None
    and m.text is not None
    and not m.text.startswith("/")
    and (m.chat.id, m.from_user.id) in _pending_cancel_reason
))
async def cancel_reason_reply(message):
    """Free-text reply to the "reason for cancelling?" prompt — consumed
    once, same mechanism as _pending_subsidy_input / _pending_payment_input."""
    try:
        cid = message.chat.id
        uid = message.from_user.id
        pending = _pending_cancel_reason.pop((cid, uid), None)
        if not pending:
            return
        reason = message.text.strip() or None
        await _execute_cancel(cid, message.from_user, pending, pending["message_id"], reason=reason)
    except Exception as e:
        await reply_error(message, e)


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("btn_"))
async def callback_handler(call):
    try:
        raw_data = call.data or ""
        cid = call.message.chat.id

        data = raw_data.split("_")
        if len(data) not in (3, 4) or data[0] != "btn":
            await bot.answer_callback_query(call.id, "Invalid action")
            return

        action = data[1]

        try:
            rc_number = int(data[2])
            rc_id = int(data[3]) if len(data) == 4 else None
        except ValueError:
            await bot.answer_callback_query(call.id, "Invalid rollcall number")
            return

        if action == "cancelskip":
            # Acts on the rollcall stashed by btn_cancelyes (by db id), not
            # on anything in this button — and must still answer after that
            # rollcall closed.
            await _cb_cancelrc_skip(call, cid)
            return

        if rc_id is not None:
            idx = find_rollcall_index(cid, rc_id, manager)
            if idx is None:
                await _refuse_closed_panel(call, cid)
                return
            rc_number = idx + 1
        else:
            idx = await _resolve_legacy_panel(call, cid, rc_number)
            if idx is None:
                return
            rc_number = idx + 1

        rc = manager.get_rollcalls(cid)[rc_number - 1]

        # ── IN / OUT / MAYBE ──────────────────────────────────────────────────
        if action in ("in", "out", "maybe"):
            await _cb_vote(call, cid, rc_number, rc, action)
            return

        # ── Lists submenu ─────────────────────────────────────────────────────
        if action == "lists":
            await _cb_lists_menu(call, cid, rc_number, rc)
            return

        if action in ("wi", "wo", "wm", "ww"):
            await _cb_list_view(call, cid, rc_number, rc, action)
            return

        # ── Back to main panel ────────────────────────────────────────────────
        if action == "status":
            await _cb_status(call, cid, rc_number, rc)
            return

        # ── Refresh ───────────────────────────────────────────────────────────
        if action == "refresh":
            await _cb_refresh(call, cid, rc_number, rc)
            return

        # ── Show end confirmation ─────────────────────────────────────────────
        if action == "end":
            await _cb_end_prompt(call, cid, rc_number, rc)
            return

        # ── Confirm end rollcall ──────────────────────────────────────────────
        if action == "endconfirm":
            await _cb_end_confirm(call, cid, rc_number, rc)
            return

        # ── Cancel end rollcall ───────────────────────────────────────────────
        if action == "endcancel":
            await _cb_end_cancel(call, cid, rc_number, rc)
            return

        # ── Cancel rollcall (no stats) — show confirmation ──────────────────────
        if action == "cancelrc":
            await _cb_cancelrc_prompt(call, cid, rc_number, rc)
            return

        # ── Confirm cancel rollcall — ask for optional reason ───────────────────
        if action == "cancelyes":
            await _cb_cancelrc_yes(call, cid, rc_number, rc)
            return

        # ── Dismiss cancel-rollcall confirmation ────────────────────────────────
        if action == "canceldismiss":
            await _cb_cancelrc_dismiss(call, cid, rc_number, rc)
            return

        await bot.answer_callback_query(call.id, "Unknown action")

    except Exception as e:
        err_str = str(e)
        if "query is too old" in err_str or "query ID is invalid" in err_str:
            logging.warning(f"[{_ts()}] Stale callback ignored ({call.data[:50]}): {err_str[:80]}")
            return
        logging.exception("Error in callback_handler")
        try:
            await bot.answer_callback_query(call.id, "⚠️ Something went wrong.")
        except Exception:
            pass  # already logged above; nothing more useful to do if even the alert fails
