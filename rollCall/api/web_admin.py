"""
Shared "is this Telegram user currently an admin of this chat" check.

Extracted from the group web page's admin-status endpoint (routes/web.py)
so the admin console's Telegram-based sign-in (routes/auth.py) can reuse
the exact same live-check-with-cache-fallback reasoning instead of a
second, potentially divergent copy of security-sensitive logic.
"""

import logging
import time

import db as _db


# How long a "still in the group" answer from Telegram is reused. One page
# load makes several admin-gated requests; without this each one would be its
# own get_chat_member call. Short enough that someone removed from the group
# loses the web page within a minute.
_MEMBERSHIP_TTL_SECONDS = 60
_membership_cache: dict = {}


def reset_membership_cache_for_tests() -> None:
    _membership_cache.clear()


async def _has_left_chat(chat_id: int, tg_user_id: int) -> bool:
    """True only when Telegram positively says this user is no longer in the
    group (left, or removed). Unknown — Telegram unreachable, the bot itself
    removed — is False: the caller keeps whatever it already trusted, so an
    outage never locks an admin out of the surface meant to survive one.
    """
    if chat_id > 0:
        return False    # a private chat with the bot has no membership to lose
    key = (chat_id, tg_user_id)
    now = time.monotonic()
    hit = _membership_cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        from bot_state import bot
        member = await bot.get_chat_member(chat_id, tg_user_id)
    except Exception:
        logging.warning(
            "[check_web_admin_live] membership check failed chat=%s user=%s"
            " — keeping the cached grant", chat_id, tg_user_id, exc_info=True,
        )
        return False
    status = getattr(member, "status", None)
    gone = status in ("left", "kicked") or (
        status == "restricted" and getattr(member, "is_member", None) is False
    )
    if len(_membership_cache) > 4096:
        _membership_cache.clear()
    _membership_cache[key] = (now + _MEMBERSHIP_TTL_SECONDS, gone)
    return gone


async def check_web_admin_live(chat_id: int, tg_user_id: int) -> bool:
    """Return whether tg_user_id is currently a web admin of chat_id.

    Two modes, matching the gate functions.admin_rights() uses for every
    Telegram-side admin command.

    DEFAULT-OPEN GROUP (no /set_admins). A cached grant wins for as long as
    its holder is still in the group — that's how an ordinary member who ran
    /weblink keeps the access the open default deliberately hands out. "Still
    in the group" is asked of Telegram (see _has_left_chat): the open default
    means every MEMBER is an admin, and a grant used to outlive membership
    for ever, so someone removed from the group could go on ending rollcalls
    and editing dues from the web page. Without a cached
    grant, a real Telegram admin/creator is still recognised on the spot:
    requiring the group owner to run an unrelated link-sharing command
    before the web page would show them admin controls was surprising, and
    a reasonable person reads "I own this group" as sufficient. This grants
    no authority that wasn't already reachable — anyone passing
    admin_rights() could self-grant by running /weblink, and in an open
    group that is everyone. Crucially it never revokes a grant for not being
    a Telegram *admin*: a member's cached grant is left alone, since admin
    status was never the criterion for issuing it — membership was.

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
            if await _has_left_chat(chat_id, tg_user_id):
                # Through the service, for the same last-owner rule as the
                # locked-down branch below. A refused revoke leaves a stale
                # row behind, but the answer here is still no.
                from services import admin as _admin_svc
                _admin_svc.revoke_admin(chat_id, tg_user_id)
                logging.info(
                    "[web-admin] chat=%s user=%s -> DENY (open group, cached grant,"
                    " but no longer in the group)", chat_id, tg_user_id,
                )
                return False
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
        # Through the service, not straight to the database: the last-owner
        # rule lives there, and deleting the row is the same outcome as a
        # demotion by a different route. A refused revoke keeps a stale grant,
        # which is the recoverable half of that trade.
        from services import admin as _admin_svc
        _admin_svc.revoke_admin(chat_id, tg_user_id)
    return is_admin_now


def has_chat_standing(chat_id: int, tg_user_id: int) -> bool:
    """Whether this identity has any business seeing a group's data: voting
    history there, tracked chat activity, or a web-admin grant.

    The exact set /portal/groups unions to decide what to list, so a group
    visible in the picker is always reachable — and one only, so the Mini App
    group switch and the portal history view can't drift into answering
    "may this user see that chat?" differently. A client-supplied chat_id is
    never trusted on its own; history previously took any chat id at all.
    """
    chat_id = int(chat_id)
    return (
        any(int(r["chat_id"]) == chat_id for r in _db.get_user_voted_chats(tg_user_id))
        or any(int(r["chat_id"]) == chat_id for r in _db.get_member_chats(tg_user_id))
        or bool(_db.is_web_admin(chat_id, tg_user_id))
    )
