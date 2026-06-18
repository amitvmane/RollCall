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
