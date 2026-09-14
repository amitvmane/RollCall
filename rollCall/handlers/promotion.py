"""
One announcer for every waitlist→IN promotion.

Promotions used to be announced inline by whichever adapter happened to cause
one — `handlers/voting.py` and `handlers/proxy.py` each carried their own
`_send_promoted*` helper, `handlers/lifecycle.py` open-coded a third, and the
web/REST routes announced nothing at all. Five copies of one event meant five
chances to drift, and they had: the proxy copy omitted the rollcall title and
number, so in a chat with two open rollcalls nobody could tell which one they
had just been promoted into.

Everything that frees an IN slot now calls `announce_promotions` with the
`promoted` list its service returned. Best-effort throughout: a Telegram
failure must never fail the mutation that caused it.
"""
import asyncio
import logging

from bot_state import (
    bot, _dm_promoted_real_user, _esc_md, _log_task_exc,
    format_mention_with_name_md,
)
from rollcall_manager import manager


async def announce_promotions(
    cid: int,
    promoted: list,
    rc_title: str,
    rc_number_1based: int,
    rc=None,
) -> None:
    """Announce each waitlist→IN promotion in `promoted` (serialized users).

    Group message (unless shh), a DM to the promoted member, and — when `rc`
    is supplied — a heads-up to a proxy's owner. `rc` is optional because a
    concurrent /erc can remove the rollcall between the vote and the
    announcement; the promotion still happened and still gets announced, only
    the proxy-owner lookup is skipped.
    """
    if not promoted:
        return

    from models import User

    shh = manager.get_shh_mode(cid)

    for p in promoted:
        try:
            p_id = p["user_id"]
            p_obj = User(p["name"], p.get("username"), p_id, [])
            if not shh:
                if isinstance(p_id, int):
                    await bot.send_message(
                        cid,
                        f"{format_mention_with_name_md(p_obj)} → IN (from WAITING) "
                        f"for '{_esc_md(rc_title)}' (#{rc_number_1based})",
                        parse_mode="Markdown",
                    )
                else:
                    await bot.send_message(
                        cid,
                        f"{p['name']} → IN (from WAITING) for '{rc_title}' (#{rc_number_1based})",
                    )
            if isinstance(p_id, int):
                _t = asyncio.create_task(_dm_promoted_real_user(p_id, rc_title, rc_number_1based))
                _t.add_done_callback(_log_task_exc)
            if rc is not None:
                from handlers.lifecycle import notify_proxy_owner_wait_to_in
                await notify_proxy_owner_wait_to_in(rc, p_obj, cid, rc_title, rc_number_1based)
        except Exception:
            logging.warning("announce_promotions failed for %r in chat %s", p, cid, exc_info=True)


async def announce_one(cid: int, promoted: dict, rc_title: str, rc_number_1based: int, rc=None) -> None:
    """Single-promotion convenience — the vote paths can only ever free one slot."""
    await announce_promotions(cid, [promoted] if promoted else [], rc_title, rc_number_1based, rc)
