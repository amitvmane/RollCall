"""
Web voting service — magic-link rollcall access for non-Telegram users.

Two access modes:
  Per-rollcall token  /web/join/{web_token}   — single rollcall, expires with rollcall
  Group token         /web/group/{group_token} — permanent, always shows active rollcalls

Framework-agnostic.
"""
import hashlib
import logging
from typing import Optional

import db
from exceptions import incorrectParameter, parameterMissing
from rollcall_manager import manager
from services import proxy as proxy_svc
from services import templates as _tmpl_svc
from services.templates import list_templates as _list_templates


def hash_login_token(token: str) -> str:
    """SHA-256 hex of a /mytoken login code — the only form ever stored.
    Shared by the bot handler that issues codes and the API route that
    redeems them."""
    return hashlib.sha256(token.encode()).hexdigest()


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
        return {"name": u.name, "comment": u.comment or "", "is_proxy": is_proxy,
                "user_id": None if is_proxy else uid}

    return {
        "rollcall_id": rc.id,
        "web_token": _ensure_web_token(rc),
        "title": rc.title,
        "finalize_date": finalize_str,
        "finalize_epoch": finalize_epoch,
        "limit": rc.inListLimit,
        "location": rc.location,
        "fee": rc.event_fee,
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


def _drop_own_web_guest_entry(rc, chat_id: int, tg_user_id: int, canonical_name: str):
    """Remove the guest entry a member left before verifying, if there is one.

    Someone who first votes on the web page as a guest ("Priya") and later
    verifies with Telegram would otherwise appear twice. Only an entry that is
    unmistakably that leftover is removed:

      * it is a proxy whose name equals the member's Telegram name — never the
        raw name typed into the form, which is attacker-controlled once an
        identity is verified and would let anyone delete any guest; and
      * it was self-entered on the web (proxy owner 0). A guest an admin added
        with /sif or the web proxy form belongs to that admin, is very likely
        a DIFFERENT person who shares a first name, and is left alone.

    Removed by exact id, so the guest's ghost history is untouched (the
    name-based delete wipes ghost_records chat-wide — a vote must never do
    that). Audited. Returns {"name", "status"} or None. Caller holds the lock.
    """
    wanted = (canonical_name or "").strip().lower()
    if not wanted:
        return None
    for lst, status in ((rc.inList, "in"), (rc.outList, "out"),
                        (rc.maybeList, "maybe"), (rc.waitList, "waitlist")):
        for u in list(lst):
            if not isinstance(u.user_id, str) or u.name.lower() != wanted:
                continue
            if rc.get_proxy_owner(u.user_id) != 0:
                continue
            if not db.delete_user_by_id(rc.id, u.user_id):
                return None
            rc._load_users_from_db()
            try:
                db.log_admin_action(
                    chat_id, tg_user_id, canonical_name, "web_guest_replaced",
                    target_name=u.name, rollcall_id=rc.id, details=rc.title,
                )
            except Exception:
                logging.warning("web_guest_replaced audit failed", exc_info=True)
            return {"name": u.name, "status": status}
    return None


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

    chat_id, _, rc_pre = _resolve_rc(token)
    rc_id = rc_pre.id

    is_real_user = isinstance(tg_user_id, int) and tg_user_id > 0

    if is_real_user:
        # Prefer the canonical Telegram first_name and username stored in
        # chat_members over whatever the client submitted. Falls back to the
        # submitted name/username for first-time web voters with no bot history.
        member_info = db.get_member_display_info(chat_id, tg_user_id)
        if member_info:
            name = member_info["first_name"] or name
            username = member_info.get("username") or username
    else:
        # For proxy votes, normalise to canonical casing of any existing same-name entry
        name = _find_canonical_name(rc_pre, name)

    from exceptions import alreadyInList, duplicateProxy, repeatlyName
    from services import voting as voting_svc
    from services.common import fill_waitlist_slots, resolve_rollcall_by_id_or_raise

    vote_result = None
    replaced = None
    extra_promoted = []
    # One lock hold for the whole vote, and the rollcall found by its db id
    # inside it. This used to resolve the token to a POSITION up front and
    # vote by that position later — a vote racing /erc or the scheduler's
    # auto-close landed in whichever rollcall slid into the old number.
    async with manager.get_chat_write_lock(chat_id):
        rc_index, rc = resolve_rollcall_by_id_or_raise(chat_id, rc_id, manager)
        if is_real_user:
            replaced = _drop_own_web_guest_entry(rc, chat_id, tg_user_id, name)
        try:
            if is_real_user:
                # Real Telegram user identified via a signed token — vote as
                # their actual user_id so stats, ghost tracking, and
                # is_proxy=false all work correctly.
                fn = {"in": voting_svc.vote_in_locked, "out": voting_svc.vote_out_locked,
                      "maybe": voting_svc.vote_maybe_locked}[vote_type]
                vote_result = fn(chat_id, tg_user_id, name, username, comment, rc_index, rc_id)
            else:
                # Guest / external user — proxy entry identified by display name.
                # Owner 0 marks it as self-entered on the web (see
                # _drop_own_web_guest_entry).
                fn = {"in": proxy_svc.set_in_for_locked, "out": proxy_svc.set_out_for_locked,
                      "maybe": proxy_svc.set_maybe_for_locked}[vote_type]
                vote_result = fn(chat_id, 0, "web", name, comment, rc_index, rc_id)
        except (duplicateProxy, repeatlyName):
            # Proxy double-tap — treat as idempotent.
            pass
        except alreadyInList:
            # A verified user who is already IN/OUT sees the same "already"
            # message as from the bot — unless this vote just replaced their
            # own guest entry, which is a real change the caller must report.
            if not replaced:
                raise
        finally:
            if replaced:
                # The removed guest may have held an IN slot.
                extra_promoted = fill_waitlist_slots(rc)
                rc.save()

    # Re-resolve so we return the updated state
    _, _, rc = _resolve_rc(token)
    out = _serialize_web_rollcall(rc)
    # Carried out-of-band (the route pops it) so the caller can announce the
    # waitlist→IN promotion this vote caused. Without it a web /out frees a
    # slot and the promoted member is never told.
    promoted = vote_result.get("promoted") if isinstance(vote_result, dict) else None
    out["promoted"] = ([promoted] if promoted else []) + list(extra_promoted)
    # Also out-of-band: the stale guest entry this verified vote replaced, so
    # the route can say so in the group (a removal nobody announces is a
    # member silently vanishing from the list).
    out["replaced_guest"] = replaced
    return out


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

    # Route through templates_svc.list_templates (not db.get_templates directly):
    # schedule_enabled is a SQLite TEXT column, so a disabled schedule can be
    # stored as the STRING "0" — a naive `if t.get("schedule_enabled")` treats
    # that as truthy (any non-empty string is truthy in Python) and a paused
    # template would still show as "upcoming" on the web page even though
    # /schedules correctly shows it disabled. list_templates already applies
    # the safe string-aware normalization used by every Telegram-facing path.
    templates = _list_templates(chat_id)
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
            "fee": t.get("fee"),
            "limit": t.get("limit"),
        }
        for t in templates
        # schedule_day is None for a daily recurring template (no weekday to
        # match) — don't require it, only require schedule_enabled+time.
        if t.get("schedule_enabled") and t.get("schedule_time")
        and (t.get("schedule_day") or t.get("recurrence_type") == "daily")
    ]

    # One-time entries (Schedule -> Once) — same shared resolver the web
    # page's own "Scheduled (one-time)" section and /schedules use, so this
    # list can't silently drift out of sync with either of them.
    for p in _tmpl_svc.list_pending_once(chat_id):
        upcoming.append({
            "name": p["title"],
            "title": p.get("display_title") or p["title"],
            "schedule_day": None,
            "schedule_time": None,
            "recurrence_type": "once",
            "event_day": None,
            "event_time": None,
            "location": p.get("location"),
            "fee": p.get("fee"),
            "limit": p.get("limit"),
            "scheduled_at": p["scheduled_at"],
        })

    return {
        "group_token": group_token,
        "group_name": chat.get("group_name") or "",
        "rollcalls": [_serialize_web_rollcall(rc) for rc in rollcalls],
        "upcoming": upcoming,
        "shh_mode": bool(chat.get("shh_mode", False)),
        "dues_enabled": bool(chat.get("dues_enabled", False)),
        "bot_username": db.get_system_config("bot_username") or "",
        "timezone": chat.get("timezone") or "Asia/Kolkata",
        "admin_rights": bool(chat.get("admin_rights", False)),
        "ghost_tracking_enabled": bool(chat.get("ghost_tracking_enabled", True)),
        "absent_limit": int(chat.get("absent_limit") or 1),
    }
