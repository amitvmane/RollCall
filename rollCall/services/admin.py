"""
Admin services — delete_user_from_rollcall, set_user_status.

These are platform-agnostic: they take primitive arguments and return
plain dicts. Telegram-specific flows (confirmation buttons, _pending_deletes
state) live in the handlers; web / REST clients call these services directly
without a confirmation step (the web UI provides its own confirmation dialog).
"""

import logging
from datetime import datetime

from exceptions import incorrectParameter, parameterMissing, rollCallNotStarted
from rollcall_manager import manager
from db import log_admin_action, delete_user_by_id, get_admin_audit_log, count_admin_audit_log

from .common import resolve_rollcall_or_raise, serialize_rollcall


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_audit_log(chat_id: int, page: int = 1, per_page: int = 15) -> dict:
    """
    Return a page of admin audit log entries for a chat.

    Returns:
      {"total": int, "page": int, "total_pages": int, "per_page": int, "records": list}
    """
    total = count_admin_audit_log(chat_id)
    if total == 0:
        return {"total": 0, "page": 1, "total_pages": 1, "per_page": per_page, "records": []}
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    records = get_admin_audit_log(chat_id, limit=per_page, offset=offset)
    return {
        "total": total,
        "page": page,
        "total_pages": total_pages,
        "per_page": per_page,
        "records": list(records),
    }


def delete_user_from_rollcall(
    chat_id: int,
    rc_number: int,
    name: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Remove a user (real or proxy) from any list in the specified rollcall.

    Args:
      chat_id      — chat owning the rollcall
      rc_number    — 0-based rollcall index
      name         — display name or @username of the user to remove
      admin_user_* — identity of the admin performing the action

    Returns:
      {"deleted": name, "rc_number_1based": int, "rollcall": {...serialized rollcall...}}

    Raises:
      rollCallNotStarted — no active rollcall
      incorrectParameter — rc_number out of range or user not found
      parameterMissing   — name is empty
    """
    if not name or not name.strip():
        raise parameterMissing("User name is required.")
    rc = resolve_rollcall_or_raise(chat_id, rc_number)
    if not rc.delete_user(name):
        raise incorrectParameter(f"User '{name}' not found in rollcall #{rc_number + 1}.")
    rc.save()
    logging.info(f"[{_ts()}] [CHAT {chat_id}] delete_user: '{name}' from RC #{rc_number + 1} by {admin_name}")
    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "delete_user", target_name=name,
        rollcall_id=getattr(rc, "db_id", None) or getattr(rc, "id", None),
        details=rc.title,
    )
    return {
        "deleted": name,
        "rc_number_1based": rc_number + 1,
        "rollcall": serialize_rollcall(rc, rc_number),
    }


def set_user_status(
    chat_id: int,
    rc_number: int,
    name: str,
    new_status: str,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """
    Move a user to a different status list within a rollcall.

    Finds the user by display-name or @username across all lists, removes
    them from their current list, and adds them to the target list.

    Args:
      chat_id      — chat owning the rollcall
      rc_number    — 0-based rollcall index
      name         — display name or @username to match
      new_status   — "in", "out", or "maybe"
      admin_user_* — identity of the admin performing the action

    Returns:
      {"moved": name, "from_status": str, "to_status": str, "rc_number_1based": int,
       "rollcall": {...serialized rollcall...}}

    Raises:
      rollCallNotStarted — no active rollcall
      incorrectParameter — rc_number out of range / user not found / bad status / same status
      parameterMissing   — name or new_status is empty
    """
    if not name or not name.strip():
        raise parameterMissing("User name is required.")
    if new_status not in ("in", "out", "maybe"):
        raise incorrectParameter("Status must be one of: in, out, maybe")

    rc = resolve_rollcall_or_raise(chat_id, rc_number)

    bucket_map = (
        (rc.inList, "in"),
        (rc.outList, "out"),
        (rc.maybeList, "maybe"),
        (rc.waitList, "waitlist"),
    )
    wanted = name.lstrip("@").lower()
    candidates = []
    for lst, status_name in bucket_map:
        for u in lst:
            if (u.username and u.username.lower() == wanted) or u.name.lower() == name.lower():
                candidates.append((u, status_name))

    if not candidates:
        raise incorrectParameter(f"User '{name}' not found in rollcall #{rc_number + 1}.")
    if len(candidates) > 1:
        distinct = {(u.user_id, s) for u, s in candidates}
        if len(distinct) > 1:
            hint = ", ".join(sorted({u.name for u, _ in candidates}))
            raise incorrectParameter(
                f"'{name}' matches multiple users ({hint}). Use the exact @username."
            )
    found_user, current_status = candidates[0]

    if current_status == new_status:
        raise incorrectParameter(
            f"User '{found_user.name}' is already {new_status.upper()} in rollcall #{rc_number + 1}."
        )

    # Remove from current list, then add to new list.
    rc_db_id = getattr(rc, "db_id", None) or getattr(rc, "id", None)
    if rc_db_id is not None:
        delete_user_by_id(rc_db_id, found_user.user_id)
    rc._load_users_from_db()

    if new_status == "in":
        rc.addIn(found_user)
    elif new_status == "out":
        rc.addOut(found_user)
    else:
        rc.addMaybe(found_user)
    rc.save()

    logging.info(
        f"[{_ts()}] [CHAT {chat_id}] set_status: '{found_user.name}' "
        f"{current_status} → {new_status} in RC #{rc_number + 1} by {admin_name}"
    )
    log_admin_action(
        chat_id, admin_user_id, admin_name,
        "set_status", target_name=f"{found_user.name} → {new_status}",
        rollcall_id=rc_db_id, details=rc.title,
    )
    return {
        "moved": found_user.name,
        "from_status": current_status,
        "to_status": new_status,
        "rc_number_1based": rc_number + 1,
        "rollcall": serialize_rollcall(rc, rc_number),
    }
