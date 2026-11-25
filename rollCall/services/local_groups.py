"""
Web-only groups — a RollCall group with no Telegram chat behind it.

A Telegram group exists because the bot was added to a chat; its id is that
chat's. A web-only group gets an id from identity_space's local chat range
instead, so the chats row and everything keyed on chat_id (rollcalls, votes,
stats, dues, templates) work unchanged. What differs:

  * admins come from this app's own web_admins list (admin_source='local'),
    never from Telegram — the creator is the first owner, and services.admin
    refuses to ever leave the group ownerless;
  * members are whoever joins from the group link (chat_members), since no
    bot is there to see them;
  * nothing is posted anywhere: chat_outbox refuses Telegram calls for the id,
    so every "announce to the group" path quietly does nothing, and the web
    page (plus web push) is the group.

Joining also lets a person without Telegram be a real member of a TELEGRAM
group: same row, same id scheme, votes count as theirs rather than a guest's.
"""

import logging
from typing import Optional

import db
from exceptions import incorrectParameter, insufficientPermissions
from identity_space import is_local_chat, is_local_user, local_chat_id

MAX_OWNED_GROUPS = 10


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _clean(text: Optional[str], what: str, limit: int) -> str:
    from utils.text import clean_guest_text
    value = clean_guest_text(text or "", "name" if what == "name" else "comment").strip()[:limit]
    if not value:
        raise incorrectParameter(f"Give the {what}.")
    return value


def _owned_local_groups(user_id: int) -> int:
    with db._cursor() as cursor:
        cursor.execute(
            f"SELECT chat_id FROM web_admins WHERE tg_user_id={_ph()} AND role='owner'", (user_id,))
        return sum(1 for r in cursor.fetchall()
                   if is_local_chat(r["chat_id"] if isinstance(r, dict) else r[0]))


def _next_chat_id() -> int:
    from identity_space import LOCAL_CHAT_BASE
    with db._cursor() as cursor:
        cursor.execute(f"SELECT MIN(chat_id) FROM chats WHERE chat_id <= {_ph()}", (LOCAL_CHAT_BASE,))
        row = cursor.fetchone()
        lowest = (list(row.values())[0] if isinstance(row, dict) else row[0]) if row else None
    n = 1 if lowest is None else LOCAL_CHAT_BASE - int(lowest) + 1
    return local_chat_id(n)


def _insert_chat(chat_id: int) -> bool:
    """Claim chat_id. False if someone else took it first (the primary key
    decides — two concurrent creates can't share a group)."""
    try:
        with db._cursor(commit=True) as cursor:
            cursor.execute(f"INSERT INTO chats (chat_id) VALUES ({_ph()})", (chat_id,))
        return True
    except Exception:
        return False


def create_group(owner_user_id: int, owner_name: str, name: str,
                 timezone: Optional[str] = None) -> dict:
    """Make a web-only group with the caller as its owner and first member."""
    if not owner_user_id or owner_user_id <= 0:
        raise insufficientPermissions("Sign in first to create a group.")
    name = _clean(name, "group name", 64)
    owner_name = _clean(owner_name or "Organiser", "name", 64)
    tz = (timezone or "Asia/Kolkata").strip()
    try:
        import pytz
        pytz.timezone(tz)
    except Exception:
        raise incorrectParameter(f"Unknown timezone '{tz}'.")
    if _owned_local_groups(owner_user_id) >= MAX_OWNED_GROUPS:
        raise insufficientPermissions(f"You can own at most {MAX_OWNED_GROUPS} web groups.")

    for _ in range(5):
        chat_id = _next_chat_id()
        if _insert_chat(chat_id):
            break
    else:
        raise incorrectParameter("Couldn't create the group — try again.")

    chat = db.get_or_create_chat(chat_id)          # fills defaults + group_web_token
    # Private by default: the link alone doesn't make someone a member.
    db.update_chat_settings(chat_id, group_name=name, timezone=tz, admin_source="local",
                            join_approval=1)
    db.set_web_admin(chat_id, owner_user_id, owner_name)
    db.set_web_admin_role(chat_id, owner_user_id, "owner")
    db.upsert_chat_member(chat_id, owner_user_id, owner_name)
    db.log_admin_action(chat_id, owner_user_id, owner_name, "create_web_group", target_name=name)
    try:
        from rollcall_manager import manager
        manager.get_chat(chat_id)   # warm the cache with the new settings
    except Exception:
        logging.debug("local_groups: manager warm-up skipped", exc_info=True)
    logging.info("[local_groups] created chat=%s owner=%s", chat_id, owner_user_id)
    return {"chat_id": chat_id, "group_token": chat["group_web_token"], "group_name": name}


def join(chat_id: int, user_id: int, name: Optional[str]) -> dict:
    """Become a member of a group from its web link.

    Open to anyone holding the link — the same reach as guest voting, which
    that link already grants — but as a real identity rather than a typed
    name. In a Telegram group only people without Telegram join this way: a
    Telegram member is seen by the bot, and a web-typed name must not
    overwrite the name Telegram reports for them."""
    if not user_id or user_id <= 0:
        raise insufficientPermissions("Sign in first to join.")
    existing = db.get_member_display_info(chat_id, user_id)
    if existing:
        return {"joined": False, "name": existing.get("first_name")}
    if not is_local_chat(chat_id) and not is_local_user(user_id):
        raise incorrectParameter("You're on Telegram — say anything in the group and the bot adds you.")
    name = _clean(name, "name", 64)
    if needs_approval(chat_id) and not db.is_web_admin(chat_id, user_id):
        request_to_join(chat_id, user_id, name)
        return {"joined": False, "pending": True, "name": name}
    db.upsert_chat_member(chat_id, user_id, name)
    return {"joined": True, "name": name}


# ── Join approval ─────────────────────────────────────────────────────────────
# With join_approval on, the group link stops being membership: joining makes
# a request an admin approves, and web votes come from members only — guests
# included, since a typed name is exactly the reach the link gave before.
# Telegram members are untouched (Telegram already decided who's in the chat);
# this governs the people only the web page can see.

def needs_approval(chat_id: int) -> bool:
    chat = db.get_or_create_chat(chat_id) or {}
    try:
        return bool(int(chat.get("join_approval") or 0))
    except (TypeError, ValueError):
        return False


def request_to_join(chat_id: int, user_id: int, name: str) -> None:
    """File (or refresh) a request. A denied request stays denied — asking
    again doesn't put it back in front of the admins."""
    import time
    ph = _ph()
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"SELECT status FROM join_requests WHERE chat_id={ph} AND user_id={ph}",
                       (chat_id, user_id))
        row = cursor.fetchone()
        if row is None:
            cursor.execute(f"INSERT INTO join_requests (chat_id, user_id, name, status, requested_at) "
                           f"VALUES ({ph},{ph},{ph},'pending',{ph})",
                           (chat_id, user_id, name, int(time.time())))
        elif (row["status"] if isinstance(row, dict) else row[0]) == "pending":
            cursor.execute(f"UPDATE join_requests SET name={ph} WHERE chat_id={ph} AND user_id={ph}",
                           (name, chat_id, user_id))


def list_requests(chat_id: int) -> list:
    with db._cursor() as cursor:
        cursor.execute(f"SELECT user_id, name, requested_at FROM join_requests "
                       f"WHERE chat_id={_ph()} AND status='pending' ORDER BY requested_at", (chat_id,))
        return [dict(r) if isinstance(r, dict) else {"user_id": r[0], "name": r[1], "requested_at": r[2]}
                for r in cursor.fetchall()]


def _request(chat_id: int, user_id: int) -> Optional[dict]:
    with db._cursor() as cursor:
        cursor.execute(f"SELECT name, status FROM join_requests WHERE chat_id={_ph()} AND user_id={_ph()}",
                       (chat_id, user_id))
        r = cursor.fetchone()
    if r is None:
        return None
    return dict(r) if isinstance(r, dict) else {"name": r[0], "status": r[1]}


def decide_request(chat_id: int, user_id: int, approve: bool, *, actor_user_id: int, actor_name: str) -> dict:
    """An admin answers a join request (the caller has checked they're an admin)."""
    req = _request(chat_id, user_id)
    if not req or req["status"] != "pending":
        raise incorrectParameter("No pending request from that person.")
    ph = _ph()
    with db._cursor(commit=True) as cursor:
        if approve:
            cursor.execute(f"DELETE FROM join_requests WHERE chat_id={ph} AND user_id={ph}", (chat_id, user_id))
        else:
            cursor.execute(f"UPDATE join_requests SET status='denied' WHERE chat_id={ph} AND user_id={ph}",
                           (chat_id, user_id))
    if approve:
        db.upsert_chat_member(chat_id, user_id, req["name"] or "Member")
    db.log_admin_action(chat_id, actor_user_id, actor_name,
                        "approve_join" if approve else "deny_join", target_name=req["name"])
    return {"user_id": user_id, "approved": approve, "name": req["name"]}


def gate_web_vote(chat_id: int, user_id: Optional[int], member_info, name: str) -> None:
    """Refuse a web vote the group's join setting doesn't allow.

    user_id None = a guest typing a name. A Telegram id is always allowed —
    their membership is Telegram's to decide. Admins are always allowed."""
    if not needs_approval(chat_id):
        return
    if user_id is not None and (member_info or db.is_web_admin(chat_id, user_id)):
        return
    if user_id is not None and not is_local_user(user_id) and not is_local_chat(chat_id):
        return
    if user_id is None:
        raise insufficientPermissions(
            "This group takes votes from its members only — sign in and ask to join.")
    request_to_join(chat_id, user_id, name)
    raise insufficientPermissions(
        "You've asked to join this group — an admin needs to approve you before you can vote.")


def _require_local_owner(chat_id: int, actor_user_id: int) -> None:
    from services.admin import get_admin_source
    if not (is_local_chat(chat_id) or get_admin_source(chat_id) == "local"):
        raise incorrectParameter("This group's admins come from Telegram — change them there.")
    if db.get_web_admin_role(chat_id, actor_user_id) != "owner":
        raise insufficientPermissions("Only a group owner can change who administers this group.")


def grant_admin(chat_id: int, target_user_id: int, *, actor_user_id: int, actor_name: str) -> dict:
    """Make a member an admin, in a group that keeps its own admin list."""
    _require_local_owner(chat_id, actor_user_id)
    info = db.get_member_display_info(chat_id, target_user_id)
    if not info:
        raise incorrectParameter("Only a member of the group can be made an admin.")
    if db.get_web_admin_role(chat_id, target_user_id) is None:
        db.set_web_admin(chat_id, target_user_id, info.get("first_name") or "Member")
    db.log_admin_action(chat_id, actor_user_id, actor_name, "grant_admin",
                        target_name=info.get("first_name") or str(target_user_id))
    return {"user_id": target_user_id, "role": db.get_web_admin_role(chat_id, target_user_id)}


def remove_admin(chat_id: int, target_user_id: int, *, actor_user_id: int, actor_name: str) -> dict:
    """Take admin away — never from the last owner (services.admin's rule)."""
    _require_local_owner(chat_id, actor_user_id)
    from services.admin import revoke_admin
    if db.get_web_admin_role(chat_id, target_user_id) is None:
        raise incorrectParameter("That person isn't an admin of this group.")
    if not revoke_admin(chat_id, target_user_id):
        raise insufficientPermissions(
            "This is the group's only owner — promote someone else first.")
    db.log_admin_action(chat_id, actor_user_id, actor_name, "remove_admin",
                        target_name=str(target_user_id))
    return {"user_id": target_user_id, "role": None}
