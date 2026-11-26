"""
Email sign-in — the first way into this app that isn't Telegram.

Two flows, one mechanism (a single-use code mailed as a link):

  login  Anyone. Clicking the link proves they own the address; the person
         behind it is the principal bound to that email, created on first use
         with no Telegram account. They act under a local user id
         (identity_space) — or, if the address was linked to Telegram first,
         under their Telegram id, i.e. as exactly the member they already are.

  link   An existing Telegram member, signed in with a GLOBAL identity token,
         adds an email to their account. Clicking the link binds the address
         to their principal. From then on, an email login is that member.

Why a link and not a typed code: the mailed URL opens the web page, which
POSTs the code to redeem it. A mail scanner that GETs the URL does nothing.

What is deliberately refused:
  * linking an address already bound to someone else — re-pointing it would
    merge two people's history on a typo (services.principals.link);
  * removing the only way into an account (an email-only person's email);
  * a chat-bound (/weblogin) token starting a link — that token proves who
    someone is in ONE group, and an email login is valid everywhere.

The code is stored only as a SHA-256 hash and is valid for 15 minutes.
"""

import asyncio
import hashlib
import logging
import os
import re
import secrets
import smtplib
import time
from email.message import EmailMessage
from typing import Optional

import db
from exceptions import incorrectParameter, insufficientPermissions, rateLimited
from services import principals

CODE_TTL_SECONDS = 15 * 60
MAX_CODES_PER_WINDOW = 3          # per address, per CODE_TTL window
# Sender-side limits, so the per-address cap can't be dodged by spraying many
# addresses (sign-in mail sent to strangers, from our domain's reputation):
MAX_SENDS_PER_IP_HOUR = 10        # one visitor
MAX_SENDS_PER_HOUR = 200          # the whole deployment — a ceiling on any abuse
LOGIN, LINK, CHANGE = "login", "link", "change"

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")


class EmailNotConfigured(Exception):
    pass


# ── Sender limits (in memory: one process serves the web page) ───────────────
_sends: dict = {}   # key -> [timestamps in the last hour]


def _take_send_slot(client_ip: Optional[str]) -> None:
    """Reserve one email send, or raise rateLimited. Checked BEFORE the
    per-address limit and before anything is stored or mailed."""
    now = time.time()
    for key, cap, msg in ((f"ip:{client_ip or 'unknown'}", MAX_SENDS_PER_IP_HOUR,
                           "Too many sign-in emails from here — try again in an hour."),
                          ("all", MAX_SENDS_PER_HOUR,
                           "Sign-in email is busy right now — try again in a few minutes.")):
        recent = [t for t in _sends.get(key, []) if now - t < 3600]
        if len(recent) >= cap:
            _sends[key] = recent
            logging.warning("[email_auth] send limit hit for %s", key.split(":")[0])
            raise rateLimited(msg)
        _sends[key] = recent
    for key in (f"ip:{client_ip or 'unknown'}", "all"):
        _sends[key].append(now)
    if len(_sends) > 10000:   # bound memory: drop idle visitors
        for k in [k for k, v in _sends.items() if k != "all" and not any(now - t < 3600 for t in v)]:
            _sends.pop(k, None)


def reset_send_limits_for_tests() -> None:
    _sends.clear()


# ── Configuration / transport ─────────────────────────────────────────────────

def enabled() -> bool:
    """Email sign-in needs somewhere to send from and somewhere to link to."""
    return bool(os.environ.get("SMTP_HOST", "").strip()
                and os.environ.get("SMTP_FROM", "").strip()
                and os.environ.get("WEB_BASE_URL", "").strip())


def _send_blocking(to: str, subject: str, text: str, headers: Optional[dict] = None) -> None:
    host = os.environ["SMTP_HOST"].strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"].strip()
    msg["To"] = to
    msg["Subject"] = subject
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(text)
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        if os.environ.get("SMTP_STARTTLS", "true").strip().lower() not in ("0", "false", "no"):
            server.starttls()
    try:
        if user:
            server.login(user, password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def send_mail(to: str, subject: str, text: str, headers: Optional[dict] = None) -> None:
    """Send one plain-text email. Off the event loop — SMTP is blocking."""
    if not enabled():
        raise EmailNotConfigured("Email sign-in isn't set up on this server.")
    await asyncio.to_thread(_send_blocking, to, subject, text, headers)


# ── Codes ─────────────────────────────────────────────────────────────────────

def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def validate_email(email: str) -> str:
    e = principals.normalise_email(email)
    if len(e) > 254 or not _EMAIL_RE.match(e) or any(ord(c) < 32 for c in e):
        raise incorrectParameter("That doesn't look like an email address.")
    return e


def _recent_codes(email: str, now: int) -> int:
    with db._cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) FROM email_login_codes WHERE email={_ph()} AND created_at>{_ph()}",
            (email, now - CODE_TTL_SECONDS))
        row = cursor.fetchone()
        return int(list(row.values())[0] if isinstance(row, dict) else row[0])


def _store_code(email: str, purpose: str, *, tg_user_id=None, display_name=None,
                return_group=None) -> str:
    now = int(time.time())
    if _recent_codes(email, now) >= MAX_CODES_PER_WINDOW:
        raise rateLimited("Too many sign-in emails for that address — wait a few minutes and try again.")
    code = secrets.token_urlsafe(32)
    with db._cursor(commit=True) as cursor:
        ph = _ph()
        cursor.execute(
            f"INSERT INTO email_login_codes (code_hash, email, purpose, tg_user_id, display_name, "
            f"return_group, created_at, expires_at) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
            (_hash(code), email, purpose, tg_user_id, display_name, return_group,
             now, now + CODE_TTL_SECONDS))
    return code


def _link_url(code: str, return_group: Optional[str]) -> str:
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    path = f"/web/group/{return_group}" if return_group else "/web/"
    return f"{base}{path}?email_code={code}"


def _clean_name(name: Optional[str]) -> Optional[str]:
    if not name or not name.strip():
        return None
    from utils.text import clean_guest_text
    return clean_guest_text(name, "name")[:64] or None


def _valid_group(group_token: Optional[str]) -> Optional[str]:
    if group_token and db.get_chat_by_group_web_token(group_token):
        return group_token
    return None


async def start_login(email: str, name: Optional[str] = None,
                      group_token: Optional[str] = None, client_ip: Optional[str] = None) -> None:
    """Mail a sign-in link. Same outcome whether or not the address is known,
    so this can't be used to find out who has an account."""
    email = validate_email(email)
    _take_send_slot(client_ip)
    name = _clean_name(name)
    group = _valid_group(group_token)
    code = _store_code(email, LOGIN, display_name=name, return_group=group)
    await send_mail(email, "Your RollCall sign-in link",
                    "Tap to sign in to RollCall:\n\n"
                    f"{_link_url(code, group)}\n\n"
                    "The link works once and expires in 15 minutes. "
                    "If you didn't ask for it, ignore this email.")


async def start_link(tg_user_id: int, email: str, group_token: Optional[str] = None,
                     client_ip: Optional[str] = None) -> None:
    """A Telegram member asks to add an email sign-in to their account."""
    from identity_space import is_local_user
    if not tg_user_id or tg_user_id <= 0 or is_local_user(tg_user_id):
        raise insufficientPermissions("Sign in with Telegram first to add an email to your account.")
    email = validate_email(email)
    pid = principals.for_telegram(tg_user_id)
    if pid is not None and principals.resolve(principals.EMAIL, email) == pid:
        raise incorrectParameter("That email is already linked to your account.")
    _take_send_slot(client_ip)
    group = _valid_group(group_token)
    code = _store_code(email, LINK, tg_user_id=tg_user_id, return_group=group)
    await send_mail(email, "Confirm your email for RollCall",
                    "Tap to add this email as a way to sign in to your RollCall account:\n\n"
                    f"{_link_url(code, group)}\n\n"
                    "The link works once and expires in 15 minutes. "
                    "If you didn't ask for it, ignore this email — nothing changes.")


async def start_change(user_id: int, new_email: str, group_token: Optional[str] = None,
                       client_ip: Optional[str] = None) -> None:
    """Move this account's email sign-in to a new address. The link goes to the
    NEW address (proving it), and only this account, on a device signed in as
    it, can use it (redeem). The old address is told once it's done."""
    pid = principals.principal_for_user(user_id)
    old = principals.binding_for(pid, principals.EMAIL) if pid else None
    if not old:
        raise incorrectParameter("This account has no email sign-in to change — add one instead.")
    new_email = validate_email(new_email)
    if new_email == old:
        raise incorrectParameter("That's already your sign-in email.")
    _take_send_slot(client_ip)
    group = _valid_group(group_token)
    code = _store_code(new_email, CHANGE, tg_user_id=user_id, return_group=group)
    await send_mail(new_email, "Confirm your new RollCall email",
                    "Tap to make this your RollCall sign-in email:\n\n"
                    f"{_link_url(code, group)}\n\n"
                    "Open it on the device where you asked for the change. The link works once "
                    "and expires in 15 minutes. If you didn't ask for it, ignore this email.")


def _change(user_id: int, new_email: str) -> None:
    pid = principals.principal_for_user(user_id)
    old = principals.binding_for(pid, principals.EMAIL) if pid else None
    owner = principals.resolve(principals.EMAIL, new_email)
    if owner is not None and owner != pid:
        raise incorrectParameter("That email already signs in to a different account.")
    if owner == pid:
        return
    if old:
        principals.unlink(principals.EMAIL, old)
    if not principals.link(pid, principals.EMAIL, new_email):
        if old:
            principals.link(pid, principals.EMAIL, old)
        raise incorrectParameter("Couldn't change the email — try again.")
    logging.info("[email_auth] sign-in email changed for principal %s", pid)
    if old:
        from services import email_notify
        email_notify.queue_raw(
            pid, old, "Your RollCall sign-in email was changed",
            f"Your RollCall account now signs in with {new_email} instead of this address.\n\n"
            "If you didn't do this, sign in with the new address if you can and choose "
            "'Sign out everywhere', or ask your group's admin for help.",
            email_notify.SECURITY)


def _consume(code: str) -> dict:
    """Mark a code used and return its row. Atomic: of two concurrent redeems
    of one code, exactly one gets the row."""
    if not code or len(code) > 200:
        raise incorrectParameter("That sign-in link isn't valid.")
    now = int(time.time())
    h = _hash(code)
    with db._cursor(commit=True) as cursor:
        ph = _ph()
        cursor.execute(
            f"UPDATE email_login_codes SET used_at={ph} "
            f"WHERE code_hash={ph} AND used_at IS NULL AND expires_at>={ph}",
            (now, h, now))
        won = cursor.rowcount == 1
        cursor.execute(
            f"SELECT email, purpose, tg_user_id, display_name, return_group, used_at, expires_at "
            f"FROM email_login_codes WHERE code_hash={ph}", (h,))
        row = cursor.fetchone()
    if row is None:
        raise incorrectParameter("That sign-in link isn't valid.")
    row = dict(row) if isinstance(row, dict) else dict(zip(
        ("email", "purpose", "tg_user_id", "display_name", "return_group", "used_at", "expires_at"), row))
    if not won:
        if row["expires_at"] < now:
            raise incorrectParameter("That sign-in link has expired — ask for a new one.")
        raise incorrectParameter("That sign-in link was already used — ask for a new one.")
    return row


def telegram_name(tg_user_id: int) -> Optional[str]:
    try:
        with db._cursor() as cursor:
            cursor.execute(
                f"SELECT first_name FROM chat_members WHERE user_id={_ph()} AND first_name IS NOT NULL "
                f"ORDER BY last_seen DESC LIMIT 1", (tg_user_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return row["first_name"] if isinstance(row, dict) else row[0]
    except Exception:
        logging.exception("email_auth: telegram name lookup failed")
        return None


def name_for(principal_id: int, user_id: int, email: str) -> str:
    from identity_space import is_local_user
    return (principals.display_name(principal_id)
            or (None if is_local_user(user_id) else telegram_name(user_id))
            or email.split("@")[0])


def _peek(code: str) -> Optional[dict]:
    if not code or len(code) > 200:
        return None
    with db._cursor() as cursor:
        cursor.execute(f"SELECT purpose, tg_user_id FROM email_login_codes WHERE code_hash={_ph()}",
                       (_hash(code),))
        row = cursor.fetchone()
    if row is None:
        return None
    return dict(row) if isinstance(row, dict) else {"purpose": row[0], "tg_user_id": row[1]}


MERGE_TTL_SECONDS = CODE_TTL_SECONDS


def _merge_scope(local_uid: int) -> str:
    return f"account_merge:{int(local_uid)}"


def redeem(code: str, caller_user_id: Optional[int] = None) -> dict:
    """Use a mailed code. Returns {user_id, name, email, purpose, group_token}
    and, for a link that found a duplicate account, the merge offer.
    The caller mints the identity token for user_id.

    A LINK code only works for the Telegram account that asked for it, on a
    device signed in as them (caller_user_id). Otherwise anyone could type a
    victim's address into their own account: the victim clicks "confirm",
    and their email — and any account behind it — lands on the stranger's.
    Checked before the code is spent, so the right device can still use it."""
    peek = _peek(code)
    if peek and peek["purpose"] == LINK and caller_user_id != int(peek["tg_user_id"] or 0):
        raise insufficientPermissions(
            "Open this link on the device where you asked for it, signed in with Telegram.")
    if peek and peek["purpose"] == CHANGE and caller_user_id != int(peek["tg_user_id"] or 0):
        raise insufficientPermissions(
            "Open this link on the device where you asked for the change, signed in to that account.")
    row = _consume(code)
    email = row["email"]
    if row["purpose"] == CHANGE:
        uid = int(row["tg_user_id"])
        _change(uid, email)
        pid = principals.principal_for_user(uid)
        return {"user_id": uid, "name": name_for(pid, uid, email), "email": email,
                "purpose": CHANGE, "group_token": row.get("return_group")}
    if row["purpose"] == LINK:
        tg = int(row["tg_user_id"])
        pid = principals.for_telegram(tg)
        owner = principals.resolve(principals.EMAIL, email)
        if owner is not None and owner != pid:
            if principals.binding_for(owner, principals.TELEGRAM) is not None:
                raise incorrectParameter(
                    "That email already signs in to a different Telegram member's account, so it "
                    "can't be linked to this one.")
            # An email-only account — almost always this same person, who
            # signed in by email before linking. Both identities are now
            # proven on this device, so offer to merge (confirmed in the page).
            from api.identity import issue_scoped_token
            from services.account_merge import summary
            local_uid = principals.acting_user_id(owner)
            return {"user_id": tg, "name": name_for(pid, tg, email), "email": email,
                    "purpose": "merge_offer", "group_token": row.get("return_group"),
                    "merge_from_user_id": local_uid,
                    "merge_from_name": principals.display_name(owner) or email.split("@")[0],
                    "merge_token": issue_scoped_token(tg, _merge_scope(local_uid), MERGE_TTL_SECONDS),
                    **{f"merge_{k}": v for k, v in summary(local_uid).items()}}
        if owner is None and not principals.link(pid, principals.EMAIL, email):
            raise incorrectParameter("Couldn't link that email — try again.")
        logging.info("[email_auth] email linked to telegram user %s", tg)
        user_id = tg
    else:
        pid = principals.for_email(email, row.get("display_name"))
        if pid is None:
            raise incorrectParameter("Couldn't sign you in — try again.")
        if not principals.display_name(pid) and row.get("display_name"):
            principals.set_display_name(pid, row["display_name"])
        user_id = principals.acting_user_id(pid)
    return {"user_id": user_id, "name": name_for(pid, user_id, email), "email": email,
            "purpose": row["purpose"], "group_token": row.get("return_group")}


# ── The account's email ──────────────────────────────────────────────────────

def status(user_id: int) -> dict:
    """What the account menu shows: the linked email, and whether it can be removed."""
    pid = principals.principal_for_user(user_id)
    email = principals.binding_for(pid, principals.EMAIL) if pid else None
    has_telegram = bool(pid and principals.binding_for(pid, principals.TELEGRAM))
    return {"enabled": enabled(), "email": email, "can_unlink": bool(email and has_telegram),
            "has_telegram": has_telegram}


def unlink(user_id: int) -> None:
    pid = principals.principal_for_user(user_id)
    email = principals.binding_for(pid, principals.EMAIL) if pid else None
    if not email:
        raise incorrectParameter("No email is linked to this account.")
    if not principals.binding_for(pid, principals.TELEGRAM):
        raise insufficientPermissions(
            "Email is the only way into this account, so it can't be removed.")
    principals.unlink(principals.EMAIL, email)


async def merge_accounts(tg_user_id: int, local_uid: int, merge_token: str, actor_name: str) -> dict:
    """Confirm a merge offered by redeem(). The token is bound to BOTH ids, so
    it can't be replayed for another account, and only the Telegram sign-in it
    was issued to may use it."""
    from api.identity import verify_scoped_token
    from services.account_merge import merge
    if verify_scoped_token(merge_token, _merge_scope(local_uid)) != tg_user_id:
        raise insufficientPermissions("That merge offer has expired — add the email again.")
    pid_local = principals.principal_of_local(local_uid)
    email = principals.binding_for(pid_local, principals.EMAIL) if pid_local else None
    result = await merge(local_uid, tg_user_id, actor_name)
    if email:
        principals.unlink(principals.EMAIL, email)
        principals.link(principals.for_telegram(tg_user_id), principals.EMAIL, email)
    return {**result, "email": email}


async def merge_signed_in(tg_user_id: int, local_token: str, actor_name: str) -> dict:
    """Merge from an email session: the person signed in by email, then signed
    in with Telegram on the same device, so the page holds proof of BOTH —
    the old email token and the new Telegram one."""
    from api.identity import verify_identity_token_unfollowed
    from identity_space import is_local_user
    from services.account_merge import merge
    # Not followed through merges, but refused after sign-out-everywhere/delete.
    local_uid = verify_identity_token_unfollowed(local_token)
    if local_uid is None or not is_local_user(local_uid):
        raise insufficientPermissions("That email sign-in has expired — sign in by email again.")
    if is_local_user(tg_user_id):
        raise insufficientPermissions("Sign in with Telegram to merge into your Telegram account.")
    pid_local = principals.principal_of_local(local_uid)
    email = principals.binding_for(pid_local, principals.EMAIL) if pid_local else None
    owner_tg = principals.binding_for(principals.for_telegram(tg_user_id), principals.EMAIL)
    if email and owner_tg and owner_tg != email:
        raise incorrectParameter(
            f"Your Telegram account already signs in with {owner_tg}. Remove it first to use {email} instead.")
    result = await merge(local_uid, tg_user_id, actor_name)
    if email:
        principals.unlink(principals.EMAIL, email)
        principals.link(principals.for_telegram(tg_user_id), principals.EMAIL, email)
    return {**result, "email": email}
