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
from typing import List, Optional
from urllib.parse import parse_qsl, unquote

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from db import (
    _hash_token, generate_api_token, get_or_create_chat, get_user_voted_chats,
    get_web_admin_chats, insert_api_token, is_web_admin,
)

router = APIRouter()

_TOKEN_TTL_SECONDS = 3600  # 1 hour


class MiniAppAuthRequest(BaseModel):
    init_data: str


class MiniAppGroupSessionRequest(BaseModel):
    id_token: str
    chat_id: int


class MiniAppAuthResponse(BaseModel):
    token: str
    expires_in: int
    user_id: int
    chat_id: int
    # True only when initData actually carried a real chat object (Telegram
    # only populates this for attachment-menu-style launches). The button
    # this app currently ships with (a private-chat-only default menu
    # button, see runner.py) never gets one — chat_id in that case is a
    # last-resort fallback (the user's own id, i.e. their DM with the bot),
    # never a group with rollcalls in it. The client uses this flag to
    # decide whether to jump straight into that chat or show the
    # cross-group picker instead.
    chat_is_group: bool = False
    # Signed identity proof (see api/identity.py) for endpoints that key off
    # the user rather than a chat-scoped bearer token.
    id_token: Optional[str] = None
    # Permanent group web token + web-admin flag — lets the Mini App reuse
    # the exact same id_token-gated /web/group/{token}/... admin routes the
    # group web page already uses (e.g. settings), rather than needing a
    # parallel bearer-token-scoped admin surface.
    group_token: Optional[str] = None
    is_web_admin: bool = False
    timezone: str = "Asia/Kolkata"


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


def _extract_ids(pairs: dict) -> tuple[int, Optional[int], bool]:
    """Return (user_id, chat_id, chat_is_group) extracted from initData fields.

    chat_is_group is True only when a real `chat` object was present in
    initData — i.e. chat_id is trustworthy as an actual group/supergroup,
    not a fallback guess (receiver, or the user's own id for a private
    chat with the bot)."""
    user_id: Optional[int] = None
    chat_id: Optional[int] = None
    chat_is_group = False

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
            chat_is_group = True
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

    return user_id, chat_id, chat_is_group


def _mint_miniapp_session(chat_id: int, user_id: int, chat_is_group: bool) -> MiniAppAuthResponse:
    """Shared by both /auth/telegram/miniapp (initData) and
    /auth/telegram/miniapp/group (picking a group from the cross-group
    list) — everything past "we know the (chat_id, user_id) pair is
    legitimate" is identical between the two."""
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

    # A verified initData HMAC or a verified id_token just proved this
    # user's Telegram identity — mint a signed identity token alongside the
    # chat-scoped bearer token.
    from api.identity import issue_identity_token, IdentityError
    try:
        id_token = issue_identity_token(user_id)
    except IdentityError:
        id_token = None

    # Local var deliberately not named `timezone` — shadows the datetime.timezone
    # imported at module level (used just above for expires_at).
    chat_row = get_or_create_chat(chat_id)
    group_token = chat_row.get("group_web_token")
    chat_timezone = chat_row.get("timezone") or "Asia/Kolkata"
    web_admin = bool(is_web_admin(chat_id, user_id))

    logging.info(
        "[miniapp_auth] issued token chat=%s user=%s expires=%s",
        chat_id, user_id, expires_at.isoformat(),
    )

    return MiniAppAuthResponse(
        token=raw_token,
        expires_in=_TOKEN_TTL_SECONDS,
        user_id=user_id,
        chat_id=chat_id,
        chat_is_group=chat_is_group,
        id_token=id_token,
        group_token=group_token,
        is_web_admin=web_admin,
        timezone=chat_timezone,
    )


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
        user_id, chat_id, chat_is_group = _extract_ids(pairs)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )

    return _mint_miniapp_session(chat_id, user_id, chat_is_group)


@router.post(
    "/auth/telegram/miniapp/group",
    response_model=MiniAppAuthResponse,
    summary="Switch the Mini App's session into one of the user's groups",
    status_code=status.HTTP_201_CREATED,
)
async def miniapp_group_session(body: MiniAppGroupSessionRequest) -> MiniAppAuthResponse:
    """
    Mint a chat-scoped bearer token for a group the Mini App didn't open
    with real chat context for (see MiniAppAuthResponse.chat_is_group) —
    the client picked chat_id from the cross-group list at GET /portal/groups,
    keyed off the id_token /auth/telegram/miniapp already issued it.

    chat_id is client-supplied and cannot be trusted on its own, so it must
    independently match a chat this identity actually has standing in —
    voting history there, or a live web-admin grant — before a token gets
    minted for it. This mirrors how /portal/groups itself decides what to
    list in the first place.
    """
    from api.identity import require_identity
    user_id = require_identity(body.id_token, detail="Verify with Telegram to switch groups.")

    known = any(row["chat_id"] == body.chat_id for row in get_user_voted_chats(user_id))
    if not known and not is_web_admin(body.chat_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to that group.",
        )

    return _mint_miniapp_session(body.chat_id, user_id, chat_is_group=True)


class AdminGroupSummary(BaseModel):
    chat_id: int
    group_name: str
    group_web_token: Optional[str] = None


class AdminGroupsResponse(BaseModel):
    groups: List[AdminGroupSummary]


class AdminSessionRequest(BaseModel):
    id_token: str
    chat_id: int


class AdminSessionResponse(BaseModel):
    token: str
    chat_id: int
    group_name: str
    expires_in: int


_ADMIN_SESSION_TOKEN_TTL_SECONDS = 365 * 24 * 3600  # matches /gentoken's lifetime


@router.get(
    "/auth/admin/groups",
    response_model=AdminGroupsResponse,
    summary="List chats this verified Telegram user has cached web-admin status for",
)
async def admin_groups(id_token: str = "") -> AdminGroupsResponse:
    """
    Powers the admin console's Telegram-based sign-in: after the Login Widget
    proves identity, the console calls this to find which group(s) to offer —
    letting a returning admin skip /gentoken entirely. The list itself trusts
    the web_admins cache (fast, no per-candidate Telegram call); the actual
    grant is re-verified live in /auth/admin/session once a group is chosen.

    Returns an empty list for a genuinely first-time admin who has never
    triggered any web-admin cache write (no /weblink, no group web page
    visit) — /gentoken remains the bootstrap path for that case.
    """
    from api.identity import verify_identity_token
    tg_user_id = verify_identity_token(id_token)
    if not tg_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired identity token")

    chat_ids = get_web_admin_chats(tg_user_id)
    groups = []
    for cid in chat_ids:
        row = get_or_create_chat(cid)
        groups.append(AdminGroupSummary(
            chat_id=cid,
            group_name=row.get("group_name") or f"Chat {cid}",
            group_web_token=row.get("group_web_token"),
        ))
    return AdminGroupsResponse(groups=groups)


@router.post(
    "/auth/admin/session",
    response_model=AdminSessionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Exchange a verified Telegram identity for a chat-scoped admin bearer token",
)
async def admin_session(body: AdminSessionRequest) -> AdminSessionResponse:
    """
    Mints exactly the kind of token /gentoken does (scopes admin,read,vote,
    365-day expiry) — existing admin console REST calls and the bearer-token
    auth dependency need zero changes — but triggered by the Login Widget
    instead of a bot command, and gated by a LIVE re-check (not just the
    cache), so this can't be used to ride a stale grant.
    """
    from api.identity import verify_identity_token
    tg_user_id = verify_identity_token(body.id_token)
    if not tg_user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired identity token")

    from api.web_admin import check_web_admin_live
    if not await check_web_admin_live(body.chat_id, tg_user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not an admin of this group")

    chat_row = get_or_create_chat(body.chat_id)
    raw_token = generate_api_token()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=_ADMIN_SESSION_TOKEN_TTL_SECONDS)
    insert_api_token(
        token_hash=_hash_token(raw_token),
        chat_id=body.chat_id,
        scopes="admin,read,vote",
        label=f"web-signin:user={tg_user_id}",
        issued_by_user_id=tg_user_id,
        expires_at=expires_at,
    )
    logging.info("[admin_session] issued token chat=%s user=%s expires=%s", body.chat_id, tg_user_id, expires_at.isoformat())

    return AdminSessionResponse(
        token=raw_token,
        chat_id=body.chat_id,
        group_name=chat_row.get("group_name") or f"Chat {body.chat_id}",
        expires_in=_ADMIN_SESSION_TOKEN_TTL_SECONDS,
    )


_WEBLOGIN_ERROR_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Link expired — RollCall</title>
<style>body{{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f4f6fb}}
.card{{background:#fff;border-radius:16px;padding:36px;max-width:340px;text-align:center;box-shadow:0 2px 16px #0001}}
h1{{font-size:1.3rem;margin:0 0 12px}}p{{color:#555;font-size:.9rem;margin:0}}</style>
</head><body><div class="card"><h1>🔗 {title}</h1><p>{message}</p></div></body></html>"""


@router.get(
    "/auth/weblogin/{token}",
    summary="Redeem an admin-issued web login token",
)
async def weblogin_redeem(token: str):
    """
    Validate and consume a single-use admin-issued login token, then redirect to
    the group web page with a signed identity token so the user is authenticated
    without needing Telegram active.
    """
    import db as _db
    from api.identity import issue_identity_token, IdentityError

    payload = _db.consume_web_direct_login_token(token)
    if not payload:
        return HTMLResponse(
            content=_WEBLOGIN_ERROR_HTML.format(
                title="Link expired or already used",
                message="This login link has expired or was already used. Ask an admin to generate a new one with /weblogin.",
            ),
            status_code=410,
        )

    tg_user_id = payload["tg_user_id"]
    chat_id = payload["chat_id"]

    try:
        id_token = issue_identity_token(tg_user_id)
    except IdentityError:
        logging.error("[weblogin_redeem] cannot issue id_token — TELEGRAM_TOKEN not set")
        return HTMLResponse(
            content=_WEBLOGIN_ERROR_HTML.format(
                title="Server error",
                message="Could not issue identity token. Contact the group admin.",
            ),
            status_code=503,
        )

    # Resolve the group's permanent URL
    chat = _db.get_or_create_chat(chat_id)
    group_token = chat.get("group_web_token", "")
    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    if base and group_token:
        redirect_url = f"{base}/web/group/{group_token}?login_token={id_token}"
    elif group_token:
        # Relative redirect — works when client opened the link on the same origin
        redirect_url = f"/web/group/{group_token}?login_token={id_token}"
    else:
        return HTMLResponse(
            content=_WEBLOGIN_ERROR_HTML.format(
                title="Group not found",
                message="Could not locate the group page. Contact the group admin.",
            ),
            status_code=404,
        )

    logging.info(
        "[weblogin_redeem] redeemed token for user=%s chat=%s → %s",
        tg_user_id, chat_id, redirect_url.split("?")[0],
    )
    return RedirectResponse(url=redirect_url, status_code=302)
