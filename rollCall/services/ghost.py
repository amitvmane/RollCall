"""
Ghost tracking services — toggle, set limit, clear, leaderboard.

Wraps manager + db ghost ops, returns plain dicts, no Telegram formatting.
"""

from typing import Optional

from exceptions import incorrectParameter
from rollcall_manager import manager
from db import (
    get_ghost_leaderboard,
    log_admin_action,
    reset_ghost_count,
    update_chat_settings,
)


def get_ghost_settings(chat_id: int) -> dict:
    """Return current ghost tracking settings for a chat."""
    return {
        "ghost_tracking_enabled": manager.get_ghost_tracking_enabled(chat_id),
        "absent_limit": manager.get_absent_limit(chat_id),
    }


def toggle_ghost_tracking(
    chat_id: int,
    enabled: bool,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Enable or disable ghost tracking for a chat."""
    manager.set_ghost_tracking_enabled(chat_id, enabled)
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "toggle_ghost_tracking",
                     details="on" if enabled else "off")
    return get_ghost_settings(chat_id)


def set_absent_limit(
    chat_id: int,
    limit: int,
    admin_user_id: int,
    admin_name: str,
) -> dict:
    """Set the ghost absence threshold for a chat (must be >= 1)."""
    if limit < 1:
        raise incorrectParameter("Absent limit must be at least 1.")
    manager.set_absent_limit(chat_id, limit)
    log_admin_action(chat_id, admin_user_id, admin_name,
                     "set_absent_limit", details=str(limit))
    return get_ghost_settings(chat_id)


def clear_absent(
    chat_id: int,
    admin_user_id: int,
    admin_name: str,
    target_user_id: Optional[int] = None,
    proxy_name: Optional[str] = None,
) -> dict:
    """
    Clear ghost count for one user/proxy or ALL users in the chat.

    - Pass target_user_id (int) to clear one real user.
    - Pass proxy_name (str) to clear one proxy.
    - Pass neither to clear everyone (full reset).

    Returns {"cleared": True}.
    """
    if target_user_id is not None:
        reset_ghost_count(chat_id, target_user_id)
    elif proxy_name is not None:
        reset_ghost_count(chat_id, -1, proxy_name=proxy_name)
    else:
        # Clear all — iterate over the leaderboard rows
        leaderboard = get_ghost_leaderboard(chat_id)
        for row in leaderboard:
            uid = row.get("user_id")
            pname = row.get("proxy_name")
            if uid:
                reset_ghost_count(chat_id, uid)
            elif pname:
                reset_ghost_count(chat_id, -1, proxy_name=pname)

    log_admin_action(chat_id, admin_user_id, admin_name,
                     "clear_absent",
                     details=(
                         f"user:{target_user_id}" if target_user_id else
                         f"proxy:{proxy_name}" if proxy_name else "all"
                     ))
    return {"cleared": True}


def ghost_leaderboard(chat_id: int) -> list:
    """Return the ghost (no-show) leaderboard for a chat."""
    return [
        {
            "name": row.get("user_name") or row.get("proxy_name"),
            "user_id": row.get("user_id"),
            "is_proxy": row.get("user_id") is None,
            "ghost_count": row.get("ghost_count", 0),
        }
        for row in get_ghost_leaderboard(chat_id)
    ]
