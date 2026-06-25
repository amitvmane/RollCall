"""
Best-effort Telegram panel mirror for web/REST actions.

Any route that mutates rollcall state should call mirror_panel_to_telegram()
after the mutation so the group chat stays in sync even when the action was
performed through the web UI or REST API rather than directly via the bot.

Calls are fully swallowed — a Telegram outage or rate-limit must never
cause a web action to fail. Errors are logged at WARNING so they appear in
production logs without being fatal.
"""
import logging


async def mirror_panel_to_telegram(
    chat_id: int,
    rc_number_1based: int,
    force_new: bool = False,
) -> None:
    """Reflect a web/portal/REST rollcall action in the Telegram group chat."""
    try:
        from handlers.lifecycle import show_panel_for_rollcall
        logging.info(
            "[mirror] updating panel chat=%s rc=%s force_new=%s",
            chat_id, rc_number_1based, force_new,
        )
        await show_panel_for_rollcall(chat_id, rc_number_1based, force_new=force_new)
        logging.info("[mirror] panel updated ok chat=%s rc=%s", chat_id, rc_number_1based)
    except Exception:
        logging.warning(
            "[mirror] telegram panel update failed chat=%s rc=%s",
            chat_id, rc_number_1based, exc_info=True,
        )


async def send_vote_notification(
    chat_id: int,
    name: str,
    vote_type: str,
) -> None:
    """Send a brief vote-notification message to the group so the web vote
    is visible in chat history (just like the bot does for /in, /out, /maybe).

    vote_type must be "in", "out", or "maybe".
    """
    try:
        from bot_state import bot
        from rollcall_manager import manager

        if manager.get_shh_mode(chat_id):
            return

        label = {"in": "IN", "out": "OUT", "maybe": "MAYBE"}.get(vote_type, vote_type.upper())
        text = f"{name} → {label} (via 🌐 web)"
        logging.info("[mirror] sending vote notification chat=%s name=%s vote=%s", chat_id, name, vote_type)
        await bot.send_message(chat_id, text)
        logging.info("[mirror] vote notification sent ok chat=%s", chat_id)
    except Exception:
        logging.warning(
            "[mirror] vote notification failed chat=%s name=%s",
            chat_id, name, exc_info=True,
        )
