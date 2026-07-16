"""Schemas for Telegram deep-link identity verification."""
from typing import Optional
from pydantic import BaseModel


class TgVerifyStartResponse(BaseModel):
    code: str
    deep_link: str
    expires_in: int  # seconds


class TgLoginConfigResponse(BaseModel):
    """Bot username needed to render the Telegram Login Widget script tag."""
    bot_username: str


class TgLoginRequest(BaseModel):
    """Payload the Telegram Login Widget passes to its onauth callback."""
    id: int
    first_name: str
    auth_date: int
    hash: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None


class MemberTokenLoginRequest(BaseModel):
    """Persistent personal login code issued by /mytoken."""
    token: str


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
