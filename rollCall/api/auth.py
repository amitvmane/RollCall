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

    row = lookup_api_token(_hash_token(creds.credentials))
    if row is None:
        # Don't distinguish "unknown" from "expired" from "revoked" — same
        # 401 for all so clients can't probe token state.
        raise _unauthorized("Invalid or expired token")

    return AuthedToken(
        chat_id=int(row["chat_id"]),
        scopes=row["scopes"],
        label=row.get("label"),
        issued_by_user_id=row.get("issued_by_user_id"),
    )


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

        if scope not in token.scopes and "admin" not in token.scopes:
            raise _forbidden(
                f"Token lacks required scope '{scope}' "
                f"(has: {','.join(token.scopes) or 'none'})"
            )

        # Cross-chat check: if the URL path includes chat_id, it must
        # match the token's bound chat_id. This stops a token issued for
        # chat A from operating on chat B even with the right scope.
        path_chat_id = request.path_params.get("chat_id")
        if path_chat_id is not None:
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
        return token

    return _dep
