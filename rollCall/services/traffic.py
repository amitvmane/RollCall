"""
"API hits": how much traffic this deployment takes, per hour.

Two sources: `telegram` (every update the bot receives — commands, button
taps, messages) and `rest` (every REST / web-page API request). Counted in
memory on the hot path (a dict increment — no I/O per request) and flushed to
api_hits once a minute from the periodic tick, so a busy minute costs one
upsert per source, not one write per request. A crash loses at most that
minute's counts.

`peaks()` answers "how busy does it get": the busiest hour per source over a
window, plus the last 24 hours. Rows older than RETENTION_DAYS are pruned.
"""

import logging
import threading
import time
from typing import Optional

import db

SOURCES = ("telegram", "rest")
RETENTION_DAYS = 90

_counts: dict = {}           # (hour_epoch, source) -> n, not yet flushed
_lock = threading.Lock()


def _hour(ts: Optional[float] = None) -> int:
    t = int(ts if ts is not None else time.time())
    return t - t % 3600


def count(source: str, n: int = 1) -> None:
    """Hot path. Never raises."""
    try:
        key = (_hour(), source)
        with _lock:
            _counts[key] = _counts.get(key, 0) + n
    except Exception:
        pass


def flush() -> int:
    """Add the in-memory counts to api_hits. Returns rows written."""
    with _lock:
        pending = dict(_counts)
        _counts.clear()
    if not pending:
        return 0
    ph = "%s" if db.db_type == "postgresql" else "?"
    try:
        with db._cursor(commit=True) as cursor:
            for (hour, source), n in pending.items():
                cursor.execute(
                    f"INSERT INTO api_hits (hour, source, n) VALUES ({ph},{ph},{ph}) "
                    f"ON CONFLICT (hour, source) DO UPDATE SET n = api_hits.n + excluded.n"
                    if db.db_type != "postgresql" else
                    f"INSERT INTO api_hits (hour, source, n) VALUES ({ph},{ph},{ph}) "
                    f"ON CONFLICT (hour, source) DO UPDATE SET n = api_hits.n + EXCLUDED.n",
                    (hour, source, n))
        return len(pending)
    except Exception:
        # Put them back so the next tick retries rather than losing them.
        with _lock:
            for k, n in pending.items():
                _counts[k] = _counts.get(k, 0) + n
        logging.exception("traffic.flush failed")
        return 0


def peaks(days: int = 30) -> dict:
    """{source: {"peak": n, "peak_hour": epoch|None, "last_24h": n}}, flushed
    counts plus what's still in memory."""
    since = _hour() - days * 86400
    out = {s: {"peak": 0, "peak_hour": None, "last_24h": 0} for s in SOURCES}
    per_hour: dict = {}
    try:
        with db._cursor() as cursor:
            cursor.execute(f"SELECT hour, source, n FROM api_hits WHERE hour >= "
                           f"{'%s' if db.db_type == 'postgresql' else '?'}", (since,))
            for r in cursor.fetchall():
                h, s, n = (r["hour"], r["source"], r["n"]) if isinstance(r, dict) else r
                per_hour[(int(h), s)] = per_hour.get((int(h), s), 0) + int(n)
    except Exception:
        logging.exception("traffic.peaks read failed")
    with _lock:
        for k, n in _counts.items():
            per_hour[k] = per_hour.get(k, 0) + n
    day_ago = _hour() - 23 * 3600
    for (h, s), n in per_hour.items():
        if s not in out:
            continue
        if n > out[s]["peak"]:
            out[s]["peak"], out[s]["peak_hour"] = n, h
        if h >= day_ago:
            out[s]["last_24h"] += n
    return out


def prune() -> int:
    cutoff = _hour() - RETENTION_DAYS * 86400
    with db._cursor(commit=True) as cursor:
        cursor.execute(f"DELETE FROM api_hits WHERE hour < {'%s' if db.db_type == 'postgresql' else '?'}",
                       (cutoff,))
        return cursor.rowcount or 0


def summary_line(tz_name: str = "Asia/Kolkata") -> str:
    """One line for /health: 'Traffic (30d peak/h): Telegram 412 (Tue 21:00) · REST 120 (…)'."""
    import pytz
    from datetime import datetime
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.utc
    p = peaks()
    parts = []
    for s, label in (("telegram", "Telegram"), ("rest", "REST")):
        d = p[s]
        when = (datetime.fromtimestamp(d["peak_hour"], tz).strftime("%a %d %b %H:00")
                if d["peak_hour"] else "—")
        parts.append(f"{label} {d['peak']} ({when}), {d['last_24h']} in 24h")
    return "Traffic, busiest hour in 30 days: " + " · ".join(parts)
