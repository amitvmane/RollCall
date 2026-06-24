"""
Telegram deep-link identity verification.

Flow:
  1. Browser calls POST /auth/tg-verify/start — gets a one-time code + t.me deep link.
  2. Browser opens the deep link; the bot receives /start v_{code}.
  3. Bot calls db.mark_web_verify_token(), associating the code with the Telegram user.
  4. Browser polls GET /auth/tg-verify/status/{code} every 2 s.
  5. When verified the browser gets {verified:true, user_id, name} and stores the identity.

Codes expire in 10 minutes and are single-use.
"""

import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Path, Request, status

import db as _db
from bot_state import _telegram_status
from api.schemas.tg_verify import TgVerifyStartResponse, TgVerifyStatusResponse

router = APIRouter()

_CODE_TTL_SECONDS = 600  # 10 minutes

# Strict per-IP rate limit for start: 5 req / 60 s (separate from global middleware)
_verify_buckets: dict = defaultdict(deque)
_VERIFY_WINDOW = 60
_VERIFY_MAX = 5


def _check_verify_rate(request: Request) -> None:
    client = request.client
    ip = client.host if client else "unknown"
    now = time.monotonic()
    bucket = _verify_buckets[ip]
    cutoff = now - _VERIFY_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= _VERIFY_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many verification requests — try again in a minute.",
            headers={"Retry-After": "60"},
        )
    bucket.append(now)


def _bot_username() -> str:
    """Return the bot @username (without @), or raise if not yet known."""
    raw = _telegram_status.get("bot_username") or ""
    return raw.lstrip("@")


@router.post(
    "/auth/tg-verify/start",
    response_model=TgVerifyStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a one-time Telegram deep-link to verify browser identity",
)
async def tg_verify_start(request: Request) -> TgVerifyStartResponse:
    _check_verify_rate(request)
    username = _bot_username()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot not connected to Telegram yet — try again in a moment",
        )

    code = secrets.token_hex(16)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_CODE_TTL_SECONDS)
    _db.create_web_verify_token(code, expires_at)

    deep_link = f"https://t.me/{username}?start=v_{code}"
    return TgVerifyStartResponse(
        code=code,
        deep_link=deep_link,
        expires_in=_CODE_TTL_SECONDS,
    )


@router.get(
    "/auth/tg-verify/status/{code}",
    response_model=TgVerifyStatusResponse,
    summary="Poll whether the Telegram deep-link verification has been completed",
)
async def tg_verify_status(
    code: str = Path(..., min_length=1, max_length=64),
) -> TgVerifyStatusResponse:
    row = _db.get_web_verify_token(code)
    if row is None:
        raise HTTPException(status_code=404, detail="Code not found or expired")

    if row.get("used_at"):
        raise HTTPException(status_code=410, detail="Code already used")

    if not row.get("tg_user_id"):
        return TgVerifyStatusResponse(verified=False)

    # Verified and not yet consumed — consume it now
    result = _db.consume_web_verify_token(code)
    if result is None:
        # Race condition: another poll consumed it between the get and consume.
        # Return not-verified; client will get a 410 on next poll.
        return TgVerifyStatusResponse(verified=False)

    # Identity is now cryptographically established (the user proved control of
    # the Telegram account via the deep link). Mint a signed token the client
    # can present on identity-sensitive endpoints.
    from api.identity import issue_identity_token, IdentityError
    try:
        id_token = issue_identity_token(int(result["tg_user_id"]))
    except IdentityError:
        id_token = None

    return TgVerifyStatusResponse(
        verified=True,
        user_id=result["tg_user_id"],
        name=result["tg_name"],
        id_token=id_token,
    )
