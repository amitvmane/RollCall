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


def _ensure_web_token(rc) -> str:
    """Return the rollcall's web_token, generating and persisting one if missing."""
    import uuid
    token = getattr(rc, "web_token", None)
    if not token:
        token = uuid.uuid4().hex
        db.update_rollcall(rc.id, web_token=token)
        rc.web_token = token
    return token


def _serialize_web_rollcall(rc) -> dict:
    """Minimal dict for the web voting UI."""
    finalize_str = None
    finalize_epoch = None
    if rc.finalizeDate:
        try:
            import pytz
            tz = pytz.timezone(rc.timezone or "Asia/Kolkata")
            fd = rc.finalizeDate
            if fd.tzinfo is None:
                fd = pytz.utc.localize(fd).astimezone(tz)
            finalize_str = fd.strftime("%A, %d %b at %H:%M")
            finalize_epoch = fd.timestamp()
        except Exception:
            finalize_str = str(rc.finalizeDate)

    def _user_dict(u):
        uid = getattr(u, "user_id", None)
        is_proxy = not (isinstance(uid, int) and uid > 0)
        return {"name": u.name, "comment": u.comment or "", "is_proxy": is_proxy}

    return {
        "rollcall_id": rc.id,
        "web_token": _ensure_web_token(rc),
        "title": rc.title,
        "finalize_date": finalize_str,
        "finalize_epoch": finalize_epoch,
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


def locate_rollcall(token: str):
    """Return (chat_id, rc_number_1based) for a per-rollcall token, or None.

    Lets the Telegram-aware route layer mirror a web action back to the group
    chat without this framework-agnostic service touching the bot directly.
    """
    try:
        chat_id, idx, _ = _resolve_rc(token)
        return chat_id, idx + 1
    except Exception:
        return None


def _find_canonical_name(rc, name: str) -> str:
    """Return the existing display name from any list that matches case-insensitively, else name as-is."""
    n_lower = name.lower()
    for lst in (rc.inList, rc.outList, rc.maybeList, rc.waitList):
        for u in lst:
            uid = getattr(u, "user_id", None)
            is_proxy = not (isinstance(uid, int) and uid > 0)
            if is_proxy and u.name.lower() == n_lower:
                return u.name
    return name


async def vote_by_token(
    token: str,
    name: str,
    vote_type: str,
    tg_user_id: Optional[int] = None,
    comment: Optional[str] = None,
    username: Optional[str] = None,
) -> dict:
    """
    Submit a vote via magic link.

    name        — display name
    vote_type   — 'in' | 'out' | 'maybe'
    tg_user_id  — real Telegram user_id when voting from inside TG WebApp;
                  None means proxy (name-only) entry
    comment     — optional note attached to the vote

    Returns updated rollcall dict.
    Raises parameterMissing / incorrectParameter on bad input.
    """
    if not name or not name.strip():
        raise parameterMissing("Name is required")
    name = name.strip()[:64]
    comment = comment.strip()[:100] if comment and comment.strip() else None

    if vote_type not in ("in", "out", "maybe"):
        raise incorrectParameter("vote must be 'in', 'out', or 'maybe'")

    chat_id, rc_index, rc_pre = _resolve_rc(token)

    is_real_user = isinstance(tg_user_id, int) and tg_user_id > 0

    if is_real_user:
        # Prefer the canonical Telegram first_name and username stored in
        # chat_members over whatever the client submitted. Falls back to the
        # submitted name/username for first-time web voters with no bot history.
        submitted_name = name  # save original before potential override
        member_info = db.get_member_display_info(chat_id, tg_user_id)
        if member_info:
            name = member_info["first_name"] or name
            username = member_info.get("username") or username

        # If the user previously voted as a proxy under their display name (e.g.
        # they typed their name before verifying via Telegram), remove that stale
        # proxy entry now so the real-user vote replaces it rather than appearing
        # as a duplicate. Check both the canonical name and the originally
        # submitted name to cover the common case.
        async with manager.get_chat_write_lock(chat_id):
            rc_check = manager.get_rollcall(chat_id, rc_index)
            if rc_check is not None:
                for check_name in {name.lower(), submitted_name.lower()}:
                    for lst in (rc_check.inList, rc_check.outList, rc_check.maybeList, rc_check.waitList):
                        for u in list(lst):
                            uid = getattr(u, "user_id", None)
                            is_proxy_entry = not (isinstance(uid, int) and uid > 0)
                            if is_proxy_entry and u.name.lower() == check_name:
                                rc_check.delete_user(u.name)
                                break
    else:
        # For proxy votes, normalise to canonical casing of any existing same-name entry
        name = _find_canonical_name(rc_pre, name)

    try:
        if is_real_user:
            # Real Telegram user identified via WebApp SDK — vote as their actual user_id
            # so stats, ghost tracking, and is_proxy=false all work correctly.
            from services import voting as voting_svc
            if vote_type == "in":
                await voting_svc.vote_in(chat_id, tg_user_id, name, username=username, rc_number=rc_index, comment=comment)
            elif vote_type == "out":
                await voting_svc.vote_out(chat_id, tg_user_id, name, username=username, rc_number=rc_index, comment=comment)
            else:
                await voting_svc.vote_maybe(chat_id, tg_user_id, name, username=username, rc_number=rc_index, comment=comment)
        else:
            # Guest / external user — proxy entry identified by display name
            if vote_type == "in":
                await proxy_svc.set_in_for(chat_id, 0, "web", name, rc_number=rc_index, comment=comment)
            elif vote_type == "out":
                await proxy_svc.set_out_for(chat_id, 0, "web", name, rc_number=rc_index, comment=comment)
            else:
                await proxy_svc.set_maybe_for(chat_id, 0, "web", name, rc_number=rc_index, comment=comment)
    except Exception as e:
        # duplicateProxy / repeatlyName — proxy double-tap, treat as idempotent.
        # alreadyInList is NOT caught here: a verified user who is already IN/OUT
        # should see the same "already IN" message as they would from the bot.
        from exceptions import duplicateProxy, repeatlyName
        if not isinstance(e, (duplicateProxy, repeatlyName)):
            raise

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
    Return all active rollcalls and upcoming scheduled templates for the group.

    Returns {"group_token": ..., "rollcalls": [...], "upcoming": [...]}.
    Raises incorrectParameter if the token is unknown.
    """
    if not group_token:
        raise parameterMissing("Group token is required")
    chat = db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise incorrectParameter("This group link is invalid.")
    chat_id = chat["chat_id"]
    rollcalls = manager.get_rollcalls(chat_id)

    templates = db.get_templates(chat_id)
    upcoming = [
        {
            "name": t["name"],
            "title": t.get("title"),
            "schedule_day": t.get("schedule_day"),
            "schedule_time": t.get("schedule_time"),
            "recurrence_type": t.get("recurrence_type") or "weekly",
            "event_day": t.get("event_day"),
            "event_time": t.get("event_time"),
            "location": t.get("location"),
            "fee": t.get("eventfee"),
            "limit": t.get("inlistlimit"),
        }
        for t in templates
        if t.get("schedule_enabled") and t.get("schedule_day") and t.get("schedule_time")
    ]

    return {
        "group_token": group_token,
        "group_name": chat.get("group_name") or "",
        "rollcalls": [_serialize_web_rollcall(rc) for rc in rollcalls],
        "upcoming": upcoming,
    }
