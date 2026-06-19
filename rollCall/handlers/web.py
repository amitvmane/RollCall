"""
Web voting handler — /weblink command.

Shares the magic-link URL for active rollcall(s) so users can vote outside Telegram.
Requires WEB_BASE_URL env var; silently skips if not configured.
"""
import logging
import os

from bot_state import bot, reply_error, _esc_md
from functions import roll_call_not_started
from rollcall_manager import manager


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

        rollcalls = manager.get_rollcalls(cid)
        if not rollcalls:
            await bot.send_message(cid, "No active rollcalls. Start one with /src first.")
            return

        lines = []
        for i, rc in enumerate(rollcalls, start=1):
            token = getattr(rc, "web_token", None)
            if not token:
                lines.append(f"#{i} *{_esc_md(rc.title)}* — web link unavailable (restart the rollcall)")
            else:
                url = f"{base}/web/join/{token}"
                lines.append(f"#{i} *{_esc_md(rc.title)}*\n{url}")

        text = "🔗 *Web voting links:*\n\n" + "\n\n".join(lines)
        text += "\n\n_Share this link with anyone — no Telegram needed._"
        await bot.send_message(cid, text, parse_mode="Markdown")
    except Exception as e:
        await reply_error(cid, e)
