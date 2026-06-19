"""
POST /auth/telegram/miniapp — validate Telegram Mini App initData, issue token.

Telegram Mini App auth flow:
  1. Client sends the raw initData string from window.Telegram.WebApp.initData
  2. We verify the HMAC using HMAC-SHA256(data_check_string, SHA256(bot_token))
  3. We check auth_date is within the last 24 hours
  4. We extract user_id and chat_id from initData
  5. We insert a short-lived (1-hour) DB token with scope=vote bound to that chat
  6. Return the raw token — client uses it as Bearer for all REST calls

The returned token is compatible with the existing require_scope("vote") dependency
so every existing route works for Mini App users with no changes.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, unquote

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from db import _hash_token, generate_api_token, insert_api_token

router = APIRouter()

_TOKEN_TTL_SECONDS = 3600  # 1 hour


class MiniAppAuthRequest(BaseModel):
    init_data: str


class MiniAppAuthResponse(BaseModel):
    token: str
    expires_in: int
    user_id: int
    chat_id: int


def _validate_init_data(init_data: str, bot_token: str) -> dict:
    """
    Validate Telegram WebApp initData HMAC and return parsed fields.

    Raises ValueError with a user-safe message on any validation failure.
    """
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing hash in initData")

    # Build the data check string: sorted key=value pairs joined by newline
    data_check_string = "\n".join(
        f"{k}={v}" for k, v in sorted(pairs.items())
    )

    # secret_key = HMAC-SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode(),
        hashlib.sha256,
    ).digest()

    expected = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, received_hash):
        raise ValueError("initData HMAC verification failed")

    # Reject stale initData (>24 h old)
    auth_date = pairs.get("auth_date")
    if auth_date:
        try:
            age = int(time.time()) - int(auth_date)
            if age > 86_400:
                raise ValueError("initData is older than 24 hours")
        except (TypeError, ValueError) as exc:
            if "older than 24" in str(exc):
                raise
            raise ValueError("Invalid auth_date in initData") from exc

    return pairs


def _extract_ids(pairs: dict) -> tuple[int, Optional[int]]:
    """Return (user_id, chat_id) extracted from initData fields."""
    user_id: Optional[int] = None
    chat_id: Optional[int] = None

    user_str = pairs.get("user")
    if user_str:
        try:
            user_obj = json.loads(unquote(user_str))
            user_id = int(user_obj["id"])
        except Exception:
            pass

    chat_str = pairs.get("chat")
    if chat_str:
        try:
            chat_obj = json.loads(unquote(chat_str))
            chat_id = int(chat_obj["id"])
        except Exception:
            pass

    # Fallback: receiver_id is the user in private chats (bot DMs)
    if chat_id is None:
        receiver_str = pairs.get("receiver")
        if receiver_str:
            try:
                recv_obj = json.loads(unquote(receiver_str))
                chat_id = int(recv_obj["id"])
            except Exception:
                pass

    # Last resort: use user_id as chat_id (private chat with same id)
    if chat_id is None and user_id is not None:
        chat_id = user_id

    if user_id is None:
        raise ValueError("Could not extract user_id from initData")
    if chat_id is None:
        raise ValueError("Could not extract chat_id from initData")

    return user_id, chat_id


@router.post(
    "/auth/telegram/miniapp",
    response_model=MiniAppAuthResponse,
    summary="Exchange Telegram Mini App initData for a bearer token",
    status_code=status.HTTP_201_CREATED,
)
async def miniapp_auth(body: MiniAppAuthRequest) -> MiniAppAuthResponse:
    """
    Validate Telegram WebApp initData and issue a short-lived (1h) bearer token
    scoped to the chat the Mini App was opened from.

    The returned token can be used as `Authorization: Bearer <token>` on any
    REST endpoint that requires the `vote` scope.
    """
    bot_token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("API_KEY", "")
    if not bot_token:
        logging.error("[miniapp_auth] TELEGRAM_TOKEN not set — cannot validate initData")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot token not configured on server",
        )

    try:
        pairs = _validate_init_data(body.init_data, bot_token)
        user_id, chat_id = _extract_ids(pairs)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    raw_token = generate_api_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_TOKEN_TTL_SECONDS)

    insert_api_token(
        token_hash=_hash_token(raw_token),
        chat_id=chat_id,
        scopes="read,vote",
        label=f"miniapp:user={user_id}",
        issued_by_user_id=user_id,
        expires_at=expires_at,
    )

    logging.info(
        "[miniapp_auth] issued token chat=%s user=%s expires=%s",
        chat_id, user_id, expires_at.isoformat(),
    )

    return MiniAppAuthResponse(
        token=raw_token,
        expires_in=_TOKEN_TTL_SECONDS,
        user_id=user_id,
        chat_id=chat_id,
    )
