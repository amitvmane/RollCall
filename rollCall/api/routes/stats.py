"""Stats, ghost, and settings routes."""

from typing import List

from fastapi import APIRouter, Depends, Path, Query, status

from services import ghost as ghost_svc
from services import settings as settings_svc
from services import stats as stats_svc

from api.auth import AuthedToken, require_scope
from api.schemas.rollcalls import RollcallResponse
from services import rollcalls as rc_svc

from api.schemas.stats import (
    ChatSettingsResponse,
    ClearAbsentRequest,
    ClearAbsentResponse,
    GhostLeaderboardEntry,
    GhostSettingsResponse,
    GroupStatsResponse,
    HistoryEntry,
    LeaderboardEntry,
    PersonalStatsResponse,
    SetAbsentLimitRequest,
    SetFeeRequest,
    SetLimitRequest,
    SetLocationRequest,
    SetReminderRequest,
    SetShhModeRequest,
    SetTimezoneRequest,
    SetTitleRequest,
    SetWhenRequest,
    ToggleGhostRequest,
)


router = APIRouter()


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get(
    "/chats/{chat_id}/stats/users/{user_id}",
    response_model=PersonalStatsResponse,
    summary="Personal attendance stats for a real user",
)
async def personal_stats(
    chat_id: int = Path(...),
    user_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> PersonalStatsResponse:
    return PersonalStatsResponse(**stats_svc.personal_stats(chat_id, user_id))


@router.get(
    "/chats/{chat_id}/stats/group",
    response_model=GroupStatsResponse,
    summary="Aggregate group attendance stats",
)
async def group_stats(
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> GroupStatsResponse:
    return GroupStatsResponse(**stats_svc.group_stats(chat_id))


@router.get(
    "/chats/{chat_id}/stats/leaderboard",
    response_model=List[LeaderboardEntry],
    summary="Attendance leaderboard",
)
async def leaderboard(
    chat_id: int = Path(...),
    limit: int = Query(10, ge=1, le=100),
    _token: AuthedToken = Depends(require_scope("read")),
) -> List[LeaderboardEntry]:
    return [LeaderboardEntry(**e) for e in stats_svc.leaderboard(chat_id, limit)]


@router.get(
    "/chats/{chat_id}/history",
    response_model=List[HistoryEntry],
    summary="Last N ended rollcalls",
)
async def history(
    chat_id: int = Path(...),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    _token: AuthedToken = Depends(require_scope("read")),
) -> List[HistoryEntry]:
    return [HistoryEntry(**e) for e in stats_svc.history(chat_id, limit, offset)]


# ── Ghost ─────────────────────────────────────────────────────────────────────

@router.get(
    "/chats/{chat_id}/ghost/settings",
    response_model=GhostSettingsResponse,
    summary="Ghost tracking settings for a chat",
)
async def ghost_settings(
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> GhostSettingsResponse:
    return GhostSettingsResponse(**ghost_svc.get_ghost_settings(chat_id))


@router.put(
    "/chats/{chat_id}/ghost/settings/tracking",
    response_model=GhostSettingsResponse,
    summary="Enable or disable ghost tracking",
)
async def toggle_ghost_tracking(
    body: ToggleGhostRequest,
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> GhostSettingsResponse:
    return GhostSettingsResponse(
        **ghost_svc.toggle_ghost_tracking(
            chat_id, body.enabled, body.admin_user_id, body.admin_name
        )
    )


@router.put(
    "/chats/{chat_id}/ghost/settings/limit",
    response_model=GhostSettingsResponse,
    summary="Set the ghost absence threshold",
)
async def set_absent_limit(
    body: SetAbsentLimitRequest,
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> GhostSettingsResponse:
    return GhostSettingsResponse(
        **ghost_svc.set_absent_limit(
            chat_id, body.limit, body.admin_user_id, body.admin_name
        )
    )


@router.post(
    "/chats/{chat_id}/ghost/clear",
    response_model=ClearAbsentResponse,
    status_code=status.HTTP_200_OK,
    summary="Clear ghost count for one user/proxy or all",
)
async def clear_absent(
    body: ClearAbsentRequest,
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> ClearAbsentResponse:
    return ClearAbsentResponse(
        **ghost_svc.clear_absent(
            chat_id, body.admin_user_id, body.admin_name,
            target_user_id=body.target_user_id,
            proxy_name=body.proxy_name,
        )
    )


@router.get(
    "/chats/{chat_id}/ghost/leaderboard",
    response_model=List[GhostLeaderboardEntry],
    summary="Ghost (no-show) leaderboard",
)
async def ghost_leaderboard(
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> List[GhostLeaderboardEntry]:
    return [GhostLeaderboardEntry(**e) for e in ghost_svc.ghost_leaderboard(chat_id)]


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get(
    "/chats/{chat_id}/settings",
    response_model=ChatSettingsResponse,
    summary="Chat-level settings",
)
async def chat_settings(
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("read")),
) -> ChatSettingsResponse:
    return ChatSettingsResponse(**settings_svc.get_chat_settings(chat_id))


@router.put(
    "/chats/{chat_id}/settings/timezone",
    response_model=ChatSettingsResponse,
    summary="Set chat timezone",
)
async def set_timezone(
    body: SetTimezoneRequest,
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> ChatSettingsResponse:
    settings_svc.set_timezone(chat_id, body.timezone, body.admin_user_id, body.admin_name)
    return ChatSettingsResponse(**settings_svc.get_chat_settings(chat_id))


@router.put(
    "/chats/{chat_id}/settings/shh",
    response_model=ChatSettingsResponse,
    summary="Toggle silent (shh/louder) mode",
)
async def set_shh(
    body: SetShhModeRequest,
    chat_id: int = Path(...),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> ChatSettingsResponse:
    settings_svc.set_shh_mode(chat_id, body.enabled, body.admin_user_id, body.admin_name)
    return ChatSettingsResponse(**settings_svc.get_chat_settings(chat_id))


@router.put(
    "/chats/{chat_id}/rollcalls/{rc_number}/settings/limit",
    response_model=RollcallResponse,
    summary="Set IN-list cap on a rollcall (0 = no cap)",
)
async def set_limit(
    body: SetLimitRequest,
    chat_id: int = Path(...),
    rc_number: int = Path(..., ge=1),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    return RollcallResponse(
        **settings_svc.set_rollcall_limit(
            chat_id, body.limit, body.admin_user_id, body.admin_name, rc_number - 1
        )
    )


@router.put(
    "/chats/{chat_id}/rollcalls/{rc_number}/settings/location",
    response_model=RollcallResponse,
    summary="Set location on a rollcall",
)
async def set_location(
    body: SetLocationRequest,
    chat_id: int = Path(...),
    rc_number: int = Path(..., ge=1),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    return RollcallResponse(
        **settings_svc.set_location(
            chat_id, body.location, body.admin_user_id, body.admin_name, rc_number - 1
        )
    )


@router.put(
    "/chats/{chat_id}/rollcalls/{rc_number}/settings/fee",
    response_model=RollcallResponse,
    summary="Set event fee on a rollcall",
)
async def set_fee(
    body: SetFeeRequest,
    chat_id: int = Path(...),
    rc_number: int = Path(..., ge=1),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    return RollcallResponse(
        **settings_svc.set_event_fee(
            chat_id, body.fee, body.admin_user_id, body.admin_name, rc_number - 1
        )
    )


@router.put(
    "/chats/{chat_id}/rollcalls/{rc_number}/settings/title",
    response_model=RollcallResponse,
    summary="Set title on a rollcall",
)
async def set_title(
    body: SetTitleRequest,
    chat_id: int = Path(...),
    rc_number: int = Path(..., ge=1),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    return RollcallResponse(
        **rc_svc.set_title(
            chat_id, rc_number - 1, body.title, body.admin_user_id, body.admin_name
        )
    )


@router.put(
    "/chats/{chat_id}/rollcalls/{rc_number}/settings/when",
    response_model=RollcallResponse,
    summary="Set or cancel the finalize date/time for a rollcall",
)
async def set_when(
    body: SetWhenRequest,
    chat_id: int = Path(...),
    rc_number: int = Path(..., ge=1),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    result = settings_svc.set_rollcall_time(
        chat_id, rc_number - 1, body.datetime_str, body.admin_user_id, body.admin_name
    )
    return RollcallResponse(**result["rollcall"])


@router.put(
    "/chats/{chat_id}/rollcalls/{rc_number}/settings/reminder",
    response_model=RollcallResponse,
    summary="Set or cancel the pre-event reminder for a rollcall",
)
async def set_reminder(
    body: SetReminderRequest,
    chat_id: int = Path(...),
    rc_number: int = Path(..., ge=1),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    result = settings_svc.set_reminder(
        chat_id, rc_number - 1, body.hours, body.admin_user_id, body.admin_name
    )
    return RollcallResponse(**result["rollcall"])
