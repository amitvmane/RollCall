"""
Health check endpoint.

Returns 200 with a simple status payload. The existing runner already
exposes a more detailed /health on port 8080 (covers scheduler liveness,
DB pool, etc.); this endpoint is a lightweight API-side check so the
API itself can be probed independently of the bot's health server.
"""

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    api_version: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", api_version="v1")
