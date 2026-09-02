"""
Signed identity tokens for no-bearer web/portal endpoints.

Several public endpoints need to know *which* Telegram user is making the
request (portal stats, web-admin actions, real-user vote attribution) but
do not use the bearer-token scheme. Historically these trusted a raw
`tg_user_id` supplied by the client, which let anyone impersonate any
Telegram user simply by sending their numeric id.

An identity token is a compact HMAC-signed assertion of a verified user id.
It is minted only after the server has cryptographically established the
caller's Telegram identity — either by validating Mini App `initData`
(api/routes/auth.py) or by completing the deep-link tg-verify flow
(api/routes/tg_verify.py). The client stores it and presents it on
identity-sensitive calls; the server re-derives the user id from the
signature instead of trusting a client-supplied integer.

Format (all ASCII, URL-safe):

    <user_id>.<exp_unix>.<hex_sig>

where hex_sig = HMAC-SHA256(secret, "<user_id>.<exp_unix>"), under a context
label distinct from Telegram's own "WebAppData" derivation, so an identity
token can never be confused with — or forged from — a Telegram signature.

The secret comes from IDENTITY_SECRET when set, and falls back to the bot
token otherwise. That fallback is the historical behaviour and remains the
default, so deployments that set nothing are unaffected.

Why the env var exists: signing with the bot token makes this module require
Telegram in order to authenticate anyone. An app with no Telegram behind it
would have neither the signing key nor the user ids, so identity is the
piece that has to be decoupled first. IDENTITY_SECRET is that decoupling —
the code no longer *needs* a bot token to mint or verify.

Rotation without signing everyone out: verification accepts ANY configured
key, minting only ever uses the primary. So setting IDENTITY_SECRET on a
running deployment keeps existing tokens valid for the rest of their 30-day
life while new ones are issued under the new key. Once that window passes,
the bot token can be removed from the accepted set (or changed) with no user
impact. Without this, switching keys would silently sign out every signed-in
browser at once.

There is intentionally no DB lookup: verification is a single HMAC, so these
tokens are stateless and cheap. They are short-to-medium lived (30 days) and
cannot be individually revoked.
"""

import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import Header


# 30 days. These back a "stay verified" UX in the browser; long enough to
# avoid nagging re-verification, short enough to bound a leaked token.
IDENTITY_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


class IdentityError(RuntimeError):
    """Raised when the server cannot mint tokens (bot token unconfigured)."""


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_TOKEN") or os.environ.get("API_KEY", "")


def _identity_secret() -> str:
    """Operator-supplied signing key, independent of any chat platform."""
    return os.environ.get("IDENTITY_SECRET", "").strip()


def _derive(material: str) -> bytes:
    """Key-derivation under a dedicated context label.

    The label ("RollCallIdentityV1") rather than the raw material — or
    Telegram's "WebAppData" label — keeps this key cryptographically separate
    from the Mini App HMAC, so neither can be used to forge the other.
    """
    return hmac.new(b"RollCallIdentityV1", material.encode(), hashlib.sha256).digest()


def _signing_keys() -> list:
    """[primary, *also-accepted]. Minting uses [0]; verification tries all.

    Both are listed while IDENTITY_SECRET is set, which is what lets a
    deployment switch keys without invalidating the tokens already in
    people's browsers — see the module docstring.
    """
    keys = []
    env = _identity_secret()
    if env:
        keys.append(_derive(env))
    bot_token = _bot_token()
    if bot_token:
        keys.append(_derive(bot_token))
    if not keys:
        raise IdentityError(
            "Neither IDENTITY_SECRET nor TELEGRAM_TOKEN is configured — "
            "cannot sign identity tokens"
        )
    return keys


def _secret() -> bytes:
    return _signing_keys()[0]


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def issue_identity_token(user_id: int, ttl_seconds: int = IDENTITY_TOKEN_TTL_SECONDS) -> str:
    """Mint a signed identity token for a *already-verified* Telegram user id.

    Callers MUST have established the identity by other means (initData HMAC or
    tg-verify) before calling this — issuing a token does not itself verify
    anything.
    """
    user_id = int(user_id)
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"{user_id}.{exp}"
    return f"{payload}.{_sign(payload)}"


def verify_identity_token(token: Optional[str]) -> Optional[int]:
    """Return the verified user id if the token is valid and unexpired, else None.

    Never raises on malformed input — a bad token is simply unauthenticated.
    """
    return _verify(token, scope=None)


def issue_scoped_token(user_id: int, scope: str, ttl_seconds: int) -> str:
    """Like issue_identity_token, but bound to `scope` — only verify_scoped_token
    called with the SAME scope will accept it. Signs a different payload
    (scope is mixed into the HMAC input, not just carried alongside it), so
    a scoped token can't be replayed against verify_identity_token or a
    different scope's endpoints, and a generic identity token can't be used
    where a scoped one is required. Wire format is unchanged (still
    `<user_id>.<exp>.<sig>`) — only the signature differs.

    Use for short-lived, single-purpose tokens (e.g. the dues QR image URL)
    where a leak (browser history, access logs — it's embedded in a URL)
    should only be useful for that one purpose, not as a general-purpose
    identity credential for whatever's left of its TTL.
    """
    user_id = int(user_id)
    exp = int(time.time()) + int(ttl_seconds)
    payload = f"{scope}.{user_id}.{exp}"
    return f"{user_id}.{exp}.{_sign(payload)}"


def verify_scoped_token(token: Optional[str], scope: str) -> Optional[int]:
    """Counterpart to issue_scoped_token — verifies against the same scope."""
    return _verify(token, scope=scope)


def _verify(token: Optional[str], scope: Optional[str]) -> Optional[int]:
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    user_str, exp_str, sig = parts
    try:
        user_id = int(user_str)
        exp = int(exp_str)
    except (TypeError, ValueError):
        return None

    payload = f"{scope}.{user_id}.{exp}" if scope is not None else f"{user_id}.{exp}"
    try:
        keys = _signing_keys()
    except IdentityError:
        return None
    # Every configured key is checked, so a token minted under the previous
    # one stays valid for the rest of its life after a key change.
    #
    # Constant-time comparison so a timing side channel can't be used to
    # recover the valid signature byte by byte — and the result is OR-ed
    # across all keys without an early return, so the time taken doesn't
    # reveal WHICH key matched either.
    matched = False
    for key in keys:
        expected = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        matched |= hmac.compare_digest(expected, sig)
    if not matched:
        return None
    if exp < int(time.time()):
        return None
    return user_id


def require_identity(
    id_token: Optional[str],
    detail: str = "Verify with Telegram to use this feature.",
) -> int:
    """verify_identity_token, raising HTTP 401 instead of returning None.

    Was duplicated identically (module-local _require_identity) in
    api/routes/dues.py and api/routes/portal.py; each site's own 401
    message is preserved via the detail param.
    """
    from fastapi import HTTPException, status
    user_id = verify_identity_token(id_token)
    if not user_id or user_id <= 0:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    return user_id


def require_scoped_identity(
    id_token: Optional[str], scope: str,
    detail: str = "Verify with Telegram to use this feature.",
) -> int:
    """verify_scoped_token, raising HTTP 401 instead of returning None —
    the scoped counterpart to require_identity."""
    from fastapi import HTTPException, status
    user_id = verify_scoped_token(id_token, scope)
    if not user_id or user_id <= 0:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
    return user_id


async def identity_from_header(
    x_identity_token: Optional[str] = Header(None, alias="X-Identity-Token"),
) -> Optional[str]:
    """FastAPI dependency: extracts id_token from the X-Identity-Token
    header instead of a URL query param. A query param leaks a 30-day
    non-revocable credential via browser history, server access logs, and
    Referer headers on navigation away from the page — a header doesn't.

    Routes that previously declared `id_token: str = ""` (a query param)
    switch to `id_token: Optional[str] = Depends(identity_from_header)` —
    the downstream require_identity()/verify_identity_token() call is
    unchanged, since verification only cares about the string value, not
    where it came from.
    """
    return x_identity_token
