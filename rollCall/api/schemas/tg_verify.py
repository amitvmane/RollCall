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
    # Telegram @handle (without the @) — passed to the vote so the model can
    # format the display name as "First (@handle)" when a name conflict exists.
    username: Optional[str] = None
    # Signed proof of the verified identity. The client stores this and
    # presents it on identity-sensitive calls (portal, web-admin, real-user
    # votes) instead of a raw, forgeable tg_user_id.
    id_token: Optional[str] = None
