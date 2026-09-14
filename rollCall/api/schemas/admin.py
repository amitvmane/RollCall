"""Pydantic models for admin user-management endpoints."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from .common import UserPayload
from .rollcalls import RollcallResponse


class AdminRequest(BaseModel):
    """Common admin identity payload required for audit logging."""

    admin_user_id: int = Field(..., description="Telegram user id of the acting admin")
    admin_name: str = Field(..., description="Display name of the acting admin")


class DeleteUserRequest(AdminRequest):
    pass


class DeleteUserResponse(BaseModel):
    deleted: str = Field(..., description="Name of the user that was removed")
    rc_number_1based: int
    promoted: List[UserPayload] = Field(
        default_factory=list,
        description="Members pulled waitlist→IN by the slot this removal freed",
    )
    rollcall: RollcallResponse


class SetStatusRequest(AdminRequest):
    new_status: Literal["in", "out", "maybe"] = Field(
        ..., description="Target status list to move the user into"
    )


class SetStatusResponse(BaseModel):
    moved: str = Field(..., description="Name of the user that was moved")
    from_status: str
    to_status: str
    rc_number_1based: int
    promoted: List[UserPayload] = Field(
        default_factory=list,
        description="Members pulled waitlist→IN by the slot this move freed",
    )
    rollcall: RollcallResponse
