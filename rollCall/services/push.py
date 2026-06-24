"""
Web push notification service.

VAPID keys are auto-generated on first use and stored in system_config.
Environment variables VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY take priority.

Sending is fire-and-forget: callers await notify_rollcall_started() which
schedules work on a small thread pool (webpush is synchronous) and does not
raise on individual send failures.
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


def subscribe(group_token: str, endpoint: str, p256dh: str, auth: str) -> None:
    _db.save_push_subscription(group_token, endpoint, p256dh, auth)
    logging.info("[push] subscribe: group=%s endpoint=%.40s", group_token[:12], endpoint)


def unsubscribe(endpoint: str) -> None:
    _db.delete_push_subscription(endpoint)
    logging.info("[push] unsubscribe: endpoint=%.40s", endpoint)


def _send_one(sub: dict, payload: str, priv_pem: str) -> None:
    """Blocking send — runs in thread pool."""
    from pywebpush import webpush, WebPushException
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
    except WebPushException as exc:
        resp = exc.response
        if resp is not None and resp.status_code in (404, 410):
            _db.delete_push_subscription(sub["endpoint"])
            logging.debug("[push] Removed expired subscription %.40s", sub["endpoint"])
        else:
            logging.warning("[push] send failed %.40s: %s", sub["endpoint"], exc)
    except Exception:
        logging.exception("[push] send_one unexpected error %.40s", sub["endpoint"])


async def notify_rollcall_started(group_token: str, title: str, url: str) -> None:
    """
    Fire push notifications to all active subscribers for this group.
    Non-blocking — failures are logged but never raised.
    """
    if not group_token:
        return
    try:
        subs = _db.get_push_subscriptions(group_token)
        if not subs:
            return
        priv_pem, _ = _load_or_create_vapid()
        payload = json.dumps({
            "title": f"\U0001f3af {title}",
            "body": "Rollcall just opened — tap to vote",
            "url": url,
            "icon": "/web/logo.svg",
            "badge": "/web/logo.svg",
        })
        loop = asyncio.get_running_loop()
        tasks = [
            loop.run_in_executor(_executor, _send_one, sub, payload, priv_pem)
            for sub in subs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        sent = sum(1 for r in results if not isinstance(r, Exception))
        logging.info("[push] notify: sent=%d/%d group=%s title=%r", sent, len(subs), group_token[:12], title)
    except Exception:
        logging.exception("[push] notify_rollcall_started failed for group=%s", group_token[:12])
