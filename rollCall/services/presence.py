"""
Lightweight presence tracking — who is viewing a group page right now.

Uses an in-memory dict (resets on restart — acceptable for traffic awareness)
plus a DB-backed total-view counter that persists across restarts.

Session TTL: 90 s.  Clients heartbeat every 30 s so a tab that closes
disappears from the active count within 90 s.
"""
import time
from typing import Dict

_SESSION_TTL = 90  # seconds

# {group_token: {session_id: last_seen_epoch}}
_sessions: Dict[str, Dict[str, float]] = {}


def heartbeat(token: str, session_id: str) -> bool:
    """Record a heartbeat for session_id on token.

    Returns True if this is a brand-new session (use to increment DB counter).
    """
    bucket = _sessions.setdefault(token, {})
    is_new = session_id not in bucket
    bucket[session_id] = time.time()
    return is_new


def active_count(token: str) -> int:
    """Number of sessions that sent a heartbeat within the last SESSION_TTL seconds."""
    cutoff = time.time() - _SESSION_TTL
    return sum(1 for ts in _sessions.get(token, {}).values() if ts >= cutoff)


def prune() -> None:
    """Drop stale sessions — called by the bot's memory_prune_loop."""
    cutoff = time.time() - _SESSION_TTL
    for token in list(_sessions):
        _sessions[token] = {s: t for s, t in _sessions[token].items() if t >= cutoff}
        if not _sessions[token]:
            del _sessions[token]
