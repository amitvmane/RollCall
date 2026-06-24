"""
Proxy-vote route: an admin marks a non-Telegram member as in/out/maybe.

Requires the 'vote' scope (proxy votes are still votes; not a destructive
action). The `admin_*` fields in the body MUST identify the human
performing the action — these go into the audit log.
"""

from fastapi import APIRouter, Depends, HTTPException, Path, status

from services import proxy as proxy_svc

from api.auth import AuthedToken, require_scope
from api.schemas.proxy import ProxyVoteRequest, ProxyVoteResponse
from api.telegram_mirror import mirror_panel_to_telegram


router = APIRouter()


@router.post(
    "/chats/{chat_id}/rollcalls/{rc_number}/proxy-votes",
    response_model=ProxyVoteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cast a proxy vote (in/out/maybe) on behalf of a non-Telegram member",
)
async def cast_proxy_vote(
    body: ProxyVoteRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    rc_number: int = Path(..., ge=1, description="1-based rollcall number"),
    _token: AuthedToken = Depends(require_scope("vote")),
) -> ProxyVoteResponse:
    common = dict(
        chat_id=chat_id,
        admin_user_id=body.admin_user_id,
        admin_name=body.admin_name,
        proxy_name=body.proxy_name,
        comment=body.comment,
        rc_number=rc_number - 1,
    )

    if body.vote == "in":
        result = await proxy_svc.set_in_for(**common)
    elif body.vote == "out":
        result = await proxy_svc.set_out_for(**common)
    elif body.vote == "maybe":
        result = await proxy_svc.set_maybe_for(**common)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown vote choice: {body.vote!r}",
        )

    await mirror_panel_to_telegram(chat_id, rc_number)
    return ProxyVoteResponse(**result)
