"""
Public web voting routes — no bearer token required.

Per-rollcall token (expires with rollcall):
  GET  /api/v1/web/{token}          → fetch single rollcall state
  POST /api/v1/web/{token}/vote     → submit a vote (in/out/maybe)

Permanent group token (never expires, bookmarkable):
  GET  /api/v1/web/group/{token}    → fetch all active rollcalls for the group

Push notifications:
  GET  /api/v1/web/vapid-public-key              → VAPID public key for browser subscription
  POST /api/v1/web/group/{token}/push-subscribe  → register a push subscription
  POST /api/v1/web/group/{token}/push-unsubscribe → remove a push subscription
  GET  /api/v1/web/group/{token}/manifest.json   → dynamic PWA manifest
"""
import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, Response

import db as _db
from services import web as web_svc
from services import stats as stats_svc
from services import presence as presence_svc
from services import push as push_svc
from api.schemas.web import (
    PushSubscribeRequest,
    PushUnsubscribeRequest,
    VapidPublicKeyResponse,
    WebAdminStatusResponse,
    WebGroupResponse,
    WebGroupStatsResponse,
    WebHeartbeatRequest,
    WebPresenceResponse,
    WebRollcallResponse,
    WebStartRollcallRequest,
    WebVoteRequest,
    UpcomingRollcall,
)

router = APIRouter()


# ── VAPID / push endpoints ────────────────────────────────────────────────────

@router.get(
    "/web/vapid-public-key",
    response_model=VapidPublicKeyResponse,
    summary="Return the VAPID public key so browsers can subscribe to push",
)
async def vapid_public_key() -> VapidPublicKeyResponse:
    return VapidPublicKeyResponse(public_key=push_svc.get_public_key())


@router.post(
    "/web/group/{group_token}/push-subscribe",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Register a web-push subscription for this group",
)
async def push_subscribe(
    body: PushSubscribeRequest,
    group_token: str = Path(...),
) -> None:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(404, "Invalid group token")
    push_svc.subscribe(group_token, body.endpoint, body.keys.p256dh, body.keys.auth, tg_user_id=body.tg_user_id)


@router.post(
    "/web/group/{group_token}/push-unsubscribe",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a web-push subscription",
)
async def push_unsubscribe(
    body: PushUnsubscribeRequest,
    group_token: str = Path(...),
) -> None:
    push_svc.unsubscribe(body.endpoint)


@router.get(
    "/web/group/{group_token}/manifest.json",
    summary="Dynamic PWA manifest for this group",
    include_in_schema=False,
)
async def group_manifest(
    group_token: str = Path(...),
) -> Response:
    chat = _db.get_chat_by_group_web_token(group_token)
    group_name = (chat or {}).get("group_name") or "RollCall"
    web_base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    start_url = f"{web_base}/web/group/{group_token}" if web_base else f"/web/group/{group_token}"
    manifest = {
        "name": f"RollCall — {group_name}",
        "short_name": "RollCall",
        "description": f"Vote on rollcalls for {group_name}",
        "start_url": start_url,
        "scope": "/web/",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": "#2563eb",
        "background_color": "#f0f4f8",
        "icons": [
            {"src": "/web/logo.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"},
            {"src": "/web/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/web/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(
        content=json.dumps(manifest),
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-store"},
    )


# ── Group endpoint (permanent) ────────────────────────────────────────────────

@router.post(
    "/web/group/{group_token}/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Record viewer heartbeat (no auth) — increments view count on first visit",
)
async def web_group_heartbeat(
    body: WebHeartbeatRequest,
    group_token: str = Path(..., description="Permanent group token"),
) -> None:
    is_new = presence_svc.heartbeat(group_token, body.session_id)
    if is_new:
        _db.increment_group_view_count(group_token)


@router.get(
    "/web/group/{group_token}/presence",
    response_model=WebPresenceResponse,
    summary="Active viewers now + total views (no auth)",
)
async def web_group_presence(
    group_token: str = Path(..., description="Permanent group token"),
) -> WebPresenceResponse:
    return WebPresenceResponse(
        active_now=presence_svc.active_count(group_token),
        total_views=_db.get_group_view_count(group_token),
    )


@router.get(
    "/web/group/{group_token}/stats",
    response_model=WebGroupStatsResponse,
    summary="Get stats for a group via permanent token (no auth required)",
)
async def get_web_group_stats(
    group_token: str = Path(..., description="Permanent group token"),
    name: Optional[str] = Query(None, description="Display name to personalise the response with personal stats"),
    user_id: Optional[int] = Query(None, description="Telegram user_id to personalise the response"),
) -> WebGroupStatsResponse:
    data = stats_svc.web_group_stats(group_token, lookup_name=name, lookup_user_id=user_id)
    return WebGroupStatsResponse(**data)


@router.get(
    "/web/group/{group_token}",
    response_model=WebGroupResponse,
    summary="Get all active rollcalls for a group via permanent token",
)
async def get_web_group(
    group_token: str = Path(..., description="Permanent group token"),
) -> WebGroupResponse:
    data = web_svc.get_rollcalls_by_group_token(group_token)
    return WebGroupResponse(**data)


# ── Web admin endpoints ───────────────────────────────────────────────────────

@router.get(
    "/web/group/{group_token}/admin-status",
    response_model=WebAdminStatusResponse,
    summary="Check whether a verified Telegram user is a web admin for this group",
)
async def web_admin_status(
    group_token: str = Path(...),
    tg_user_id: int = 0,
) -> WebAdminStatusResponse:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat or not tg_user_id:
        return WebAdminStatusResponse(is_admin=False)
    return WebAdminStatusResponse(
        is_admin=_db.is_web_admin(int(chat["chat_id"]), tg_user_id)
    )


@router.post(
    "/web/group/{group_token}/start-rollcall",
    response_model=WebRollcallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a rollcall via web (requires web-admin identity)",
)
async def web_start_rollcall(
    body: WebStartRollcallRequest,
    group_token: str = Path(...),
) -> WebRollcallResponse:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")

    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, body.tg_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a web admin for this group. Run /weblink in Telegram first.",
        )

    from services import rollcalls as rc_svc
    result = await rc_svc.start_rollcall(
        chat_id=chat_id,
        title=body.title,
        started_by_user_id=body.tg_user_id,
        started_by_name="(web)",
    )
    return WebRollcallResponse(**result)


# ── Per-rollcall endpoints (expire with rollcall) ────────────────────────────

@router.get(
    "/web/{token}",
    response_model=WebRollcallResponse,
    summary="Get rollcall state via magic-link token",
)
async def get_web_rollcall(
    token: str = Path(..., description="Per-rollcall magic-link token"),
) -> WebRollcallResponse:
    data = web_svc.get_rollcall_by_token(token)
    return WebRollcallResponse(**data)


@router.post(
    "/web/{token}/vote",
    response_model=WebRollcallResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a vote via magic-link token",
)
async def vote_web(
    body: WebVoteRequest,
    token: str = Path(..., description="Per-rollcall magic-link token"),
) -> WebRollcallResponse:
    data = await web_svc.vote_by_token(token, body.name, body.vote, tg_user_id=body.tg_user_id, comment=body.comment)
    return WebRollcallResponse(**data)
