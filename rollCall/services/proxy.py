"""
Proxy voting services — admin votes on behalf of a non-Telegram member.

Mirrors the voting service shape: primitives in, dicts out, same curated
exceptions, internally serializes via `manager.get_chat_write_lock`.

A proxy user has a string `user_id` (the proxy's name), distinguishing
them from real Telegram users whose `user_id` is an int. All waitlist /
promotion semantics are identical to real voting.
"""

import logging
from datetime import datetime

from exceptions import (
    duplicateProxy,
    parameterMissing,
    repeatlyName,
    rollCallNotStarted,
)
from models import User
from rollcall_manager import manager
from db import (
    get_ghost_count_by_proxy_name,
    increment_rollcall_stat,
    increment_user_stat,
    log_admin_action,
)

from .common import resolve_rollcall_or_raise, serialize_rollcall, serialize_user


_MAX_PROXY_NAME_LEN = 40


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _rc_db_id(rc) -> int | None:
    return getattr(rc, "id", None) or getattr(rc, "db_id", None)


def _validate_proxy_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise parameterMissing("Proxy name is required")
    if len(name) > _MAX_PROXY_NAME_LEN:
        raise parameterMissing(
            f"Proxy name is too long (max {_MAX_PROXY_NAME_LEN} characters). "
            f"Got {len(name)}."
        )
    return name


def check_proxy_ghost_reconfirmation_needed(chat_id: int, proxy_name: str) -> dict:
    """
    Mirror of voting.check_ghost_reconfirmation_needed but for proxies.
    Returns:
      {"needed": bool, "ghost_count": int, "absent_limit": int, "proxy_name": str}
    """
    proxy_name = _validate_proxy_name(proxy_name)
    if not manager.get_ghost_tracking_enabled(chat_id):
        return {"needed": False, "ghost_count": 0, "absent_limit": 0, "proxy_name": proxy_name}
    ghost_count = get_ghost_count_by_proxy_name(chat_id, proxy_name)
    absent_limit = manager.get_absent_limit(chat_id)
    return {
        "needed": ghost_count >= absent_limit and ghost_count > 0,
        "ghost_count": ghost_count,
        "absent_limit": absent_limit,
        "proxy_name": proxy_name,
    }


async def set_in_for(
    chat_id: int,
    admin_user_id: int,
    admin_name: str,
    proxy_name: str,
    comment: str | None = None,
    rc_number: int = 0,
) -> dict:
    """
    Admin adds a proxy user to the IN list. Returns same shape as
    voting.vote_in plus an `proxy_owner` field for the admin.

    Raises:
      rollCallNotStarted, incorrectParameter, parameterMissing,
      duplicateProxy (already in/waiting), repeatlyName (collides
      with a different proxy already on the lists).
    """
    proxy_name = _validate_proxy_name(proxy_name)

    async with manager.get_chat_write_lock(chat_id):
        rc = resolve_rollcall_or_raise(chat_id, rc_number)

        # Pre-check matches the bot handler: hard-stop if this proxy is
        # already on IN or WAIT lists (a clearer message than the generic
        # duplicateProxy raise the addIn path produces).
        already_present = any(
            u.name == proxy_name and isinstance(u.user_id, str)
            for u in rc.inList + rc.waitList
        )
        if already_present:
            raise duplicateProxy(
                f"'{proxy_name}' is already IN or WAITING for '{rc.title}'."
            )

        user = User(proxy_name, None, proxy_name, rc.allNames)
        user.comment = comment or ""
        rc.set_proxy_owner(user.user_id, admin_user_id)

        result = rc.addIn(user)
        rc.save()

        log_admin_action(
            chat_id, admin_user_id, admin_name,
            "sif", target_name=proxy_name,
            rollcall_id=getattr(rc, "id", None), details=rc.title,
        )

        if result == "AB":
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        if result == "AA":
            raise repeatlyName("That name already exists!")

        action = "waitlisted" if result == "AC" else "added"
        return {
            "action": action,
            "rollcall": serialize_rollcall(rc, rc_number),
            "user": serialize_user(user),
            "proxy_owner_id": admin_user_id,
            "rc_number_1based": rc_number + 1,
        }


async def set_out_for(
    chat_id: int,
    admin_user_id: int,
    admin_name: str,
    proxy_name: str,
    comment: str | None = None,
    rc_number: int = 0,
) -> dict:
    """
    Admin moves a proxy to the OUT list (or adds them there fresh).
    If the proxy was previously IN and a waitlister exists, the waitlister
    is promoted; the promoted user is included in `promoted` so the
    adapter can announce / DM them.

    Returns the same shape as `voting.vote_out`.
    """
    proxy_name = _validate_proxy_name(proxy_name)

    async with manager.get_chat_write_lock(chat_id):
        rc = resolve_rollcall_or_raise(chat_id, rc_number)
        user = User(proxy_name, None, proxy_name, rc.allNames)
        user.comment = comment or ""

        was_in = any(
            (u.user_id == user.user_id or u.name == user.name) for u in rc.inList
        )

        result = rc.addOut(user)
        rc.save()

        log_admin_action(
            chat_id, admin_user_id, admin_name,
            "sof", target_name=proxy_name,
            rollcall_id=getattr(rc, "id", None), details=rc.title,
        )

        if result == "AB":
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        if result == "AA":
            raise repeatlyName("That name already exists!")

        promoted = None
        action = "added"
        rc_db_id = _rc_db_id(rc)
        if isinstance(result, User):
            promoted = serialize_user(result)
            action = "moved"
            if rc_db_id is not None and isinstance(result.user_id, int):
                # Mirror the same /sof glitch-fix the handler ships: bump
                # promotion stats for real users moved waitlist→IN.
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
            "proxy_owner_id": admin_user_id,
            "rc_number_1based": rc_number + 1,
        }


async def set_maybe_for(
    chat_id: int,
    admin_user_id: int,
    admin_name: str,
    proxy_name: str,
    comment: str | None = None,
    rc_number: int = 0,
) -> dict:
    """Admin moves a proxy to MAYBE. Same promotion semantics as set_out_for."""
    proxy_name = _validate_proxy_name(proxy_name)

    async with manager.get_chat_write_lock(chat_id):
        rc = resolve_rollcall_or_raise(chat_id, rc_number)
        user = User(proxy_name, None, proxy_name, rc.allNames)
        user.comment = comment or ""

        was_in = any(
            (u.user_id == user.user_id or u.name == user.name) for u in rc.inList
        )

        result = rc.addMaybe(user)
        rc.save()

        log_admin_action(
            chat_id, admin_user_id, admin_name,
            "smf", target_name=proxy_name,
            rollcall_id=getattr(rc, "id", None), details=rc.title,
        )

        if result == "AB":
            raise duplicateProxy("No duplicate proxy please :-), Thanks!")
        if result == "AA":
            raise repeatlyName("That name already exists!")

        promoted = None
        action = "added"
        rc_db_id = _rc_db_id(rc)
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
            "proxy_owner_id": admin_user_id,
            "rc_number_1based": rc_number + 1,
        }
