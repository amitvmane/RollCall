"""Pydantic models for vote endpoints."""

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from .common import UserPayload
from .rollcalls import RollcallResponse


class VoteRequest(BaseModel):
    vote: Literal["in", "out", "maybe"] = Field(
        ...,
        description="Vote choice. One of: in, out, maybe.",
    )
    user_id: Union[int, str] = Field(
        ...,
        description="Telegram user id for real users, name for proxies",
    )
    first_name: str
    username: Optional[str] = None
    comment: Optional[str] = None


class VoteResponse(BaseModel):
    action: str = Field(
        ...,
        description="'added' (fresh vote) | 'waitlisted' (IN past cap) | 'moved' (status change)",
    )
    rollcall: RollcallResponse
    user: UserPayload
    rc_number_1based: int

    # Out/maybe-specific. None on 'in' votes.
    was_in: Optional[bool] = None
    promoted: Optional[UserPayload] = None
