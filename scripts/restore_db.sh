#!/bin/sh
# Restore a gzipped SQLite backup produced by backup_db.sh.
#
# Run this on the HOST (the data directory is bind-mounted from DATA_DIR,
# which lives outside the git tree — see docker-compose.yml), with the bot
# stopped:
#   docker-compose stop rollcall-bot
#   BACKUP_DB_PATH=../rollcall-data/rollcall.db \
#     ./scripts/restore_db.sh ../rollcall-data/backups/rollcall-20260804-120000.db.gz
#   docker-compose start rollcall-bot
#
# Why this script exists instead of "just gunzip the file over rollcall.db":
# the container runs as root but with cap_drop: ALL (docker-compose.yml) —
# without CAP_DAC_OVERRIDE, that means it's subject to normal Unix
# permission checks like any other user, not root's usual bypass-everything
# behavior. A file created by `gunzip` on the host lands with the
# *operator's* host UID/GID and umask (bind mounts preserve them exactly).
# If that doesn't happen to be writable by the container's UID, the bot
# crashes on its first write after restart — a real incident this
# reproduces exactly, not a hypothetical. The dockerfile already works
# around the equivalent problem for the data *directory* (chmod 777 at
# build time); this script applies the same fix to a *restored file*,
# which the directory's own permissions don't touch since the file's mode
# comes from whatever created it, not from the directory it's dropped into.
#
# Env (optional):
#   BACKUP_DB_PATH   path to restore into (default $DATA_DIR/rollcall.db,
#                    falling back to ../rollcall-data/rollcall.db)
set -eu

DB_PATH="${BACKUP_DB_PATH:-${DATA_DIR:-../rollcall-data}/rollcall.db}"

if [ $# -ne 1 ]; then
    echo "Usage: $0 <path-to-backup.db.gz>" >&2
    exit 1
fi

SRC="$1"

if [ ! -f "$SRC" ]; then
    echo "[restore] backup file not found: $SRC" >&2
    exit 1
fi

if [ -f "$DB_PATH" ]; then
    safety="${DB_PATH}.pre-restore-$(date -u +%Y%m%d-%H%M%S)"
    echo "[restore] existing $DB_PATH found — copying it to $safety before overwriting"
    cp "$DB_PATH" "$safety"
fi

# Clear any leftover write-ahead log FIRST. A -wal/-shm pair belongs to the
# database file it was created beside; if the bot crashed rather than shut
# down cleanly, those files survive, and SQLite will happily checkpoint them
# onto whatever now sits at $DB_PATH. Restoring a snapshot next to a stale WAL
# therefore corrupts the very backup you are restoring — this is not
# theoretical, it destroyed a recovery candidate during the 2026-08-24
# incident. The snapshot is already a fully checkpointed image and needs no
# WAL of its own.
for stale in "${DB_PATH}-wal" "${DB_PATH}-shm"; do
    if [ -e "$stale" ]; then
        echo "[restore] removing stale $stale (belongs to the old database)"
        rm -f "$stale"
    fi
done

echo "[restore] restoring $SRC -> $DB_PATH"
case "$SRC" in
    *.gz) gunzip -c "$SRC" > "$DB_PATH" ;;
    *)    cp "$SRC" "$DB_PATH" ;;   # plain .db snapshots restore too
esac

# The fix: match the data directory's own chmod 777 (dockerfile) so the
# container's root-without-capabilities process can write to this file
# regardless of the host UID/umask that created it just now.
chmod 666 "$DB_PATH"

echo "[restore] done. Verify with: sqlite3 \"$DB_PATH\" 'PRAGMA integrity_check;'"
echo "[restore] then: make up"
