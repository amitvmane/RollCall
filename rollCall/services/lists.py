"""
Lists service — get_non_responders.

Returns the set of known chat members who haven't voted on a rollcall (or any
active rollcall when rc_number is None). Platform-agnostic: it does not check
whether members have left the Telegram group — that membership filter lives in
the handler (needs bot.get_chat_member). REST/Discord callers can skip that
filter or do their own equivalent.
"""
from __future__ import annotations

import logging
from datetime import datetime

from exceptions import incorrectParameter, rollCallNotStarted
from rollcall_manager import manager
from db import get_active_members

from .common import resolve_rollcall_or_raise


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_non_responders(
    chat_id: int,
    rc_number: int | None = None,
) -> dict:
    """
    Return chat members who haven't yet voted on the specified rollcall(s).

    Args:
      chat_id   — Telegram chat id
      rc_number — 0-based rollcall index, or None to check ALL active rollcalls
                  (a member who voted on any one rollcall is considered responsive)

    Returns:
      {
        "candidates": [{"user_id": int, "first_name": str, "username": str|None}, ...],
        "rollcall_titles": [str, ...],   # titles of the targeted rollcall(s)
        "has_active_rollcall": bool,
      }

    Raises:
      incorrectParameter — rc_number is out of range
    """
    candidates = get_active_members(chat_id)
    rollcalls = manager.get_rollcalls(chat_id)
    has_active = bool(rollcalls)

    if not has_active:
        return {
            "candidates": candidates or [],
            "rollcall_titles": [],
            "has_active_rollcall": False,
        }

    if rc_number is not None:
        if rc_number < 0 or rc_number >= len(rollcalls):
            raise incorrectParameter(
                f"Rollcall #{rc_number + 1} doesn't exist. Check /rollcalls."
            )
        rc = rollcalls[rc_number]
        voted_ids = {
            u.user_id
            for u in rc.inList + rc.outList + rc.maybeList + rc.waitList
            if isinstance(u.user_id, int)
        }
        titles = [rc.title]
    else:
        # Union — anyone who voted on ANY active rollcall is excluded.
        voted_ids: set = set()
        for rc in rollcalls:
            voted_ids |= {
                u.user_id
                for u in rc.inList + rc.outList + rc.maybeList + rc.waitList
                if isinstance(u.user_id, int)
            }
        titles = [rc.title for rc in rollcalls]

    remaining = [u for u in (candidates or []) if u.get("user_id") not in voted_ids]
    return {
        "candidates": remaining,
        "rollcall_titles": titles,
        "has_active_rollcall": True,
    }
