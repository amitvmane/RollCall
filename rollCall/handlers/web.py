"""
Web voting handler — /weblink command.

Shares the permanent group URL (bookmark once, always works) and per-rollcall
deep links for active rollcalls. Requires WEB_BASE_URL env var.
"""
import logging
import os

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import db as _db
from bot_state import bot, reply_error, _esc_md
from rollcall_manager import manager
from services.web import get_group_web_token


def _web_base_url() -> str:
    return os.environ.get("WEB_BASE_URL", "").rstrip("/")


@bot.message_handler(func=lambda m: m.text.split()[0].split("@")[0].lower() == "/weblink")
async def weblink_cmd(message):
    cid = message.chat.id
    try:
        base = _web_base_url()
        if not base:
            await bot.send_message(
                cid,
                "Web voting is not configured. Set WEB_BASE_URL on the server to enable it."
            )
            return

        group_token = get_group_web_token(cid)
        group_url = f"{base}/web/group/{group_token}"

        # Cache the caller as a web admin so they can start rollcalls from the web
        # when Telegram is unavailable. Only stored when the caller is verifiably
        # a Telegram user (message.from_user is set).
        if message.from_user:
            user = message.from_user
            tg_name = user.first_name or (f"@{user.username}" if user.username else str(user.id))
            _db.set_web_admin(cid, user.id, tg_name)

        rollcalls = manager.get_rollcalls(cid)

        lines = [
            "🔗 *Web voting links*",
            "",
            "📌 *Bookmark this link* — works even when Telegram is down:",
            group_url,
        ]

        if rollcalls:
            lines += ["", "Direct links for active rollcalls:"]
            for i, rc in enumerate(rollcalls, start=1):
                token = getattr(rc, "web_token", None)
                if token:
                    lines.append(f"#{i} *{_esc_md(rc.title)}*: {base}/web/join/{token}")
                else:
                    lines.append(f"#{i} *{_esc_md(rc.title)}* — direct link unavailable")

        lines += ["", "_Share the bookmark link with anyone — no Telegram needed._"]

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(InlineKeyboardButton("🌐 Open Voting Page", url=group_url))

        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown", reply_markup=markup)
    except Exception as e:
        await reply_error(cid, e)
