"""
Telegram identity verification — two independent browser flows.

Deep-link flow (works when the user has the Telegram app):
  1. Browser calls POST /auth/tg-verify/start — gets a one-time code + t.me deep link.
  2. Browser opens the deep link; the bot receives /start v_{code}.
  3. Bot calls db.mark_web_verify_token(), associating the code with the Telegram user.
  4. Browser polls GET /auth/tg-verify/status/{code} every 2 s.
  5. When verified the browser gets {verified:true, user_id, name} and stores the identity.

Codes expire in 10 minutes and are single-use.

Login Widget flow (works in any browser, no app or prior bot chat needed):
  1. Portal renders Telegram's official widget (bot username from GET /auth/tg-login/config).
  2. User authorizes in Telegram's OAuth popup; the widget hands the browser a signed
     payload {id, first_name, auth_date, hash, ...}.
  3. Browser POSTs it to /auth/tg-login; we verify the HMAC and mint the same
     signed id_token the deep-link flow issues.

NOTE: the widget only appears on domains registered with @BotFather via /setdomain.
"""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Path, Request, status

import db as _db
from bot_state import _telegram_status
from api.schemas.tg_verify import (
    MemberTokenLoginRequest,
    TgLoginConfigResponse,
    TgLoginRequest,
    TgVerifyStartResponse,
    TgVerifyStatusResponse,
)

router = APIRouter()

_CODE_TTL_SECONDS = 600  # 10 minutes

# Login Widget payloads are generated at click time; allow generous clock skew
# but reject stale replays.
_LOGIN_WIDGET_MAX_AGE_SECONDS = 3600  # 1 hour

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
        username=result.get("tg_username"),
        id_token=id_token,
    )


# ── Telegram Login Widget ─────────────────────────────────────────────────────

def _verify_login_widget(payload: dict, bot_token: str,
                         max_age: int = _LOGIN_WIDGET_MAX_AGE_SECONDS) -> dict:
    """
    Verify a Telegram Login Widget auth payload.

    Per https://core.telegram.org/widgets/login#checking-authorization:
      data_check_string = sorted "key=value" lines of all fields except hash
      secret_key        = SHA256(bot_token)   ← plain digest, NOT the
                          HMAC("WebAppData", …) used for Mini App initData
      valid iff HMAC-SHA256(secret_key, data_check_string) == hash

    Returns the verified payload dict. Raises ValueError with a user-safe
    message on any failure.
    """
    fields = {k: v for k, v in payload.items() if v is not None}
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing hash in login payload")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("Login signature verification failed")

    try:
        auth_date = int(fields.get("auth_date", 0))
    except (TypeError, ValueError):
        raise ValueError("Invalid auth_date in login payload")
    if time.time() - auth_date > max_age:
        raise ValueError("Login data is stale — please log in again")

    return fields


@router.get(
    "/auth/tg-login/config",
    response_model=TgLoginConfigResponse,
    summary="Bot username for rendering the Telegram Login Widget",
)
async def tg_login_config() -> TgLoginConfigResponse:
    username = _bot_username()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot not connected to Telegram yet — try again in a moment",
        )
    return TgLoginConfigResponse(bot_username=username)


@router.post(
    "/auth/tg-login",
    response_model=TgVerifyStatusResponse,
    summary="Verify a Telegram Login Widget payload and issue an identity token",
)
async def tg_login(body: TgLoginRequest, request: Request) -> TgVerifyStatusResponse:
    _check_verify_rate(request)

    bot_token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("API_KEY", "")
    if not bot_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot token not configured — cannot verify login",
        )

    try:
        _verify_login_widget(body.model_dump(), bot_token)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    name = body.first_name + (f" {body.last_name}" if body.last_name else "")

    from api.identity import issue_identity_token, IdentityError
    try:
        id_token = issue_identity_token(body.id)
    except IdentityError:
        id_token = None

    return TgVerifyStatusResponse(
        verified=True,
        user_id=body.id,
        name=name,
        username=body.username,
        id_token=id_token,
    )


# ── Persistent member login code (/mytoken) ──────────────────────────────────

@router.post(
    "/auth/member-token",
    response_model=TgVerifyStatusResponse,
    summary="Redeem a persistent /mytoken login code for an identity token",
)
async def member_token_login(body: MemberTokenLoginRequest, request: Request) -> TgVerifyStatusResponse:
    """Fully Telegram-independent login: the code was DM'd once by /mytoken
    and stored hashed; redeeming it maps to the tg_user_id and mints the same
    signed id_token as the deep-link / Login Widget flows. Reusable until the
    owner replaces or revokes it."""
    _check_verify_rate(request)

    token = body.token.strip()
    if not token or len(token) > 128:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid login code")

    from services.web import hash_login_token
    row = _db.get_member_login_token_by_hash(hash_login_token(token))
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid login code")

    user_id = int(row["user_id"])
    _db.touch_member_login_token(user_id)

    from api.identity import issue_identity_token, IdentityError
    try:
        id_token = issue_identity_token(user_id)
    except IdentityError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Identity signing not configured on the server",
        )

    return TgVerifyStatusResponse(
        verified=True,
        user_id=user_id,
        name=row.get("first_name"),
        username=row.get("username"),
        id_token=id_token,
    )
