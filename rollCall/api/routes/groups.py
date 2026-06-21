"""
Admin group-management routes.

GET  /admin/groups                  — list all known groups with summary info
GET  /admin/groups/{chat_id}        — full settings for one group
PATCH /admin/groups/{chat_id}       — update one or more settings for a group

All routes require the `admin` scope.
"""

from typing import List

from fastapi import APIRouter, Depends, Path

from services import settings as settings_svc

from api.auth import AuthedToken, require_scope
from api.schemas.groups import (
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
