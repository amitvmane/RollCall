"""
Shared "is this Telegram user currently an admin of this chat" check.

Extracted from the group web page's admin-status endpoint (routes/web.py)
so the admin console's Telegram-based sign-in (routes/auth.py) can reuse
the exact same live-check-with-cache-fallback reasoning instead of a
second, potentially divergent copy of security-sensitive logic.
"""

import logging

import db as _db


async def check_web_admin_live(chat_id: int, tg_user_id: int) -> bool:
    """Return whether tg_user_id is currently a web admin of chat_id.

    Only live-checks Telegram when the group has locked itself down with
    /set_admins (get_admin_rights) — same gate functions.admin_rights()
    uses for every Telegram-side admin command. In a default-open group
    (the default), Telegram admin/creator status was never the criterion
    for granting web-admin in the first place (/weblink hands it to
    anyone), so treating "not a Telegram admin" as "revoke" here would
    silently undo that grant for the common case.

    When the group *is* locked down, live-checks against Telegram on every
    call instead of trusting a snapshot forever — promotes a real Telegram
    admin automatically and revokes anyone who's lost their admin role
    since the cache was last set. Falls back to the cached flag if
    Telegram itself is unreachable, so an outage doesn't lock an admin out
    of the one surface meant to keep working when Telegram is down.
    """
    from rollcall_manager import manager as _mgr
    if not _mgr.get_admin_rights(chat_id):
        return _db.is_web_admin(chat_id, tg_user_id)

    try:
        from bot_state import bot
        member = await bot.get_chat_member(chat_id, tg_user_id)
        is_admin_now = member.status in ("administrator", "creator")
    except Exception:
        logging.warning(
            "[check_web_admin_live] live check failed chat=%s user=%s — using cached value",
            chat_id, tg_user_id, exc_info=True,
        )
        return _db.is_web_admin(chat_id, tg_user_id)

    # Bookkeeping is intentionally outside the try above — a bug here
    # shouldn't be silently reinterpreted as "Telegram is down" and masked
    # by the cache fallback.
    if is_admin_now:
        name = getattr(getattr(member, "user", None), "first_name", None) or f"user{tg_user_id}"
        _db.set_web_admin(chat_id, tg_user_id, name)
    else:
        _db.revoke_web_admin(chat_id, tg_user_id)
    return is_admin_now
