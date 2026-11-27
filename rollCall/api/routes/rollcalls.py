"""
Rollcall lifecycle routes — start, end, list, get.

Each route is a thin wrapper around a service call. URL path uses
`/chats/{chat_id}` as parent so a chat's rollcalls have a clear
RESTful hierarchy. A single rollcall is named by its permanent id
(`/rollcalls/by-id/{rollcall_id}`); the 1-based display number path still
works but is deprecated — see api/rollcall_ref.py.
"""

from typing import List

from fastapi import APIRouter, Depends, Path, status

from services import rollcalls as rc_svc

from api.auth import AuthedToken, require_scope
from api.rollcall_ref import BY_ID, RcRef, dual, rollcall_ref, stable_path
from api.telegram_mirror import mirror_panel_to_telegram, send_event_notification
from api.schemas.rollcalls import (
    EndRollcallRequest,
    EndRollcallResponse,
    LineupResponse,
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


@dual(router.get, "/chats/{chat_id}/rollcalls/{rc_number}",
      response_model=RollcallResponse, summary="Get one open rollcall")
async def get_rollcall(
    _token: AuthedToken = Depends(require_scope("read")),
    ref: RcRef = Depends(rollcall_ref),
) -> RollcallResponse:
    return RollcallResponse(**rc_svc.get_rollcall(ref.chat_id, ref.index_now()))


# By id only: new routes never get a (retiring) number path.
@router.get(stable_path("/chats/{chat_id}/rollcalls/{rc_number}/lineup"), dependencies=BY_ID,
            response_model=LineupResponse,
            summary="The lineup: both teams by position and formation, and their subs")
async def get_lineup(
    _token: AuthedToken = Depends(require_scope("read")),
    ref: RcRef = Depends(rollcall_ref),
) -> LineupResponse:
    from rollcall_manager import manager as _mgr
    from services import lineup as lineup_svc
    rc = _mgr.get_rollcall(ref.chat_id, ref.index_now())
    return LineupResponse(**lineup_svc.api_payload(rc, lineup_svc.view(rc)))


@dual(router.delete, "/chats/{chat_id}/rollcalls/{rc_number}",
      response_model=EndRollcallResponse, summary="End a rollcall")
async def end_rollcall(
    body: EndRollcallRequest,
    _token: AuthedToken = Depends(require_scope("admin")),
    ref: RcRef = Depends(rollcall_ref),
) -> EndRollcallResponse:
    # Serialise with concurrent votes and /erc — CLAUDE.md's chat-mutation
    # rule — and find the rollcall by id INSIDE the lock, so a call that
    # waited behind another /erc can't end whichever one slid into its place.
    chat_id = ref.chat_id
    from rollcall_manager import manager as _mgr
    async with _mgr.get_chat_write_lock(chat_id):
        idx = ref.index_now()
        result = await rc_svc.end_rollcall(
            chat_id=chat_id,
            rc_number=idx,
            ended_by_user_id=body.ended_by_user_id,
            ended_by_name=body.ended_by_name,
            ended_by_username=body.ended_by_username,
        )
    # Mirroring the panel alone never says WHO ended it — without this an
    # API-driven end is the one silent path, unlike /erc and the panel
    # button, which both post "Ended by X" as part of the finish list.
    title = result.get("ended", {}).get("title") or "Rollcall"
    await send_event_notification(
        chat_id, f"🏁 '{title}' ended by {body.ended_by_name} (via API)."
    )
    await mirror_panel_to_telegram(chat_id, result.get("rc_number_ended_1based", idx + 1))
    return EndRollcallResponse(**result)
