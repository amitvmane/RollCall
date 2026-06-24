"""
Portal routes — personal cross-group stats for verified members.

No bearer token required. Accepts tg_user_id as a query parameter
(obtained from tg-verify). Rate-limited by the shared middleware.
"""
from fastapi import APIRouter, HTTPException, Path, Query

import db as _db
from rollcall_manager import manager
from api.schemas.portal import (
    PortalGroupHistoryResponse,
    PortalGroupsResponse,
    PortalGroupSummary,
    PortalSessionEntry,
)

router = APIRouter()


@router.get(
    "/portal/groups",
    response_model=PortalGroupsResponse,
    summary="All groups where this Telegram user has voted, with per-group stats",
)
async def portal_groups(
    tg_user_id: int = Query(..., description="Telegram user ID (from tg-verify)"),
) -> PortalGroupsResponse:
    if tg_user_id <= 0:
        raise HTTPException(status_code=422, detail="tg_user_id must be a positive integer")

    chats = _db.get_user_voted_chats(tg_user_id)
    groups = []
    for row in chats:
        cid = int(row["chat_id"])
        attended = int(row.get("sessions_attended") or 0)
        total = int(row.get("total_sessions") or 0)
        rate = round(attended / total * 100, 1) if total > 0 else None
        rank = _db.get_user_rank_in_chat(cid, tg_user_id)
        has_active = len(manager.get_rollcalls(cid)) > 0

        groups.append(PortalGroupSummary(
            chat_id=cid,
            group_name=row.get("group_name"),
            timezone=row.get("timezone") or "Asia/Kolkata",
            group_web_token=row.get("group_web_token"),
            sessions_attended=attended,
            total_sessions=total,
            attendance_rate=rate,
            current_streak=int(row.get("current_streak") or 0),
            best_streak=int(row.get("best_streak") or 0),
            rank=rank,
            has_active_rollcall=has_active,
        ))

    return PortalGroupsResponse(tg_user_id=tg_user_id, groups=groups)


@router.get(
    "/portal/groups/{chat_id}/history",
    response_model=PortalGroupHistoryResponse,
    summary="Recent session history for this user in one group",
)
async def portal_group_history(
    chat_id: int = Path(...),
    tg_user_id: int = Query(..., description="Telegram user ID (from tg-verify)"),
    limit: int = Query(20, ge=1, le=50),
) -> PortalGroupHistoryResponse:
    if tg_user_id <= 0:
        raise HTTPException(status_code=422, detail="tg_user_id must be a positive integer")

    sessions = _db.get_user_session_history(chat_id, tg_user_id, limit=limit)
    return PortalGroupHistoryResponse(
        chat_id=chat_id,
        tg_user_id=tg_user_id,
        sessions=[
            PortalSessionEntry(
                id=s.get("id"),
                title=s.get("title"),
                ended_at=str(s["ended_at"]) if s.get("ended_at") else None,
                status=s.get("status", "miss"),
            )
            for s in sessions
        ],
    )
