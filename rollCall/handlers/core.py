"""
Core handlers: /start, /help, /rollcalls, /version, /set_admins, /unset_admins, /timezone, /broadcast
"""
import json
import logging

from bot_state import bot, data_file_path, reply_error
from config import ADMINS
from exceptions import parameterMissing
from functions import admin_rights, auto_complete_timezone
from rollcall_manager import manager
from db import get_all_chat_ids
from services import settings as settings_svc
from commands_registry import (
    COMMANDS, lookup_command, all_names_and_aliases,
    USER_CATEGORY_ORDER, ADMIN_CATEGORY_ORDER,
)

try:
    # Already pinned in requirements.lock — used here for "did you mean…?"
    from Levenshtein import distance as _lev_distance  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - extremely defensive
    def _lev_distance(a, b):
        return abs(len(a) - len(b)) + sum(1 for x, y in zip(a, b) if x != y)


def _esc_md(text):
    """Escape Markdown v1 special characters in a value we're about to render."""
    if not text:
        return text or ""
    for c in ('_', '*', '`', '['):
        text = text.replace(c, f'\\{c}')
    return text


def _format_cmd_line(cmd):
    """One-line summary entry: `/name (also /alias)  args  — summary`."""
    aliases = cmd.get("aliases") or []
    alias_part = f" (also /{', /'.join(aliases)})" if aliases else ""
    args = cmd.get("args") or ""
    args_part = f" {_esc_md(args)}" if args else ""
    return f"/{_esc_md(cmd['name'])}{_esc_md(alias_part)}{args_part} — {_esc_md(cmd['summary'])}"


def _render_command_list(scope_set, category_order, header):
    """Render the user or admin /help view from the COMMANDS registry."""
    by_cat = {}
    for c in COMMANDS:
        if c["scope"] not in scope_set:
            continue
        by_cat.setdefault(c["category"], []).append(c)

    ordered_cats = [c for c in category_order if c in by_cat]
    # Any category that wasn't in the order list goes to the end, alphabetised.
    for cat in sorted(by_cat):
        if cat not in ordered_cats:
            ordered_cats.append(cat)

    parts = [header, ""]
    for cat in ordered_cats:
        parts.append(f"*{_esc_md(cat)}*")
        for c in by_cat[cat]:
            parts.append(_format_cmd_line(c))
        parts.append("")

    parts.append("💡 `/help <command>` shows details — e.g. `/help start_roll_call`")
    parts.append("💡 Add `::2` or `::3` to target a specific rollcall when multiple are active")
    return "\n".join(parts)


def _render_command_detail(cmd):
    """Detail card for /help <name>."""
    aliases = cmd.get("aliases") or []
    alias_line = ", ".join(f"/{a}" for a in aliases) if aliases else "—"
    args = cmd.get("args") or "—"
    sample = cmd.get("sample") or "—"
    details = cmd.get("details") or cmd.get("summary") or ""

    lines = [
        f"*/{_esc_md(cmd['name'])}*",
        f"_{_esc_md(cmd['scope'].replace('_', ' ').title())} command — {_esc_md(cmd['category'])}_",
        "",
        _esc_md(cmd["summary"]),
        "",
        f"*Aliases:* {_esc_md(alias_line)}",
        f"*Arguments:* {_esc_md(args)}",
        f"*Example:* `{sample}`",  # leave sample raw inside code-span for readability
        "",
        _esc_md(details),
    ]
    return "\n".join(lines)


def _suggest_command(query):
    """Return the closest command name/alias by Levenshtein distance, or None
    if nothing's within 2 edits. Defends against typos in /help <cmd>."""
    if not query:
        return None
    q = query.strip().lstrip('/').lower()
    if not q:
        return None
    best, best_d = None, 999
    for candidate in all_names_and_aliases():
        d = _lev_distance(q, candidate)
        if d < best_d:
            best, best_d = candidate, d
    if best_d <= 2:
        return best
    return None


@bot.message_handler(func=lambda m: (
    m.chat.type == "private"
    and m.text.split()[0].split("@")[0].lower() == "/start"
    and len(m.text.split()) > 1
    and m.text.split()[1].startswith("v_")
))
async def handle_tg_verify(message):
    """Telegram deep-link identity verification: /start v_{code}"""
    import db as _db
    code = message.text.split()[1][2:]  # strip "v_" prefix
    user = message.from_user
    tg_name = user.first_name or (f"@{user.username}" if user.username else str(user.id))
    success = _db.mark_web_verify_token(code, user.id, tg_name, tg_username=user.username or None)
    if success:
        await bot.send_message(
            message.chat.id,
            f"✅ *{tg_name}*, your browser has been verified.\n\nReturn to the web page — it will update automatically.",
            parse_mode="Markdown",
        )
    else:
        await bot.send_message(
            message.chat.id,
            "⚠️ This verification link has expired or already been used. Go back to the web page and try again.",
        )


def _onboarding_keyboard(cid):
    """Inline buttons for the bot-join welcome. Web buttons appear only when
    WEB_BASE_URL is configured; the intro/web-panel links can't be typed as
    commands, so surfacing them as buttons is the only place they add value.
    Returns None when web voting isn't configured (plain-text welcome)."""
    import os
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    if not base:
        return None
    try:
        from services.web import get_group_web_token
        token = get_group_web_token(cid)
    except Exception:
        logging.exception("onboarding keyboard: get_group_web_token failed")
        return None

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🌐 What is RollCall?", url=base + "/"))
    kb.add(InlineKeyboardButton("🔗 Open group web panel", url=f"{base}/web/group/{token}"))
    return kb


@bot.message_handler(content_types=["new_chat_members"])
async def on_new_chat_members(message):
    """Send onboarding when the bot itself is added to a group."""
    try:
        me = await bot.get_me()
        if not any(m.id == me.id for m in (message.new_chat_members or [])):
            return
        cid = message.chat.id
        manager.get_chat(cid)
        await bot.send_message(
            cid,
            "👋 Hi! I'm *RollCall* — attendance for your group, made simple.\n\n"
            "Quick start:\n"
            "• /src — Start a rollcall\n"
            "• /in /out /maybe — Vote attendance\n"
            "• /erc — End & finalize\n"
            "• /weblink — Get a permanent web voting link\n"
            "• /help — See all commands\n\n"
            "Run /help at any time, or /help admin for admin commands.",
            parse_mode="Markdown",
            reply_markup=_onboarding_keyboard(cid),
        )
    except Exception:
        logging.exception("on_new_chat_members error")


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/start")
async def welcome_and_explanation(message):
    cid = message.chat.id
    manager.get_chat(cid)
    if await admin_rights(message, manager) == False:
        await bot.send_message(cid, "Error - User does not have sufficient permissions for this operation")
        return
    await bot.send_message(cid, 'Hi! im RollCall!\n\nUse /help to see all the commands')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/help")
async def help_commands(message):
    """Three forms:
      /help              → user-command list
      /help admin        → admin-command list (incl. super-admin docs)
      /help <command>    → detail card for one command (name OR any alias)
    Unknown <command> falls back to a fuzzy "did you mean…?" hint.
    Everything renders from the COMMANDS registry in commands_registry.py."""
    parts = message.text.strip().split()
    if len(parts) <= 1:
        await bot.send_message(
            message.chat.id,
            _render_command_list({"user"}, USER_CATEGORY_ORDER, "🎯 *RollCall — User Commands*"),
            parse_mode='Markdown',
        )
        return

    arg = parts[1].lower().lstrip('/')
    if arg == "admin":
        await bot.send_message(
            message.chat.id,
            _render_command_list({"user", "admin", "super_admin"}, ADMIN_CATEGORY_ORDER, "⚙️ *RollCall — Admin Commands*"),
            parse_mode='Markdown',
        )
        return

    cmd = lookup_command(arg)
    if cmd is not None:
        await bot.send_message(message.chat.id, _render_command_detail(cmd), parse_mode='Markdown')
        return

    # Unknown command — offer a suggestion if one is close.
    suggestion = _suggest_command(arg)
    if suggestion:
        await bot.send_message(
            message.chat.id,
            f"No command `/{_esc_md(arg)}` — did you mean `/{_esc_md(suggestion)}`? Try `/help {suggestion}`.",
            parse_mode='Markdown',
        )
    else:
        await bot.send_message(
            message.chat.id,
            f"No command `/{_esc_md(arg)}`. Use /help or /help admin to see the list.",
            parse_mode='Markdown',
        )


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/set_admins")
async def set_admins(message):
    cid = message.chat.id
    member = await bot.get_chat_member(cid, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return
    settings_svc.set_admin_rights(cid, True, message.from_user.id, message.from_user.first_name)
    await bot.send_message(cid, 'Admin permissions activated')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0] == "/unset_admins")
async def unset_admins(message):
    cid = message.chat.id
    member = await bot.get_chat_member(cid, message.from_user.id)
    if member.status not in ['administrator', 'creator']:
        await bot.send_message(cid, "You don't have permissions to use this command :(")
        return
    settings_svc.set_admin_rights(cid, False, message.from_user.id, message.from_user.first_name)
    await bot.send_message(cid, 'Admin permissions disabled')


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/broadcast" and message.from_user.id in ADMINS)
async def broadcast(message):
    if len(message.text.split(" ")) < 2:
        await bot.send_message(message.chat.id, "Message is missing")
        return

    broadcast_text = " ".join(message.text.split(" ")[1:])
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        await bot.send_message(message.chat.id, "No chats found to broadcast to.")
        return

    success, failed = 0, 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, broadcast_text)
            success += 1
        except Exception as e:
            logging.warning(f"[broadcast] Failed to send to chat {chat_id}: {e}")
            failed += 1

    await bot.send_message(message.chat.id, f"Broadcast complete. Sent: {success}, Failed: {failed}")


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/timezone")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/tz")
async def config_timezone(message):
    try:
        msg = message.text
        cid = message.chat.id

        if len(msg.split(" ")) < 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")
        if len(msg.split(" ")[1].split("/")) < 2:
            raise parameterMissing("The correct format is: /timezone continent/country or continent/state")

        manager.get_chat(cid)
        response = auto_complete_timezone(" ".join(msg.split(" ")[1:]))

        if response is not None:
            settings_svc.set_timezone(cid, response, message.from_user.id, message.from_user.first_name)
            await bot.send_message(cid, f"Your timezone has been set to {response}")
        else:
            await bot.send_message(cid, f"Given timezone is invalid , check this <a href='https://gist.github.com/heyalexej/8bf688fd67d7199be4a1682b3eec7568'>website</a>", parse_mode='HTML')

    except Exception as e:
        from bot_state import _USER_FACING_EXCEPTIONS
        if not isinstance(e, _USER_FACING_EXCEPTIONS):
            logging.exception("[config_timezone] Unexpected error")
        await reply_error(message, e)


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/version")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/v")
async def version_command(message):
    try:
        with open(data_file_path('version.json'), 'r') as file:
            data = json.load(file)
    except FileNotFoundError:
        logging.error("[version_command] version.json not found")
        await bot.send_message(message.chat.id, "Version information is currently unavailable.")
        return
    except json.JSONDecodeError as e:
        logging.error(f"[version_command] Failed to parse version.json: {e}")
        await bot.send_message(message.chat.id, "Version information is currently unavailable.")
        return

    for i in range(len(data)):
        version = data[-1 - i]
        if version.get("DeployedOnProd") == 'Y':
            txt = (
                f"Version: {version['Version']}\n"
                f"Description: {version['Description']}\n"
                f"Deployed: {version['DeployedOnProd']}\n"
                f"Deployed datetime: {version['DeployedDatetime']}"
            )
            await bot.send_message(message.chat.id, txt)
            return

    logging.warning("[version_command] No deployed version found in version.json")
    await bot.send_message(message.chat.id, "No released version information found.")


@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/rollcalls")
@bot.message_handler(func=lambda message: message.text.lower().split("@")[0].split(" ")[0] == "/r")
async def show_reminders(message):
    cid = message.chat.id
    rollcalls = manager.get_rollcalls(cid)

    if len(rollcalls) == 0:
        await bot.send_message(cid, "Rollcall list is empty")
        return

    for rollcall in rollcalls:
        id = rollcalls.index(rollcall) + 1
        await bot.send_message(cid, f"Rollcall number {id}\n\n" + rollcall.allList().replace("__RCID__", str(id)))
