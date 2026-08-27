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

    Two modes, matching the gate functions.admin_rights() uses for every
    Telegram-side admin command.

    DEFAULT-OPEN GROUP (no /set_admins). A cached grant wins outright and
    costs nothing — that's how an ordinary member who ran /weblink keeps
    the access the open default deliberately hands out. Without a cached
    grant, a real Telegram admin/creator is still recognised on the spot:
    requiring the group owner to run an unrelated link-sharing command
    before the web page would show them admin controls was surprising, and
    a reasonable person reads "I own this group" as sufficient. This grants
    no authority that wasn't already reachable — anyone passing
    admin_rights() could self-grant by running /weblink, and in an open
    group that is everyone. Crucially it never *revokes*: a non-admin's
    cached grant is left alone, since Telegram admin status was never the
    criterion for issuing it.

    LOCKED-DOWN GROUP (/set_admins). Live-checks Telegram on every call
    rather than trusting a snapshot forever — promotes a real admin
    automatically and revokes anyone who has lost the role since the cache
    was set. Falls back to the cached flag when Telegram is unreachable, so
    an outage doesn't lock an admin out of the one surface meant to keep
    working when Telegram is down.
    """
    from rollcall_manager import manager as _mgr
    if not _mgr.get_admin_rights(chat_id):
        if _db.is_web_admin(chat_id, tg_user_id):
            logging.info(
                "[web-admin] chat=%s user=%s -> ALLOW (cached grant, open group)",
                chat_id, tg_user_id,
            )
            return True
        try:
            from bot_state import bot
            member = await bot.get_chat_member(chat_id, tg_user_id)
        except Exception:
            # The cache already said "no" and Telegram can't confirm
            # otherwise. Answer no — never worse than the old behaviour,
            # which never asked Telegram at all here.
            logging.warning(
                "[check_web_admin_live] open-group live check failed chat=%s user=%s"
                " — falling back to uncached 'not admin'",
                chat_id, tg_user_id, exc_info=True,
            )
            return False
        if member.status not in ("administrator", "creator"):
            logging.info(
                "[web-admin] chat=%s user=%s -> DENY (open group, no cached grant,"
                " Telegram says status=%r)",
                chat_id, tg_user_id, getattr(member, "status", None),
            )
            return False
        name = getattr(getattr(member, "user", None), "first_name", None) or f"user{tg_user_id}"
        _db.set_web_admin(chat_id, tg_user_id, name)
        logging.info(
            "[web-admin] chat=%s user=%s -> ALLOW (open group, Telegram status=%r, cached)",
            chat_id, tg_user_id, member.status,
        )
        return True

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
