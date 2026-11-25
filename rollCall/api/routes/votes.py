"""
Vote routes — POST a vote (in/out/maybe) on a rollcall.

The route dispatches to the appropriate service based on the `vote` field
in the request body, returning a uniform VoteResponse. This keeps the URL
simple (no /vote-in, /vote-out, etc.) while still letting clients
explicitly express what they want.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from services import voting as vote_svc

from api.auth import AuthedToken, require_scope
from api.rollcall_ref import RcRef, dual, rollcall_ref
from api.schemas.votes import VoteRequest, VoteResponse
from api.telegram_mirror import mirror_panel_to_telegram, send_vote_notification


router = APIRouter()


@dual(router.post, "/chats/{chat_id}/rollcalls/{rc_number}/votes",
      response_model=VoteResponse, status_code=status.HTTP_201_CREATED,
      summary="Cast a vote (in/out/maybe) on a rollcall")
async def cast_vote(
    body: VoteRequest,
    _token: AuthedToken = Depends(require_scope("vote")),
    ref: RcRef = Depends(rollcall_ref),
) -> VoteResponse:
    chat_id = ref.chat_id
    # Self-service tokens (miniapp initData exchange) are bound to the
    # Telegram user who authenticated — they must not be able to vote as
    # someone else by putting a different user_id in the body, whether that's
    # another real Telegram id (int) or a fabricated proxy name (str) — proxy
    # adds are admin-only, same as /sif and routes/proxy_votes.py. Tokens with
    # no bound user (out-of-band service tokens) or the admin scope are
    # trusted to vote on behalf of others, same as proxy votes.
    if (
        "admin" not in _token.scopes
        and not _token.app_key
        and _token.issued_by_user_id is not None
        and body.user_id != _token.issued_by_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token can only cast votes for its own issuing user",
        )

    # A partner app key votes for MEMBERS: an int id the group already knows.
    # Not a name (adding a guest is admin-only — routes/proxy_votes.py) and not
    # an invented id, which would plant a phantom member in the group.
    if _token.app_key and "admin" not in _token.scopes:
        import db as _db
        if not isinstance(body.user_id, int) or _db.get_member_display_info(chat_id, body.user_id) is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="An app key votes for members of this group only (guests need an admin key's proxy-votes)",
            )

    common_args = dict(
        chat_id=chat_id,
        user_id=body.user_id,
        first_name=body.first_name,
        username=body.username,
        comment=body.comment,
        # By id: the service resolves it inside the chat lock.
        rollcall_id=ref.rollcall_id,
    )

    if body.vote == "in":
        result = await vote_svc.vote_in(**common_args)
    elif body.vote == "out":
        result = await vote_svc.vote_out(**common_args)
    elif body.vote == "maybe":
        result = await vote_svc.vote_maybe(**common_args)
    else:
        # Pydantic's Literal validation should have caught this already;
        # defensive guard for the impossible case.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown vote choice: {body.vote!r}",
        )

    await send_vote_notification(chat_id, body.first_name, body.vote)
    await mirror_panel_to_telegram(chat_id, ref.number_now())
    return VoteResponse(**result)
