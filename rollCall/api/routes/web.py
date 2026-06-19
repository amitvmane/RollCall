"""
Public web voting routes — no bearer token required, only the rollcall's
magic-link web_token in the URL path.

GET  /api/v1/web/{token}       → fetch rollcall state
POST /api/v1/web/{token}/vote  → submit a vote (in/out/maybe)
"""
from fastapi import APIRouter, Path, status

from services import web as web_svc
from api.schemas.web import WebRollcallResponse, WebVoteRequest

router = APIRouter()


@router.get(
    "/web/{token}",
    response_model=WebRollcallResponse,
    summary="Get rollcall state via magic-link token",
)
async def get_web_rollcall(
    token: str = Path(..., description="Magic-link token from the rollcall URL"),
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
    token: str = Path(..., description="Magic-link token from the rollcall URL"),
) -> WebRollcallResponse:
    data = await web_svc.vote_by_token(token, body.name, body.vote)
    return WebRollcallResponse(**data)
