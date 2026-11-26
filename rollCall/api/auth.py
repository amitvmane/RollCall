"""
Bearer-token authentication for the REST API.

Tokens are issued out-of-band (CLI script in `scripts/issue_api_token.py`)
and presented by clients via the `Authorization: Bearer <token>` header.

Scopes:
  read   — read-only endpoints (GET rollcalls, GET single rollcall)
  vote   — voting + creating rollcalls (POST routes)
  admin  — destructive ops (DELETE rollcall) and future settings mutations

Tokens are scoped to a single chat (`chat_id` on the row). Routes that
take `chat_id` from the URL path also verify that the token's chat_id
matches — i.e. a token issued for chat A can't operate on chat B even
with the right scope.

Usage in a route:

    from api.auth import require_scope, AuthedToken

    @router.post(...)
    async def create_rollcall(
        ...,
        token: AuthedToken = Depends(require_scope("vote")),
    ):
        # token.chat_id is the chat the token is bound to
        ...
"""

from dataclasses import dataclass
from typing import List, Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db import _hash_token, lookup_api_token


# auto_error=False so we can return our own JSON error instead of
# FastAPI's default {"detail": "Not authenticated"} string.
_bearer_scheme = HTTPBearer(auto_error=False)


@dataclass
class AuthedToken:
    """The verified token's claims, attached to the request via Depends."""

    chat_id: int
    scopes: List[str]
    label: Optional[str]
    issued_by_user_id: Optional[int]
    # A partner app key (web page → Integrations): trusted to vote for any
    # member, like an admin, while issued_by_user_id stays the admin who made
    # it — so the issuer-still-admin re-check keeps applying.
    app_key: bool = False
    token_hash: Optional[str] = None
    expires_at: Optional[object] = None      # naive UTC datetime, or None
    allowed_ips: Optional[str] = None        # app keys: comma-separated CIDRs


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": 'Bearer realm="rollcall"'},
    )


def _forbidden(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


def _verify_token(creds: Optional[HTTPAuthorizationCredentials]) -> AuthedToken:
    """Look up the supplied bearer token, raising HTTPException on failure."""
    if creds is None or not creds.credentials:
        raise _unauthorized("Missing Authorization: Bearer <token> header")
    if creds.scheme.lower() != "bearer":
        raise _unauthorized("Authorization scheme must be 'Bearer'")

    token_hash = _hash_token(creds.credentials)
    row = lookup_api_token(token_hash)
    if row is None:
        # Don't distinguish "unknown" from "expired" from "revoked" — same
        # 401 for all so clients can't probe token state.
        raise _unauthorized("Invalid or expired token")

    return AuthedToken(
        chat_id=int(row["chat_id"]),
        scopes=row["scopes"],
        label=row.get("label"),
        issued_by_user_id=row.get("issued_by_user_id"),
        app_key=bool(int(row.get("app_key") or 0)),
        token_hash=token_hash,
        expires_at=row.get("expires_at_parsed"),
        allowed_ips=row.get("allowed_ips"),
    )


# A token minted for an admin carries the admin scope until it expires — but
# the person can stop being an admin long before that. Re-check the issuer's
# current web-admin status whenever an admin-scope token is used. Cached
# briefly so a burst of console requests isn't one Telegram call each.
_ADMIN_RECHECK_TTL_SECONDS = 60
_admin_recheck_cache: dict = {}


def reset_admin_recheck_cache_for_tests() -> None:
    _admin_recheck_cache.clear()


async def _issuer_still_admin(chat_id: int, user_id: int) -> bool:
    import time
    now = time.monotonic()
    hit = _admin_recheck_cache.get((chat_id, user_id))
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        from api.web_admin import check_web_admin_live
        ok = bool(await check_web_admin_live(chat_id, user_id))
    except Exception:
        # Fail closed: an admin-scope request we can't vouch for is refused.
        import logging
        logging.exception("admin-scope re-check failed chat=%s user=%s", chat_id, user_id)
        return False
    if len(_admin_recheck_cache) > 4096:
        _admin_recheck_cache.clear()
    _admin_recheck_cache[(chat_id, user_id)] = (now + _ADMIN_RECHECK_TTL_SECONDS, ok)
    return ok


def require_scope(scope: str):
    """
    Dependency factory: returns a FastAPI dependency that verifies a
    bearer token and asserts the token has the required scope. The
    chat_id-from-URL match check happens here too if `chat_id` is in
    the request path.

    Tokens with the `admin` scope implicitly satisfy any other scope —
    admin is a superset.
    """

    async def _dep(
        request: Request,
        creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    ) -> AuthedToken:
        token = _verify_token(creds)

        # A partner app key may be limited to the partner's own addresses
        # (services/partner_api). Checked before anything else is revealed.
        if token.app_key and token.allowed_ips:
            from services.partner_api import ip_allowed
            client_ip = request.client.host if request.client else None
            if not ip_allowed(token.allowed_ips, client_ip):
                raise _forbidden("This key can't be used from this address")

        # Admin-scope tokens only stay admin while their issuer does — and so
        # does EVERY app key: a vote key votes for any member, which is an
        # admin's power, and a read key shows the group to an outside app on
        # an admin's say-so. Tokens with no issuer (CLI-minted) or the global
        # chat_id=0 token have no person to re-check; a member's own
        # (non-app, non-admin) token is untouched.
        if (("admin" in token.scopes or token.app_key) and token.issued_by_user_id
                and token.chat_id != 0):
            if not await _issuer_still_admin(token.chat_id, int(token.issued_by_user_id)):
                raise _forbidden(
                    "The admin who issued this token is no longer an admin of this chat"
                    + (" — ask the group for a new key" if token.app_key else "")
                )

        # "any": endpoints about the key itself (its own usage) — every key
        # may see those, whatever it is scoped to.
        if scope != "any" and scope not in token.scopes and "admin" not in token.scopes:
            raise _forbidden(
                f"Token lacks required scope '{scope}' "
                f"(has: {','.join(token.scopes) or 'none'})"
            )

        # Cross-chat check: if the URL path includes chat_id, it must
        # match the token's bound chat_id. This stops a token issued for
        # chat A from operating on chat B even with the right scope.
        # Exception: a token issued with chat_id=0 is a global admin token
        # that may operate on any chat (used by the admin dashboard).
        path_chat_id = request.path_params.get("chat_id")
        if path_chat_id is not None and token.chat_id != 0:
            try:
                if int(path_chat_id) != token.chat_id:
                    raise _forbidden(
                        f"Token bound to chat {token.chat_id}, "
                        f"cannot operate on chat {path_chat_id}"
                    )
            except (TypeError, ValueError):
                # path_chat_id is non-int — FastAPI's int converter should
                # already reject this before us, but guard anyway.
                raise _forbidden("chat_id in URL is invalid")

        # Attach the token to the request so middleware (e.g. rate-limit)
        # can read it without a second DB lookup.
        request.state.api_token = token
        if token.app_key and token.token_hash:
            from services.partner_api import record_use
            record_use(token.token_hash, request.client.host if request.client else None)
        return token

    return _dep
