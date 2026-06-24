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

where hex_sig = HMAC-SHA256(secret, "<user_id>.<exp_unix>"). The secret is
derived from the bot token (already a server-only secret) under a context
label distinct from Telegram's own "WebAppData" derivation, so an identity
token can never be confused with — or forged from — a Telegram signature.

There is intentionally no DB lookup: verification is a single HMAC, so these
tokens are stateless and cheap. They are short-to-medium lived (30 days) and
cannot be individually revoked; rotating the bot token invalidates all of
them at once, which is the same blast radius as every other bot secret.
"""

import hashlib
import hmac
import os
import time
from typing import Optional


# 30 days. These back a "stay verified" UX in the browser; long enough to
# avoid nagging re-verification, short enough to bound a leaked token.
IDENTITY_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


class IdentityError(RuntimeError):
    """Raised when the server cannot mint tokens (bot token unconfigured)."""


def _bot_token() -> str:
    return os.environ.get("TELEGRAM_TOKEN") or os.environ.get("API_KEY", "")


def _secret() -> bytes:
    """Derive the signing key from the bot token under a dedicated context.

    Using a distinct label ("RollCallIdentityV1") rather than the raw bot
    token — or Telegram's "WebAppData" label — keeps this key cryptographically
    separate from the Mini App HMAC, so neither can be used to forge the other.
    """
    bot_token = _bot_token()
    if not bot_token:
        raise IdentityError("TELEGRAM_TOKEN not configured — cannot sign identity tokens")
    return hmac.new(b"RollCallIdentityV1", bot_token.encode(), hashlib.sha256).digest()


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

    payload = f"{user_id}.{exp}"
    try:
        expected = _sign(payload)
    except IdentityError:
        return None
    # Constant-time comparison so a timing side channel can't be used to
    # recover the valid signature byte by byte.
    if not hmac.compare_digest(expected, sig):
        return None
    if exp < int(time.time()):
        return None
    return user_id
