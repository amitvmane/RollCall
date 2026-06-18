"""
Vote routes — POST a vote (in/out/maybe) on a rollcall.

The route dispatches to the appropriate service based on the `vote` field
in the request body, returning a uniform VoteResponse. This keeps the URL
simple (no /vote-in, /vote-out, etc.) while still letting clients
explicitly express what they want.
"""

from fastapi import APIRouter, HTTPException, Path, status

from services import voting as vote_svc

from api.schemas.votes import VoteRequest, VoteResponse


router = APIRouter()


@router.post(
    "/chats/{chat_id}/rollcalls/{rc_number}/votes",
    response_model=VoteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cast a vote (in/out/maybe) on a rollcall",
)
async def cast_vote(
    body: VoteRequest,
    chat_id: int = Path(..., description="Telegram chat id"),
    rc_number: int = Path(..., ge=1, description="1-based rollcall number"),
) -> VoteResponse:
    common_args = dict(
        chat_id=chat_id,
        user_id=body.user_id,
        first_name=body.first_name,
        username=body.username,
        comment=body.comment,
        rc_number=rc_number - 1,
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

    return VoteResponse(**result)
