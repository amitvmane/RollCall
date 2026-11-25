"""
Proxy-vote route: an admin marks a non-Telegram member as in/out/maybe.

Requires the 'admin' scope — this mutates another (non-Telegram) member's
vote on someone else's behalf, matching the Telegram /sif /sof /smf
handlers' admin_rights gate and the web portal's is_web_admin gate. The
`admin_*` fields in the body MUST identify the human performing the
action — these go into the audit log.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from services import proxy as proxy_svc

from api.auth import AuthedToken, require_scope
from api.rollcall_ref import RcRef, dual, rollcall_ref
from api.schemas.proxy import ProxyVoteRequest, ProxyVoteResponse
from api.telegram_mirror import mirror_panel_to_telegram, send_vote_notification


router = APIRouter()


@dual(router.post, "/chats/{chat_id}/rollcalls/{rc_number}/proxy-votes",
      response_model=ProxyVoteResponse, status_code=status.HTTP_201_CREATED,
      summary="Cast a proxy vote (in/out/maybe) on behalf of a non-Telegram member")
async def cast_proxy_vote(
    body: ProxyVoteRequest,
    _token: AuthedToken = Depends(require_scope("admin")),
    ref: RcRef = Depends(rollcall_ref),
) -> ProxyVoteResponse:
    chat_id = ref.chat_id
    common = dict(
        chat_id=chat_id,
        admin_user_id=body.admin_user_id,
        admin_name=body.admin_name,
        proxy_name=body.proxy_name,
        comment=body.comment,
        rollcall_id=ref.rollcall_id,   # resolved inside the chat lock
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

    await send_vote_notification(chat_id, body.proxy_name, body.vote)
    await mirror_panel_to_telegram(chat_id, ref.number_now())
    return ProxyVoteResponse(**result)
