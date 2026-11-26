"""
The partner-facing conventions every REST call gets (docs/API.md §5):

  X-Request-Id        echoed if the caller sent a sane one, otherwise made
                      here; on every /api/ response and in every error body
                      (`request_id`), so a partner and an operator can talk
                      about one call.
  Idempotency-Key     on POST/PUT/PATCH/DELETE under /api/v1/chats/ with a
                      live bearer key: the first answer is stored for 24h and
                      replayed (`Idempotent-Replayed: true`) for a retry with
                      the same key — a timeout-and-retry can't start a second
                      rollcall. The same key with a different request is a
                      422; while the first is still running, a 409. 5xx
                      answers are not stored, so a retry really retries.
  X-RollCall-Key-Expires   on calls made with an app key: when it stops
                      working (RFC 3339, UTC), so partners can alert before.

Rate-limit headers are added by api/rate_limit.py.
"""

import hashlib
import logging
import re
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, Response

import db

IDEMPOTENCY_TTL_SECONDS = 24 * 3600
_MAX_STORED_BODY = 256 * 1024
_REQ_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_IDEM_RE = re.compile(r"^[\x21-\x7e]{1,255}$")
_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_PARTNER_PREFIX = "/api/v1/chats/"


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _error(status: int, error: str, detail: str, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail, "request_id": request_id})


def _claim(token_hash: str, key: str, request_hash: str):
    """Insert the in-progress row. Returns None if we own it now, else the
    existing row (status, request_hash, content_type, response)."""
    ph = _ph()
    try:
        with db._cursor(commit=True) as cursor:
            cursor.execute(
                f"INSERT INTO api_idempotency (token_hash, idem_key, request_hash, status, created_at) "
                f"VALUES ({ph},{ph},{ph},0,{ph})", (token_hash, key, request_hash, int(time.time())))
        return None
    except Exception:
        pass   # primary key taken: someone used this key already
    with db._cursor() as cursor:
        cursor.execute(
            f"SELECT status, request_hash, content_type, response, created_at FROM api_idempotency "
            f"WHERE token_hash={ph} AND idem_key={ph}", (token_hash, key))
        row = cursor.fetchone()
    if row is None:
        return {"status": 0, "request_hash": request_hash}   # raced a prune; treat as busy
    row = dict(row) if isinstance(row, dict) else dict(
        zip(("status", "request_hash", "content_type", "response", "created_at"), row))
    if int(row.get("created_at") or 0) < time.time() - IDEMPOTENCY_TTL_SECONDS:
        # Stale (prune hasn't run yet): start over under this key.
        _release(token_hash, key)
        return _claim(token_hash, key, request_hash)
    return row


def _release(token_hash: str, key: str) -> None:
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM api_idempotency WHERE token_hash={_ph()} AND idem_key={_ph()}",
                       (token_hash, key))


def _store(token_hash: str, key: str, status: int, content_type: str, body: bytes) -> None:
    ph = _ph()
    with db._cursor(commit=True) as cursor:
        cursor.execute(
            f"UPDATE api_idempotency SET status={ph}, content_type={ph}, response={ph} "
            f"WHERE token_hash={ph} AND idem_key={ph}",
            (status, content_type, body.decode("utf-8", "replace"), token_hash, key))


def prune(now: float = None) -> int:
    now = int(now or time.time())
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM api_idempotency WHERE created_at<{_ph()}", (now - IDEMPOTENCY_TTL_SECONDS,))
        return cursor.rowcount or 0


async def _collect(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


def _live_token_hash(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw = auth[7:].strip()
    if not raw:
        return None
    from api.rate_limit import _is_valid_token
    h = db._hash_token(raw)
    return h if _is_valid_token(h, time.monotonic()) else None


def _decorate(response, request: Request, request_id: str):
    response.headers["X-Request-Id"] = request_id
    tok = getattr(request.state, "api_token", None)
    exp = getattr(tok, "expires_at", None) if tok is not None else None
    if tok is not None and getattr(tok, "app_key", False) and exp is not None:
        try:
            response.headers["X-RollCall-Key-Expires"] = exp.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            pass
    return response


def request_id_for(request: Request) -> str:
    """This request's id: the caller's if it's sane, else a new one. Stored
    on request.state so every layer (rate limiter, error handlers) agrees."""
    rid = getattr(request.state, "request_id", None)
    if rid:
        return rid
    sent = request.headers.get("X-Request-Id", "")
    rid = sent if _REQ_ID_RE.match(sent) else uuid.uuid4().hex
    request.state.request_id = rid
    return rid


async def partner_gateway_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    request_id = request_id_for(request)

    idem = request.headers.get("Idempotency-Key")
    if idem is None or request.method not in _WRITE_METHODS or not path.startswith(_PARTNER_PREFIX):
        return _decorate(await call_next(request), request, request_id)
    if not _IDEM_RE.match(idem):
        return _decorate(_error(422, "invalidIdempotencyKey",
                                "Idempotency-Key must be 1–255 visible ASCII characters", request_id),
                         request, request_id)
    token_hash = _live_token_hash(request)
    if token_hash is None:
        # No live key: auth will refuse it anyway — nothing worth remembering.
        return _decorate(await call_next(request), request, request_id)

    body = await request.body()
    request_hash = hashlib.sha256(
        request.method.encode() + b" " + path.encode() + b"?" + request.url.query.encode() + b"\n" + body
    ).hexdigest()
    existing = _claim(token_hash, idem, request_hash)
    if existing is not None:
        if existing.get("request_hash") != request_hash:
            return _decorate(_error(422, "idempotencyKeyReused",
                                    "This Idempotency-Key was already used for a different request", request_id),
                             request, request_id)
        if int(existing.get("status") or 0) == 0:
            return _decorate(_error(409, "idempotencyInProgress",
                                    "A request with this Idempotency-Key is still being processed — retry shortly",
                                    request_id), request, request_id)
        replay = Response(content=(existing.get("response") or "").encode(),
                          status_code=int(existing["status"]),
                          media_type=existing.get("content_type") or "application/json")
        replay.headers["Idempotent-Replayed"] = "true"
        return _decorate(replay, request, request_id)

    try:
        response = await call_next(request)
    except Exception:
        _release(token_hash, idem)
        raise
    content = await _collect(response)
    headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length",)}
    out = Response(content=content, status_code=response.status_code, headers=headers,
                   media_type=response.media_type)
    try:
        if response.status_code >= 500 or len(content) > _MAX_STORED_BODY:
            _release(token_hash, idem)
        else:
            _store(token_hash, idem, response.status_code,
                   response.headers.get("content-type") or "application/json", content)
    except Exception:
        logging.exception("[partner_gateway] idempotency store failed")
    return _decorate(out, request, request_id)


def add_request_id(payload: dict, request: Request) -> dict:
    """Error bodies carry the request id (additive field — docs/API.md)."""
    rid = getattr(request.state, "request_id", None)
    if rid:
        payload = {**payload, "request_id": rid}
    return payload


__all__ = ["partner_gateway_middleware", "prune", "add_request_id", "request_id_for"]
