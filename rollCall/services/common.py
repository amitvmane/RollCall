"""
Common helpers for the service layer.

Pure functions that translate between the manager's in-memory objects
(RollCall, User) and the dicts that services return. Adapters consume
these dicts.
"""

from typing import Optional

from models import RollCall, User


MAX_ROLLCALLS_PER_CHAT = 3


def serialize_user(u: User) -> dict:
    """Convert a User into a plain dict adapters can format or JSON-encode."""
    return {
        "user_id": u.user_id,
        "name": u.name,
        "username": u.username,
        "comment": getattr(u, "comment", "") or "",
        "is_proxy": not isinstance(u.user_id, int),
    }


def serialize_rollcall(rc: RollCall, rc_index: int) -> dict:
    """
    Convert a RollCall into a JSON-friendly dict.

    rc_index is the 0-based position; the returned dict exposes a 1-based
    `number` (matching how rollcalls are addressed in commands like
    `/in ::2`) plus a `rc_index` for internal use.
    """
    return {
        "id": getattr(rc, "id", None),
        "number": rc_index + 1,
        "rc_index": rc_index,
        "title": rc.title,
        "in_list": [serialize_user(u) for u in rc.inList],
        "out_list": [serialize_user(u) for u in rc.outList],
        "maybe_list": [serialize_user(u) for u in rc.maybeList],
        "wait_list": [serialize_user(u) for u in rc.waitList],
        "in_count": len(rc.inList),
        "out_count": len(rc.outList),
        "maybe_count": len(rc.maybeList),
        "wait_count": len(rc.waitList),
        "limit": getattr(rc, "inListLimit", None),
        "location": getattr(rc, "location", None),
        "event_fee": getattr(rc, "event_fee", None),
        "individual_fee": getattr(rc, "individual_fee", None),
        "timezone": getattr(rc, "timezone", None),
        "finalize_date": rc.finalizeDate.isoformat() if getattr(rc, "finalizeDate", None) else None,
        "reminder_hours": getattr(rc, "reminder", None),
    }


def parse_rc_number_suffix(text: str) -> tuple[int, str]:
    """
    Extract a trailing `::N` rollcall index from a command's text.

    Returns (rc_index_0_based, text_without_suffix).
    If no suffix is found, returns (0, text).
    Raises ValueError if the suffix is present but malformed.

    Adapters (bot handlers) use this to peel `/in foo ::2` into
    rc_index=1 + "foo".
    """
    if not text:
        return 0, text
    parts = text.strip().split()
    if not parts:
        return 0, text
    last = parts[-1]
    if not last.startswith("::"):
        return 0, text
    try:
        n = int(last[2:])
    except ValueError as e:
        raise ValueError(f"Invalid rollcall suffix: {last!r}") from e
    if n <= 0:
        raise ValueError(f"Rollcall number must be positive: {last!r}")
    return n - 1, " ".join(parts[:-1])


def ensure_rc_number(chat_id: int, rc_number: int, mgr=None) -> None:
    """Raise if `rc_number` (0-based) isn't an open rollcall in this chat.

    The handler-layer twin of `resolve_rollcall_or_raise`, for commands that
    validate an explicit `::N` suffix before doing any work. It was 17 copies
    across four handler modules, and they had already split into two variants
    — five of them omitted the lower-bound check.

    Handlers pass their own `manager` rather than letting this resolve one:
    the module-level alias is what the handler test suite patches, so taking
    it as an argument keeps the check honest about which state it read.
    """
    from exceptions import incorrectParameter

    if mgr is None:
        from rollcall_manager import manager as mgr

    rollcalls = mgr.get_rollcalls(chat_id)
    if rc_number < 0 or len(rollcalls) < rc_number + 1:
        raise incorrectParameter(
            "The rollcall number doesn't exist, check /rollcalls to see all rollcalls"
        )


def resolve_rollcall_or_raise(chat_id: int, rc_number: int):
    """
    Fetch the rollcall at rc_number (0-based) from the manager, raising the
    same curated exceptions handlers already raise so error messages are
    consistent across adapters.
    """
    from exceptions import rollCallNotStarted, incorrectParameter
    from rollcall_manager import manager

    rollcalls = manager.get_rollcalls(chat_id)
    if len(rollcalls) == 0:
        raise rollCallNotStarted("Roll call is not active")
    if rc_number < 0 or rc_number >= len(rollcalls):
        raise incorrectParameter(
            "The rollcall number doesn't exist, check /rollcalls to see all rollcalls"
        )
    rc = manager.get_rollcall(chat_id, rc_number)
    if rc is None:
        # Defensive — manager.get_rollcalls() said it exists, but a race
        # with /erc could have removed it. Treat as not-active for the user.
        raise rollCallNotStarted("Roll call is not active")
    return rc


def record_promotion_stats(chat_id: int, rc_db_id, user_id) -> None:
    """Count one waitlist→IN promotion.

    A promotion is a real IN, so it bumps `total_in` alongside the
    `total_waiting_to_in` counter that makes it visible in /stats. Proxies
    have string ids and no stats row, so they are skipped.

    Every path that promotes calls this — `addOut`/`addMaybe` via the voting
    and proxy services, `set_wait_limit`, and `fill_waitlist_slots`. It used
    to be six hand-written copies of the same three lines.
    """
    from db import increment_rollcall_stat, increment_user_stat

    if chat_id is None or rc_db_id is None or not isinstance(user_id, int):
        return
    increment_user_stat(chat_id, user_id, "total_waiting_to_in")
    increment_user_stat(chat_id, user_id, "total_in")
    increment_rollcall_stat(rc_db_id, "total_in")


def fill_waitlist_slots(rc) -> list:
    """
    Promote waitlisters into any free IN slots and return them serialized.

    The promotion rule lives in three places that each free a slot:
    `addOut`/`addMaybe` (a member leaves), `set_limit` (the cap moves), and
    here (a member is removed outright). Deletion is the one path that does
    NOT go through a vote, so without this the IN list silently sits under
    its cap and the waitlist never drains — see
    `services.admin.delete_user_from_rollcall`.

    Caller is responsible for `rc.save()`; user rows are persisted here.
    """
    if rc.inListLimit is None:
        return []

    limit = int(rc.inListLimit)
    slots = limit - len(rc.inList)
    if slots <= 0 or not rc.waitList:
        return []

    moving = rc.waitList[:slots]
    rc.inList.extend(moving)
    rc.waitList = rc.waitList[slots:]

    rc_db_id = getattr(rc, "db_id", None) or getattr(rc, "id", None)
    chat_id = getattr(rc, "chat_id", None)
    for u in moving:
        rc._save_user_to_db(u, "in")
        record_promotion_stats(chat_id, rc_db_id, u.user_id)

    return [serialize_user(u) for u in moving]
