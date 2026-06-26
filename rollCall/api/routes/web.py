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
from api.identity import verify_identity_token
from api.telegram_mirror import mirror_panel_to_telegram as _mirror_panel_to_telegram, send_vote_notification as _send_vote_notification
from services import web as web_svc
from services import stats as stats_svc
from services import presence as presence_svc
from services import push as push_svc
from api.schemas.web import (
    PushSubscribeRequest,
    PushUnsubscribeRequest,
    VapidPublicKeyResponse,
    WebAdminStatusResponse,
    WebEndRollcallRequest,
    WebGroupResponse,
    WebGroupSettingsRequest,
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
    id_token: Optional[str] = Query(None, description="Signed identity token to personalise the response with personal stats"),
) -> WebGroupStatsResponse:
    # Resolve the requesting identity from a signed token only — never from a
    # raw user_id so callers cannot supply an arbitrary Telegram id and read
    # another member's personal stats (IDOR).
    user_id = verify_identity_token(id_token) if id_token else None
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
    id_token: str = "",
) -> WebAdminStatusResponse:
    # Identity must be proven by a signed token; a raw user id can't grant
    # admin status because the server never trusts it.
    tg_user_id = verify_identity_token(id_token)
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat or not tg_user_id:
        return WebAdminStatusResponse(is_admin=False)
    return WebAdminStatusResponse(
        is_admin=_db.is_web_admin(int(chat["chat_id"]), tg_user_id)
    )


@router.patch(
    "/web/group/{group_token}/settings",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Update group settings (requires web-admin identity)",
)
async def update_group_settings(
    body: WebGroupSettingsRequest,
    group_token: str = Path(...),
) -> None:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = verify_identity_token(body.id_token)
    if not actor_user_id:
        raise HTTPException(status_code=401, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    if body.shh_mode is not None:
        from rollcall_manager import manager as _mgr
        _mgr.set_shh_mode(chat_id, body.shh_mode)


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

    # Resolve the actor from the signed identity token — never from a
    # client-supplied user id — before checking web-admin rights.
    actor_user_id = verify_identity_token(body.id_token)
    if not actor_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Verify with Telegram before starting a rollcall.",
        )

    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a web admin for this group. Run /weblink in Telegram first.",
        )

    from services import rollcalls as rc_svc
    from services.web import _serialize_web_rollcall
    from rollcall_manager import manager as _mgr
    result = await rc_svc.start_rollcall(
        chat_id=chat_id,
        title=body.title,
        started_by_user_id=actor_user_id,
        started_by_name="(web)",
    )
    rc = _mgr.get_rollcall(chat_id, result["rc_index"])
    if rc is None:
        raise HTTPException(status_code=500, detail="Rollcall created but could not be retrieved")

    # Post the panel into the Telegram group so a web-started rollcall is
    # visible and votable there too (best-effort — see helper).
    await _mirror_panel_to_telegram(chat_id, result["rc_index"] + 1, force_new=True)

    return WebRollcallResponse(**_serialize_web_rollcall(rc))


@router.post(
    "/web/group/{group_token}/end-rollcall",
    status_code=status.HTTP_200_OK,
    summary="End a rollcall via web (requires web-admin identity)",
)
async def web_end_rollcall(
    body: WebEndRollcallRequest,
    group_token: str = Path(...),
) -> dict:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")

    actor_user_id = verify_identity_token(body.id_token)
    if not actor_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Verify with Telegram before ending a rollcall.",
        )

    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a web admin for this group.",
        )

    from services import rollcalls as rc_svc
    from rollcall_manager import manager as _mgr

    rc_index = body.rollcall_num - 1
    async with _mgr.get_chat_write_lock(chat_id):
        result = await rc_svc.end_rollcall(
            chat_id=chat_id,
            rc_number=rc_index,
            ended_by_user_id=actor_user_id,
            ended_by_name="(web)",
        )

    rc_num_ended = result["rc_number_ended_1based"]
    await _mirror_panel_to_telegram(chat_id, rc_num_ended)

    return {"ended": result["rc_number_ended_1based"]}


# ── Scheduled rollcalls ───────────────────────────────────────────────────────

@router.post(
    "/web/group/{group_token}/scheduled-rollcalls",
    status_code=status.HTTP_201_CREATED,
    summary="Schedule a one-shot rollcall to auto-start at a future time (admin only)",
)
async def create_scheduled_rollcall(
    body: "ScheduledRollcallRequest",
    group_token: str = Path(...),
) -> dict:
    from api.schemas.web import ScheduledRollcallRequest as _Req
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = verify_identity_token(body.id_token)
    if not actor_user_id:
        raise HTTPException(status_code=401, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")

    # Basic ISO datetime validation
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", body.scheduled_at):
        raise HTTPException(status_code=422, detail="scheduled_at must be ISO 8601 datetime (e.g. 2026-07-01T09:00:00Z)")

    from db import upsert_chat_member as _upsert
    actor_name = "(web admin)"
    try:
        from db import get_member_display_info as _gmi
        info = _gmi(chat_id, actor_user_id)
        if info:
            actor_name = info.get("first_name") or actor_name
    except Exception:
        pass

    row_id = _db.create_scheduled_rollcall(
        chat_id=chat_id,
        title=body.title,
        scheduled_at=body.scheduled_at,
        created_by_uid=actor_user_id,
        created_by_name=actor_name,
    )
    return {"id": row_id, "title": body.title, "scheduled_at": body.scheduled_at}


@router.get(
    "/web/group/{group_token}/scheduled-rollcalls",
    summary="List upcoming scheduled rollcalls for a group (admin only)",
)
async def list_scheduled_rollcalls(
    group_token: str = Path(...),
    id_token: Optional[str] = Query(None),
) -> dict:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = verify_identity_token(id_token) if id_token else None
    if not actor_user_id:
        raise HTTPException(status_code=401, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    rows = _db.get_upcoming_scheduled_rollcalls(chat_id)
    return {
        "items": [
            {
                "id": r["id"],
                "title": r["title"],
                "scheduled_at": r["scheduled_at"],
                "created_by_name": r["created_by_name"],
            }
            for r in rows
        ]
    }


@router.delete(
    "/web/group/{group_token}/scheduled-rollcalls/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel a pending scheduled rollcall (admin only)",
)
async def delete_scheduled_rollcall(
    group_token: str = Path(...),
    item_id: int = Path(..., ge=1),
    id_token: Optional[str] = Query(None),
) -> None:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = verify_identity_token(id_token) if id_token else None
    if not actor_user_id:
        raise HTTPException(status_code=401, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    deleted = _db.delete_scheduled_rollcall(item_id, chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scheduled rollcall not found or already fired.")


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
    # Only attribute a vote to a real Telegram account when the caller proves
    # that identity with a signed token. Otherwise it's a name-only proxy entry,
    # so nobody can forge another member's attendance via the magic link.
    verified_user_id = verify_identity_token(body.id_token)
    data = await web_svc.vote_by_token(
        token, body.name, body.vote,
        tg_user_id=verified_user_id, comment=body.comment,
        username=body.username or None,
    )

    # Reflect the web vote in the Telegram group — notification so the vote is
    # visible in chat history, then panel update so the list stays current.
    loc = web_svc.locate_rollcall(token)
    if loc:
        await _send_vote_notification(loc[0], body.name, body.vote)
        await _mirror_panel_to_telegram(loc[0], loc[1])

    return WebRollcallResponse(**data)
