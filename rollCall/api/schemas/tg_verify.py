"""Schemas for Telegram deep-link identity verification."""
from typing import Optional
from pydantic import BaseModel


class TgVerifyStartResponse(BaseModel):
    code: str
    deep_link: str
    expires_in: int  # seconds


class TgLoginConfigResponse(BaseModel):
    """Bot username needed to render the Telegram Login Widget script tag.

    ``widget_enabled`` reports whether this deployment has actually completed
    the widget's other half — /setdomain in BotFather, which registers the
    site's domain against the bot. Telegram gives the browser no way to ask:
    an unregistered domain simply renders Telegram's own error text ("Bot
    domain invalid" / "Username invalid") inside a cross-origin iframe we
    can't read. So the deployment declares it, and the UI offers the widget
    only when it will work rather than showing a stranger's error message.
    """
    bot_username: str
    widget_enabled: bool = False


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
