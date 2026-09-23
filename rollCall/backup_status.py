"""How fresh the local database backups are.

Its own module rather than living in runner.py because runner is the entry
point: `import runner` from a handler while runner is running as __main__
creates a *second* module object and re-executes its module-level code
(logging setup, env validation, health-state globals). Both /health and the
/health bot command need this, so it belongs somewhere neither owns.

Why this exists at all: every other signal in /health answers "is this
subsystem alive". Backups are the one subsystem whose death produces no
symptom until the day you need it — the db-backup sidecar sat stopped from
2026-08-03 to 2026-08-24 and said nothing, so the newest snapshot was three
weeks old at the moment it mattered.
"""
import os
import time

# Same default as `make backup-check`: snapshots are daily, so 48h allows one
# missed cycle before complaining.
BACKUP_MAX_AGE_HOURS = int(os.environ.get("BACKUP_MAX_AGE_HOURS", "48"))

# Off-site sync runs hourly by default, so 6h rides out a few missed cycles
# (a flaky network, a remote rate-limiting us) before it counts as broken.
BACKUP_SYNC_MAX_AGE_HOURS = int(os.environ.get("BACKUP_SYNC_MAX_AGE_HOURS", "6"))


def backup_freshness() -> dict:
    """Return {status, label, age_hours, newest}.

    status is one of:
      OK       a snapshot exists and is younger than BACKUP_MAX_AGE_HOURS
      STALE    newest snapshot is older than that — sidecar probably stopped
      MISSING  no snapshots, or no backup directory at all
      NA       Postgres or MEMORY_MODE: no SQLite file to snapshot, so the
               sidecar correctly no-ops and there is nothing to check
    """
    try:
        import db as _dbmod
        if getattr(_dbmod, "db_type", "sqlite") == "postgresql":
            return {"status": "NA", "label": "n/a(pg)", "age_hours": None, "newest": None}
    except Exception:
        pass

    if os.environ.get("MEMORY_MODE", "").lower() in ("1", "true", "yes"):
        return {"status": "NA", "label": "n/a(mem)", "age_hours": None, "newest": None}

    backup_dir = os.environ.get("BACKUP_DIR") or "/app/data/backups"
    try:
        names = [n for n in os.listdir(backup_dir)
                 if n.startswith("rollcall-") and n.endswith(".db.gz")]
    except OSError:
        return {"status": "MISSING", "label": "MISSING(no dir)", "age_hours": None, "newest": None}

    if not names:
        return {"status": "MISSING", "label": "MISSING(none)", "age_hours": None, "newest": None}

    newest = max(names, key=lambda n: os.path.getmtime(os.path.join(backup_dir, n)))
    age_h = (time.time() - os.path.getmtime(os.path.join(backup_dir, newest))) / 3600.0
    stale = age_h >= BACKUP_MAX_AGE_HOURS
    return {
        "status": "STALE" if stale else "OK",
        "label": f"{'STALE' if stale else 'ok'}({age_h:.0f}h)",
        "age_hours": round(age_h, 1),
        "newest": newest,
    }


def remote_sync_freshness() -> dict:
    """How long ago the off-site sync last SUCCEEDED. Same shape as backup_freshness().

    Why this exists: the local snapshot check above answers "is a backup being
    taken", and nothing answered "is it getting off this machine". The
    backup-sync sidecar ran `rclone copy ... || true`, so a remote that had
    been rejecting writes for weeks logged failures into a container nobody
    reads and otherwise looked identical to one that was working — while
    `make status` reported a green tick for the container merely being up.
    That is the same silent-failure shape as the backup sidecar which sat dead
    from 2026-08-03 to 2026-08-24, and it matters more, because this is the
    copy that survives losing the machine.

    The sidecar now writes a timestamp file only on a successful copy, so the
    file's age is the real answer rather than the container's uptime.

    status is one of:
      OK       a successful copy within BACKUP_SYNC_MAX_AGE_HOURS
      STALE    the last success is older than that — the remote is rejecting
               writes, credentials expired, or the sidecar is not running
      MISSING  a remote is configured but no copy has ever succeeded
      NA       no RCLONE_REMOTE — off-site backup isn't set up, which is a
               choice and not a fault, so it must not read as a problem
    """
    if not os.environ.get("RCLONE_REMOTE", "").strip():
        return {"status": "NA", "label": "n/a(unset)", "age_hours": None, "at": None}

    state_dir = os.environ.get("BACKUP_SYNC_STATE_DIR") or "/app/data/sync-state"
    stamp = os.path.join(state_dir, "last-success")
    try:
        mtime = os.path.getmtime(stamp)
    except OSError:
        return {"status": "MISSING", "label": "MISSING(never)", "age_hours": None, "at": None}

    age_h = (time.time() - mtime) / 3600.0
    stale = age_h >= BACKUP_SYNC_MAX_AGE_HOURS
    at = None
    try:
        with open(stamp) as fh:
            at = fh.read().strip()[:32] or None
    except OSError:
        pass
    return {
        "status": "STALE" if stale else "OK",
        "label": f"{'STALE' if stale else 'ok'}({age_h:.0f}h)",
        "age_hours": round(age_h, 1),
        "at": at,
    }
