"""
Portal routes — personal cross-group stats for verified members.

No bearer token required, but every route returns data for exactly one
Telegram user, so it must prove that identity. The caller presents a signed
identity token (`id_token`, from tg-verify or Mini App auth); the user id is
derived from its signature server-side. A raw numeric tg_user_id is NOT
accepted — trusting one would let anyone read any user's attendance history.
Rate-limited by the shared middleware.
"""
from fastapi import APIRouter, HTTPException, Path, Query, status

import db as _db
from rollcall_manager import manager
from api.identity import verify_identity_token
from api.schemas.portal import (
    PortalGroupHistoryResponse,
    PortalGroupsResponse,
    PortalGroupSummary,
    PortalSessionEntry,
)

router = APIRouter()


def _require_identity(id_token: str) -> int:
    """Resolve a signed identity token to a verified user id, or 401."""
    user_id = verify_identity_token(id_token)
    if user_id is None or user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Verify with Telegram to view your portal.",
        )
    return user_id


@router.get(
    "/portal/groups",
    response_model=PortalGroupsResponse,
    summary="All groups where this Telegram user has voted, with per-group stats",
)
async def portal_groups(
    id_token: str = Query(..., description="Signed identity token (from tg-verify / Mini App auth)"),
) -> PortalGroupsResponse:
    tg_user_id = _require_identity(id_token)

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
    id_token: str = Query(..., description="Signed identity token (from tg-verify / Mini App auth)"),
    limit: int = Query(20, ge=1, le=50),
) -> PortalGroupHistoryResponse:
    tg_user_id = _require_identity(id_token)

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
