"""Pydantic models for proxy-vote routes."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from .common import UserPayload
from .rollcalls import RollcallResponse


class ProxyVoteRequest(BaseModel):
    vote: Literal["in", "out", "maybe"] = Field(
        ..., description="Vote choice for the proxy"
    )
    proxy_name: str = Field(..., max_length=40)
    admin_user_id: int = Field(..., description="Telegram user id of the admin acting")
    admin_name: str = Field(..., description="Display name of the admin")
    comment: Optional[str] = None


class ProxyVoteResponse(BaseModel):
    action: str = Field(..., description="'added' | 'waitlisted' | 'moved'")
    rollcall: RollcallResponse
    user: UserPayload
    proxy_owner_id: int
    rc_number_1based: int

    was_in: Optional[bool] = None
    promoted: Optional[UserPayload] = None
