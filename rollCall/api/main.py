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
    duesGameAlreadyClosed,
    duesNothingToClose,
    incorrectParameter,
    insufficientPermissions,
    parameterMissing,
    rollCallAlreadyStarted,
    rollCallNotStarted,
    timeError,
)
from api.rate_limit import rate_limit_middleware
from api.security_headers import security_headers_middleware
from api.routes import admin, auth, commands as commands_routes, dues as dues_routes, groups, health, portal, proxy_votes, rollcalls, stats, templates, tg_verify, votes, web as web_routes
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


def _docs_enabled() -> bool:
    """Whether to expose /api/docs, /api/redoc and the OpenAPI schema.

    Default false — see the docs_url comment in create_app(). Matches the
    truthy-string convention used by REST_API_ENABLED in runner.py.
    """
    return os.environ.get("API_DOCS_ENABLED", "").strip().lower() in ("true", "1", "yes", "on")


def create_app() -> FastAPI:
    app = FastAPI(
        title="RollCall API",
        description=(
            "REST surface for the RollCall Telegram bot. Backs future "
            "web/Mini-App clients and third-party integrations. Calls "
            "the same `services/` layer the bot will use internally."
        ),
        version="0.1.0",
        # Interactive docs are OFF by default. They are unauthenticated and
        # sit on the rate-limiter's bypass list, so on a tunnel-exposed
        # deployment they hand any visitor a complete, rate-unlimited map of
        # every endpoint and payload shape. Nothing in the product needs them
        # at runtime — they're a development aid — so the default is closed
        # and operators opt in with API_DOCS_ENABLED=true (e.g. on localhost
        # while working on the API).
        docs_url="/api/docs" if _docs_enabled() else None,
        redoc_url="/api/redoc" if _docs_enabled() else None,
        openapi_url=f"{API_PREFIX}/openapi.json" if _docs_enabled() else None,
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
        duesNothingToClose: 404,
        duesGameAlreadyClosed: 409,
    }

    @app.exception_handler(rollCallNotStarted)
    @app.exception_handler(rollCallAlreadyStarted)
    @app.exception_handler(alreadyInList)
    @app.exception_handler(amountOfRollCallsReached)
    @app.exception_handler(incorrectParameter)
    @app.exception_handler(parameterMissing)
    @app.exception_handler(insufficientPermissions)
    @app.exception_handler(timeError)
    @app.exception_handler(duesNothingToClose)
    @app.exception_handler(duesGameAlreadyClosed)
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
    # Configure CORS_ALLOWED_ORIGINS (comma-separated) to restrict further.
    # Default derives from WEB_BASE_URL (already required for web voting to
    # work at all, so this tightens the out-of-the-box default without any
    # extra operator action) rather than a blanket "*" — identity tokens are
    # long-lived (30 days) and can end up in access logs/Referer headers via
    # URL query params, so a wildcard origin means anyone who obtains a
    # leaked token cross-origin can also read authenticated API responses
    # with it, not just send the request. Falls back to "*" only when
    # WEB_BASE_URL itself is also unset (no known origin to restrict to —
    # local/dev use).
    _web_base = os.environ.get("WEB_BASE_URL", "").strip().rstrip("/")
    _cors_default = _web_base or "*"
    # .strip() before the `or` so an empty-but-present value (e.g. a .env
    # file with a blank "CORS_ALLOWED_ORIGINS=" line, left uncommented by
    # habit) falls through to the default too, not just a truly absent key.
    _cors_origins = (os.environ.get("CORS_ALLOWED_ORIGINS", "").strip() or _cors_default).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Identity-Token"],
    )

    # Rate-limit middleware. Runs before routes; skips /health.
    app.middleware("http")(rate_limit_middleware)

    # Security headers on every response, including static files and the
    # 429s the rate limiter returns above. Registered last so it wraps
    # outermost — see api/security_headers.py for why the CSP allows
    # 'unsafe-inline' and why /miniapp/ is framing-exempt.
    app.middleware("http")(security_headers_middleware)

    # Route mounting
    app.include_router(auth.router, prefix=API_PREFIX, tags=["auth"])
    app.include_router(tg_verify.router, prefix=API_PREFIX, tags=["auth"])
    app.include_router(health.router, prefix=API_PREFIX, tags=["health"])
    app.include_router(commands_routes.router, prefix=API_PREFIX, tags=["commands"])
    app.include_router(rollcalls.router, prefix=API_PREFIX, tags=["rollcalls"])
    app.include_router(votes.router, prefix=API_PREFIX, tags=["votes"])
    app.include_router(proxy_votes.router, prefix=API_PREFIX, tags=["proxy-votes"])
    app.include_router(templates.router, prefix=API_PREFIX, tags=["templates"])
    app.include_router(stats.router, prefix=API_PREFIX, tags=["stats", "ghost", "settings"])
    app.include_router(admin.router, prefix=API_PREFIX, tags=["admin"])
    app.include_router(groups.router, prefix=API_PREFIX, tags=["admin", "groups"])
    app.include_router(web_routes.router, prefix=API_PREFIX, tags=["web-voting"])
    app.include_router(dues_routes.router, prefix=API_PREFIX, tags=["dues"])
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
            if not uname:
                # Falls back to the persisted value when the process
                # restarted while Telegram was unreachable (get_me() never
                # ran, so _telegram_status was never populated) — otherwise
                # the "Add to Telegram" CTA silently breaks until reconnect.
                from db import get_system_config
                uname = (get_system_config("bot_username") or "").lstrip("@")
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

    # Browsers request /favicon.ico by default (bookmarks, old tabs, curl
    # users); the HTML <link rel="icon"> tags don't cover that path.
    _favicon = Path(__file__).parent / "web" / "logo.svg"

    @app.get("/favicon.ico", include_in_schema=False)
    async def _favicon_fallback():
        from fastapi.responses import FileResponse
        if _favicon.is_file():
            return FileResponse(str(_favicon), media_type="image/svg+xml")
        return RedirectResponse(url="/web/logo.svg", status_code=302)

    # Serve shared design tokens (CSS vars, dark-mode init script, and the
    # base stylesheet portal builds on — formerly served from the now-
    # retired admin console) consumed by all web surfaces at /shared/
    _shared_dir = Path(__file__).parent / "shared"
    if _shared_dir.is_dir():
        app.mount("/shared", StaticFiles(directory=str(_shared_dir)), name="shared")

    # The standalone Mini App that used to live at /miniapp/ is gone. It was a
    # second, smaller voting UI — pick a group, vote, and nothing else: no
    # stats, no dues, no admin — so every feature added to the web app had to
    # be either duplicated there or left missing, and it was always the one
    # left missing. The group web page now runs as the Mini App itself
    # (body.tg-mode, and its home screen is the group picker), so MINIAPP_URL
    # should point at /web/. Same retirement as the admin console in 10.0.
    #
    # The endpoints it used are NOT retired: /auth/telegram/miniapp still
    # authenticates the web app from Telegram initData, and /portal/groups
    # still backs the group picker.
    #
    # MINIAPP_URL lives in each deployment's own .env, so it can still point
    # here after an upgrade — and the menu button is registered with Telegram
    # at startup, meaning a 404 would be what every member's "Open RollCall"
    # button did until someone noticed. Redirect instead, exactly as /admin
    # does. 302, not 301: this one is expected to stop being needed once the
    # env var is updated, and a permanently-cached redirect would outlive it.
    @app.get("/miniapp", include_in_schema=False)
    @app.get("/miniapp/{_path:path}", include_in_schema=False)
    async def _miniapp_retired_redirect(_path: str = ""):
        return RedirectResponse(url="/web/", status_code=302)

    # Serve web voting static files at /web/
    _web_dir = Path(__file__).parent / "web"
    if _web_dir.is_dir():
        app.mount("/web", StaticFiles(directory=str(_web_dir), html=True), name="web")
        logging.info("[api] Web voting page served at /web/")

    # The standalone admin console SPA is retired (2026-08-10) — every task
    # it did has lived on the group web page since 2026-08-05 (see
    # commit a0068ab and later). Old bookmarks/links redirect to /portal
    # rather than 404ing outright. The bearer-token REST API it used to
    # call (api/routes/admin.py, still used for /gentoken scripted access)
    # is unaffected — this only removes the static UI.
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/{_path:path}", include_in_schema=False)
    async def _admin_retired_redirect(_path: str = ""):
        return RedirectResponse(url="/portal", status_code=301)

    # Serve member portal at /portal/
    _portal_dir = Path(__file__).parent / "portal"
    if _portal_dir.is_dir():
        app.mount("/portal", StaticFiles(directory=str(_portal_dir), html=True), name="portal")
        logging.info("[api] Member portal served at /portal/")

    # Serve the command reference page at /help/ — renders GET /api/v1/commands
    # (commands_registry.py, the same source /help renders in Telegram).
    _help_dir = Path(__file__).parent / "help"
    if _help_dir.is_dir():
        app.mount("/help", StaticFiles(directory=str(_help_dir), html=True), name="help")
        logging.info("[api] Command reference served at /help/")

    return app


app = create_app()
