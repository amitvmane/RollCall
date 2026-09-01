"""
Rollcall lifecycle routes — start, end, list, get.

Each route is a thin wrapper around a service call. URL path uses
`/chats/{chat_id}` as parent so a chat's rollcalls have a clear
RESTful hierarchy; rc_number in the URL is the 1-based number users
see (matching `/in ::2`), so we subtract 1 before passing to the
service which expects 0-based.
"""

from typing import List

from fastapi import APIRouter, Depends, Path, status

from services import rollcalls as rc_svc

from api.auth import AuthedToken, require_scope
from api.telegram_mirror import mirror_panel_to_telegram
from api.schemas.rollcalls import (
    EndRollcallRequest,
    EndRollcallResponse,
    RollcallResponse,
    StartRollcallRequest,
)


router = APIRouter()


@router.post(
    "/chats/{chat_id}/rollcalls",
    response_model=RollcallResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start a new rollcall in the chat",
)
async def start_rollcall(
    body: StartRollcallRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> RollcallResponse:
    # Serialise with concurrent votes and /erc — CLAUDE.md's chat-mutation
    # rule. The web routes took this lock; these token-authenticated ones
    # were missed, so an API-driven change could interleave with a vote
    # landing from Telegram.
    from rollcall_manager import manager as _mgr
    async with _mgr.get_chat_write_lock(chat_id):
        result = await rc_svc.start_rollcall(
            chat_id=chat_id,
            title=body.title,
            started_by_user_id=body.started_by_user_id,
            started_by_name=body.started_by_name,
            started_by_username=body.started_by_username,
        )
    await mirror_panel_to_telegram(chat_id, result["rc_index"] + 1, force_new=True)
    return RollcallResponse(**result)


@router.get(
    "/chats/{chat_id}/rollcalls",
    response_model=List[RollcallResponse],
    summary="List active rollcalls in the chat",
)
async def list_rollcalls(
    chat_id: int = Path(..., description="Telegram chat id"),
    _token: AuthedToken = Depends(require_scope("read")),
) -> List[RollcallResponse]:
    return [RollcallResponse(**r) for r in rc_svc.list_rollcalls(chat_id)]


@router.get(
    "/chats/{chat_id}/rollcalls/{rc_number}",
    response_model=RollcallResponse,
    summary="Get one rollcall by its 1-based number",
)
async def get_rollcall(
    chat_id: int = Path(..., description="Telegram chat id"),
    rc_number: int = Path(..., ge=1, description="1-based rollcall number"),
    _token: AuthedToken = Depends(require_scope("read")),
) -> RollcallResponse:
    return RollcallResponse(**rc_svc.get_rollcall(chat_id, rc_number - 1))


@router.delete(
    "/chats/{chat_id}/rollcalls/{rc_number}",
    response_model=EndRollcallResponse,
    summary="End a rollcall by its 1-based number",
)
async def end_rollcall(
    body: EndRollcallRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    rc_number: int = Path(..., ge=1, description="1-based rollcall number"),
    _token: AuthedToken = Depends(require_scope("admin")),
) -> EndRollcallResponse:
    # Serialise with concurrent votes and /erc — CLAUDE.md's chat-mutation
    # rule. The web routes took this lock; these token-authenticated ones
    # were missed, so an API-driven change could interleave with a vote
    # landing from Telegram.
    from rollcall_manager import manager as _mgr
    async with _mgr.get_chat_write_lock(chat_id):
        result = await rc_svc.end_rollcall(
            chat_id=chat_id,
            rc_number=rc_number - 1,
            ended_by_user_id=body.ended_by_user_id,
            ended_by_name=body.ended_by_name,
            ended_by_username=body.ended_by_username,
        )
    await mirror_panel_to_telegram(chat_id, result.get("rc_number_ended_1based", rc_number))
    return EndRollcallResponse(**result)
