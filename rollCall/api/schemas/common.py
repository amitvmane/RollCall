"""
Shared request/response shapes.

UserPayload mirrors the dict returned by `services.common.serialize_user`.
ErrorResponse is the body returned by handlers for non-2xx responses.
"""

from typing import Optional, Union

from pydantic import BaseModel, Field


class UserPayload(BaseModel):
    user_id: Union[int, str] = Field(
        ...,
        description="Telegram user id (int) for real users, name (str) for proxies",
    )
    name: str
    username: Optional[str] = None
    comment: Optional[str] = ""
    is_proxy: bool = False


class ErrorResponse(BaseModel):
    error: str = Field(..., description="Exception class name")
    detail: str = Field(..., description="Human-readable message safe to show users")
