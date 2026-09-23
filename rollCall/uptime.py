"""How long the bot has been up, and how long it was down before that.

Its own module for the same reason `backup_status.py` is: runner.py is the
entry point, and importing it from a handler while it runs as ``__main__``
creates a *second* module object. Both /health surfaces need this, so it
belongs somewhere neither one owns.

Why this exists: /health answers "is every subsystem alive **right now**".
Nothing answered "how long were you dead". The watchdog notices an outage and
says so, but it only sees what it could poll — when the whole host loses power
or network, the watchdog is down too, and the outage leaves no trace anywhere.
The bot itself is the only observer that survives that, because it can write a
liveness stamp to the database and read it back on the next boot.

The mechanism is one row in `system_config`:

  uptime_heartbeat   unix seconds, rewritten on every minute tick
  uptime_downtime    JSON list of the most recent gaps (bounded, see _MAX_LOG)
  uptime_since       unix seconds of the first boot that ever tracked this,
                     so an availability figure can say what window it covers
                     instead of implying 30 days of history it doesn't have

On boot we compare the stamp to the clock. The true outage lies somewhere in
[gap - HEARTBEAT_INTERVAL, gap] — the bot may have died up to one whole tick
after its last stamp — so every figure here is reported as approximate and the
message names both timestamps, which are exact. At minute granularity this is
noise against the outages anyone cares about.

No migration: `system_config` is the existing arbitrary key/value store.
"""
import json
import logging
import os
import time
from typing import Optional

# Matches the minute tick in check_reminders.check_template_schedules() that
# calls beat(). If that loop's cadence changes, change this with it — it is
# the measurement error on every gap.
HEARTBEAT_INTERVAL = 60

HEARTBEAT_KEY = "uptime_heartbeat"
DOWNTIME_KEY = "uptime_downtime"
SINCE_KEY = "uptime_since"

# Gaps shorter than this are recorded but never announced. A `make build`
# deploy or a healthcheck restart is a 20-40 second gap that happens on
# purpose; paging a human for those would train them to ignore the channel
# that also carries real outages.
DOWNTIME_MIN_SECONDS = max(0, int(float(os.environ.get("DOWNTIME_MIN_MINUTES", "5")) * 60))

# The log is a single row rewritten in place, so it has to be bounded here or
# it grows forever — the exact failure mode the 2026-09-06 audit fixed in four
# other places.
_MAX_LOG = 50

# Set once, at import, before anything else can take time. Wall clock (not
# monotonic) because it has to be comparable with a stamp written by a
# previous process.
PROCESS_STARTED_AT = time.time()

# Filled in by record_boot_gap() so /health and the reconnect announcement can
# both read the same answer without recomputing it.
_boot_gap: dict = {"gap": None}


def format_duration(seconds: Optional[float]) -> str:
    """Human duration: '3h 12m', '2d 4h', '45s'.

    Two units, and no seconds component above a minute — every gap here is
    measured to HEARTBEAT_INTERVAL, so "59m 59s" would be precision the
    number does not have. Matches human_duration() in scripts/watchdog.sh,
    which formats the same kind of figure for the same reader.
    """
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def _read_float(key: str) -> Optional[float]:
    try:
        import db as _db
        raw = _db.get_system_config(key)
        return float(raw) if raw else None
    except Exception:
        # get_system_config already logs; a missing stamp must never be the
        # reason a boot or a tick fails.
        return None


def beat() -> None:
    """Record 'alive at this instant'. Called once per minute tick."""
    try:
        import db as _db
        _db.set_system_config(HEARTBEAT_KEY, f"{time.time():.0f}")
    except Exception:
        logging.exception("uptime: heartbeat write failed")


def read_log() -> list:
    """Recorded gaps, oldest first. Always a list, even when unreadable."""
    try:
        import db as _db
        raw = _db.get_system_config(DOWNTIME_KEY)
        if not raw:
            return []
        entries = json.loads(raw)
        return entries if isinstance(entries, list) else []
    except Exception:
        logging.exception("uptime: downtime log unreadable — treating as empty")
        return []


def record_boot_gap() -> Optional[dict]:
    """Compare this boot against the last heartbeat; log and return any gap.

    Returns ``{"from", "to", "sec", "announce"}`` when the previous process
    left a stamp, else None (a first-ever boot has nothing to compare with and
    must not be reported as an outage). ``announce`` is whether the gap
    cleared DOWNTIME_MIN_SECONDS — the caller decides what to do about it, so
    that the log stays complete even when the notification is suppressed.
    """
    import db as _db

    now = PROCESS_STARTED_AT
    try:
        if _read_float(SINCE_KEY) is None:
            _db.set_system_config(SINCE_KEY, f"{now:.0f}")
    except Exception:
        logging.exception("uptime: could not stamp tracking-start")

    last = _read_float(HEARTBEAT_KEY)
    if last is None:
        return None

    gap = now - last
    # A stamp from the future means the clock moved backwards (NTP step, or a
    # restore of an older database). There is no outage to report and writing
    # a negative one would poison every later average.
    if gap <= 0:
        return None

    entry = {
        "from": int(last),
        "to": int(now),
        "sec": int(gap),
        "announce": gap >= DOWNTIME_MIN_SECONDS,
    }
    _boot_gap["gap"] = entry

    try:
        log = read_log()
        log.append({k: entry[k] for k in ("from", "to", "sec")})
        _db.set_system_config(DOWNTIME_KEY, json.dumps(log[-_MAX_LOG:]))
    except Exception:
        logging.exception("uptime: could not append to downtime log")

    return entry


def boot_gap() -> Optional[dict]:
    """The gap this process booted into, or None. Set by record_boot_gap()."""
    return _boot_gap["gap"]


def uptime_seconds() -> float:
    return max(0.0, time.time() - PROCESS_STARTED_AT)


def summary(days: int = 30) -> dict:
    """Downtime over the trailing window, clipped to what is actually known.

    ``window_sec`` is the *measured* window — trailing `days`, or the time
    since tracking began if that is shorter. Reporting 100% availability over
    30 days when the feature has been live for two would be a lie told by
    arithmetic.
    """
    now = time.time()
    window_start = now - days * 86400
    since = _read_float(SINCE_KEY) or PROCESS_STARTED_AT
    measured_from = max(window_start, since)
    window_sec = max(0.0, now - measured_from)

    # One read: this is a database round-trip, and /health calls it.
    log = read_log()

    total = 0
    count = 0
    for entry in log:
        try:
            end = float(entry.get("to", 0))
            sec = float(entry.get("sec", 0))
        except (TypeError, ValueError):
            continue
        if end < measured_from:
            continue
        # A gap straddling the window edge counts only the part inside it.
        total += min(sec, end - measured_from)
        count += 1

    availability = None
    if window_sec > 0:
        availability = max(0.0, min(100.0, 100.0 * (1 - total / window_sec)))

    return {
        "days": days,
        "window_sec": window_sec,
        "downtime_sec": int(total),
        "outages": count,
        "availability": availability,
        "last": log[-1] if log else None,
    }
