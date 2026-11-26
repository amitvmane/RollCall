"""
A person's own account settings — the part Telegram does for its members and
nothing did for people who sign in by email.

  summary              what the Account sheet shows
  set_email_notify     when notification emails go out (services/email_notify)
  sign_out_everywhere  refuse every identity token issued before now
  delete               an email-only account, by its owner

Sign out everywhere: identity tokens are stateless HMACs, so "revoke" means a
per-person cutoff (principals.tokens_valid_after). Every identity token is
minted with the same lifetime (api.identity.IDENTITY_TOKEN_TTL_SECONDS), so
its issue time is exp - TTL and the wire format doesn't change. Checked in
api.identity.verify_identity_token through tokens_valid_after() below, which
caches for CACHE_SECONDS: one DB read per person per half-minute, not per
request. A Telegram member can use it too — it ends web sessions; the Telegram
Mini App simply signs in again from Telegram.

Delete keeps the groups' records: past votes, stats and the dues ledger
(append-only — CLAUDE.md) stay under the person's id, as a Telegram member's
past votes stay when they leave a group. What goes: the email, the name on
the account, group memberships and admin grants, API keys they issued, push
subscriptions, queued emails, sign-in codes, and every session. Refused while it would leave a
group with members and no owner, or leave a game short (they're IN, MAYBE or
waiting on an open rollcall).
"""

import logging
import time
import db
from exceptions import incorrectParameter, insufficientPermissions
from identity_space import is_local_user
from services import email_notify, principals

CACHE_SECONDS = 30
_cutoffs: dict = {}   # user id -> (cutoff or 0, fetched_at)


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _pid(user_id: int) -> int:
    pid = principals.principal_for_user(user_id)
    if not pid:
        raise insufficientPermissions("Sign in again — this account wasn't found.")
    return pid


# ── Summary / preferences ────────────────────────────────────────────────────

def summary(user_id: int) -> dict:
    from services import email_auth
    pid = _pid(user_id)
    email = principals.binding_for(pid, principals.EMAIL)
    has_tg = bool(principals.binding_for(pid, principals.TELEGRAM))
    return {
        "user_id": user_id,
        "name": email_auth.name_for(pid, user_id, email) if email else (principals.display_name(pid) or ""),
        "email": email,
        "has_telegram": has_tg,
        "email_only": is_local_user(user_id),
        "email_enabled": email_auth.enabled(),
        # Only email-only accounts are emailed (Telegram members hear in Telegram).
        "email_notify": email_notify.get_pref(pid) if is_local_user(user_id) else None,
        "can_delete": is_local_user(user_id),
    }


def set_email_notify(user_id: int, pref: str) -> str:
    if pref not in email_notify.PREFS:
        raise incorrectParameter("Choose auto, always or off.")
    if not is_local_user(user_id):
        raise incorrectParameter("Telegram members get these notices in Telegram, not by email.")
    email_notify.set_pref(_pid(user_id), pref)
    return pref


# ── Sign out everywhere ──────────────────────────────────────────────────────

# A deleted account refuses every token, whenever it was issued — not just
# those from before the deletion second (a token minted a second later from
# a key that still verifies would otherwise pass and act as nobody).
DELETED_CUTOFF = 2 ** 62


def _read_cutoff(pid: int) -> int:
    with db._cursor() as cursor:
        cursor.execute(f"SELECT tokens_valid_after, deleted_at FROM principals WHERE id={_ph()}", (pid,))
        row = cursor.fetchone()
    if row is None:
        return 0
    v, deleted = (row["tokens_valid_after"], row["deleted_at"]) if isinstance(row, dict) else (row[0], row[1])
    if isinstance(deleted, (int, float)) and not isinstance(deleted, bool) and deleted > 0:
        return DELETED_CUTOFF
    return int(v or 0)


def tokens_valid_after(user_id: int) -> int:
    """The cutoff for this person's tokens (0 = none), cached per user id.
    Fails open on a DB error — a broken read must not sign every member out;
    the cutoff is a second line behind the signature and expiry."""
    now = time.time()
    hit = _cutoffs.get(user_id)
    if hit and now - hit[1] < CACHE_SECONDS:
        return hit[0]
    try:
        from identity_space import principal_of_local_user
        pid = (principal_of_local_user(user_id) if is_local_user(user_id)
               else principals.resolve(principals.TELEGRAM, user_id))
        cutoff = _read_cutoff(pid) if pid else 0
    except Exception:
        logging.exception("account.tokens_valid_after failed")
        return 0
    if len(_cutoffs) > 50000:
        _cutoffs.clear()
    _cutoffs[user_id] = (cutoff, now)
    return cutoff


def _set_cutoff(pid: int, when: int) -> None:
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"UPDATE principals SET tokens_valid_after={_ph()} WHERE id={_ph()}", (when, pid))
    _cutoffs.clear()   # keyed by user id; a principal can have several — rare, so drop all


def sign_out_everywhere(user_id: int) -> str:
    """Every identity token issued up to now stops working, including ones
    minted earlier in this same second (the cutoff is the NEXT second).
    Returns a fresh token for the device that asked: minted with one extra
    second of life, so its derived issue time is that next second and passes."""
    from api.identity import IDENTITY_TOKEN_TTL_SECONDS, issue_identity_token
    _set_cutoff(_pid(user_id), int(time.time()) + 1)
    logging.info("[account] signed out everywhere: user %s", user_id)
    return issue_identity_token(user_id, IDENTITY_TOKEN_TTL_SECONDS + 1)


def reset_cache_for_tests() -> None:
    _cutoffs.clear()


# ── Delete ───────────────────────────────────────────────────────────────────

def _member_chats(user_id: int) -> list:
    with db._cursor() as cursor:
        cursor.execute(f"SELECT chat_id FROM chat_members WHERE user_id={_ph()}", (user_id,))
        return [int(r["chat_id"] if isinstance(r, dict) else r[0]) for r in cursor.fetchall()]


def _blockers(user_id: int) -> list:
    from services.local_groups import on_open_rollcall
    out = []
    for cid in _member_chats(user_id):
        name = (db.get_or_create_chat(cid) or {}).get("group_name") or "a group"
        title = on_open_rollcall(cid, user_id)
        if title:
            out.append(f"you're on '{title}' in {name} — vote OUT first")
        if db.get_web_admin_role(cid, user_id) == "owner" and db.count_web_admin_owners(cid) <= 1:
            if _other_members(cid, user_id):
                out.append(f"you're the only owner of {name} — make someone else owner first")
    return out


def _other_members(chat_id: int, user_id: int) -> list:
    return [m for m in db.get_active_members(chat_id) if m.get("user_id") != user_id]


def delete(user_id: int) -> dict:
    """Delete an email-only account. Returns {"groups": n} it was removed from."""
    if not is_local_user(user_id):
        raise insufficientPermissions(
            "This account comes from Telegram, so it can't be deleted here — you can remove its email sign-in.")
    pid = _pid(user_id)
    problems = _blockers(user_id)
    if problems:
        raise incorrectParameter("Can't delete yet: " + "; ".join(problems) + ".")
    email = principals.binding_for(pid, principals.EMAIL)
    chats = _member_chats(user_id)
    ph = _ph()
    now = int(time.time())
    from services.admin import revoke_admin
    for cid in chats:
        db.mark_member_inactive(cid, user_id)
        # Through the one rule that keeps a last owner: refused only for a
        # group they're alone in (_blockers allowed it), which nobody else
        # can reach anyway.
        if db.get_web_admin_role(cid, user_id) is not None:
            revoke_admin(cid, user_id)
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM join_requests WHERE user_id={ph}", (user_id,))
        cursor.execute(f"DELETE FROM push_subscriptions WHERE tg_user_id={ph}", (user_id,))
        if email:
            cursor.execute(f"DELETE FROM email_login_codes WHERE email={ph}", (email,))
        cursor.execute(
            f"UPDATE principals SET display_name=NULL, deleted_at={ph}, tokens_valid_after={ph}, "
            f"email_notify='off' WHERE id={ph}", (now, now + 1, pid))   # +1: same-second tokens too
    email_notify.forget(pid)
    # App keys / API tokens they issued die with the account: a partner key
    # stays tied to the admin who vouched for it (services/partner_api).
    from services.partner_api import revoke_issued_by
    revoke_issued_by(user_id)
    for b in principals.bindings(pid):
        principals.unlink(b["platform"], b["platform_user_id"])
    _cutoffs.clear()
    logging.info("[account] deleted email-only account %s (%d group(s))", user_id, len(chats))
    return {"groups": len(chats)}
