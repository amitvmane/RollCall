"""
FastAPI app factory.

Builds the FastAPI instance with all routes mounted. The runner imports
`app` from here and runs it under uvicorn as a parallel asyncio task,
gated by the REST_API_ENABLED env var.

Exception mapping: services raise curated user-facing exceptions from
`exceptions.py`. The exception handler installed here translates those
into proper HTTP status codes with a consistent ErrorResponse body, so
each route doesn't need to repeat the same try/except boilerplate.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from exceptions import (
    alreadyInList,
    amountOfRollCallsReached,
    incorrectParameter,
    insufficientPermissions,
    parameterMissing,
    rollCallAlreadyStarted,
    rollCallNotStarted,
)
from api.rate_limit import rate_limit_middleware
from api.routes import admin, auth, groups, health, proxy_votes, rollcalls, stats, templates, votes, web as web_routes
from api.schemas.common import ErrorResponse


API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.info("[api] REST API ready at %s (docs: /api/docs)", API_PREFIX)
    logging.warning(
        "[api] No authentication configured — endpoints are OPEN. "
        "Bind to localhost only and gate via reverse proxy until "
        "PR 3 (auth tokens) lands."
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="RollCall API",
        description=(
            "REST surface for the RollCall Telegram bot. Backs future "
            "web/Mini-App clients and third-party integrations. Calls "
            "the same `services/` layer the bot will use internally."
        ),
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=_lifespan,
    )

    # Exception → HTTP status mapping. Curated user-facing exceptions
    # only — anything else bubbles up to FastAPI's default 500 handler
    # (with traceback logged once via _USER_FACING_EXCEPTIONS check).
    _exception_map = {
        rollCallNotStarted: 404,
        rollCallAlreadyStarted: 409,
        alreadyInList: 409,
        amountOfRollCallsReached: 409,
        incorrectParameter: 422,
        parameterMissing: 422,
        insufficientPermissions: 403,
    }

    @app.exception_handler(rollCallNotStarted)
    @app.exception_handler(rollCallAlreadyStarted)
    @app.exception_handler(alreadyInList)
    @app.exception_handler(amountOfRollCallsReached)
    @app.exception_handler(incorrectParameter)
    @app.exception_handler(parameterMissing)
    @app.exception_handler(insufficientPermissions)
    async def _curated_exception_handler(request: Request, exc: Exception):
        status = _exception_map.get(type(exc), 400)
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error=type(exc).__name__,
                detail=str(exc) or type(exc).__name__,
            ).model_dump(),
        )

    # Rate-limit middleware. Runs before routes; skips /health.
    app.middleware("http")(rate_limit_middleware)

    # Route mounting
    app.include_router(auth.router, prefix=API_PREFIX, tags=["auth"])
    app.include_router(health.router, prefix=API_PREFIX, tags=["health"])
    app.include_router(rollcalls.router, prefix=API_PREFIX, tags=["rollcalls"])
    app.include_router(votes.router, prefix=API_PREFIX, tags=["votes"])
    app.include_router(proxy_votes.router, prefix=API_PREFIX, tags=["proxy-votes"])
    app.include_router(templates.router, prefix=API_PREFIX, tags=["templates"])
    app.include_router(stats.router, prefix=API_PREFIX, tags=["stats", "ghost", "settings"])
    app.include_router(admin.router, prefix=API_PREFIX, tags=["admin"])
    app.include_router(groups.router, prefix=API_PREFIX, tags=["admin", "groups"])
    app.include_router(web_routes.router, prefix=API_PREFIX, tags=["web-voting"])

    # Map proxy-specific exceptions to HTTP status codes
    from exceptions import duplicateProxy, repeatlyName
    from fastapi import status as _status

    @app.exception_handler(duplicateProxy)
    @app.exception_handler(repeatlyName)
    async def _proxy_exception_handler(request, exc):
        return JSONResponse(
            status_code=_status.HTTP_409_CONFLICT,
            content=ErrorResponse(
                error=type(exc).__name__,
                detail=str(exc) or type(exc).__name__,
            ).model_dump(),
        )

    # Web voting pages — self-contained HTML served for both URL patterns.
    # Registered before the /web static mount so explicit routes take priority.
    _web_index = Path(__file__).parent / "web" / "index.html"

    @app.get("/web/join/{token}", response_class=HTMLResponse, include_in_schema=False)
    async def _web_join_page(token: str):
        return HTMLResponse(content=_web_index.read_text())

    @app.get("/web/group/{group_token}", response_class=HTMLResponse, include_in_schema=False)
    async def _web_group_page(group_token: str):
        return HTMLResponse(content=_web_index.read_text())

    # Serve Mini App static files at /miniapp/
    _miniapp_dir = Path(__file__).parent / "miniapp"
    if _miniapp_dir.is_dir():
        app.mount("/miniapp", StaticFiles(directory=str(_miniapp_dir), html=True), name="miniapp")
        logging.info("[api] Mini App served at /miniapp/")

    # Serve web voting static files at /web/
    _web_dir = Path(__file__).parent / "web"
    if _web_dir.is_dir():
        app.mount("/web", StaticFiles(directory=str(_web_dir), html=True), name="web")
        logging.info("[api] Web voting page served at /web/")

    # Serve admin dashboard at /admin/
    _admin_dir = Path(__file__).parent / "admin"
    if _admin_dir.is_dir():
        app.mount("/admin", StaticFiles(directory=str(_admin_dir), html=True), name="admin")
        logging.info("[api] Admin dashboard served at /admin/")

    return app


app = create_app()
