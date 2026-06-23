"""
Health check endpoint.

Returns 200 with a simple status payload. The existing runner already
exposes a more detailed /health on port 8080 (covers scheduler liveness,
DB pool, etc.); this endpoint is a lightweight API-side check so the
API itself can be probed independently of the bot's health server.
"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    api_version: str
    telegram_ok: Optional[bool] = None
    telegram_bot: Optional[str] = None
    telegram_checked_at: Optional[str] = None


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    try:
        from bot_state import _telegram_status
        tg_ok = _telegram_status.get("ok")
        tg_bot = _telegram_status.get("bot_username")
        tg_at = _telegram_status.get("checked_at")
    except Exception:
        tg_ok = None
        tg_bot = None
        tg_at = None

    return HealthResponse(
        status="ok",
        api_version="v1",
        telegram_ok=tg_ok,
        telegram_bot=tg_bot,
        telegram_checked_at=tg_at,
    )
