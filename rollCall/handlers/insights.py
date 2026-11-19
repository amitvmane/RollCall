"""
Group insights and quieter notices.

/vote_digest  [hours|off]  — bundled vote notices (services.vote_digest)
/weather [::N]             — forecast for a rollcall, on demand
/weather alerts on|off     — forecast heads-up before games (services.weather)
/group_health              — the admin checklist (services.group_health)
/recap                     — your own recap (services.stats.recap)
inline mode (@bot in any chat) — share a game card with a vote link

Each command is a thin adapter: the same service functions back the web
routes, so the two surfaces can't disagree.
"""
import logging
import os

from telebot.types import (InlineKeyboardButton, InlineKeyboardMarkup,
                           InlineQueryResultArticle, InputTextMessageContent)

import db as _db
from bot_state import bot, reply_error
from exceptions import incorrectParameter, insufficientPermissions, rollCallNotStarted
from functions import admin_rights
from rollcall_manager import manager
from services import group_health as health_svc
from services import stats as stats_svc
from services import vote_digest as digest_svc
from services import weather as weather_svc
from utils.text import parse_rc_suffix


def _is(name):
    return lambda m: bool(m.text) and m.text.split()[0].split("@")[0].lower() == name


def _args(message) -> list:
    return message.text.split()[1:]


async def _require_admin(message):
    if not await admin_rights(message, manager):
        raise insufficientPermissions("Error - user does not have sufficient permissions for this operation")


def _mode_text(m: dict) -> str:
    if m["mode"] == "bundled":
        return f"📦 Vote notices: bundled — one summary every {m['hours']}h, only when the list changed."
    if m["mode"] == "silent":
        return "🔇 Vote notices: silent (/shh)."
    return "🔔 Vote notices: one message per vote."


@bot.message_handler(func=_is("/vote_digest"))
async def vote_digest_cmd(message):
    cid = message.chat.id
    try:
        args = _args(message)
        if not args:
            await bot.send_message(cid, _mode_text(digest_svc.mode(cid)) + "\n\n"
                                   "/vote_digest 6 — bundle every 6 hours\n"
                                   "/vote_digest off — back to one message per vote\n"
                                   "/shh — no vote messages at all")
            return
        await _require_admin(message)
        arg = args[0].lower()
        if arg in ("off", "each"):
            m = digest_svc.set_mode(cid, "each", None, message.from_user.id, message.from_user.first_name)
        elif arg in ("on", "bundled"):
            m = digest_svc.set_mode(cid, "bundled", None, message.from_user.id, message.from_user.first_name)
        else:
            try:
                hours = int(arg.rstrip("h"))
            except ValueError:
                raise incorrectParameter("Use /vote_digest <hours> (1-48) or /vote_digest off.")
            m = digest_svc.set_mode(cid, "bundled", hours, message.from_user.id, message.from_user.first_name)
        await bot.send_message(cid, _mode_text(m))
    except Exception as e:
        await reply_error(message, e)


async def _weather_alerts_setting(message, args):
    """/weather alerts [on|off] — a sub-command, not its own command:
    Telegram caps the command menu at 100 entries and it is full."""
    cid = message.chat.id
    if not args:
        state = "on" if weather_svc.alerts_enabled(cid) else "off"
        await bot.send_message(cid, f"🌦 Weather alerts are {state}.\n\n"
                               "When on, I post a forecast the evening before and a few hours "
                               "before any game that has a location and a game time — "
                               "only if rain is expected. Use /weather alerts on|off.")
        return
    await _require_admin(message)
    if args[0].lower() not in ("on", "off"):
        raise incorrectParameter("Use /weather alerts on or /weather alerts off.")
    enabled = args[0].lower() == "on"
    weather_svc.set_alerts(cid, enabled, message.from_user.id, message.from_user.first_name)
    await bot.send_message(cid, "🌦 Weather alerts on — I'll warn the group if rain is expected before a game."
                           if enabled else "🌦 Weather alerts off.")


@bot.message_handler(func=_is("/weather"))
async def weather_cmd(message):
    cid = message.chat.id
    try:
        args = _args(message)
        if args and args[0].lower() == "alerts":
            await _weather_alerts_setting(message, args[1:])
            return
        idx, _rest = parse_rc_suffix(args)
        rollcalls = manager.get_rollcalls(cid)
        if not rollcalls:
            raise rollCallNotStarted("No rollcall is open — the forecast is for an open game.")
        if idx >= len(rollcalls):
            raise incorrectParameter(f"There is no rollcall ::{idx + 1}.")
        rc = rollcalls[idx]
        result = await weather_svc.rollcall_weather(rc)
        if result["ok"]:
            weather_svc.remember(rc.id, result["text"])
        await bot.send_message(cid, ("🌦 " if result["ok"] else "") + result["text"])
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=_is("/group_health"))
async def group_health_cmd(message):
    cid = message.chat.id
    try:
        await _require_admin(message)
        await bot.send_message(cid, health_svc.render_text(health_svc.check(cid)))
    except Exception as e:
        await reply_error(message, e)


@bot.message_handler(func=_is("/recap"))
async def recap_cmd(message):
    cid = message.chat.id
    uid = message.from_user.id
    name = message.from_user.first_name or ""
    try:
        if cid < 0:
            r = stats_svc.recap(cid, uid, name)
            await bot.send_message(cid, stats_svc.recap_text(r))
            return
        # In a DM: one recap per group this member plays in.
        chats = [c for c in _db.get_user_voted_chats(uid) if int(c["chat_id"]) < 0][:3]
        if not chats:
            await bot.send_message(cid, "No games on record yet — vote in a group rollcall and check back.")
            return
        for c in chats:
            r = stats_svc.recap(int(c["chat_id"]), uid, name)
            await bot.send_message(cid, stats_svc.recap_text(r, c.get("group_name") or ""))
    except Exception as e:
        await reply_error(message, e)


# ── Inline mode: share the next game in any chat ─────────────────────────────

def _game_card(chat: dict, rc) -> str:
    lines = [f"🏏 {rc.title}"]
    if chat.get("group_name"):
        lines.append(f"👥 {chat['group_name']}")
    when = weather_svc._game_time(rc)
    if when is not None:
        import pytz
        local = when.astimezone(pytz.timezone(rc.timezone or "Asia/Kolkata"))
        lines.append(f"🕖 {local.strftime('%a %d %b, %I:%M %p')}")
    if rc.location:
        lines.append(f"📍 {rc.location}")
    cap = f"/{rc.inListLimit}" if rc.inListLimit else ""
    lines.append(f"✅ {len(rc.inList)}{cap} in" + (f" · ⏳ {len(rc.waitList)} waiting" if rc.waitList else ""))
    return "\n".join(lines)


def inline_games(user_id: int) -> list:
    """Open games in groups this user plays in and still belongs to:
    [{"id", "title", "description", "text", "url"}], at most 10."""
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    games = []
    for c in _db.get_user_voted_chats(user_id):
        chat_id = int(c["chat_id"])
        if chat_id >= 0 or not _db.is_active_chat_member(chat_id, user_id):
            continue
        token = c.get("group_web_token")
        for rc in manager.get_rollcalls(chat_id):
            games.append({
                "id": f"rc{rc.id}",
                "title": rc.title,
                "description": (c.get("group_name") or "") + f" · {len(rc.inList)} in",
                "text": _game_card(c, rc),
                "url": f"{base}/web/group/{token}" if base and token else "",
            })
            if len(games) >= 10:
                return games
    return games


def _article(game: dict):
    markup = None
    if game["url"]:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🗳 Vote on the web", url=game["url"]))
    return InlineQueryResultArticle(
        id=game["id"], title=game["title"], description=game["description"],
        input_message_content=InputTextMessageContent(game["text"]), reply_markup=markup,
    )


@bot.inline_handler(func=lambda q: True)
async def share_game_inline(query):
    try:
        results = [_article(g) for g in inline_games(query.from_user.id)]
        await bot.answer_inline_query(query.id, results, cache_time=30, is_personal=True)
    except Exception:
        logging.exception("[inline] share failed for user %s", getattr(query.from_user, "id", None))
