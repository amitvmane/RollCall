"""
Public web voting routes — no bearer token required.

Per-rollcall token (expires with rollcall):
  GET  /api/v1/web/{token}          → fetch single rollcall state
  POST /api/v1/web/{token}/vote     → submit a vote (in/out/maybe)

Permanent group token (never expires, bookmarkable):
  GET  /api/v1/web/group/{token}    → fetch all active rollcalls for the group
"""
from fastapi import APIRouter, Path, status

from services import web as web_svc
from api.schemas.web import WebGroupResponse, WebRollcallResponse, WebVoteRequest

router = APIRouter()


# ── Group endpoint (permanent) ────────────────────────────────────────────────

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
    data = await web_svc.vote_by_token(token, body.name, body.vote)
    return WebRollcallResponse(**data)
