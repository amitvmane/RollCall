"""
Partner webhooks — tell another app the moment something changes in a group.

Events (docs/API.md has the payloads):
  rollcall.opened   a rollcall started (any surface: bot, web, schedule, REST)
  rollcall.closed   it ended or was cancelled ("cancelled": true)
  vote.changed      a member's entry was written (in/out/maybe/waitlist).
                    Also sent for a rename or comment change, so the same
                    status can repeat — treat it as "this entry is now X".
  vote.removed      a member was taken off the rollcall
  dues.posted       a dues ledger entry was added (charges, payments,
                    adjustments — the ledger is append-only, so this is all)
  ping              sent by the "Test" button

How it stays cheap and safe:
  * The hooks are in db.py, at the five functions every surface already goes
    through, so nothing can change without an event. They run inside the
    chat write lock, so they never touch the network: `emit` only writes an
    outbox row, and only for a group that HAS a webhook (an in-memory set —
    for every other group the cost is one set lookup).
  * Delivery is async, after the lock: an immediate attempt, then retries
    with backoff from the minute tick (periodic_jobs), up to MAX_ATTEMPTS.
  * Every request is signed: X-RollCall-Signature: t=<unix>,v1=<hex>, where
    v1 = HMAC-SHA256(secret, "<t>.<body>"). Receivers must check it and
    reject old timestamps.
  * URLs must be https, and the connection only ever goes to a public
    address — the resolver drops private, loopback and link-local results at
    CONNECT time, so a hostname that re-resolves to 10.x can't be used to
    reach this server's network. No redirects are followed.
  * An endpoint that keeps failing is switched off (DISABLE_AFTER_FAILURES
    deliveries in a row); the admin sees it and can switch it back on.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from typing import Optional
from urllib.parse import urlsplit

import db

EVENTS = ("rollcall.opened", "rollcall.closed", "vote.changed", "vote.removed", "dues.posted")
MAX_ATTEMPTS = 8
DISABLE_AFTER_FAILURES = 20
MAX_ENDPOINTS_PER_CHAT = 5
TIMEOUT_SECONDS = 5
_BACKOFF = (60, 120, 300, 900, 1800, 3600, 7200)


def _ph() -> str:
    return "%s" if db.db_type == "postgresql" else "?"


def _row(r, keys):
    return dict(r) if isinstance(r, dict) else dict(zip(keys, r))


# ── Which chats have webhooks (the hot-path check) ───────────────────────────

_hooked_chats: Optional[set] = None
_rc_chat: dict = {}


def _load_hooked() -> set:
    global _hooked_chats
    try:
        with db._cursor() as cursor:
            cursor.execute("SELECT DISTINCT chat_id FROM webhook_endpoints WHERE active = 1")
            _hooked_chats = {int(r["chat_id"] if isinstance(r, dict) else r[0]) for r in cursor.fetchall()}
    except Exception:
        logging.debug("webhooks: endpoints table not readable yet", exc_info=True)
        _hooked_chats = set()
    return _hooked_chats


def _invalidate() -> None:
    global _hooked_chats
    _hooked_chats = None


def _chat_of_rollcall(rollcall_id) -> Optional[int]:
    if rollcall_id in _rc_chat:
        return _rc_chat[rollcall_id]
    try:
        with db._cursor() as cursor:
            cursor.execute(f"SELECT chat_id FROM rollcalls WHERE id = {_ph()}", (rollcall_id,))
            r = cursor.fetchone()
        cid = int(r["chat_id"] if isinstance(r, dict) else r[0]) if r else None
    except Exception:
        return None
    if len(_rc_chat) > 5000:
        _rc_chat.clear()
    _rc_chat[rollcall_id] = cid
    return cid


# ── Emitting (called from db.py; never raises, never touches the network) ───

def emit(event: str, *, chat_id: Optional[int] = None, rollcall_id: Optional[int] = None,
         data: Optional[dict] = None) -> None:
    try:
        hooked = _hooked_chats if _hooked_chats is not None else _load_hooked()
        if not hooked:
            return
        if chat_id is None and rollcall_id is not None:
            chat_id = _chat_of_rollcall(rollcall_id)
        if chat_id is None or int(chat_id) not in hooked:
            return
        _enqueue(int(chat_id), event, {**(data or {}), **({"rollcall_id": rollcall_id} if rollcall_id else {})})
    except Exception:
        logging.exception("webhooks.emit failed for %s", event)


def _enqueue(chat_id: int, event: str, data: dict, only_endpoint: Optional[int] = None) -> int:
    now = int(time.time())
    body = {"type": event, "chat_id": chat_id, "created_at": now, "data": data}
    ph = _ph()
    n = 0
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"SELECT id, events FROM webhook_endpoints WHERE chat_id = {ph} AND active = 1", (chat_id,))
        for r in cursor.fetchall():
            ep = _row(r, ("id", "events"))
            if only_endpoint is not None and int(ep["id"]) != only_endpoint:
                continue
            if event != "ping" and event not in (ep["events"] or "").split(","):
                continue
            cursor.execute(
                f"INSERT INTO webhook_deliveries (endpoint_id, event, payload, next_attempt, created_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph})",
                (int(ep["id"]), event, json.dumps(body, default=str), now, now))
            n += 1
    if n:
        _kick()
    return n


def _kick() -> None:
    """Send soon — after the caller's lock is released, never inside it."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return   # no loop (a script, a sync test): the minute tick sends it

    def _start():
        t = loop.create_task(deliver_due())
        _tasks.add(t)                    # an unreferenced task can be collected mid-run
        t.add_done_callback(_tasks.discard)
    loop.call_soon(_start)


_tasks: set = set()


# ── Endpoints (admin) ─────────────────────────────────────────────────────────

class WebhookRejected(ValueError):
    pass


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise WebhookRejected("The URL must start with https://")
    if len(url) > 500:
        raise WebhookRejected("That URL is too long.")
    host = parts.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None   # a hostname — checked again at connect time (_PublicOnlyResolver)
    # Outside the try: WebhookRejected IS a ValueError, and catching it as
    # "not an IP literal" once let https://127.0.0.1 through.
    if ip is not None and not ip.is_global:
        raise WebhookRejected("The URL must point at a public address.")
    if ip is None and (host == "localhost" or host.endswith((".local", ".internal", ".localhost"))
                       or "." not in host):
        raise WebhookRejected("The URL must point at a public address.")
    return url


def create_endpoint(chat_id: int, url: str, events, created_by: int) -> dict:
    url = _validate_url(url)
    events = [e for e in (events or EVENTS) if e in EVENTS] or list(EVENTS)
    ph = _ph()
    with db._cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM webhook_endpoints WHERE chat_id = {ph}", (chat_id,))
        r = cursor.fetchone()
        if int(list(r.values())[0] if isinstance(r, dict) else r[0]) >= MAX_ENDPOINTS_PER_CHAT:
            raise WebhookRejected(f"A group can have at most {MAX_ENDPOINTS_PER_CHAT} webhooks.")
    secret = "whsec_" + secrets.token_urlsafe(32)
    with db._cursor(commit=True) as cursor:
        cursor.execute(
            f"INSERT INTO webhook_endpoints (chat_id, url, secret, events, created_by, created_at) "
            f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
            (chat_id, url, secret, ",".join(events), created_by, int(time.time())))
    _invalidate()
    ep = [e for e in list_endpoints(chat_id) if e["url"] == url][-1]
    return {**ep, "secret": secret}


def list_endpoints(chat_id: int) -> list:
    keys = ("id", "url", "events", "active", "failures", "created_at", "last_status", "last_delivery_at")
    with db._cursor() as cursor:
        cursor.execute(f"SELECT {', '.join(keys)} FROM webhook_endpoints WHERE chat_id = {_ph()} ORDER BY id",
                       (chat_id,))
        rows = [_row(r, keys) for r in cursor.fetchall()]
    for r in rows:
        r["events"] = [e for e in (r["events"] or "").split(",") if e]
        r["active"] = bool(int(r["active"] or 0))
    return rows


def _owned(chat_id: int, endpoint_id: int) -> None:
    if not any(e["id"] == endpoint_id for e in list_endpoints(chat_id)):
        raise WebhookRejected("No such webhook in this group.")


def delete_endpoint(chat_id: int, endpoint_id: int) -> None:
    _owned(chat_id, endpoint_id)
    ph = _ph()
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM webhook_deliveries WHERE endpoint_id = {ph}", (endpoint_id,))
        cursor.execute(f"DELETE FROM webhook_endpoints WHERE id = {ph} AND chat_id = {ph}", (endpoint_id, chat_id))
    _invalidate()


def set_active(chat_id: int, endpoint_id: int, active: bool) -> None:
    _owned(chat_id, endpoint_id)
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"UPDATE webhook_endpoints SET active = {_ph()}, failures = 0 WHERE id = {_ph()}",
                       (1 if active else 0, endpoint_id))
    _invalidate()


def send_test(chat_id: int, endpoint_id: int) -> int:
    _owned(chat_id, endpoint_id)
    return _enqueue(chat_id, "ping", {"message": "Webhook test from RollCall"}, only_endpoint=endpoint_id)


# ── Delivery ──────────────────────────────────────────────────────────────────

def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


class _PublicOnlyResolver:
    """aiohttp resolver that drops every non-public address at connect time."""

    def __init__(self, inner=None):
        if inner is None:
            from aiohttp.resolver import DefaultResolver
            inner = DefaultResolver()
        self._inner = inner

    async def resolve(self, host, port=0, family=socket.AF_INET):
        hosts = await self._inner.resolve(host, port, family)
        public = [h for h in hosts if ipaddress.ip_address(h["host"]).is_global]
        if not public:
            raise OSError(f"{host} does not resolve to a public address")
        return public

    async def close(self):
        await self._inner.close()


async def _post(url: str, secret: str, delivery_id: int, event: str, body: bytes) -> Optional[str]:
    """Send one delivery. Returns None on success, else a short error."""
    import aiohttp
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "User-Agent": "RollCall-Webhooks/1",
               "X-RollCall-Event": event, "X-RollCall-Delivery": str(delivery_id),
               "X-RollCall-Signature": sign(secret, ts, body)}
    connector = aiohttp.TCPConnector(resolver=_PublicOnlyResolver())
    try:
        async with aiohttp.ClientSession(connector=connector,
                                         timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as s:
            async with s.post(url, data=body, headers=headers, allow_redirects=False) as resp:
                if 200 <= resp.status < 300:
                    return None
                return f"HTTP {resp.status}"
    except Exception as e:
        return type(e).__name__ + (f": {e}" if str(e) else "")[:200]


# A flag, not an asyncio.Lock: a module-level Lock binds to whichever event
# loop first waits on it, and every send runs on the one loop anyway.
_delivering = {"on": False}


async def deliver_due(limit: int = 50) -> int:
    """Send every delivery that's due. One at a time per process (a second
    call while one runs just returns). Returns how many were attempted."""
    if _delivering["on"]:
        return 0
    _delivering["on"] = True
    try:
        return await _deliver_due(limit)
    finally:
        _delivering["on"] = False


async def _deliver_due(limit: int) -> int:
    now = int(time.time())
    ph = _ph()
    keys = ("id", "endpoint_id", "event", "payload", "attempts", "url", "secret", "chat_id")
    with db._cursor() as cursor:
        cursor.execute(
            f"SELECT d.id, d.endpoint_id, d.event, d.payload, d.attempts, e.url, e.secret, e.chat_id "
            f"FROM webhook_deliveries d JOIN webhook_endpoints e ON e.id = d.endpoint_id "
            f"WHERE d.status = 'pending' AND d.next_attempt <= {ph} AND e.active = 1 "
            f"ORDER BY d.id LIMIT {int(limit)}", (now,))
        due = [_row(r, keys) for r in cursor.fetchall()]
    for d in due:
        body = json.dumps({"id": d["id"], **json.loads(d["payload"])}).encode()
        err = await _post(d["url"], d["secret"], d["id"], d["event"], body)
        _record(d, err)
    return len(due)


def _record(d: dict, err: Optional[str]) -> None:
    ph = _ph()
    now = int(time.time())
    attempts = int(d["attempts"]) + 1
    with db._cursor(commit=True) as cursor:
        if err is None:
            cursor.execute(f"UPDATE webhook_deliveries SET status='delivered', attempts={ph}, last_error=NULL "
                           f"WHERE id={ph}", (attempts, d["id"]))
            cursor.execute(f"UPDATE webhook_endpoints SET failures=0, last_status='ok', last_delivery_at={ph} "
                           f"WHERE id={ph}", (now, d["endpoint_id"]))
            return
        final = attempts >= MAX_ATTEMPTS
        nxt = now + _BACKOFF[min(attempts - 1, len(_BACKOFF) - 1)]
        cursor.execute(f"UPDATE webhook_deliveries SET status={ph}, attempts={ph}, next_attempt={ph}, "
                       f"last_error={ph} WHERE id={ph}",
                       ("failed" if final else "pending", attempts, nxt, err, d["id"]))
        cursor.execute(f"UPDATE webhook_endpoints SET last_status={ph}, last_delivery_at={ph}"
                       + (", failures = failures + 1" if final else "") + f" WHERE id={ph}",
                       (err[:200], now, d["endpoint_id"]))
        if final:
            cursor.execute(f"UPDATE webhook_endpoints SET active = 0 WHERE id = {ph} AND failures >= {ph}",
                           (d["endpoint_id"], DISABLE_AFTER_FAILURES))
    if final:
        _invalidate()
        logging.warning("[webhooks] delivery %s to endpoint %s gave up: %s", d["id"], d["endpoint_id"], err)


def prune(days: int = 31) -> int:
    """Delivered/failed rows older than `days` (the outbox mustn't grow for
    ever). 31, not less: the usage dashboard's 30-day view counts them."""
    cutoff = int(time.time()) - days * 86400
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM webhook_deliveries WHERE status <> 'pending' AND created_at < {_ph()}",
                       (cutoff,))
        return cursor.rowcount or 0
