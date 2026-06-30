#!/bin/sh
# Daily SQLite backup with rotation.
#
# Uses the SQLite online-backup API (`.backup`), which is safe to run while the
# bot is writing — unlike `cp`, which can capture a torn mid-write file. The
# backup is gzipped and old ones are pruned.
#
# Env (all optional):
#   BACKUP_DB_PATH          path to the live DB   (default /app/data/rollcall.db)
#   BACKUP_DIR              where backups land     (default /app/data/backups)
#   BACKUP_RETENTION_DAYS   delete backups older than N days (default 7)
#
# Postgres deployments don't use this — the script no-ops if the DB file is
# absent (e.g. DATABASE_URL points at postgres, or MEMORY_MODE is on).
set -eu

DB_PATH="${BACKUP_DB_PATH:-/app/data/rollcall.db}"
BACKUP_DIR="${BACKUP_DIR:-/app/data/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"

if [ ! -f "$DB_PATH" ]; then
    echo "[backup] no SQLite db at $DB_PATH — nothing to back up (skipping)"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%d-%H%M%S)"
out="$BACKUP_DIR/rollcall-$ts.db"

# Online backup → consistent snapshot even while the bot holds the DB open.
sqlite3 "$DB_PATH" ".backup '$out'"
gzip -f "$out"
echo "[backup] wrote ${out}.gz"

# Rotation: drop snapshots older than the retention window.
find "$BACKUP_DIR" -name 'rollcall-*.db.gz' -type f -mtime +"$RETENTION_DAYS" -delete 2>/dev/null || true
echo "[backup] retention: kept last ${RETENTION_DAYS} day(s); current count: $(find "$BACKUP_DIR" -name 'rollcall-*.db.gz' | wc -l | tr -d ' ')"
