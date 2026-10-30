"""Response security headers for every web surface.

Added 2026-08-17 after an adversarial review found the API served no security
headers at all. The headers here are chosen around two constraints that make
the obvious "just set a strict CSP and DENY framing" answer wrong for this app:

1. The Mini App is embedded by Telegram Web in an IFRAME. A blanket
   `X-Frame-Options: DENY` / `frame-ancestors 'none'` would break it for anyone
   using web.telegram.org rather than a native client. So framing is denied
   everywhere EXCEPT the Mini App surfaces (/web and the retired /miniapp),
   which allow Telegram origins only.

2. Script: since 10.9 there is no inline script anywhere — handlers are
   declared as data-on-click="fn" and wired by /shared/actions.js — so
   script-src refuses 'unsafe-inline'. Markup that slips past escaping can no
   longer run code; at most it can name one of the page's own functions.
   (Until 10.9 every button was an inline onclick, 'unsafe-inline' was
   load-bearing, and this CSP was no XSS defence at all.)

   'unsafe-eval' is added ONLY when TG_LOGIN_WIDGET is on: Telegram's login
   widget eval()s its data-onauth attribute. Our own code never evals.
   style-src keeps 'unsafe-inline' — templates set style="" everywhere, and
   CSS injection can't execute script.

Why clickjacking matters for this app specifically: the group page carries
one-click destructive admin actions (end a rollcall, delete a template), and it
authenticates from a token in localStorage rather than a cookie — so a
same-origin iframe carries full admin authority. SameSite cookie protections
that would normally blunt this do not apply.
"""

import os

# Telegram Web embeds Mini Apps from these origins.
_TELEGRAM_FRAME_ANCESTORS = "https://web.telegram.org https://*.telegram.org"

# Paths that legitimately get framed. Prefix match on the request path.
#
# /web is here because it IS the Mini App now — the Telegram menu button
# points at the group web page, and Telegram Web embeds Mini Apps in an
# iframe. Without this the page is served with frame-ancestors 'none' and
# renders as a blank panel inside web.telegram.org, with the failure visible
# only in the browser console of a client we don't control.
#
# /miniapp stays listed for the retired standalone app: a deployment that
# hasn't updated MINIAPP_URL yet still has Telegram pointing at it.
#
# Framing is still refused to everyone except Telegram, so this is not a
# clickjacking hole — an attacker's page cannot embed either surface.
_FRAMEABLE_PREFIXES = ("/miniapp", "/web")

# Third-party origins the pages genuinely load from. Everything here was
# blocked outright by the original 'self'-only policy, silently — a CSP
# violation is a console message in the visitor's browser and nothing at all
# on the server, so all three failed without a single log line:
#
#   telegram.org        the Mini App SDK (telegram-web-app.js) and the Login
#                       Widget (telegram-widget.js). With it blocked,
#                       window.Telegram never exists, so a member opening the
#                       app from Telegram is treated as an anonymous visitor:
#                       asked to sign in, then asked to paste a group link.
#   oauth.telegram.org  the iframe the Login Widget renders into.
#   fonts.googleapis    the stylesheet, and fonts.gstatic the font files. Both
#   / fonts.gstatic     blocked, so every page has been falling back to system
#                       fonts rather than the typography it ships with.
_TELEGRAM_SCRIPTS = "https://telegram.org"
_TELEGRAM_FRAMES = "https://oauth.telegram.org"
_GOOGLE_FONTS_CSS = "https://fonts.googleapis.com"
_GOOGLE_FONTS_FILES = "https://fonts.gstatic.com"

def _login_widget_enabled() -> bool:
    return os.environ.get("TG_LOGIN_WIDGET", "").strip().lower() in ("1", "true", "yes")


def _csp_common() -> str:
    # Read per request, not at import: tests and a changed env must see it.
    evaluate = " 'unsafe-eval'" if _login_widget_enabled() else ""
    return (
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        # No 'unsafe-inline' — see module docstring.
        f"script-src 'self'{evaluate} {_TELEGRAM_SCRIPTS}; "
        # Inline <script> blocks are refused by the line above; this one
        # also refuses inline on*= attributes explicitly, for browsers that
        # honour the -attr directive separately.
        "script-src-attr 'none'; "
        f"style-src 'self' 'unsafe-inline' {_GOOGLE_FONTS_CSS}; "
        f"font-src 'self' data: {_GOOGLE_FONTS_FILES}; "
        f"frame-src {_TELEGRAM_FRAMES}; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


# Assets whose staleness desyncs the client from the server. Served through
# Cloudflare, which caches by file extension and — when the origin says nothing
# about caching — applies its own default TTL of four hours.
#
# Starlette's StaticFiles sends ETag and Last-Modified but no Cache-Control, so
# that default is exactly what happened on the 2026-09-04 deploy: the edge kept
# serving a 24 July copy of /shared/tokens.css, the file carrying the type
# scale, while serving the current version of every other stylesheet. Visitors
# got 13.6px or 17px body text from the same URL depending on which edge node
# they reached, and browsers were told to hold their own copy for four hours on
# top of that.
#
# `no-cache` does not mean "don't cache" — the edge still stores the file and
# still serves it. It means "ask first", so an unchanged file comes back as a
# 304 with no body (StaticFiles already sends the validators that make that
# work) and a deployed change is visible on the next request instead of up to
# eight hours later.
#
# Images are deliberately left on the default TTL: they're effectively
# content-addressed by filename, and a stale icon can't desync anything — the
# same reasoning the service worker uses for its cache-first image branch.
_REVALIDATE_SUFFIXES = (".css", ".js")


def _should_revalidate(path: str, response) -> bool:
    if path.startswith("/api/"):
        return False
    if path.endswith(_REVALIDATE_SUFFIXES):
        return True
    # Directory indexes ("/web/") carry no suffix, and the HTML is what
    # references the CSS — caching it stale pins the whole page.
    return response.headers.get("content-type", "").startswith("text/html")


def _wants_hsts() -> bool:
    """Only advertise HSTS when the deployment is actually reachable over
    HTTPS. Sending it from a plain-HTTP local run would pin a browser to a
    scheme that host does not serve, which is a self-inflicted outage."""
    return os.environ.get("WEB_BASE_URL", "").strip().lower().startswith("https://")


async def security_headers_middleware(request, call_next):
    response = await call_next(request)

    path = request.url.path
    frameable = any(path.startswith(p) for p in _FRAMEABLE_PREFIXES)

    if frameable:
        # No X-Frame-Options: it has no allowlist form, and its ALLOW-FROM
        # variant is dead in every current browser. frame-ancestors is the
        # mechanism that actually works, and it supersedes XFO where both
        # are understood.
        csp = f"{_csp_common()}; frame-ancestors {_TELEGRAM_FRAME_ANCESTORS}"
    else:
        csp = f"{_csp_common()}; frame-ancestors 'none'"
        response.headers.setdefault("X-Frame-Options", "DENY")

    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # same-origin, not no-referrer: the group page URL carries the group token
    # in its PATH (/web/group/{token}), so a full Referer sent to a third-party
    # host would hand over the credential. same-origin keeps internal
    # navigation working while never leaking the token off-site.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=(), payment=()"
    )

    # setdefault, so a route that already chose no-store (the group page, the
    # dues QR) keeps the stricter answer.
    if _should_revalidate(path, response):
        response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")

    if _wants_hsts():
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    return response
