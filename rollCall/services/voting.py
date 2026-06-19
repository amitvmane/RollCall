"""
Voting services — vote_in, vote_out, vote_maybe.

Framework-agnostic. Takes primitive args, returns a structured dict
describing what happened so adapters can render or respond accordingly.

The ghost-reconfirmation prompt (the "you've ghosted N times; commit?"
flow) is INTENTIONALLY split out into a separate `check_ghost_reconfirmation_needed`
helper. Adapters call that first; if it returns `needed=True` they render
their own confirmation UX (Telegram inline buttons, REST 409 + token, etc.)
without the service ever having an opinion about UI.

`vote_in/out/maybe` all acquire `manager.get_chat_write_lock(chat_id)`
internally — same serialization the existing handlers use to prevent
vote-vs-/erc and vote-vs-vote races.
"""
from __future__ import annotations

import logging
from datetime import datetime

from exceptions import (
    alreadyInList,
    rollCallNotStarted,
)
from models import User
from rollcall_manager import manager
from db import (
    get_ghost_count,
    increment_rollcall_stat,
    increment_user_stat,
    upsert_chat_member,
)

from .common import resolve_rollcall_or_raise, serialize_rollcall, serialize_user


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rc_db_id(rc) -> int | None:
    """Mirror of bot_state.get_rc_db_id without importing bot_state (which
    would pull in telebot). Checks both .id and the legacy .db_id alias."""
    return getattr(rc, "id", None) or getattr(rc, "db_id", None)


def check_ghost_reconfirmation_needed(
    chat_id: int,
    user_id: int,
    rc_number: int = 0,
) -> dict:
    """
    Check whether a real user trying to vote IN should be prompted to
    reconfirm because they've ghosted past the absent limit.

    Returns:
      {
        "needed": bool,                # True → adapter should show a confirm UI
        "ghost_count": int,            # how many sessions they've ghosted
        "absent_limit": int,           # configured limit for this chat
        "already_in": bool,            # True → skip prompt (they're already on list)
        "rollcall_title": str,         # for the prompt copy
      }

    For proxy users (string user_ids), always returns `needed=False` —
    proxies can't ghost-reconfirm themselves.

    For chats with ghost tracking disabled, always returns `needed=False`.
    """
    if not isinstance(user_id, int):
        return {"needed": False, "ghost_count": 0, "absent_limit": 0,
                "already_in": False, "rollcall_title": ""}

    if not manager.get_ghost_tracking_enabled(chat_id):
        return {"needed": False, "ghost_count": 0, "absent_limit": 0,
                "already_in": False, "rollcall_title": ""}

    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    ghost_count = get_ghost_count(chat_id, user_id)
    absent_limit = manager.get_absent_limit(chat_id)
    already_in = any(u.user_id == user_id for u in rc.inList)
    needed = (ghost_count >= absent_limit) and not already_in
    return {
        "needed": needed,
        "ghost_count": ghost_count,
        "absent_limit": absent_limit,
        "already_in": already_in,
        "rollcall_title": rc.title,
    }


async def vote_in(
    chat_id: int,
    user_id: int | str,
    first_name: str,
    username: str | None = None,
    comment: str | None = None,
    rc_number: int = 0,
) -> dict:
    """
    Cast an IN vote.

    Args:
      chat_id    — chat owning the rollcall
      user_id    — int for real Telegram users, str for proxies (but
                    proxy voting normally goes through services.proxy in a
                    future PR; this accepts both for symmetry today)
      first_name — display name
      username   — optional Telegram @handle
      comment    — optional vote comment
      rc_number  — 0-based index of the rollcall (default first)

    Returns:
      {
        "action": "added" | "waitlisted",
        "rollcall": {...serialized rollcall after the vote...},
        "user": {...serialized user...},
        "rc_number_1based": int,
      }

    Raises:
      rollCallNotStarted   — no active rollcall
      incorrectParameter   — rc_number out of range
      alreadyInList        — user is already on the IN list
    """
    async with manager.get_chat_write_lock(chat_id):
        rc = resolve_rollcall_or_raise(chat_id, rc_number)
        user = User(first_name, username, user_id, rc.allNames)
        user.comment = comment or ""

        if isinstance(user.user_id, int):
            upsert_chat_member(chat_id, user.user_id, first_name, username)

        result = rc.addIn(user)
        rc.save()

        rc_db_id = _rc_db_id(rc)
        if result not in ("AB", "AC", "AU") and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(chat_id, user.user_id, "total_in")
            increment_rollcall_stat(rc_db_id, "total_in")

        if result == "AB":
            raise alreadyInList(f"{user.name}, you're already IN for '{rc.title}'.")

        action = "waitlisted" if result == "AC" else "added"
        return {
            "action": action,
            "rollcall": serialize_rollcall(rc, rc_number),
            "user": serialize_user(user),
            "rc_number_1based": rc_number + 1,
        }


async def vote_out(
    chat_id: int,
    user_id: int | str,
    first_name: str,
    username: str | None = None,
    comment: str | None = None,
    rc_number: int = 0,
) -> dict:
    """
    Cast an OUT vote. If the user was previously IN and a waitlist exists,
    the first waitlister gets promoted; the promoted user is included in
    the return so the adapter can announce / DM them.

    Returns:
      {
        "action": "added" | "moved",
        "was_in": bool,
        "promoted": {...serialized promoted user...} | None,
        "rollcall": {...serialized rollcall after the vote...},
        "user": {...serialized voter...},
        "rc_number_1based": int,
      }

    Raises:
      rollCallNotStarted, incorrectParameter, alreadyInList
    """
    async with manager.get_chat_write_lock(chat_id):
        rc = resolve_rollcall_or_raise(chat_id, rc_number)
        user = User(first_name, username, user_id, rc.allNames)
        user.comment = comment or ""

        if isinstance(user.user_id, int):
            upsert_chat_member(chat_id, user.user_id, first_name, username)

        was_in = any(u.user_id == user.user_id for u in rc.inList)
        result = rc.addOut(user)
        rc.save()

        rc_db_id = _rc_db_id(rc)
        if result not in ("AB", "AU") and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(chat_id, user.user_id, "total_out")
            increment_rollcall_stat(rc_db_id, "total_out")

        if result == "AB":
            raise alreadyInList(f"{user.name}, you're already OUT for '{rc.title}'.")

        promoted = None
        action = "added"
        if isinstance(result, User):
            promoted = serialize_user(result)
            action = "moved"
            if rc_db_id is not None and isinstance(result.user_id, int):
                increment_user_stat(chat_id, result.user_id, "total_waiting_to_in")
                increment_user_stat(chat_id, result.user_id, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")
        elif was_in:
            # The voter was on IN list, now moved to OUT — still "added" semantics
            # for the caller, but flagging via was_in tells adapter to phrase it
            # as a move rather than a fresh OUT.
            action = "moved"

        return {
            "action": action,
            "was_in": was_in,
            "promoted": promoted,
            "rollcall": serialize_rollcall(rc, rc_number),
            "user": serialize_user(user),
            "rc_number_1based": rc_number + 1,
        }


async def vote_maybe(
    chat_id: int,
    user_id: int | str,
    first_name: str,
    username: str | None = None,
    comment: str | None = None,
    rc_number: int = 0,
) -> dict:
    """
    Cast a MAYBE vote. Same promotion semantics as vote_out — if the voter
    was IN and a waitlister exists, that waitlister gets promoted.

    Returns:
      {
        "action": "added" | "moved",
        "was_in": bool,
        "promoted": {...serialized promoted user...} | None,
        "rollcall": {...serialized rollcall...},
        "user": {...serialized voter...},
        "rc_number_1based": int,
      }

    Raises:
      rollCallNotStarted, incorrectParameter, alreadyInList
    """
    async with manager.get_chat_write_lock(chat_id):
        rc = resolve_rollcall_or_raise(chat_id, rc_number)
        user = User(first_name, username, user_id, rc.allNames)
        user.comment = comment or ""

        if isinstance(user.user_id, int):
            upsert_chat_member(chat_id, user.user_id, first_name, username)

        was_in = any(u.user_id == user.user_id for u in rc.inList)
        result = rc.addMaybe(user)
        rc.save()

        rc_db_id = _rc_db_id(rc)
        if result not in ("AB", "AU") and rc_db_id is not None and isinstance(user.user_id, int):
            increment_user_stat(chat_id, user.user_id, "total_maybe")
            increment_rollcall_stat(rc_db_id, "total_maybe")

        if result == "AB":
            raise alreadyInList(f"{user.name}, you're already MAYBE for '{rc.title}'.")

        promoted = None
        action = "added"
        if isinstance(result, User):
            promoted = serialize_user(result)
            action = "moved"
            if rc_db_id is not None and isinstance(result.user_id, int):
                increment_user_stat(chat_id, result.user_id, "total_waiting_to_in")
                increment_user_stat(chat_id, result.user_id, "total_in")
                increment_rollcall_stat(rc_db_id, "total_in")
        elif was_in:
            action = "moved"

        return {
            "action": action,
            "was_in": was_in,
            "promoted": promoted,
            "rollcall": serialize_rollcall(rc, rc_number),
            "user": serialize_user(user),
            "rc_number_1based": rc_number + 1,
        }
