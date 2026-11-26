"""
Partner app keys — bearer tokens a group's admins make on the web page for
another app or service to use the REST API (docs/API.md).

Built on the existing api_tokens table and auth (api/auth.py): a key is a
token bound to ONE group, with scopes, an expiry, and app_key=1, which lets a
`vote`-scope key vote on behalf of any member (a member's own Mini App token
can only vote as them). The plaintext is shown once; only its hash is kept.

Keys are issued the way a bank issues API credentials to a partner: under an
agreement made off the platform. Each key records who the partner is, who to
contact, the agreement's reference, and optionally the addresses it may be
used from (an IP allowlist, enforced in api/auth). It always expires (1–730
days); its issuer and the group's owners are warned 14 and 3 days before
(notify_expiring, daily); and it can be rotated — a new key with the same
terms, the old one kept working for a short grace period so the partner can
switch without downtime.

Who may make what: any web admin can make read/vote keys; an `admin`-scope key
(everything an admin can do, from outside) needs an owner. Every key stays
tied to the admin who made it — api/auth re-checks that they're still an admin
of the group whenever ANY app key is used, so a key dies with its issuer's
standing (a vote key votes for anyone, which is an admin's power).
"""

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
from exceptions import incorrectParameter, insufficientPermissions

SCOPES = ("read", "vote", "admin")
MAX_KEYS_PER_CHAT = 20
MAX_DAYS = 730
MAX_ALLOWED_NETWORKS = 20
ROTATE_GRACE_DAYS = 7
EXPIRY_NOTICE_DAYS = (14, 3)


def _key_id(token_hash: str) -> str:
    return token_hash[:16]


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean(text: Optional[str], limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def parse_allowed_ips(value) -> list:
    """'203.0.113.0/24, 198.51.100.7' or a list → normalised CIDR strings.
    Private ranges are allowed (a partner on the same network is legitimate);
    malformed input and a catch-all /0 are refused."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    out = []
    for raw in items:
        raw = str(raw).strip()
        if not raw:
            continue
        try:
            net = ipaddress.ip_network(raw, strict=False)
        except ValueError:
            raise incorrectParameter(f"'{raw[:50]}' isn't an IP address or range (e.g. 203.0.113.0/24).")
        if net.prefixlen == 0:
            raise incorrectParameter("An allowlist of 'everywhere' is no allowlist — leave it empty instead.")
        out.append(str(net))
    if len(out) > MAX_ALLOWED_NETWORKS:
        raise incorrectParameter(f"At most {MAX_ALLOWED_NETWORKS} addresses or ranges per key.")
    return sorted(set(out))


def ip_allowed(allowed: Optional[str], client_ip: Optional[str]) -> bool:
    """True if the key has no allowlist, or client_ip is inside it."""
    nets = [n for n in (allowed or "").split(",") if n.strip()]
    if not nets:
        return True
    try:
        ip = ipaddress.ip_address((client_ip or "").strip())
    except ValueError:
        return False
    for n in nets:
        try:
            if ip in ipaddress.ip_network(n.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False


def _status(r: dict) -> str:
    if r.get("revoked_at") is not None:
        return "revoked"
    exp = _as_dt(r.get("expires_at"))
    if exp is not None and exp < _now():
        return "expired"
    return "active"


def _as_dt(v) -> Optional[datetime]:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else v.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d.replace(tzinfo=None) if d.tzinfo is None else d.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _is_personal(r: dict) -> bool:
    """A /gentoken admin token: not an app key, but just as powerful and just
    as able to leave the group — so owners see and can revoke it here too."""
    return not int(r.get("app_key") or 0) and "/gentoken" in str(r.get("label") or "")


def list_keys(chat_id: int) -> list:
    out = []
    for r in db.list_api_tokens(chat_id):
        app = bool(int(r.get("app_key") or 0))
        if not app and not _is_personal(r):
            continue
        out.append({
            "key_id": _key_id(r["token_hash"]),
            "kind": "app" if app else "personal",
            "label": r.get("label") or "",
            "scopes": r["scopes"] if isinstance(r["scopes"], list) else str(r["scopes"]).split(","),
            "created_at": str(r.get("created_at") or ""),
            "expires_at": str(r.get("expires_at") or ""),
            "last_used_at": str(r.get("last_used_at") or "") or None,
            "last_used_ip": r.get("last_used_ip") or None,
            "revoked": r.get("revoked_at") is not None,
            "status": _status(r),
            "partner": r.get("partner") or None,
            "contact": r.get("contact") or None,
            "agreement_ref": r.get("agreement_ref") or None,
            "allowed_ips": [n for n in (r.get("allowed_ips") or "").split(",") if n],
            "issued_by_user_id": r.get("issued_by_user_id"),
            "rotated_to": r.get("rotated_to") or None,
        })
    return out


def _active_count(chat_id: int) -> int:
    return sum(1 for k in list_keys(chat_id) if k["kind"] == "app" and k["status"] == "active")


def _validate_contact(contact: Optional[str]) -> Optional[str]:
    contact = _clean(contact, 254)
    if not contact:
        return None
    if "@" in contact:
        from services.email_auth import validate_email
        return validate_email(contact)
    return contact


def create_key(chat_id: int, actor_user_id: int, actor_name: str, label: str, scopes, days: int, *,
               partner: Optional[str] = None, contact: Optional[str] = None,
               agreement_ref: Optional[str] = None, allowed_ips=None) -> dict:
    scopes = sorted({s for s in (scopes or []) if s in SCOPES})
    if not scopes:
        raise incorrectParameter("Pick at least one scope: read, vote or admin.")
    if "admin" in scopes and db.get_web_admin_role(chat_id, actor_user_id) != "owner":
        raise insufficientPermissions("Only a group owner can make a key with the admin scope.")
    if not 1 <= int(days) <= MAX_DAYS:
        raise incorrectParameter(f"A key can last 1–{MAX_DAYS} days.")
    label = _clean(label, 60)
    if not label:
        raise incorrectParameter("Name the app this key is for.")
    partner = _clean(partner, 100) or None
    contact = _validate_contact(contact)
    agreement_ref = _clean(agreement_ref, 100) or None
    nets = parse_allowed_ips(allowed_ips)
    if _active_count(chat_id) >= MAX_KEYS_PER_CHAT:
        raise incorrectParameter(f"A group can have at most {MAX_KEYS_PER_CHAT} active keys.")
    token = db.generate_api_token()
    expires_at = _now() + timedelta(days=int(days))
    token_hash = db._hash_token(token)
    db.insert_api_token(token_hash, chat_id, ",".join(scopes), label=label,
                        issued_by_user_id=actor_user_id, expires_at=expires_at)
    ph = _ph()
    with db._cursor(commit=True) as cursor:
        cursor.execute(
            f"UPDATE api_tokens SET app_key=1, partner={ph}, contact={ph}, agreement_ref={ph}, "
            f"allowed_ips={ph} WHERE token_hash={ph}",
            (partner, contact, agreement_ref, ",".join(nets) or None, token_hash))
    db.log_admin_action(chat_id, actor_user_id, actor_name, "create_app_key",
                        target_name=label,
                        details=",".join(scopes) + (f" · {agreement_ref}" if agreement_ref else ""))
    return {"key_id": _key_id(token_hash), "token": token, "label": label, "scopes": scopes,
            "expires_at": expires_at.strftime("%Y-%m-%d"), "partner": partner, "contact": contact,
            "agreement_ref": agreement_ref, "allowed_ips": nets}


def _find(chat_id: int, key_id: str) -> dict:
    match = [r for r in db.list_api_tokens(chat_id)
             if (int(r.get("app_key") or 0) or _is_personal(r)) and _key_id(r["token_hash"]) == key_id]
    if not match:
        raise incorrectParameter("No such key in this group.")
    return match[0]


def revoke_key(chat_id: int, key_id: str, actor_user_id: int, actor_name: str) -> None:
    row = _find(chat_id, key_id)
    db.revoke_api_token(row["token_hash"])
    db.log_admin_action(chat_id, actor_user_id, actor_name, "revoke_app_key",
                        target_name=row.get("label") or key_id)


def rotate_key(chat_id: int, key_id: str, actor_user_id: int, actor_name: str,
               days: Optional[int] = None) -> dict:
    """A new key on the same terms; the old one keeps working for
    ROTATE_GRACE_DAYS (or until it would have expired, if sooner) so the
    partner can deploy the new one without an outage."""
    row = _find(chat_id, key_id)
    if not int(row.get("app_key") or 0):
        raise incorrectParameter("Only app keys can be rotated — revoke this one and run /gentoken again.")
    if _status(row) != "active":
        raise incorrectParameter("This key is no longer active — make a new one instead.")
    if row.get("rotated_to"):
        raise incorrectParameter("This key was already rotated — rotate the new one instead.")
    scopes = row["scopes"] if isinstance(row["scopes"], list) else str(row["scopes"]).split(",")
    if days is None:
        created, exp = _as_dt(row.get("created_at")), _as_dt(row.get("expires_at"))
        days = max(1, min(MAX_DAYS, (exp - created).days)) if created and exp else 365
    new = create_key(chat_id, actor_user_id, actor_name, row.get("label") or "app", scopes, days,
                     partner=row.get("partner"), contact=row.get("contact"),
                     agreement_ref=row.get("agreement_ref"), allowed_ips=row.get("allowed_ips"))
    grace_end = min(_as_dt(row.get("expires_at")) or _now(), _now() + timedelta(days=ROTATE_GRACE_DAYS))
    ph = _ph()
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"UPDATE api_tokens SET expires_at={ph}, rotated_to={ph} WHERE token_hash={ph}",
                       (grace_end, new["key_id"], row["token_hash"]))
    db.log_admin_action(chat_id, actor_user_id, actor_name, "rotate_app_key",
                        target_name=row.get("label") or key_id, details=f"→ {new['key_id']}")
    return {**new, "previous_key_id": key_id, "previous_expires_at": grace_end.strftime("%Y-%m-%d %H:%M UTC")}


def revoke_issued_by(user_id: int) -> int:
    """Revoke every API token a person issued, in every group (account deletion)."""
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"UPDATE api_tokens SET revoked_at=CURRENT_TIMESTAMP "
                       f"WHERE issued_by_user_id={_ph()} AND revoked_at IS NULL", (user_id,))
        return cursor.rowcount or 0


def record_use(token_hash: str, client_ip: Optional[str]) -> None:
    """Remember where a key was last used from (shown to admins). Best effort."""
    if not client_ip:
        return
    try:
        with db._cursor(commit=True) as cursor:
            cursor.execute(f"UPDATE api_tokens SET last_used_ip={_ph()} WHERE token_hash={_ph()} "
                           f"AND (last_used_ip IS NULL OR last_used_ip<>{_ph()})",
                           (client_ip[:64], token_hash, client_ip[:64]))
    except Exception:
        logging.debug("partner_api.record_use failed", exc_info=True)


async def notify_expiring() -> int:
    """Warn the issuer and the group's owners when an app key is 14, then 3,
    days from expiring — once each (expiry_notice). Daily job. Never raises."""
    sent = 0
    try:
        ph = _ph()
        now = _now()
        horizon = now + timedelta(days=max(EXPIRY_NOTICE_DAYS))
        with db._cursor() as cursor:
            cursor.execute(
                f"SELECT token_hash, chat_id, label, partner, expires_at, issued_by_user_id, expiry_notice "
                f"FROM api_tokens WHERE app_key=1 AND revoked_at IS NULL AND rotated_to IS NULL "
                f"AND expires_at IS NOT NULL AND expires_at>{ph} AND expires_at<={ph}", (now, horizon))
            rows = [dict(r) for r in cursor.fetchall()]
        from services.push import notify_users
        for r in rows:
            exp = _as_dt(r["expires_at"])
            if exp is None:
                continue
            left = max(0, (exp - now).days)
            due = min((d for d in EXPIRY_NOTICE_DAYS if left <= d), default=None)
            if due is None or (r.get("expiry_notice") is not None and int(r["expiry_notice"]) <= due):
                continue
            chat_id = int(r["chat_id"])
            who = {int(a["tg_user_id"]) for a in db.list_web_admins(chat_id) if a.get("role") == "owner"}
            if r.get("issued_by_user_id"):
                who.add(int(r["issued_by_user_id"]))
            name = r.get("partner") or r.get("label") or "an app"
            when = "tomorrow" if left <= 1 else f"in {left} days"
            await notify_users(chat_id, who, "🔑 App key expiring",
                               f"The key for {name} stops working {when} ({exp:%d %b %Y}). "
                               "Rotate it under Admin → Integrations and give the partner the new key.",
                               "key-expiry")
            # Telegram members hear it by DM too (local ids are refused by
            # chat_outbox's guard anyway; skipped here to keep logs quiet).
            try:
                from bot_state import bot
                from identity_space import is_local_user
                for uid in who:
                    if not is_local_user(uid):
                        try:
                            await bot.send_message(uid, f"🔑 The RollCall app key for {name} stops working "
                                                        f"{when}. Rotate it on the group's web page → Admin "
                                                        "→ Integrations.")
                        except Exception:
                            pass
            except Exception:
                logging.debug("key expiry DM skipped", exc_info=True)
            with db._cursor(commit=True) as cursor:
                cursor.execute(f"UPDATE api_tokens SET expiry_notice={ph} WHERE token_hash={ph}",
                               (due, r["token_hash"]))
            sent += 1
    except Exception:
        logging.exception("partner_api.notify_expiring failed")
    return sent
