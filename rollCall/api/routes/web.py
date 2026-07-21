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
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import db as _db
from api.identity import require_identity, verify_identity_token
from api.telegram_mirror import mirror_panel_to_telegram as _mirror_panel_to_telegram, send_vote_notification as _send_vote_notification, send_event_notification as _send_event_notification
from services import web as web_svc
from services import stats as stats_svc
from services import presence as presence_svc
from services import push as push_svc
from api.schemas.web import (
    PushSubscribeRequest,
    PushUnsubscribeRequest,
    ScheduledRollcallCreateResponse,
    ScheduledRollcallItem,
    ScheduledRollcallsResponse,
    VapidPublicKeyResponse,
    WebAdminStatusResponse,
    WebEndRollcallRequest,
    WebEndRollcallResponse,
    WebGroupResponse,
    WebGroupSettingsRequest,
    WebGroupStatsResponse,
    WebHeartbeatRequest,
    WebPresenceResponse,
    WebProxyVoteRequest,
    WebRollcallResponse,
    WebSetScheduleRequest,
    WebStartRollcallRequest,
    WebStartTemplateRequest,
    WebTemplateResponse,
    WebToggleScheduleRequest,
    WebUpdateTemplateRequest,
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
    chat_id = int(chat["chat_id"])

    # Live-check against Telegram on every load instead of trusting a
    # snapshot forever: promotes a real Telegram admin automatically (no
    # /weblink needed — that command still exists and still calls
    # set_web_admin as a convenience for command-line users, but it's no
    # longer the only way in) and revokes anyone who's lost their admin
    # role since the cache was last set.
    try:
        from bot_state import bot
        member = await bot.get_chat_member(chat_id, tg_user_id)
        is_admin_now = member.status in ("administrator", "creator")
        if is_admin_now:
            name = getattr(getattr(member, "user", None), "first_name", None) or f"user{tg_user_id}"
            _db.set_web_admin(chat_id, tg_user_id, name)
        else:
            _db.revoke_web_admin(chat_id, tg_user_id)
        return WebAdminStatusResponse(is_admin=is_admin_now)
    except Exception:
        # Telegram unreachable, bot removed from the group, rate-limited,
        # etc. — fall back to the cached flag rather than locking an admin
        # out of the one surface that's supposed to keep working when
        # Telegram itself is down.
        logging.warning(
            "[web_admin_status] live check failed chat=%s user=%s — using cached value",
            chat_id, tg_user_id, exc_info=True,
        )
        return WebAdminStatusResponse(is_admin=_db.is_web_admin(chat_id, tg_user_id))


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
    actor_user_id = require_identity(body.id_token, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    if body.shh_mode is not None:
        from rollcall_manager import manager as _mgr
        _mgr.set_shh_mode(chat_id, body.shh_mode)
        actor_name = "(web admin)"
        try:
            from db import get_member_display_info as _gmi
            info = _gmi(chat_id, actor_user_id)
            if info:
                actor_name = info.get("first_name") or actor_name
        except Exception:
            pass
        _icon = "🔇" if body.shh_mode else "🔔"
        _desc = "enabled — per-vote messages suppressed" if body.shh_mode else "disabled — per-vote messages active"
        await _send_event_notification(
            chat_id,
            f"{_icon} Silent mode {_desc} (by {actor_name}, via web)",
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

    # Resolve the actor from the signed identity token — never from a
    # client-supplied user id — before checking web-admin rights.
    actor_user_id = require_identity(
        body.id_token, detail="Verify with Telegram before starting a rollcall."
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
    response_model=WebEndRollcallResponse,
    summary="End a rollcall via web (requires web-admin identity)",
)
async def web_end_rollcall(
    body: WebEndRollcallRequest,
    group_token: str = Path(...),
) -> WebEndRollcallResponse:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")

    actor_user_id = require_identity(
        body.id_token, detail="Verify with Telegram before ending a rollcall."
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

    return WebEndRollcallResponse(ended=result["rc_number_ended_1based"])


@router.post(
    "/web/group/{group_token}/proxy-vote",
    response_model=WebRollcallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cast a proxy vote for a non-Telegram member (requires web-admin identity)",
)
async def web_proxy_vote(
    body: WebProxyVoteRequest,
    group_token: str = Path(...),
) -> WebRollcallResponse:
    """Web parity for /sif /sof /smf — a verified web admin votes on behalf
    of a member who isn't on Telegram (guest, +1, etc.)."""
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")

    actor_user_id = require_identity(
        body.id_token, detail="Verify with Telegram before casting proxy votes."
    )

    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a web admin for this group.",
        )

    actor_name = "(web admin)"
    try:
        info = _db.get_member_display_info(chat_id, actor_user_id)
        if info:
            actor_name = info.get("first_name") or actor_name
    except Exception:
        pass

    from services import proxy as proxy_svc
    common = dict(
        chat_id=chat_id,
        admin_user_id=actor_user_id,
        admin_name=actor_name,
        proxy_name=body.proxy_name,
        comment=body.comment,
        rc_number=body.rollcall_num - 1,
    )
    if body.vote == "in":
        await proxy_svc.set_in_for(**common)
    elif body.vote == "out":
        await proxy_svc.set_out_for(**common)
    else:
        await proxy_svc.set_maybe_for(**common)

    await _send_vote_notification(chat_id, body.proxy_name, body.vote)
    await _mirror_panel_to_telegram(chat_id, body.rollcall_num)

    from rollcall_manager import manager as _mgr
    from services.web import _serialize_web_rollcall
    rc = _mgr.get_rollcall(chat_id, body.rollcall_num - 1)
    if rc is None:
        raise HTTPException(status_code=404, detail="Rollcall not found after vote")
    return WebRollcallResponse(**_serialize_web_rollcall(rc))


# ── Recurring template schedules ─────────────────────────────────────────────
# Self-serve (id_token + is_web_admin), unlike /admin/'s bearer-API-token
# routes in api/routes/templates.py which need out-of-band `make token` —
# a real bottleneck for any group admin who isn't the server operator. Any
# web admin (auto-registered by running /weblink in their group) can edit
# their own group's recurring schedules here without ever touching the
# server. Wraps the same services.templates functions the /schedule_template
# Telegram command and the token-gated REST routes already call.

def _require_web_admin(group_token: str, id_token: str) -> tuple[int, int]:
    """Resolve + admin-check in one place for the schedule-editor routes.
    Returns (chat_id, actor_user_id). Raises 404/401/403 as appropriate."""
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = require_identity(id_token, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    return chat_id, actor_user_id


async def _actor_display_name(chat_id: int, actor_user_id: int) -> str:
    try:
        info = _db.get_member_display_info(chat_id, actor_user_id)
        if info:
            return info.get("first_name") or "(web admin)"
    except Exception:
        pass
    return "(web admin)"


@router.get(
    "/web/group/{group_token}/templates",
    response_model=list[WebTemplateResponse],
    summary="List templates with schedule status (requires web-admin identity)",
)
async def web_list_templates(
    group_token: str = Path(...),
    id_token: str = "",
) -> list[WebTemplateResponse]:
    chat_id, _ = _require_web_admin(group_token, id_token)
    from services import templates as tmpl_svc
    return [WebTemplateResponse(**t) for t in tmpl_svc.list_templates(chat_id)]


@router.put(
    "/web/group/{group_token}/templates/{name}/schedule",
    response_model=WebTemplateResponse,
    summary="Set a template's recurring auto-start schedule (requires web-admin identity)",
)
async def web_set_template_schedule(
    body: WebSetScheduleRequest,
    group_token: str = Path(...),
    name: str = Path(...),
) -> WebTemplateResponse:
    chat_id, actor_user_id = _require_web_admin(group_token, body.id_token)
    actor_name = await _actor_display_name(chat_id, actor_user_id)

    from services import templates as tmpl_svc
    result = tmpl_svc.set_schedule(
        chat_id=chat_id, name=name,
        admin_user_id=actor_user_id, admin_name=actor_name,
        recurrence_type=body.recurrence_type,
        schedule_day=body.schedule_day,
        schedule_time=body.schedule_time,
        monthly_day=body.monthly_day,
    )
    tmpl = tmpl_svc.get_one_template(chat_id, name)

    recurrence_label = {"weekly": "weekly", "biweekly": "every 2 weeks", "monthly": "monthly"}.get(
        body.recurrence_type, body.recurrence_type)
    when = (f"day {body.monthly_day} of each month at {body.schedule_time}"
           if body.recurrence_type == "monthly"
           else f"{(body.schedule_day or '').capitalize()} {body.schedule_time} ({recurrence_label})")
    await _send_event_notification(
        chat_id, f"🗓 Schedule updated for '{name}' (via web): opens {when}."
    )
    return WebTemplateResponse(**{**tmpl, **result})


@router.post(
    "/web/group/{group_token}/templates/{name}/schedule/enable",
    response_model=WebTemplateResponse,
    summary="Re-enable a template's schedule (requires web-admin identity)",
)
async def web_enable_template_schedule(
    body: WebToggleScheduleRequest,
    group_token: str = Path(...),
    name: str = Path(...),
) -> WebTemplateResponse:
    chat_id, actor_user_id = _require_web_admin(group_token, body.id_token)
    actor_name = await _actor_display_name(chat_id, actor_user_id)
    from services import templates as tmpl_svc
    tmpl_svc.enable_schedule(chat_id, name, actor_user_id, actor_name)
    tmpl = tmpl_svc.get_one_template(chat_id, name)
    await _send_event_notification(chat_id, f"🟢 Schedule enabled for '{name}' (via web).")
    return WebTemplateResponse(**tmpl)


@router.post(
    "/web/group/{group_token}/templates/{name}/schedule/disable",
    response_model=WebTemplateResponse,
    summary="Disable a template's schedule (requires web-admin identity)",
)
async def web_disable_template_schedule(
    body: WebToggleScheduleRequest,
    group_token: str = Path(...),
    name: str = Path(...),
) -> WebTemplateResponse:
    chat_id, actor_user_id = _require_web_admin(group_token, body.id_token)
    actor_name = await _actor_display_name(chat_id, actor_user_id)
    from services import templates as tmpl_svc
    tmpl_svc.disable_schedule(chat_id, name, actor_user_id, actor_name)
    tmpl = tmpl_svc.get_one_template(chat_id, name)
    await _send_event_notification(chat_id, f"🔴 Schedule disabled for '{name}' (via web).")
    return WebTemplateResponse(**tmpl)


@router.put(
    "/web/group/{group_token}/templates/{name}",
    response_model=WebTemplateResponse,
    summary="Edit a template's content — title/location/fee/limit/event day+time (requires web-admin identity)",
)
async def web_update_template(
    body: WebUpdateTemplateRequest,
    group_token: str = Path(...),
    name: str = Path(...),
) -> WebTemplateResponse:
    chat_id, actor_user_id = _require_web_admin(group_token, body.id_token)
    actor_name = await _actor_display_name(chat_id, actor_user_id)
    from services import templates as tmpl_svc
    tmpl = tmpl_svc.upsert_template(
        chat_id=chat_id, name=name,
        admin_user_id=actor_user_id, admin_name=actor_name,
        title=body.title, location=body.location, fee=body.fee, limit=body.limit,
        event_day=body.event_day, event_time=body.event_time,
    )
    await _send_event_notification(chat_id, f"✏️ Template '{name}' updated (via web).")
    return WebTemplateResponse(**tmpl)


@router.post(
    "/web/group/{group_token}/templates/{name}/start",
    response_model=WebRollcallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a rollcall from a template right now (requires web-admin identity)",
)
async def web_start_template(
    body: WebStartTemplateRequest,
    group_token: str = Path(...),
    name: str = Path(...),
) -> WebRollcallResponse:
    chat_id, actor_user_id = _require_web_admin(group_token, body.id_token)
    actor_name = await _actor_display_name(chat_id, actor_user_id)
    from services import templates as tmpl_svc
    from services.web import _serialize_web_rollcall
    from rollcall_manager import manager as _mgr

    result = await tmpl_svc.start_template(
        chat_id=chat_id, name=name,
        admin_user_id=actor_user_id, admin_name=actor_name,
        extra_title=body.extra_title,
    )
    rc = _mgr.get_rollcall(chat_id, result["rc_index"])
    if rc is None:
        raise HTTPException(status_code=500, detail="Rollcall created but could not be retrieved")

    # Same visibility as any other rollcall start — post the panel into the
    # Telegram group so it's votable there too, not just on the web.
    await _mirror_panel_to_telegram(chat_id, result["rc_index"] + 1, force_new=True)
    return WebRollcallResponse(**_serialize_web_rollcall(rc))


# ── Scheduled rollcalls ───────────────────────────────────────────────────────

@router.post(
    "/web/group/{group_token}/scheduled-rollcalls",
    status_code=status.HTTP_201_CREATED,
    response_model=ScheduledRollcallCreateResponse,
    summary="Schedule a one-shot rollcall to auto-start at a future time (admin only)",
)
async def create_scheduled_rollcall(
    body: "ScheduledRollcallRequest",
    group_token: str = Path(...),
) -> ScheduledRollcallCreateResponse:
    from api.schemas.web import ScheduledRollcallRequest as _Req
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = require_identity(body.id_token, detail="Verify with Telegram first.")
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

    # Non-blocking event log so the group knows a rollcall was scheduled via web
    import re as _re2
    _dt_str = body.scheduled_at
    _dt_match = _re2.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})", _dt_str)
    _dt_label = _dt_match.group(1).replace("T", " ") if _dt_match else _dt_str
    await _send_event_notification(
        chat_id,
        f"📅 Rollcall scheduled: \"{body.title}\" at {_dt_label} (by {actor_name}, via web)",
    )

    return ScheduledRollcallCreateResponse(id=row_id, title=body.title, scheduled_at=body.scheduled_at)


@router.get(
    "/web/group/{group_token}/scheduled-rollcalls",
    response_model=ScheduledRollcallsResponse,
    summary="List upcoming scheduled rollcalls for a group (admin only)",
)
async def list_scheduled_rollcalls(
    group_token: str = Path(...),
    id_token: Optional[str] = Query(None),
) -> ScheduledRollcallsResponse:
    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")
    actor_user_id = require_identity(id_token, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")
    rows = _db.get_upcoming_scheduled_rollcalls(chat_id)
    return ScheduledRollcallsResponse(
        items=[
            ScheduledRollcallItem(
                id=r["id"],
                title=r["title"],
                scheduled_at=r["scheduled_at"],
                created_by_name=r["created_by_name"],
            )
            for r in rows
        ]
    )


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
    actor_user_id = require_identity(id_token, detail="Verify with Telegram first.")
    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")

    # Grab title before deletion so we can include it in the notification
    _sched_title = next(
        (r["title"] for r in _db.get_upcoming_scheduled_rollcalls(chat_id) if r["id"] == item_id),
        None,
    )

    deleted = _db.delete_scheduled_rollcall(item_id, chat_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scheduled rollcall not found or already fired.")

    # Non-blocking event log so the group knows the scheduled rollcall was cancelled
    _label = f'"{_sched_title}"' if _sched_title else f"#{item_id}"
    await _send_event_notification(chat_id, f"🗑 Scheduled rollcall {_label} cancelled (via web)")


# ── Web login token issuance (admin → member) ────────────────────────────────

class _WebloginRequest(BaseModel):
    id_token: str
    member_name: str


class _WebloginResponse(BaseModel):
    login_url: str
    member_name: str
    expires_in_days: int = 7


@router.post(
    "/web/group/{group_token}/issue-weblogin",
    response_model=_WebloginResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Admin issues a single-use login URL for a group member",
)
async def issue_weblogin(
    body: _WebloginRequest,
    group_token: str = Path(...),
) -> _WebloginResponse:
    import uuid
    from datetime import datetime, timedelta, timezone

    chat = _db.get_chat_by_group_web_token(group_token)
    if not chat:
        raise HTTPException(status_code=404, detail="Invalid group token")

    actor_user_id = require_identity(body.id_token, detail="Verify with Telegram first.")

    chat_id = int(chat["chat_id"])
    if not _db.is_web_admin(chat_id, actor_user_id):
        raise HTTPException(status_code=403, detail="You are not a web admin for this group.")

    # Resolve member name against chat_members
    needle = body.member_name.strip().lstrip("@").lower()
    if not needle:
        raise HTTPException(status_code=400, detail="member_name is required")
    members = _db.get_active_members(chat_id)
    matched = [
        m for m in members
        if (m.get("first_name") or "").lower() == needle
        or (m.get("username") or "").lower() == needle
    ]
    if not matched:
        raise HTTPException(
            status_code=404,
            detail=f"No member '{body.member_name}' found in this group's history. They must have voted at least once.",
        )
    if len(matched) > 1:
        names = ", ".join(m.get("first_name") or m.get("username") or str(m["user_id"]) for m in matched)
        raise HTTPException(status_code=409, detail=f"Ambiguous name — multiple matches: {names}")

    member = matched[0]
    tg_user_id = member["user_id"]
    display_name = member.get("first_name") or (f"@{member['username']}" if member.get("username") else str(tg_user_id))

    # Lookup actor name for audit
    actor_info = _db.get_member_display_info(chat_id, actor_user_id)
    actor_name = (actor_info.get("first_name") if actor_info else None) or "web admin"

    token = uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    _db.create_web_direct_login_token(
        token=token,
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        tg_name=display_name,
        created_by_uid=actor_user_id,
        created_by_name=actor_name,
        expires_at=expires_at,
    )

    base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    login_url = f"{base}/api/v1/auth/weblogin/{token}" if base else f"/api/v1/auth/weblogin/{token}"

    return _WebloginResponse(login_url=login_url, member_name=display_name)


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
