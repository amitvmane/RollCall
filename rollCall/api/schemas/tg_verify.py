"""Schemas for Telegram deep-link identity verification."""
from typing import Optional
from pydantic import BaseModel


class TgVerifyStartResponse(BaseModel):
    code: str
    deep_link: str
    expires_in: int  # seconds


class TgVerifyStatusResponse(BaseModel):
    verified: bool
    user_id: Optional[int] = None
    name: Optional[str] = None
