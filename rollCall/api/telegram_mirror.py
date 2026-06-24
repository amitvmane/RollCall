"""
Best-effort Telegram panel mirror for web/REST actions.

Any route that mutates rollcall state should call mirror_panel_to_telegram()
after the mutation so the group chat stays in sync even when the action was
performed through the web UI or REST API rather than directly via the bot.

The call is fully swallowed — a Telegram outage or rate-limit must never
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
        await show_panel_for_rollcall(chat_id, rc_number_1based, force_new=force_new)
    except Exception:
        logging.warning(
            "[mirror] telegram panel update failed chat=%s rc=%s",
            chat_id, rc_number_1based, exc_info=True,
        )
