"""
Partner app keys — bearer tokens a group's admins make on the web page for
another app or service to use the REST API (docs/API.md).

Built on the existing api_tokens table and auth (api/auth.py): a key is a
token bound to ONE group, with scopes, an expiry, and app_key=1, which lets a
`vote`-scope key vote on behalf of any member (a member's own Mini App token
can only vote as them). The plaintext is shown once; only its hash is kept.

Who may make what: any web admin can make read/vote keys; an `admin`-scope key
(everything an admin can do, from outside) needs an owner. Keys stay tied to
the admin who made it — api/auth re-checks that they're still an admin
whenever an admin-scope key is used.
"""

from datetime import datetime, timedelta, timezone

import db
from exceptions import incorrectParameter, insufficientPermissions

SCOPES = ("read", "vote", "admin")
MAX_KEYS_PER_CHAT = 20
MAX_DAYS = 730


def _key_id(token_hash: str) -> str:
    return token_hash[:16]


def list_keys(chat_id: int) -> list:
    out = []
    for r in db.list_api_tokens(chat_id):
        if not int(r.get("app_key") or 0):
            continue
        out.append({
            "key_id": _key_id(r["token_hash"]),
            "label": r.get("label") or "",
            "scopes": r["scopes"] if isinstance(r["scopes"], list) else str(r["scopes"]).split(","),
            "created_at": str(r.get("created_at") or ""),
            "expires_at": str(r.get("expires_at") or ""),
            "last_used_at": str(r.get("last_used_at") or "") or None,
            "revoked": r.get("revoked_at") is not None,
        })
    return out


def create_key(chat_id: int, actor_user_id: int, actor_name: str, label: str, scopes, days: int) -> dict:
    scopes = sorted({s for s in (scopes or []) if s in SCOPES})
    if not scopes:
        raise incorrectParameter("Pick at least one scope: read, vote or admin.")
    if "admin" in scopes and db.get_web_admin_role(chat_id, actor_user_id) != "owner":
        raise insufficientPermissions("Only a group owner can make a key with the admin scope.")
    if not 1 <= int(days) <= MAX_DAYS:
        raise incorrectParameter(f"A key can last 1–{MAX_DAYS} days.")
    label = " ".join((label or "").split())[:60]
    if not label:
        raise incorrectParameter("Name the app this key is for.")
    if sum(1 for k in list_keys(chat_id) if not k["revoked"]) >= MAX_KEYS_PER_CHAT:
        raise incorrectParameter(f"A group can have at most {MAX_KEYS_PER_CHAT} active keys.")
    token = db.generate_api_token()
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=int(days))
    token_hash = db._hash_token(token)
    db.insert_api_token(token_hash, chat_id, ",".join(scopes), label=label,
                        issued_by_user_id=actor_user_id, expires_at=expires_at)
    with db._cursor(commit=True) as cursor:
        ph = "%s" if db.db_type == "postgresql" else "?"
        cursor.execute(f"UPDATE api_tokens SET app_key = 1 WHERE token_hash = {ph}", (token_hash,))
    db.log_admin_action(chat_id, actor_user_id, actor_name, "create_app_key",
                        target_name=label, details=",".join(scopes))
    return {"key_id": _key_id(token_hash), "token": token, "label": label, "scopes": scopes,
            "expires_at": expires_at.strftime("%Y-%m-%d")}


def revoke_key(chat_id: int, key_id: str, actor_user_id: int, actor_name: str) -> None:
    match = [r for r in db.list_api_tokens(chat_id)
             if int(r.get("app_key") or 0) and _key_id(r["token_hash"]) == key_id]
    if not match:
        raise incorrectParameter("No such key in this group.")
    db.revoke_api_token(match[0]["token_hash"])
    db.log_admin_action(chat_id, actor_user_id, actor_name, "revoke_app_key",
                        target_name=match[0].get("label") or key_id)
