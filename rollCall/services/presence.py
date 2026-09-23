"""
Lightweight presence tracking — who is viewing a group page right now.

Two separate questions, deliberately keyed on two different things:

  active_now   how many tabs are open  -> keyed on the client's session id,
               which is per-tab and exactly right for this. In memory only,
               reset on restart; traffic awareness, not a record.

  total_views  how many people have ever looked -> keyed on something the
               client CANNOT choose, because it increments a DB counter that
               is displayed on the group page and never goes down.

That second distinction is the whole reason this module has two dicts. The
counter used to increment on every previously-unseen session_id, and
session_id arrives in the request body — so anyone could POST random UUIDs in
a loop and move a public number as far as they liked. It is keyed on the
viewer (their address) with its own long window now, which also makes the
number mean something better than it did: distinct visitors, not distinct
random strings.

Session TTL: 90 s. Clients heartbeat every 30 s so a tab that closes
disappears from the active count within 90 s.
"""
import time
from typing import Dict, Optional

_SESSION_TTL = 90  # seconds

# How long before the same viewer counts as a new view again. Long enough that
# a refresh, a reopened tab, or coming back after lunch is the same visit;
# short enough that "total views" still moves with real repeat traffic.
_COUNT_WINDOW = 12 * 3600

# {group_token: {session_id: last_seen_epoch}} — presence only.
_sessions: Dict[str, Dict[str, float]] = {}

# {group_token: {viewer_key: last_counted_epoch}} — gate for the DB counter.
_counted: Dict[str, Dict[str, float]] = {}


def heartbeat(token: str, session_id: str, viewer_key: Optional[str] = None) -> bool:
    """Record a heartbeat. Returns True if this VIEW should be counted.

    `viewer_key` is server-derived (the client address), never taken from the
    request body — it is the thing that makes the return value unspoofable.
    When the caller genuinely cannot determine one it falls back to the
    session id, which is the old behaviour and no worse than it was; that
    path is for a missing client address, not for an attacker to select.
    """
    now = time.time()
    _sessions.setdefault(token, {})[session_id] = now

    key = viewer_key or session_id
    seen = _counted.setdefault(token, {})
    last = seen.get(key)
    if last is not None and now - last < _COUNT_WINDOW:
        return False
    seen[key] = now
    return True


def active_count(token: str) -> int:
    """Number of sessions that sent a heartbeat within the last SESSION_TTL seconds."""
    cutoff = time.time() - _SESSION_TTL
    return sum(1 for ts in _sessions.get(token, {}).values() if ts >= cutoff)


def prune() -> None:
    """Drop stale entries — called by the bot's memory_prune_loop.

    Both dicts, on their own clocks. _counted holds entries far longer than
    _sessions by design, so it needs its own cutoff rather than sharing one;
    pruning it on the 90 s session TTL would reset every viewer's gate a
    minute and a half after they arrived and hand the inflation back.
    """
    now = time.time()
    for store, ttl in ((_sessions, _SESSION_TTL), (_counted, _COUNT_WINDOW)):
        cutoff = now - ttl
        for token in list(store):
            store[token] = {k: t for k, t in store[token].items() if t >= cutoff}
            if not store[token]:
                del store[token]
