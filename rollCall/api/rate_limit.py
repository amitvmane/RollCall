"""
Per-token sliding-window rate limiter.

In-memory, keyed by the token's SHA-256 hash (so unauthenticated requests
share a single anonymous bucket and are throttled aggressively to dampen
unauth flood patterns).

State is lost on bot restart — acceptable for the protection level we
need here. If burst patterns become a real concern we move to Redis.
The window and per-window limit are env-configurable:

  REST_API_RATE_LIMIT_WINDOW_SECONDS=60   (default 60s)
  REST_API_RATE_LIMIT_MAX_REQUESTS=60     (default 60 requests / window)

Skips paths in `_BYPASS_PATHS` (currently /health and OpenAPI metadata).
"""

import os
import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import Request, status
from fastapi.responses import JSONResponse


_BYPASS_PATHS = (
    "/api/v1/health",
    "/api/v1/openapi.json",
    "/api/docs",
    "/api/redoc",
    "/docs/oauth2-redirect",
)


def _settings() -> tuple[int, int]:
    """(window_seconds, max_requests). Read every call so tests can mutate
    env between runs; cost is negligible vs the cost of a rate decision."""
    window = int(os.environ.get("REST_API_RATE_LIMIT_WINDOW_SECONDS", "60"))
    maxreq = int(os.environ.get("REST_API_RATE_LIMIT_MAX_REQUESTS", "60"))
    return window, maxreq


# Per-key timestamp deques. Each entry is a unix timestamp of a request.
# We trim from the left to drop entries older than the window.
_buckets: Dict[str, Deque[float]] = defaultdict(deque)


def reset_buckets_for_tests() -> None:
    """Test-only: clear all rate-limit state. Avoid mutating _buckets
    directly from tests so we can change the implementation freely."""
    _buckets.clear()


def _bucket_key(request: Request) -> str:
    """
    Choose a bucket key. Prefer the authed token's hash (set on
    request.state by api.auth.require_scope). Falls back to client IP
    so unauthenticated requests still get a (shared, aggressive) cap.
    """
    token = getattr(request.state, "api_token", None)
    if token is not None:
        # Use chat_id + label as a stable handle without re-hashing.
        return f"token:{token.chat_id}:{token.label or ''}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


async def rate_limit_middleware(request: Request, call_next):
    """ASGI middleware: enforce per-bucket sliding window."""
    if request.url.path in _BYPASS_PATHS:
        return await call_next(request)

    window, maxreq = _settings()
    now = time.monotonic()
    cutoff = now - window

    key = _bucket_key(request)
    bucket = _buckets[key]

    # Trim expired entries (entries older than `window` seconds ago).
    while bucket and bucket[0] < cutoff:
        bucket.popleft()

    if len(bucket) >= maxreq:
        # Oldest entry in the bucket dictates how soon the client can retry.
        retry_after = max(1, int(bucket[0] + window - now) + 1)
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "rateLimitExceeded",
                "detail": f"Rate limit: {maxreq} requests / {window}s. "
                          f"Retry after {retry_after}s.",
            },
            headers={"Retry-After": str(retry_after)},
        )

    bucket.append(now)
    return await call_next(request)
