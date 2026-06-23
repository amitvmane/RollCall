"""
Admin group-management routes.

GET  /admin/groups                  — list all known groups with summary info
GET  /admin/groups/{chat_id}        — full settings for one group
PATCH /admin/groups/{chat_id}       — update one or more settings for a group
GET  /chats/{chat_id}/qrcode        — SVG QR code for the group's web voting link

All routes require the `admin` scope.
"""

import io
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import Response

from db import get_active_members, get_or_create_chat
from services import settings as settings_svc

from api.auth import AuthedToken, require_scope
from api.schemas.groups import (
    GroupMemberEntry,
    GroupSettings,
    GroupSummary,
    UpdateGroupSettingsRequest,
)


router = APIRouter()


@router.get(
    "/admin/groups",
    response_model=List[GroupSummary],
    summary="List all groups the bot is in",
)
async def list_groups(
    _token: AuthedToken = Depends(require_scope("admin")),
) -> List[GroupSummary]:
    return [GroupSummary(**g) for g in settings_svc.list_groups()]


@router.get(
    "/admin/groups/{chat_id}",
    response_model=GroupSettings,
    summary="Get settings for one group",
)
async def get_group_settings(
    chat_id: int = Path(..., description="Telegram chat id"),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> GroupSettings:
    return GroupSettings(**settings_svc.get_chat_settings(chat_id))


@router.get(
    "/chats/{chat_id}/members",
    response_model=List[GroupMemberEntry],
    summary="List active known members of a group (from member-tracking table)",
)
async def list_group_members(
    chat_id: int = Path(..., description="Telegram chat id"),
    _token: AuthedToken = Depends(require_scope("read")),
) -> List[GroupMemberEntry]:
    return [GroupMemberEntry(**m) for m in get_active_members(chat_id)]


@router.patch(
    "/admin/groups/{chat_id}",
    response_model=GroupSettings,
    summary="Update one or more settings for a group",
)
async def update_group_settings(
    body: UpdateGroupSettingsRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> GroupSettings:
    result = settings_svc.update_group_settings(
        chat_id=chat_id,
        admin_user_id=body.admin_user_id,
        admin_name=body.admin_name,
        timezone=body.timezone,
        shh_mode=body.shh_mode,
        admin_rights=body.admin_rights,
        ghost_tracking_enabled=body.ghost_tracking_enabled,
        absent_limit=body.absent_limit,
    )
    return GroupSettings(**result)


@router.get(
    "/chats/{chat_id}/qrcode",
    summary="SVG QR code for the group's web voting link",
)
async def get_group_qrcode(
    request: Request,
    chat_id: int = Path(..., description="Telegram chat id"),
    _token: AuthedToken = Depends(require_scope("read")),
) -> Response:
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except ImportError:
        raise HTTPException(500, "QR library not installed (qrcode package missing)")

    chat = get_or_create_chat(chat_id)
    token = chat.get("group_web_token")
    if not token:
        raise HTTPException(404, "No web link configured for this group")

    base = str(request.base_url).rstrip("/")
    url = f"{base}/web/group/{token}"

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    logging.info("[api] qrcode generated for chat=%d url=%s", chat_id, url)
    return Response(
        content=buf.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )
