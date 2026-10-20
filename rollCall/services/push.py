"""
Web push notification service.

VAPID keys are auto-generated on first use and stored in system_config.
Environment variables VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY take priority.

Sending is fire-and-forget: callers await notify_rollcall_started() /
notify_users() which schedule work on a small thread pool (webpush is
synchronous) and never raise on send failures.

No VAPID env vars is a working configuration, not a disabled one: the keypair
is generated on first use and persisted. Setting the env vars only matters to
pin the keys across a lost database (new keys = every member re-subscribes).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import db as _db

_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="webpush")
_vapid_cache: Optional[dict] = None


def _load_or_create_vapid() -> tuple[str, str]:
    """Return (private_key_pem, public_key_b64url). Cached after first call."""
    global _vapid_cache
    if _vapid_cache:
        return _vapid_cache["private"], _vapid_cache["public"]

    priv_env = os.environ.get("VAPID_PRIVATE_KEY")
    pub_env = os.environ.get("VAPID_PUBLIC_KEY")
    if priv_env and pub_env:
        _vapid_cache = {"private": priv_env, "public": pub_env}
        return priv_env, pub_env

    priv_db = _db.get_system_config("vapid_private_key")
    pub_db = _db.get_system_config("vapid_public_key")
    if priv_db and pub_db:
        _vapid_cache = {"private": priv_db, "public": pub_db}
        return priv_db, pub_db

    # Generate new EC P-256 key pair for VAPID
    from py_vapid import Vapid
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    v = Vapid()
    v.generate_keys()
    priv_pem = v.private_pem().decode()
    pub_bytes = v.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    _db.set_system_config("vapid_private_key", priv_pem)
    _db.set_system_config("vapid_public_key", pub_b64)
    _vapid_cache = {"private": priv_pem, "public": pub_b64}
    logging.info("[push] Generated new VAPID key pair and persisted to DB")
    return priv_pem, pub_b64


def get_public_key() -> str:
    """Return the VAPID public key as a base64url string (served to browsers)."""
    _, pub = _load_or_create_vapid()
    return pub


# push-subscribe is unauthenticated (the group's public link is enough), and
# every stored endpoint later receives a server-side POST. Without these
# bounds it was a blind SSRF primitive (point it at an internal URL, then wait
# for a rollcall to start) and an unbounded table.
MAX_ENDPOINT_LENGTH = 1024
MAX_ACTIVE_SUBSCRIPTIONS_PER_GROUP = 500

_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


class PushSubscriptionRejected(ValueError):
    """User-safe reason a subscription was refused."""


# The browsers' own push services — Chrome/Edge-on-Android/Opera/Samsung
# (FCM), Firefox (autopush), Safari/iOS (APNs web push), Edge on Windows
# (WNS). A push endpoint is a URL the SERVER fetches, so it is an SSRF
# vector; checking that a hostname "looks public" is not enough, because a
# name can resolve to a public IP when checked and to 127.0.0.1 or the LAN
# when the push is sent (DNS rebinding). Hosts under these suffixes are
# DNS-controlled by Google/Mozilla/Apple/Microsoft, so a caller cannot
# re-point them.
_PUSH_SERVICE_SUFFIXES = (
    "fcm.googleapis.com", "android.googleapis.com",
    "push.services.mozilla.com",
    "push.apple.com",
    "notify.windows.com",
)


def _is_push_service(host: str) -> bool:
    return any(host == s or host.endswith("." + s) for s in _PUSH_SERVICE_SUFFIXES)


def is_allowed_endpoint(endpoint: str) -> bool:
    try:
        validate_endpoint(endpoint)
        return True
    except PushSubscriptionRejected:
        return False


def validate_endpoint(endpoint: str) -> None:
    """Accept only an https URL on a known browser push service.

    Also checked again at send time (_send_one): rows stored before the
    allowlist existed are skipped there rather than fetched.
    """
    import ipaddress
    from urllib.parse import urlsplit

    if not endpoint or len(endpoint) > MAX_ENDPOINT_LENGTH:
        raise PushSubscriptionRejected("Invalid push endpoint.")
    try:
        parts = urlsplit(endpoint)
        host = (parts.hostname or "").rstrip(".").lower()
        port = parts.port
    except ValueError:
        raise PushSubscriptionRejected("Invalid push endpoint.")
    if parts.scheme != "https" or not host or parts.username or parts.password:
        raise PushSubscriptionRejected("Push endpoint must be an https URL.")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip or "." not in host or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise PushSubscriptionRejected("Push endpoint must be a public push service.")
    if port not in (None, 443) or not _is_push_service(host):
        raise PushSubscriptionRejected(
            "This browser's notification service isn't supported. "
            "Try Chrome, Firefox, Safari or Edge.")


def subscribe(group_token: str, endpoint: str, p256dh: str, auth: str, tg_user_id: Optional[int] = None) -> None:
    """Store (or refresh) a subscription. Raises PushSubscriptionRejected."""
    validate_endpoint(endpoint)
    # Refreshing a subscription this group already has is always fine — the
    # cap only stops NEW rows, so a full group can't lock out existing members.
    if not _db.is_push_subscribed(endpoint, group_token):
        if len(_db.get_push_subscriptions(group_token)) >= MAX_ACTIVE_SUBSCRIPTIONS_PER_GROUP:
            raise PushSubscriptionRejected(
                "This group has reached its notification subscriber limit."
            )
    _db.save_push_subscription(group_token, endpoint, p256dh, auth, tg_user_id=tg_user_id)
    logging.info("[push] subscribe: group=%s tg_user_id=%s endpoint=%.40s", group_token[:12], tg_user_id, endpoint)


def unsubscribe(endpoint: str, group_token: str) -> bool:
    """Remove one group's subscription for an endpoint. Returns whether it matched.

    group_token is required, not optional: this is reachable from an
    unauthenticated route, so it is the only bound on what a caller can
    cancel. Making it a positional argument means a new call site cannot
    forget it and silently get the unscoped behaviour back.
    """
    removed = _db.delete_push_subscription(endpoint, group_token=group_token)
    logging.info("[push] unsubscribe: group=%s endpoint=%.40s matched=%s",
                 group_token[:12], endpoint, removed)
    return removed


def _send_one(sub: dict, payload: str, priv_pem: str):
    """Blocking send — runs in the thread pool.

    Returns a status the caller acts on; it never touches the database. Expired
    endpoints are reported back as ("expired", endpoint) and pruned by the
    caller on the event-loop thread, so SQLite's single shared connection is
    only ever used from one thread.
    """
    from pywebpush import webpush, WebPushException
    if not is_allowed_endpoint(sub["endpoint"]):
        logging.warning("[push] skipped non-push-service endpoint %.40s", sub["endpoint"])
        return "failed"
    try:
        webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=payload,
            vapid_private_key=priv_pem,
            vapid_claims={"sub": "mailto:rollcall-push@rollcall.bot"},
        )
        return "ok"
    except WebPushException as exc:
        resp = exc.response
        if resp is not None and resp.status_code in (404, 410):
            return ("expired", sub["endpoint"])
        logging.warning("[push] send failed %.40s: %s", sub["endpoint"], exc)
        return "failed"
    except Exception:
        logging.exception("[push] send_one unexpected error %.40s", sub["endpoint"])
        return "failed"


async def _dispatch(subs: list, payload: str, priv_pem: str) -> int:
    """Fan out sends across the thread pool, then prune expired endpoints.

    The deletes run here on the event-loop thread — never inside a worker —
    so the shared SQLite connection stays single-threaded. Returns the number
    of pushes successfully delivered.
    """
    loop = asyncio.get_running_loop()
    tasks = [loop.run_in_executor(_executor, _send_one, sub, payload, priv_pem) for sub in subs]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    sent = 0
    expired: list[str] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        if isinstance(r, tuple) and r and r[0] == "expired":
            expired.append(r[1])
        elif r == "ok":
            sent += 1

    # Unscoped on purpose: the push provider has told us this endpoint no
    # longer exists, so it is dead in every group that holds it, not just
    # the one we happened to be sending for.
    for endpoint in expired:
        try:
            _db.delete_push_subscription(endpoint)
            logging.debug("[push] Removed expired subscription %.40s", endpoint)
        except Exception:
            logging.exception("[push] failed to remove expired subscription %.40s", endpoint)

    return sent


def group_link(chat_id: int) -> tuple[Optional[str], Optional[str]]:
    """Return (group_token, page_url) for a chat, or (None, None) if it has no web page.

    The one place every push call site resolves where a notification should
    open, so "which URL does a tap land on" can't drift between them.
    get_or_create_chat, not manager.get_chat -- that cache only ever populates
    a hardcoded 6-field subset and never included group_web_token, so it
    always read None and push silently no-op'd.
    """
    chat = _db.get_or_create_chat(chat_id)
    group_token = chat.get("group_web_token") if chat else None
    if not group_token:
        return None, None
    web_base = os.environ.get("WEB_BASE_URL", "").rstrip("/")
    return group_token, f"{web_base}/web/group/{group_token}"


async def _notify(group_token: str, subs: list, title: str, body: str, url: str,
                  tag: str, label: str) -> int:
    """Send one notification to `subs`. Never raises; returns how many landed.

    `tag` groups notifications on the device: a new one with the same tag
    replaces the old rather than stacking, so it must differ between kinds
    (a waitlist promotion must not overwrite "rollcall opened").
    """
    if not subs:
        return 0
    try:
        priv_pem, _ = _load_or_create_vapid()
        payload = json.dumps({
            "title": title,
            "body": body,
            "url": url,
            "tag": tag,
            "icon": "/web/icon-192.png",
            "badge": "/web/icon-192.png",
        })
        sent = await _dispatch(subs, payload, priv_pem)
        logging.info("[push] %s: sent=%d/%d group=%s", label, sent, len(subs), group_token[:12])
        return sent
    except Exception:
        logging.exception("[push] %s failed for group=%s", label, group_token[:12])
        return 0


async def notify_rollcall_ended(group_token: str, title: str, url: str) -> None:
    """Fire push to all active subscribers when a rollcall closes."""
    if not group_token:
        return
    await _notify(group_token, _db.get_push_subscriptions(group_token),
                  f"\U0001f4cb {title} closed",
                  "The rollcall has ended — tap to see the final list",
                  url, "rollcall-ended", "notify_ended")


async def notify_rollcall_started(group_token: str, title: str, url: str) -> None:
    """
    Fire push notifications to all active subscribers for this group.
    Non-blocking — failures are logged but never raised.
    """
    if not group_token:
        return
    await _notify(group_token, _db.get_push_subscriptions(group_token),
                  f"\U0001f3af {title}", "Rollcall just opened — tap to vote",
                  url, "rollcall-open", "notify")


async def notify_users(chat_id: int, user_ids, title: str, body: str, tag: str) -> int:
    """Push to specific members of a group, on every device they subscribed from.

    The web-app counterpart of a Telegram DM. Only subscriptions that were made
    while signed in carry a tg_user_id, so an anonymous subscriber is never a
    target here -- there is no way to know it is them. Never raises.
    """
    wanted = {u for u in user_ids if isinstance(u, int)}
    if not wanted:
        return 0
    try:
        group_token, url = group_link(chat_id)
        if not group_token:
            return 0
        subs = [s for s in _db.get_push_subscriptions(group_token)
                if s.get("tg_user_id") in wanted]
    except Exception:
        logging.exception("[push] notify_users lookup failed chat=%s", chat_id)
        return 0
    return await _notify(group_token, subs, title, body, url, tag, f"notify_users[{tag}]")
