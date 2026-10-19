"""
Per-token sliding-window rate limiter.

In-memory. A request carrying a real API token is bucketed by that token's
hash; everything else (no token, or a bearer that isn't a live token) is
bucketed by client IP, so unauthenticated floods share one bucket per IP and
are throttled aggressively.

State is lost on bot restart — acceptable for the protection level we
need here. If burst patterns become a real concern we move to Redis.
The window and per-window limit are env-configurable:

  REST_API_RATE_LIMIT_WINDOW_SECONDS=60   (default 60s)
  REST_API_RATE_LIMIT_MAX_REQUESTS=60     (default 60 requests / window)

Skips paths in `_BYPASS_PATHS` (currently /health and OpenAPI metadata).
"""

import hashlib
import logging
import os
import time
from collections import OrderedDict, deque
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


# Per-key timestamp deques, in least-recently-touched order (every request
# moves its key to the end). Each entry is a monotonic timestamp of a request;
# we trim from the left to drop entries older than the window.
_buckets: "OrderedDict[str, Deque[float]]" = OrderedDict()

# Hard ceiling on tracked keys. Past it the least recently seen key goes —
# that resets an idle client's counter, never an active flooder's (each of
# its requests moves it to the end), and keeps memory bounded however many
# source IPs show up.
_MAX_BUCKETS = 10_000

# Stale keys are swept at most once per window, not on every request: the old
# "sweep whenever >1000 keys" re-scanned the whole dict on EVERY request once
# 1000 clients were active inside one window — O(n) per request.
# -inf, not 0.0: time.monotonic() is seconds since boot, so on a host up
# for less than one window a 0.0 start meant no sweep at all in that window.
_last_sweep = [float('-inf')]

# Bearer hashes known to be live tokens -> monotonic time that knowledge
# expires. Only a VALID token earns its own bucket: keying on any bearer
# string let a client send a fresh random bearer per request and get a fresh
# bucket each time, bypassing the per-IP limit entirely.
_valid_tokens: Dict[str, float] = {}
_VALID_TOKEN_CACHE_SECONDS = 60
_VALID_TOKEN_CACHE_MAX = 4096


def reset_buckets_for_tests() -> None:
    """Test-only: clear all rate-limit state. Avoid mutating _buckets
    directly from tests so we can change the implementation freely."""
    _buckets.clear()
    _valid_tokens.clear()
    _last_sweep[0] = float('-inf')


def _is_valid_token(token_hash: str, now: float) -> bool:
    exp = _valid_tokens.get(token_hash)
    if exp is not None and exp > now:
        return True
    try:
        from db import lookup_api_token
        ok = lookup_api_token(token_hash) is not None
    except Exception:
        logging.warning("rate_limit: token lookup failed — bucketing by IP", exc_info=True)
        return False
    if ok:
        if len(_valid_tokens) >= _VALID_TOKEN_CACHE_MAX:
            _valid_tokens.clear()
        _valid_tokens[token_hash] = now + _VALID_TOKEN_CACHE_SECONDS
    return ok


def _bucket_key(request: Request, now: float = None) -> str:
    """
    Choose a bucket key. Middleware runs before FastAPI dependencies so
    request.state.api_token is never set here. Instead, read the
    Authorization header directly: a bearer that is a live API token gets its
    own isolated bucket; anything else — no header, or a bearer we don't
    recognise — shares the client IP's bucket.
    """
    if now is None:
        now = time.monotonic()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        raw = auth[7:].strip()
        if raw:
            from db import _hash_token
            if _is_valid_token(_hash_token(raw), now):
                return "token:" + hashlib.sha256(raw.encode()).hexdigest()[:24]
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def _sweep(cutoff: float) -> None:
    """Drop keys with no request inside the window. LRU order puts the stale
    ones at the front, so this stops at the first live key."""
    while _buckets:
        k, dq = next(iter(_buckets.items()))
        if dq and dq[-1] >= cutoff:
            break
        del _buckets[k]


async def rate_limit_middleware(request: Request, call_next):
    """ASGI middleware: enforce per-bucket sliding window."""
    if request.url.path in _BYPASS_PATHS:
        return await call_next(request)

    window, maxreq = _settings()
    now = time.monotonic()
    cutoff = now - window

    if now - _last_sweep[0] >= window:
        _sweep(cutoff)
        _last_sweep[0] = now

    key = _bucket_key(request, now)
    bucket = _buckets.get(key)
    if bucket is None:
        bucket = deque()
        _buckets[key] = bucket
        while len(_buckets) > _MAX_BUCKETS:
            _buckets.popitem(last=False)
    else:
        _buckets.move_to_end(key)

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
