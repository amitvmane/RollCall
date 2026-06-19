"""
Web voting service — magic-link rollcall access for non-Telegram users.

Two access modes:
  Per-rollcall token  /web/join/{web_token}   — single rollcall, expires with rollcall
  Group token         /web/group/{group_token} — permanent, always shows active rollcalls

Framework-agnostic.
"""
import logging
from typing import Optional

import db
from exceptions import incorrectParameter, parameterMissing
from rollcall_manager import manager
from services import proxy as proxy_svc


def _serialize_web_rollcall(rc) -> dict:
    """Minimal dict for the web voting UI."""
    finalize_str = None
    if rc.finalizeDate:
        try:
            import pytz
            tz = pytz.timezone(rc.timezone or "Asia/Kolkata")
            fd = rc.finalizeDate
            if fd.tzinfo is None:
                fd = pytz.utc.localize(fd).astimezone(tz)
            finalize_str = fd.strftime("%A, %d %b at %H:%M")
        except Exception:
            finalize_str = str(rc.finalizeDate)

    def _user_dict(u):
        return {"name": u.name, "comment": u.comment or ""}

    return {
        "rollcall_id": rc.id,
        "web_token": getattr(rc, "web_token", None) or "",
        "title": rc.title,
        "finalize_date": finalize_str,
        "limit": rc.inListLimit,
        "location": rc.location,
        "in": [_user_dict(u) for u in rc.inList],
        "out": [_user_dict(u) for u in rc.outList],
        "maybe": [_user_dict(u) for u in rc.maybeList],
        "waiting": [_user_dict(u) for u in rc.waitList],
    }


def _resolve_rc(token: str):
    """Return (chat_id, rc_index, rc) for a valid active token, or raise."""
    if not token:
        raise parameterMissing("Token is required")
    row = db.get_rollcall_by_web_token(token)
    if not row:
        raise incorrectParameter("This link is invalid or has expired.")
    chat_id = row["chat_id"]
    rc_id = row["id"]
    rollcalls = manager.get_rollcalls(chat_id)
    for idx, rc in enumerate(rollcalls):
        if rc.id == rc_id:
            return chat_id, idx, rc
    raise incorrectParameter("This rollcall has ended.")


def get_rollcall_by_token(token: str) -> dict:
    """Return rollcall info for the web voting page."""
    _, _, rc = _resolve_rc(token)
    return _serialize_web_rollcall(rc)


async def vote_by_token(token: str, name: str, vote_type: str) -> dict:
    """
    Submit a vote via magic link.

    name      — display name (stored as proxy user_id)
    vote_type — 'in' | 'out' | 'maybe'

    Returns updated rollcall dict.
    Raises parameterMissing / incorrectParameter on bad input.
    """
    if not name or not name.strip():
        raise parameterMissing("Name is required")
    name = name.strip()[:64]

    if vote_type not in ("in", "out", "maybe"):
        raise incorrectParameter("vote must be 'in', 'out', or 'maybe'")

    chat_id, rc_index, _ = _resolve_rc(token)

    if vote_type == "in":
        await proxy_svc.set_in_for(chat_id, name, rc_index=rc_index)
    elif vote_type == "out":
        await proxy_svc.set_out_for(chat_id, name, rc_index=rc_index)
    else:
        await proxy_svc.set_maybe_for(chat_id, name, rc_index=rc_index)

    # Re-resolve so we return the updated state
    _, _, rc = _resolve_rc(token)
    return _serialize_web_rollcall(rc)


# ── Group token (permanent per-group URL) ────────────────────────────────────

def get_group_web_token(chat_id: int) -> str:
    """Return (and lazily generate) the permanent group web token for a chat."""
    chat = db.get_or_create_chat(chat_id)
    return chat["group_web_token"]


def get_rollcalls_by_group_token(group_token: str) -> dict:
    """
    Return all active rollcalls for the group identified by group_token.

    Returns {"group_token": ..., "rollcalls": [...]}.
    Raises incorrectParameter if the token is unknown.
    """
    if not group_token:
        raise parameterMissing("Group token is required")
    chat = db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise incorrectParameter("This group link is invalid.")
    chat_id = chat["chat_id"]
    rollcalls = manager.get_rollcalls(chat_id)
    return {
        "group_token": group_token,
        "rollcalls": [_serialize_web_rollcall(rc) for rc in rollcalls],
    }
