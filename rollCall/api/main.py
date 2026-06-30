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
from fastapi.middleware.cors import CORSMiddleware
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
    timeError,
)
from api.rate_limit import rate_limit_middleware
from api.routes import admin, auth, groups, health, portal, proxy_votes, rollcalls, stats, templates, tg_verify, votes, web as web_routes
from api.schemas.common import ErrorResponse


API_VERSION = "v1"
API_PREFIX = f"/api/{API_VERSION}"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    logging.info("[api] REST API ready at %s (docs: /api/docs)", API_PREFIX)
    # Warn only if no tokens have been issued yet
    try:
        from db import get_connection, release_connection, db_type
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM api_tokens WHERE revoked_at IS NULL")
        row = cur.fetchone()
        cur.close()
        if db_type == "postgresql":
            release_connection(conn)
        count = int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
        if count == 0:
            logging.warning(
                "[api] No API tokens issued — web endpoints are open to anyone "
                "who can reach this port. Issue tokens via scripts/issue_api_token.py "
                "or restrict access via reverse proxy."
            )
        else:
            logging.info("[api] Auth active — %d API token(s) in use", count)
    except Exception:
        logging.warning("[api] Could not check token count — auth status unknown")
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
        timeError: 422,
    }

    @app.exception_handler(rollCallNotStarted)
    @app.exception_handler(rollCallAlreadyStarted)
    @app.exception_handler(alreadyInList)
    @app.exception_handler(amountOfRollCallsReached)
    @app.exception_handler(incorrectParameter)
    @app.exception_handler(parameterMissing)
    @app.exception_handler(insufficientPermissions)
    @app.exception_handler(timeError)
    async def _curated_exception_handler(request: Request, exc: Exception):
        status = _exception_map.get(type(exc), 400)
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error=type(exc).__name__,
                detail=str(exc) or type(exc).__name__,
            ).model_dump(),
        )

    # CORS — allow cross-origin requests from browser-based clients.
    # Configure CORS_ALLOWED_ORIGINS (comma-separated) to restrict in production.
    _cors_origins = os.environ.get("CORS_ALLOWED_ORIGINS", "*").split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # Rate-limit middleware. Runs before routes; skips /health.
    app.middleware("http")(rate_limit_middleware)

    # Route mounting
    app.include_router(auth.router, prefix=API_PREFIX, tags=["auth"])
    app.include_router(tg_verify.router, prefix=API_PREFIX, tags=["auth"])
    app.include_router(health.router, prefix=API_PREFIX, tags=["health"])
    app.include_router(rollcalls.router, prefix=API_PREFIX, tags=["rollcalls"])
    app.include_router(votes.router, prefix=API_PREFIX, tags=["votes"])
    app.include_router(proxy_votes.router, prefix=API_PREFIX, tags=["proxy-votes"])
    app.include_router(templates.router, prefix=API_PREFIX, tags=["templates"])
    app.include_router(stats.router, prefix=API_PREFIX, tags=["stats", "ghost", "settings"])
    app.include_router(admin.router, prefix=API_PREFIX, tags=["admin"])
    app.include_router(groups.router, prefix=API_PREFIX, tags=["admin", "groups"])
    app.include_router(web_routes.router, prefix=API_PREFIX, tags=["web-voting"])
    app.include_router(portal.router, prefix=API_PREFIX, tags=["portal"])

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

    # Public landing page at the site root. Introduces the bot and offers an
    # "Add to Telegram" deep link. The bot username is injected at request time
    # from the live Telegram status so no extra env var is needed.
    _index_index = Path(__file__).parent / "index" / "index.html"

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def _landing_page():
        html = _index_index.read_text()
        try:
            from bot_state import _telegram_status
            uname = (_telegram_status.get("bot_username") or "").lstrip("@")
        except Exception:
            uname = ""
        add_url = f"https://t.me/{uname}?startgroup=true" if uname else "https://telegram.org"
        html = html.replace("{{BOT_USERNAME}}", uname or "RollCall").replace("{{ADD_URL}}", add_url)
        return HTMLResponse(content=html)

    # Web voting pages — self-contained HTML served for both URL patterns.
    # Registered before the /web static mount so explicit routes take priority.
    _web_index = Path(__file__).parent / "web" / "index.html"

    @app.get("/web/join/{token}", response_class=HTMLResponse, include_in_schema=False)
    async def _web_join_page(token: str):
        return HTMLResponse(content=_web_index.read_text())

    @app.get("/web/group/{group_token}", response_class=HTMLResponse, include_in_schema=False)
    async def _web_group_page(group_token: str):
        return HTMLResponse(content=_web_index.read_text())

    # Clean /join/{token} alias — redirects to the web group page.
    # Gives admins a shorter, shareable invite URL.
    from fastapi.responses import RedirectResponse

    @app.get("/join/{token}", include_in_schema=False)
    async def _join_redirect(token: str):
        return RedirectResponse(url=f"/web/group/{token}", status_code=302)

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

    # Serve member portal at /portal/
    _portal_dir = Path(__file__).parent / "portal"
    if _portal_dir.is_dir():
        app.mount("/portal", StaticFiles(directory=str(_portal_dir), html=True), name="portal")
        logging.info("[api] Member portal served at /portal/")

    return app


app = create_app()
