"""
Notification emails for people without Telegram.

Someone who signs in by email has no Telegram DM, and web push reaches them
only if they turned the bell on (on an iPhone, only in the installed app).
Without this, an email-only member would miss "you're IN from the waitlist",
"you haven't voted", dues reminders and new games entirely.

Who gets them: email-only accounts (identity_space local ids) with an email
bound. Telegram members — including those who linked an email — already get
the same news in Telegram, so they are never emailed.

When (principals.email_notify):
  auto    (default) only if this person has no web push on for that group,
          so nobody gets the same news twice.
  always  every time.
  off     never (also what one-click unsubscribe sets).

How: queue_* writes a row to email_outbox where the event happens (no
network, safe anywhere); deliver_due() sends from the minute tick, with
retries. Limits: MAX_PER_DAY per person (a chatty group can't flood an inbox),
MAX_PER_HOUR for the deployment (the sender domain's reputation), and the same
message to the same person is not repeated within DEDUPE_SECONDS.

Every email carries a one-click unsubscribe (RFC 8058 List-Unsubscribe-Post)
whose token never expires — an old email's link must still work.
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Iterable, Optional

import db
from identity_space import LOCAL_USER_BASE, is_local_user

AUTO, ALWAYS, OFF = "auto", "always", "off"
PREFS = (AUTO, ALWAYS, OFF)

MAX_PER_DAY = 10
MAX_PER_HOUR = 300
DEDUPE_SECONDS = 6 * 3600
BATCH = 20
MAX_ATTEMPTS = 4
BACKOFF_MINUTES = (1, 5, 15, 60)
KEEP_DAYS = 30
SECURITY = "security"   # account notices: never skipped by prefs or caps


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _one(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    return list(row.values())[0] if isinstance(row, dict) else row[0]


# ── Preference ───────────────────────────────────────────────────────────────

def get_pref(principal_id: int) -> str:
    try:
        with db._cursor() as cursor:
            cursor.execute(f"SELECT email_notify FROM principals WHERE id={_ph()}", (principal_id,))
            v = _one(cursor)
        return v if v in PREFS else AUTO
    except Exception:
        logging.exception("email_notify.get_pref failed")
        return AUTO


def set_pref(principal_id: int, pref: str) -> None:
    if pref not in PREFS:
        raise ValueError(pref)
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"UPDATE principals SET email_notify={_ph()} WHERE id={_ph()}", (pref, principal_id))


# ── Unsubscribe tokens (no expiry: an old email's link must still work) ──────

def _keys() -> list:
    from api.identity import _signing_keys
    return [hashlib.sha256(b"RollCallEmailUnsubV1" + k).digest() for k in _signing_keys()]


def unsubscribe_token(principal_id: int) -> str:
    sig = hmac.new(_keys()[0], f"unsub:{int(principal_id)}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{int(principal_id)}.{sig}"


def verify_unsubscribe(token: Optional[str]) -> Optional[int]:
    try:
        pid_s, sig = (token or "").split(".")
        pid = int(pid_s)
    except (ValueError, AttributeError):
        return None
    ok = False
    for k in _keys():
        want = hmac.new(k, f"unsub:{pid}".encode(), hashlib.sha256).hexdigest()[:32]
        ok |= hmac.compare_digest(want, sig)
    return pid if ok and pid > 0 else None


def unsubscribe_url(principal_id: int) -> str:
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    return f"{base}/api/v1/auth/email/unsubscribe?t={unsubscribe_token(principal_id)}"


# ── Queueing ─────────────────────────────────────────────────────────────────

def _enabled() -> bool:
    from services.email_auth import enabled
    return enabled()


def _recipient(user_id) -> Optional[tuple]:
    """(principal_id, email) for an email-only account that may be emailed."""
    if not isinstance(user_id, int) or not is_local_user(user_id):
        return None
    from services import principals
    from services.account_merge import merged_into
    if merged_into(user_id):
        return None   # now a Telegram member: they hear about it there
    pid = principals.principal_of_local(user_id)
    if not pid or principals.binding_for(pid, principals.TELEGRAM):
        return None
    email = principals.binding_for(pid, principals.EMAIL)
    return (pid, email) if email else None


def _has_push(group_token: Optional[str], user_id: int) -> bool:
    if not group_token:
        return False
    try:
        return any(s.get("tg_user_id") == user_id for s in db.get_push_subscriptions(group_token))
    except Exception:
        return False


def _sent_recently(cursor, pid: int, subject: str, body: str, now: int) -> bool:
    cursor.execute(
        f"SELECT COUNT(*) FROM email_outbox WHERE principal_id={_ph()} AND subject={_ph()} "
        f"AND body={_ph()} AND created_at>{_ph()}", (pid, subject, body, now - DEDUPE_SECONDS))
    return int(_one(cursor) or 0) > 0


def _today_count(cursor, pid: int, now: int) -> int:
    cursor.execute(
        f"SELECT COUNT(*) FROM email_outbox WHERE principal_id={_ph()} AND tag<>{_ph()} AND created_at>{_ph()}",
        (pid, SECURITY, now - 86400))
    return int(_one(cursor) or 0)


def queue_raw(principal_id: int, email: str, subject: str, body: str, tag: str,
              chat_id: Optional[int] = None) -> bool:
    """Queue one email as-is. Callers have decided it should go."""
    if not _enabled():
        return False
    # One line: a rollcall title can hold a newline, and the email library
    # refuses a header with one — the mail would fail all four attempts.
    subject = " ".join(str(subject or "").split())[:200] or "RollCall"
    now = int(time.time())
    ph = _ph()
    try:
        with db._cursor(commit=True) as cursor:
            if tag != SECURITY:
                if _sent_recently(cursor, principal_id, subject, body, now):
                    return False
                if _today_count(cursor, principal_id, now) >= MAX_PER_DAY:
                    logging.info("[email_notify] daily cap reached for principal %s", principal_id)
                    return False
            cursor.execute(
                f"INSERT INTO email_outbox (principal_id, email, chat_id, tag, subject, body, status, "
                f"attempts, next_attempt, created_at) VALUES ({ph},{ph},{ph},{ph},{ph},{ph},'pending',0,{ph},{ph})",
                (principal_id, email, chat_id, tag, subject[:200], body, now, now))
        return True
    except Exception:
        logging.exception("[email_notify] queue failed")
        return False


def _compose(pid: int, title: str, body: str, url: Optional[str], auto: bool) -> str:
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    why = (" and don't have notifications turned on for this group in your browser" if auto else "")
    parts = [title, "", body]
    if url:
        parts += ["", f"Open the group: {url}"]
    parts += ["", "—",
              f"You get this because you sign in to RollCall with this address{why}.",
              f"Email settings: {base}/web/?account=1",
              f"Unsubscribe: {unsubscribe_url(pid)}"]
    return "\n".join(parts)


def queue_for_users(chat_id: int, user_ids: Iterable, title: str, body: str, tag: str) -> int:
    """The email twin of services.push.notify_users. Never raises."""
    try:
        if not _enabled():
            return 0
        from services.push import group_link
        group_token, url = group_link(chat_id)
        n = 0
        for uid in {u for u in user_ids if isinstance(u, int)}:
            rec = _recipient(uid)
            if not rec:
                continue
            pid, email = rec
            pref = get_pref(pid)
            if pref == OFF or (pref == AUTO and _has_push(group_token, uid)):
                continue
            if queue_raw(pid, email, title, _compose(pid, title, body, url, pref == AUTO), tag, chat_id):
                n += 1
        return n
    except Exception:
        logging.exception("[email_notify] queue_for_users failed chat=%s", chat_id)
        return 0


def local_members(chat_id: int) -> list:
    ph = _ph()
    active = True if db.db_type == "postgresql" else 1
    with db._cursor() as cursor:
        cursor.execute(
            f"SELECT user_id FROM chat_members WHERE chat_id={ph} AND is_active={ph} AND user_id>={ph}",
            (chat_id, active, LOCAL_USER_BASE))
        rows = cursor.fetchall()
    return [int(r["user_id"] if isinstance(r, dict) else r[0]) for r in rows]


def queue_rollcall_opened(chat_id: int, title: str) -> int:
    """Every email-only member of the group hears a new game opened. Never raises."""
    try:
        if not _enabled():
            return 0
        return queue_for_users(chat_id, local_members(chat_id), f"🎯 {title}",
                               "A new rollcall just opened — vote in, out or maybe.", "open")
    except Exception:
        logging.exception("[email_notify] queue_rollcall_opened failed chat=%s", chat_id)
        return 0


# ── Delivery (minute tick) ───────────────────────────────────────────────────

def _current_email(principal_id: int) -> Optional[str]:
    try:
        from services import principals
        return principals.binding_for(principal_id, principals.EMAIL)
    except Exception:
        logging.exception("[email_notify] email lookup failed")
        return None


def _sent_last_hour(cursor, now: int) -> int:
    cursor.execute(f"SELECT COUNT(*) FROM email_outbox WHERE status='sent' AND next_attempt>{_ph()}",
                   (now - 3600,))
    return int(_one(cursor) or 0)


async def deliver_due(budget_seconds: float = 20) -> int:
    """Send what is due. Stops starting new sends once the budget is spent
    (the rest go next tick). Returns how many went out."""
    if not _enabled():
        return 0
    from services.email_auth import send_mail
    now = int(time.time())
    ph = _ph()
    with db._cursor() as cursor:
        room = MAX_PER_HOUR - _sent_last_hour(cursor, now)
        if room <= 0:
            logging.warning("[email_notify] hourly cap reached — the rest wait")
            return 0
        cursor.execute(
            f"SELECT id, principal_id, email, subject, body, attempts, tag FROM email_outbox "
            f"WHERE status='pending' AND next_attempt<={ph} ORDER BY id LIMIT {ph}",
            (now, min(BATCH, room)))
        rows = [dict(r) if isinstance(r, dict) else dict(zip(
            ("id", "principal_id", "email", "subject", "body", "attempts", "tag"), r))
            for r in cursor.fetchall()]
    sent = 0
    deadline = time.monotonic() + budget_seconds
    for r in rows:
        if time.monotonic() > deadline:
            break
        # Unsubscribed (or switched off) after this was queued, or the sign-in
        # email changed since: honour it now. (A security notice is meant
        # for the address it was queued to — e.g. the OLD one, on a change.)
        if r.get("tag") != SECURITY and (get_pref(int(r["principal_id"])) == OFF
                                         or _current_email(int(r["principal_id"])) != r["email"]):
            with db._cursor(commit=True) as cursor:
                cursor.execute(f"UPDATE email_outbox SET status='skipped' WHERE id={ph}", (r["id"],))
            continue
        try:
            await send_mail(r["email"], r["subject"], r["body"], headers={
                "List-Unsubscribe": f"<{unsubscribe_url(r['principal_id'])}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            })
            with db._cursor(commit=True) as cursor:
                cursor.execute(f"UPDATE email_outbox SET status='sent', attempts=attempts+1, "
                               f"next_attempt={ph}, last_error=NULL WHERE id={ph}", (int(time.time()), r["id"]))
            sent += 1
        except Exception as e:
            attempts = int(r["attempts"]) + 1
            done = attempts >= MAX_ATTEMPTS
            wait = BACKOFF_MINUTES[min(attempts - 1, len(BACKOFF_MINUTES) - 1)] * 60
            logging.warning("[email_notify] send failed (attempt %s): %s", attempts, type(e).__name__)
            with db._cursor(commit=True) as cursor:
                cursor.execute(
                    f"UPDATE email_outbox SET status={ph}, attempts={ph}, next_attempt={ph}, last_error={ph} "
                    f"WHERE id={ph}",
                    ("failed" if done else "pending", attempts, int(time.time()) + wait,
                     str(e)[:300], r["id"]))
    if sent:
        logging.info("[email_notify] sent %d email(s)", sent)
    return sent


def forget(principal_id: int) -> None:
    """Drop everything queued or kept for this person (account deletion)."""
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM email_outbox WHERE principal_id={_ph()}", (principal_id,))


def prune(now: Optional[int] = None) -> int:
    now = int(now or time.time())
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM email_outbox WHERE created_at<{_ph()} AND status<>'pending'",
                       (now - KEEP_DAYS * 86400,))
        return cursor.rowcount or 0
