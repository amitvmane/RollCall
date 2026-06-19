"""
Web voting handler — /weblink command.

Shares the permanent group URL (bookmark once, always works) and per-rollcall
deep links for active rollcalls. Requires WEB_BASE_URL env var.
"""
import logging
import os

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
        await bot.send_message(cid, "\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await reply_error(cid, e)
