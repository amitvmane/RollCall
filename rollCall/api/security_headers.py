"""Response security headers for every web surface.

Added 2026-08-17 after an adversarial review found the API served no security
headers at all. The headers here are chosen around two constraints that make
the obvious "just set a strict CSP and DENY framing" answer wrong for this app:

1. The Mini App at /miniapp/ is embedded by Telegram Web in an IFRAME. A blanket
   `X-Frame-Options: DENY` / `frame-ancestors 'none'` would break it for anyone
   using web.telegram.org rather than a native client. So framing is denied
   everywhere EXCEPT /miniapp/, which allows Telegram origins only.

2. Every web surface uses inline `onclick="..."` handlers (the app has no build
   step). A CSP without `'unsafe-inline'` in script-src would kill every button.

Consequence worth being honest about: because of (2) this CSP is NOT an XSS
defense. Its value here is `frame-ancestors` (clickjacking), `object-src 'none'`,
`base-uri 'self'` (base-tag injection), and blocking off-origin form posts. XSS
defense still rests on the esc()/escJsAttr() discipline in the front-end JS.

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
_FRAMEABLE_PREFIXES = ("/miniapp",)

_CSP_COMMON = (
    "default-src 'self'; "
    "img-src 'self' data: blob:; "
    # 'unsafe-inline' is load-bearing — see module docstring.
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


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
        csp = f"{_CSP_COMMON}; frame-ancestors {_TELEGRAM_FRAME_ANCESTORS}"
    else:
        csp = f"{_CSP_COMMON}; frame-ancestors 'none'"
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

    if _wants_hsts():
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )

    return response
