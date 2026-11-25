"""
"Are you a person?" — a CAPTCHA for the actions anyone can take without
signing in: sending a sign-in email, and voting as a guest (which posts a
name into the Telegram group).

Two modes (HUMAN_CHECK):

  pow        (default) Self-hosted proof of work, no third party and no keys.
             The server hands out a signed, single-use challenge; the browser
             finds a nonce whose SHA-256 has POW_BITS leading zero bits
             (~65k hashes ≈ a second on a phone, invisible to a person). It
             doesn't prove a human, it makes every request cost real work, so
             scripted spam at volume becomes expensive. Works with the strict
             CSP — nothing external is loaded.
  turnstile  Cloudflare Turnstile (TURNSTILE_SITE_KEY + TURNSTILE_SECRET_KEY).
             Stronger against bots and automated AI agents: Cloudflare scores
             the browser itself. The page loads Cloudflare's script, which the
             CSP then allows.
  off        No check.

Passing once gives the browser a HUMAN PASS — a scoped token valid
PASS_TTL_SECONDS — so a person is checked once per visit, not per tap. A pass
is only a "probably not a script" signal: everything behind it keeps its own
limits (per-address and per-IP email caps, rate limiter, vote rules).
Signed-in identities (Telegram, email) skip the check: they already proved
more than a CAPTCHA does.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from exceptions import insufficientPermissions

POW_BITS = 16
CHALLENGE_TTL_SECONDS = 300
PASS_TTL_SECONDS = 12 * 3600
_PASS_SCOPE = "human_pass"
_PASS_USER = 1   # a pass names no one; the token format needs an integer

_used: dict = {}   # challenge id -> expiry, so a solved challenge works once
# Protected actions one pass covers. Without a cap, one solved check would buy
# a script 12 hours of unlimited guest votes / sign-in emails; with it, spam
# at volume needs a fresh proof of work every few actions.
MAX_USES_PER_PASS = 30
_uses: dict = {}   # pass signature -> [uses, expiry]


class HumanCheckFailed(insufficientPermissions):
    """A 403 the page answers by running the check and retrying."""


def mode() -> str:
    m = os.environ.get("HUMAN_CHECK", "pow").strip().lower() or "pow"
    if m == "turnstile" and not (os.environ.get("TURNSTILE_SITE_KEY") and os.environ.get("TURNSTILE_SECRET_KEY")):
        logging.warning("[human_check] HUMAN_CHECK=turnstile without keys — using proof of work")
        return "pow"
    return m if m in ("pow", "turnstile", "off") else "pow"


def config() -> dict:
    m = mode()
    out = {"mode": m}
    if m == "pow":
        out["bits"] = POW_BITS
    if m == "turnstile":
        out["site_key"] = os.environ.get("TURNSTILE_SITE_KEY", "")
    return out


def _key() -> bytes:
    from api.identity import _signing_keys
    return hashlib.sha256(b"RollCallHumanCheckV1" + _signing_keys()[0]).digest()


# ── Proof of work ─────────────────────────────────────────────────────────────

def new_challenge() -> dict:
    """{challenge, bits}. challenge = <id>.<expiry>.<sig>: stateless to hand out."""
    cid = secrets.token_hex(12)
    exp = int(time.time()) + CHALLENGE_TTL_SECONDS
    sig = hmac.new(_key(), f"{cid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return {"challenge": f"{cid}.{exp}.{sig}", "bits": POW_BITS}


def _leading_zero_bits(digest: bytes) -> int:
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        return n + (8 - b.bit_length())
    return n


def _verify_pow(challenge: str, nonce: str) -> bool:
    try:
        cid, exp_s, sig = challenge.split(".")
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    want = hmac.new(_key(), f"{cid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    now = time.time()
    if not hmac.compare_digest(want, sig) or exp < now:
        return False
    if not nonce or len(nonce) > 32:
        return False
    digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()
    if _leading_zero_bits(digest) < POW_BITS:
        return False
    # Single use. Expired ids are swept as we go, so this stays small.
    for k in [k for k, v in _used.items() if v < now]:
        _used.pop(k, None)
    if cid in _used:
        return False
    _used[cid] = exp
    return True


# ── Turnstile ─────────────────────────────────────────────────────────────────

async def _verify_turnstile(token: str, client_ip: Optional[str]) -> bool:
    if not token or len(token) > 4096:
        return False
    try:
        import aiohttp
        data = {"secret": os.environ.get("TURNSTILE_SECRET_KEY", ""), "response": token}
        if client_ip:
            data["remoteip"] = client_ip
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.post("https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data) as r:
                body = await r.json(content_type=None)
        return bool(body.get("success"))
    except Exception:
        # Cloudflare unreachable: refuse (fail closed) — a person retries; a
        # silent pass would hand bots exactly the outage they'd wait for.
        logging.exception("[human_check] Turnstile verify failed")
        return False


# ── Passes ────────────────────────────────────────────────────────────────────

async def issue_pass(*, challenge: Optional[str] = None, nonce: Optional[str] = None,
                     turnstile_token: Optional[str] = None, client_ip: Optional[str] = None) -> str:
    """Check a solved challenge (or Turnstile token) and return a human pass."""
    m = mode()
    ok = (m == "off"
          or (m == "pow" and _verify_pow(challenge or "", nonce or ""))
          or (m == "turnstile" and await _verify_turnstile(turnstile_token or "", client_ip)))
    if not ok:
        raise HumanCheckFailed("The human check didn't pass — please try again.")
    from api.identity import issue_scoped_token
    return issue_scoped_token(_PASS_USER, _PASS_SCOPE, PASS_TTL_SECONDS)


def has_pass(token: Optional[str]) -> bool:
    if mode() == "off":
        return True
    from api.identity import verify_scoped_token
    return verify_scoped_token(token, _PASS_SCOPE) == _PASS_USER


def use_pass(token: Optional[str]) -> bool:
    """True if the pass is valid AND has uses left; counts this use."""
    if mode() == "off":
        return True
    if not has_pass(token):
        return False
    now = time.time()
    if len(_uses) > 20000:
        for k in [k for k, v in _uses.items() if v[1] < now]:
            _uses.pop(k, None)
    sig = token.rsplit(".", 1)[-1]
    rec = _uses.setdefault(sig, [0, now + PASS_TTL_SECONDS])
    if rec[0] >= MAX_USES_PER_PASS:
        return False
    rec[0] += 1
    return True


def require(token: Optional[str]) -> None:
    """Raise HumanCheckFailed without a valid pass with uses left."""
    if not use_pass(token):
        raise HumanCheckFailed("Please confirm you're a person first.")


def reset_for_tests() -> None:
    _used.clear()
    _uses.clear()
