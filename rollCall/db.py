"""
Database layer for RollCall bot
Supports both PostgreSQL and SQLite
"""
from __future__ import annotations

import os
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any


def _utcnow_naive():
    """Naive UTC 'now' — direct replacement for the deprecated datetime.utcnow().
    Same value (no tzinfo) so all existing comparisons and stored timestamps
    continue to round-trip identically."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Try PostgreSQL first, fall back to SQLite
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import SimpleConnectionPool
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

import sqlite3
from config import DATABASE_URL

# Replace the default sqlite3 datetime adapter (deprecated as of Python 3.12,
# scheduled for removal). The bot stores naive UTC datetimes; this preserves
# byte-identical "YYYY-MM-DD HH:MM:SS[.ffffff]" output that _parse_db_datetime
# in models.py already reads back. Registered at module import time so every
# sqlite3 connection — including get_connection() — uses the explicit adapter.
def _sqlite_adapt_datetime_iso(val):
    return val.isoformat(" ")


sqlite3.register_adapter(datetime, _sqlite_adapt_datetime_iso)

# Database connection pool/connection
db_pool = None
db_conn = None
db_type = None
_pool_max = 0
_pool_in_use = 0
_pool_high_water = 0  # peak in-use count observed since boot
_pool_saturation_logged_at = 0.0  # for warn throttling
# Allowlists for safe SQL field interpolation
VALID_USER_STAT_FIELDS = {
    'total_in', 'total_out', 'total_maybe', 'total_waiting_to_in',
    'total_rollcalls', 'total_response_seconds', 'best_streak', 'current_streak'
}
VALID_ROLLCALL_STAT_FIELDS = {'total_in', 'total_out', 'total_maybe'}


def get_pool_stats():
    """Return current connection pool stats. None for SQLite (single connection)."""
    if db_type != 'postgresql':
        return None
    return {
        'in_use': _pool_in_use,
        'max': _pool_max,
        'high_water': _pool_high_water,
        'saturated': _pool_in_use >= _pool_max,
    }


def init_db():
    """Initialize database connection and create tables"""
    global db_pool, db_conn, db_type
    
    # Determine database type from URL
    if DATABASE_URL.startswith('postgresql://') or DATABASE_URL.startswith('postgres://'):
        if not HAS_POSTGRES:
            raise ImportError("PostgreSQL URL provided but psycopg2 is not installed. Run: pip install psycopg2-binary")
        db_type = 'postgresql'
        logging.debug("Using PostgreSQL database")
        init_postgresql()
    else:
        db_type = 'sqlite'
        logging.debug("Using SQLite database")
        init_sqlite()
    
    create_tables()
    create_ghost_selections_table()  # For ghost selection crash recovery
    logging.debug("Database initialized successfully")

def init_postgresql():
    """Initialize PostgreSQL connection pool. Pool bounds are tunable via
    DB_POOL_MINCONN / DB_POOL_MAXCONN (defaults 1 / 5)."""
    global db_pool, _pool_max
    try:
        minconn = int(os.environ.get("DB_POOL_MINCONN", "1"))
        maxconn = int(os.environ.get("DB_POOL_MAXCONN", "5"))
    except ValueError:
        minconn, maxconn = 1, 5
    if minconn < 1:
        minconn = 1
    if maxconn < minconn:
        maxconn = minconn
    try:
        db_pool = SimpleConnectionPool(minconn=minconn, maxconn=maxconn, dsn=DATABASE_URL)
        _pool_max = maxconn
        logging.info(f"PostgreSQL connection pool created (min={minconn}, max={maxconn})")
    except Exception as e:
        logging.error(f"Failed to create PostgreSQL connection pool: {e}")
        raise

def init_sqlite():
    """Initialize SQLite connection"""
    global db_conn
    # Extract database path from URL
    db_path = DATABASE_URL.replace('sqlite:///', '')
    try:
        db_conn = sqlite3.connect(db_path, check_same_thread=False)
        db_conn.row_factory = sqlite3.Row
        logging.debug(f"SQLite database connected: {db_path}")
    except Exception as e:
        logging.error(f"Failed to connect to SQLite database: {e}")
        raise

def get_connection():
    """Get database connection. Tracks pool usage and throttle-warns once
    every 60s if the PG pool is saturated."""
    global _pool_in_use, _pool_high_water, _pool_saturation_logged_at
    if db_type == 'postgresql':
        if _pool_in_use >= _pool_max:
            now = datetime.now().timestamp()
            if now - _pool_saturation_logged_at > 60:
                _pool_saturation_logged_at = now
                logging.warning(
                    f"PG connection pool saturated ({_pool_in_use}/{_pool_max}) — "
                    f"consider raising DB_POOL_MAXCONN. Peak={_pool_high_water}."
                )
        conn = db_pool.getconn()
        _pool_in_use += 1
        if _pool_in_use > _pool_high_water:
            _pool_high_water = _pool_in_use
        return conn
    return db_conn


def release_connection(conn):
    """Release database connection back to the pool (no-op for SQLite)."""
    global _pool_in_use
    if db_type == 'postgresql':
        db_pool.putconn(conn)
        if _pool_in_use > 0:
            _pool_in_use -= 1


@contextmanager
def _cursor(commit: bool = False):
    """Yield a cursor with the connection lifecycle handled — replaces the
    get_connection/cursor/try/finally/release_connection boilerplate repeated
    across this module's ~150 functions.

    commit=True commits after the body completes; any exception rolls back
    first. Exceptions ALWAYS propagate to the caller — each function keeps its
    own except clause and its own error convention (raise vs return
    False/None/[]), which this helper deliberately does not unify.

    Note for id-returning mutators: SELECT last_insert_rowid() (connection-
    scoped) and lastval() (PG session-scoped) both work inside the body even
    though the commit now happens after the body instead of before the id
    fetch.
    """
    conn = get_connection()
    cur = None
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        if commit:
            conn.rollback()
        raise
    finally:
        if cur is not None:
            cur.close()
        if db_type == 'postgresql':
            release_connection(conn)


def create_tables():
    """Create database tables if they don't exist"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        
        if db_type == 'postgresql':
            # PostgreSQL table definitions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id BIGINT PRIMARY KEY,
                    shh_mode BOOLEAN DEFAULT FALSE,
                    admin_rights BOOLEAN DEFAULT FALSE,
                    timezone VARCHAR(100) DEFAULT 'Asia/Kolkata',
                    absent_limit INTEGER DEFAULT 1,
                    ghost_tracking_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    group_name TEXT DEFAULT NULL,
                    upi_vpa TEXT DEFAULT NULL,
                    treasury_upi TEXT DEFAULT NULL,
                    dues_round_step INTEGER DEFAULT 10,
                    penalty_late_t1 INTEGER DEFAULT 50,
                    penalty_late_t2 INTEGER DEFAULT 75,
                    penalty_late_t3 INTEGER DEFAULT 100,
                    penalty_ditch INTEGER DEFAULT 200,
                    dues_enabled BOOLEAN DEFAULT FALSE
                )
            """)


            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rollcalls (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    title TEXT,
                    in_list_limit INTEGER,
                    reminder_hours INTEGER,
                    finalize_date TIMESTAMP,
                    timezone VARCHAR(100) DEFAULT 'Asia/Kolkata',
                    location TEXT,
                    event_fee TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    ended_at TIMESTAMP,
                    absent_marked BOOLEAN DEFAULT FALSE,
                    panel_msg_id BIGINT DEFAULT NULL,
                    collector_uid BIGINT DEFAULT NULL,
                    collector_name TEXT DEFAULT NULL,
                    collector_paid_ground INTEGER DEFAULT 0,
                    collector_upi TEXT DEFAULT NULL,
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_users (
                    id SERIAL PRIMARY KEY,
                    rollcall_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    comment TEXT,
                    proxy_owner_id BIGINT,
                    in_pos INTEGER,
                    out_pos INTEGER,
                    wait_pos INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id, name)
                )
            """)

            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    rollcall_id INTEGER NOT NULL,
                    user_id BIGINT NOT NULL,
                    first_name TEXT,
                    username TEXT,
                    status VARCHAR(20) NOT NULL,
                    comment TEXT,
                    in_pos INTEGER,
                    out_pos INTEGER,
                    wait_pos INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    total_in INTEGER DEFAULT 0,
                    total_out INTEGER DEFAULT 0,
                    total_maybe INTEGER DEFAULT 0,
                    total_waiting_to_in INTEGER DEFAULT 0,
                    total_rollcalls INTEGER DEFAULT 0,
                    total_response_seconds BIGINT DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rollcall_stats (
                    id SERIAL PRIMARY KEY,
                    rollcall_id INTEGER NOT NULL,
                    total_in INTEGER DEFAULT 0,
                    total_out INTEGER DEFAULT 0,
                    total_maybe INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id)
                )
           """)

            # proxy_stats — parallel to user_stats but keyed on the proxy's
            # TEXT name rather than an integer user_id. Lets us track streaks
            # and per-proxy aggregates for /sif /sof /smf entries; previously
            # proxies were excluded from streak tracking because user_stats
            # can't accommodate string keys.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_stats (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    proxy_name TEXT NOT NULL,
                    total_in INTEGER DEFAULT 0,
                    total_out INTEGER DEFAULT 0,
                    total_maybe INTEGER DEFAULT 0,
                    total_rollcalls INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, proxy_name)
                )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id SERIAL PRIMARY KEY,
                chatid BIGINT NOT NULL,
                name TEXT NOT NULL,
                title TEXT,
                inlistlimit INTEGER,
                location TEXT,
                eventfee TEXT,
                offsetdays INTEGER,
                offsethours INTEGER,
                offsetminutes INTEGER,
                event_day TEXT,
                event_time TEXT,
                createdat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chatid, name)
            )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_rollcalls_chat_active
                ON rollcalls(chat_id, is_active)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_rollcall
                ON users(rollcall_id, status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_proxy_users_rollcall
                ON proxy_users(rollcall_id, status)
            """)
        else:
            # SQLite table definitions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    shh_mode INTEGER DEFAULT 0,
                    admin_rights INTEGER DEFAULT 0,
                    timezone TEXT DEFAULT 'Asia/Kolkata',
                    absent_limit INTEGER DEFAULT 1,
                    ghost_tracking_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    group_name TEXT DEFAULT NULL,
                    upi_vpa TEXT DEFAULT NULL,
                    treasury_upi TEXT DEFAULT NULL,
                    dues_round_step INTEGER DEFAULT 10,
                    penalty_late_t1 INTEGER DEFAULT 50,
                    penalty_late_t2 INTEGER DEFAULT 75,
                    penalty_late_t3 INTEGER DEFAULT 100,
                    penalty_ditch INTEGER DEFAULT 200,
                    dues_enabled INTEGER DEFAULT 0
                )
            """)


            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rollcalls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    title TEXT,
                    in_list_limit INTEGER,
                    reminder_hours INTEGER,
                    finalize_date TIMESTAMP,
                    timezone TEXT DEFAULT 'Asia/Kolkata',
                    location TEXT,
                    event_fee TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    ended_at TIMESTAMP,
                    absent_marked INTEGER DEFAULT 0,
                    panel_msg_id INTEGER DEFAULT NULL,
                    collector_uid INTEGER DEFAULT NULL,
                    collector_name TEXT DEFAULT NULL,
                    collector_paid_ground INTEGER DEFAULT 0,
                    collector_upi TEXT DEFAULT NULL,
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                )
            """)


            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rollcall_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    comment TEXT,
                    proxy_owner_id INTEGER,
                    in_pos INTEGER,
                    out_pos INTEGER,
                    wait_pos INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id, name)
                )
            """)
            
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rollcall_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT,
                    username TEXT,
                    status TEXT NOT NULL,
                    comment TEXT,
                    in_pos INTEGER,
                    out_pos INTEGER,
                    wait_pos INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    total_in INTEGER DEFAULT 0,
                    total_out INTEGER DEFAULT 0,
                    total_maybe INTEGER DEFAULT 0,
                    total_waiting_to_in INTEGER DEFAULT 0,
                    total_rollcalls INTEGER DEFAULT 0,
                    total_response_seconds INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, user_id)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rollcall_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rollcall_id INTEGER NOT NULL,
                total_in INTEGER DEFAULT 0,
                total_out INTEGER DEFAULT 0,
                total_maybe INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                UNIQUE(rollcall_id)
            )
            """)

            # proxy_stats — see PG version above for rationale.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    proxy_name TEXT NOT NULL,
                    total_in INTEGER DEFAULT 0,
                    total_out INTEGER DEFAULT 0,
                    total_maybe INTEGER DEFAULT 0,
                    total_rollcalls INTEGER DEFAULT 0,
                    best_streak INTEGER DEFAULT 0,
                    current_streak INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, proxy_name)
                )
            """)

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chatid INTEGER NOT NULL,
                name TEXT NOT NULL,
                title TEXT,
                inlistlimit INTEGER,
                location TEXT,
                eventfee TEXT,
                offsetdays INTEGER,
                offsethours INTEGER,
                offsetminutes INTEGER,
                event_day TEXT,
                event_time TEXT,
                createdat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chatid, name)
            )
            """)

            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_rollcalls_chat_active
                ON rollcalls(chat_id, is_active)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_rollcall
                ON users(rollcall_id, status)
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_proxy_users_rollcall
                ON proxy_users(rollcall_id, status)
            """)

        # chat_members: one row per real Telegram user seen in a chat.
        # Kept up-to-date on every vote; used by /buzz to know who to ping.
        if db_type == 'postgresql':
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    first_name TEXT,
                    username TEXT,
                    is_active BOOLEAN DEFAULT TRUE,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_members (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT,
                    username TEXT,
                    is_active INTEGER DEFAULT 1,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)

        # Ghost tracking tables
        if db_type == 'postgresql':
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ghost_records (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL DEFAULT -1,
                    proxy_name TEXT,
                    user_name TEXT,
                    ghost_count INTEGER DEFAULT 0,
                    last_ghosted_at TIMESTAMP
                )
            """)
            # Partial unique indexes so ON CONFLICT (col) WHERE ... works correctly
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ghost_records_proxy_unique
                ON ghost_records(chat_id, proxy_name) WHERE proxy_name IS NOT NULL
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ghost_records_user_unique
                ON ghost_records(chat_id, user_id) WHERE proxy_name IS NULL
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ghost_events (
                    id SERIAL PRIMARY KEY,
                    rollcall_id INTEGER NOT NULL,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT,
                    proxy_name TEXT,
                    user_name TEXT,
                    ghosted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ghost_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT -1,
                    proxy_name TEXT,
                    user_name TEXT,
                    ghost_count INTEGER DEFAULT 0,
                    last_ghosted_at TIMESTAMP
                )
            """)
            # SQLite: use INSERT OR REPLACE to handle duplicates, but first check
            # For proxy users, check by proxy_name; for real users, check by user_id
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ghost_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rollcall_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER,
                    proxy_name TEXT,
                    user_name TEXT,
                    ghosted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE
                )
            """)

        # Identity merge: alias a proxy name to a canonical identity (another
        # proxy name, or a real user) so stats/dues/ghost-tracking treat them
        # as one person. Resolved at read time — never rewrites the historical
        # rows in ghost_records/dues_entries/user_stats/proxy_stats etc. The
        # 'dismissed' status records a rejected fuzzy-match suggestion (so it
        # doesn't keep resurfacing) without acting as a real merge.
        if db_type == 'postgresql':
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS identity_links (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    alias_proxy_name TEXT NOT NULL,
                    canonical_user_id BIGINT,
                    canonical_proxy_name TEXT,
                    status TEXT NOT NULL DEFAULT 'linked',
                    created_by BIGINT,
                    created_by_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS identity_links_alias_linked_unique
                ON identity_links (chat_id, LOWER(alias_proxy_name)) WHERE status = 'linked'
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS identity_links_dismissed_pair_unique
                ON identity_links (chat_id, LOWER(alias_proxy_name),
                                    COALESCE(CAST(canonical_user_id AS TEXT), LOWER(canonical_proxy_name)))
                WHERE status = 'dismissed'
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS identity_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    alias_proxy_name TEXT NOT NULL,
                    canonical_user_id INTEGER,
                    canonical_proxy_name TEXT,
                    status TEXT NOT NULL DEFAULT 'linked',
                    created_by INTEGER,
                    created_by_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS identity_links_alias_linked_unique
                ON identity_links (chat_id, LOWER(alias_proxy_name)) WHERE status = 'linked'
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS identity_links_dismissed_pair_unique
                ON identity_links (chat_id, LOWER(alias_proxy_name),
                                    COALESCE(CAST(canonical_user_id AS TEXT), LOWER(canonical_proxy_name)))
                WHERE status = 'dismissed'
            """)

        # Admin audit log table
        if db_type == 'postgresql':
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    admin_id BIGINT NOT NULL,
                    admin_name TEXT,
                    action_type TEXT NOT NULL,
                    target_name TEXT,
                    rollcall_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL,
                    admin_name TEXT,
                    action_type TEXT NOT NULL,
                    target_name TEXT,
                    rollcall_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

        # api_tokens: bearer tokens for REST API auth (PR 3).
        # Only the SHA-256 hash of the token is stored — plaintext is
        # shown to the issuer exactly once at creation and discarded.
        if db_type == 'postgresql':
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS api_tokens (
                    token_hash TEXT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    issued_by_user_id BIGINT,
                    scopes TEXT NOT NULL,
                    label TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    revoked_at TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_tokens_chat
                ON api_tokens(chat_id)
            """)
        else:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS api_tokens (
                    token_hash TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    issued_by_user_id INTEGER,
                    scopes TEXT NOT NULL,
                    label TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    revoked_at TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_tokens_chat
                ON api_tokens(chat_id)
            """)

        conn.commit()
        logging.debug("Database tables created successfully")

        # Migrate existing databases to add new columns if needed
        _migrate_schema(conn)

    except Exception as e:
        conn.rollback()
        logging.error(f"Error creating tables: {e}")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def _migrate_schema(conn):
    """Add new columns to existing tables for databases created before ghost tracking."""
    cursor = conn.cursor()
    try:
        _run_migrations(conn, cursor)
    finally:
        cursor.close()


# Every column that was added to a table AFTER its original CREATE. The
# reconciler adds any that are missing on startup, so a DB created by an older
# build self-heals rather than crashing on "no such column". Each entry is
# (column_name, sqlite_add_ddl, postgres_add_ddl). DDL must be ADD COLUMN-safe:
# constant/NULL defaults only (no CURRENT_TIMESTAMP), which is why created_at/
# updated_at — present since the original schema — are intentionally omitted.
_RECONCILE_COLUMNS = {
    "rollcalls": [
        ("in_list_limit",  "in_list_limit INTEGER",                    "in_list_limit INTEGER"),
        ("reminder_hours", "reminder_hours INTEGER",                   "reminder_hours INTEGER"),
        ("finalize_date",  "finalize_date TIMESTAMP",                  "finalize_date TIMESTAMP"),
        ("timezone",       "timezone TEXT DEFAULT 'Asia/Kolkata'",     "timezone VARCHAR(100) DEFAULT 'Asia/Kolkata'"),
        ("location",       "location TEXT",                            "location TEXT"),
        ("event_fee",      "event_fee TEXT",                           "event_fee TEXT"),
        ("is_active",      "is_active INTEGER DEFAULT 1",              "is_active BOOLEAN DEFAULT TRUE"),
        ("ended_at",       "ended_at TIMESTAMP",                       "ended_at TIMESTAMP"),
        ("absent_marked",  "absent_marked INTEGER DEFAULT 0",         "absent_marked BOOLEAN DEFAULT FALSE"),
        ("panel_msg_id",   "panel_msg_id INTEGER DEFAULT NULL",        "panel_msg_id BIGINT DEFAULT NULL"),
        ("web_token",      "web_token TEXT DEFAULT NULL",              "web_token TEXT DEFAULT NULL"),
        ("is_cancelled",   "is_cancelled INTEGER DEFAULT 0",          "is_cancelled BOOLEAN DEFAULT FALSE"),
        ("collector_uid",  "collector_uid INTEGER DEFAULT NULL",       "collector_uid BIGINT DEFAULT NULL"),
        ("collector_name", "collector_name TEXT DEFAULT NULL",         "collector_name TEXT DEFAULT NULL"),
        ("collector_paid_ground", "collector_paid_ground INTEGER DEFAULT 0", "collector_paid_ground INTEGER DEFAULT 0"),
        ("collector_upi",  "collector_upi TEXT DEFAULT NULL",          "collector_upi TEXT DEFAULT NULL"),
        ("auto_buzz_sent", "auto_buzz_sent INTEGER DEFAULT 0",         "auto_buzz_sent INTEGER DEFAULT 0"),
    ],
    "chats": [
        ("shh_mode",               "shh_mode INTEGER DEFAULT 0",               "shh_mode BOOLEAN DEFAULT FALSE"),
        ("admin_rights",           "admin_rights INTEGER DEFAULT 0",           "admin_rights BOOLEAN DEFAULT FALSE"),
        ("timezone",               "timezone TEXT DEFAULT 'Asia/Kolkata'",     "timezone VARCHAR(100) DEFAULT 'Asia/Kolkata'"),
        ("absent_limit",           "absent_limit INTEGER DEFAULT 1",           "absent_limit INTEGER DEFAULT 1"),
        ("ghost_tracking_enabled", "ghost_tracking_enabled INTEGER DEFAULT 1", "ghost_tracking_enabled BOOLEAN DEFAULT TRUE"),
        ("group_web_token",        "group_web_token TEXT DEFAULT NULL",        "group_web_token TEXT DEFAULT NULL"),
        ("group_name",             "group_name TEXT DEFAULT NULL",             "group_name TEXT DEFAULT NULL"),
        ("upi_vpa",                "upi_vpa TEXT DEFAULT NULL",                "upi_vpa TEXT DEFAULT NULL"),
        ("treasury_upi",           "treasury_upi TEXT DEFAULT NULL",           "treasury_upi TEXT DEFAULT NULL"),
        ("dues_round_step",        "dues_round_step INTEGER DEFAULT 10",       "dues_round_step INTEGER DEFAULT 10"),
        ("penalty_late_t1",        "penalty_late_t1 INTEGER DEFAULT 50",       "penalty_late_t1 INTEGER DEFAULT 50"),
        ("penalty_late_t2",        "penalty_late_t2 INTEGER DEFAULT 75",       "penalty_late_t2 INTEGER DEFAULT 75"),
        ("penalty_late_t3",        "penalty_late_t3 INTEGER DEFAULT 100",      "penalty_late_t3 INTEGER DEFAULT 100"),
        ("penalty_ditch",          "penalty_ditch INTEGER DEFAULT 200",        "penalty_ditch INTEGER DEFAULT 200"),
        ("dues_enabled",           "dues_enabled BOOLEAN DEFAULT FALSE",       "dues_enabled INTEGER DEFAULT 0"),
        ("dues_self_paid_mode",    "dues_self_paid_mode TEXT DEFAULT 'auto'",  "dues_self_paid_mode TEXT DEFAULT 'auto'"),
        ("auto_buzz_hours",        "auto_buzz_hours INTEGER DEFAULT 0",        "auto_buzz_hours INTEGER DEFAULT 0"),
        ("dues_weekly_nudge",      "dues_weekly_nudge INTEGER DEFAULT 0",      "dues_weekly_nudge INTEGER DEFAULT 0"),
        ("dues_report_enabled",    "dues_report_enabled INTEGER DEFAULT 0",    "dues_report_enabled INTEGER DEFAULT 0"),
        ("last_idle_nudge",        "last_idle_nudge TEXT DEFAULT NULL",        "last_idle_nudge TEXT DEFAULT NULL"),
        ("collector_rotation",     "collector_rotation INTEGER DEFAULT 0",     "collector_rotation INTEGER DEFAULT 0"),
        ("last_collector_uid",     "last_collector_uid INTEGER DEFAULT NULL",  "last_collector_uid BIGINT DEFAULT NULL"),
        ("dues_epoch",             "dues_epoch TEXT DEFAULT NULL",             "dues_epoch TEXT DEFAULT NULL"),
    ],
    "users": [
        ("in_pos",   "in_pos INTEGER DEFAULT NULL",   "in_pos INTEGER DEFAULT NULL"),
        ("out_pos",  "out_pos INTEGER DEFAULT NULL",  "out_pos INTEGER DEFAULT NULL"),
        ("wait_pos", "wait_pos INTEGER DEFAULT NULL", "wait_pos INTEGER DEFAULT NULL"),
    ],
    "proxy_users": [
        ("in_pos",         "in_pos INTEGER DEFAULT NULL",         "in_pos INTEGER DEFAULT NULL"),
        ("out_pos",        "out_pos INTEGER DEFAULT NULL",        "out_pos INTEGER DEFAULT NULL"),
        ("wait_pos",       "wait_pos INTEGER DEFAULT NULL",       "wait_pos INTEGER DEFAULT NULL"),
        ("proxy_owner_id", "proxy_owner_id INTEGER DEFAULT NULL", "proxy_owner_id BIGINT DEFAULT NULL"),
    ],
    "templates": [
        ("schedule_day",        "schedule_day TEXT DEFAULT NULL",        "schedule_day TEXT DEFAULT NULL"),
        ("schedule_time",       "schedule_time TEXT DEFAULT NULL",       "schedule_time TEXT DEFAULT NULL"),
        ("schedule_enabled",    "schedule_enabled TEXT DEFAULT 0",       "schedule_enabled BOOLEAN DEFAULT FALSE"),
        ("last_scheduled_date", "last_scheduled_date TEXT DEFAULT NULL", "last_scheduled_date TEXT DEFAULT NULL"),
        ("recurrence_type",     "recurrence_type TEXT DEFAULT 'weekly'", "recurrence_type TEXT DEFAULT 'weekly'"),
        ("schedule_expires_at", "schedule_expires_at TEXT DEFAULT NULL", "schedule_expires_at TEXT DEFAULT NULL"),
    ],
    "ghost_events": [
        ("proxy_name", "proxy_name TEXT", "proxy_name TEXT"),
    ],
    "push_subscriptions": [
        ("tg_user_id", "tg_user_id INTEGER DEFAULT NULL", "tg_user_id BIGINT DEFAULT NULL"),
    ],
    "web_verify_tokens": [
        ("tg_username", "tg_username TEXT DEFAULT NULL", "tg_username TEXT DEFAULT NULL"),
    ],
    "penalty_tiers": [
        ("late_minutes_threshold", "late_minutes_threshold INTEGER DEFAULT NULL", "late_minutes_threshold INTEGER DEFAULT NULL"),
        ("is_ditch",               "is_ditch INTEGER DEFAULT 0",                  "is_ditch INTEGER DEFAULT 0"),
    ],
}


def _existing_columns(cursor, table):
    """Return the set of column names currently on `table` (empty if absent)."""
    try:
        if db_type == 'postgresql':
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                (table,),
            )
            return {r[0] if not isinstance(r, dict) else r["column_name"] for r in cursor.fetchall()}
        cursor.execute(f"PRAGMA table_info({table})")
        return {r[1] for r in cursor.fetchall()}
    except Exception:
        return set()


def _reconcile_columns(conn, cursor):
    """Add any expected column that is missing from a table.

    Covers databases created by older builds where a column exists in the
    current CREATE TABLE but was never backfilled by a migration (e.g.
    rollcalls.absent_marked). Idempotent — existing columns are skipped, so it
    is safe to run alongside the explicit migrations below.
    """
    for table, columns in _RECONCILE_COLUMNS.items():
        existing = _existing_columns(cursor, table)
        if not existing:
            continue  # table itself doesn't exist yet — create_tables handles that
        for name, sqlite_ddl, pg_ddl in columns:
            if name in existing:
                continue
            ddl = pg_ddl if db_type == 'postgresql' else sqlite_ddl
            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                conn.commit()
                logging.warning(f"Schema reconcile: added missing column {table}.{name}")
            except Exception as e:
                conn.rollback()
                logging.error(f"Schema reconcile: could not add {table}.{name}: {e}")


def _run_migrations(conn, cursor):

    # Reconcile any columns missing on databases created by older builds. Runs
    # first so the rest of startup can rely on the full schema being present.
    _reconcile_columns(conn, cursor)

    # Backfill schedule_expires_at for templates whose recurring schedule was
    # enabled before this column existed — defaults to one year out so an
    # old schedule doesn't suddenly stop (or, worse, was never going to
    # expire at all). Every schedule created/edited going forward always
    # gets an explicit expiry from services.templates.set_schedule, so this
    # only ever touches genuinely pre-existing rows; safe to run on every
    # startup since it's scoped to NULL rows.
    try:
        ph = "%s" if db_type == "postgresql" else "?"
        # schedule_enabled's stored representation varies (1/"1"/True depending
        # on DB type and migration path — see _serialize_template's own note)
        # so match the same broad truthy check rather than a literal TRUE/1
        # comparison that could silently miss rows.
        cursor.execute(
            f"UPDATE templates SET schedule_expires_at = {ph} "
            f"WHERE schedule_expires_at IS NULL AND schedule_enabled IS NOT NULL "
            f"AND CAST(schedule_enabled AS TEXT) NOT IN ('0', 'False', 'false', 'None', '')",
            ((datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d"),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception("Schema reconcile: could not backfill templates.schedule_expires_at")

    # Add ghost_tracking_enabled to chats (may not exist in older deployments)
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS ghost_tracking_enabled BOOLEAN DEFAULT TRUE")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN ghost_tracking_enabled INTEGER DEFAULT 1")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # Add missing columns
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE ghost_events ADD COLUMN IF NOT EXISTS proxy_name TEXT")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE ghost_events ADD COLUMN proxy_name TEXT")
            conn.commit()
        except Exception:
            conn.rollback()

    # For SQLite, drop the unique constraint on ghost_records that causes issues with proxy users.
    # Guard: only run if the table still has a UNIQUE constraint (i.e. hasn't been migrated yet).
    if db_type == 'sqlite':
        try:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ghost_records'")
            row = cursor.fetchone()
            table_sql = (row[0] if row else '') or ''
            if 'UNIQUE' in table_sql.upper():
                cursor.execute("""CREATE TABLE IF NOT EXISTS ghost_records_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL DEFAULT -1,
                    proxy_name TEXT,
                    user_name TEXT,
                    ghost_count INTEGER DEFAULT 0,
                    last_ghosted_at TIMESTAMP
                )""")
                cursor.execute("""INSERT INTO ghost_records_new (chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at)
                    SELECT chat_id, COALESCE(user_id, -1), proxy_name, user_name, ghost_count, last_ghosted_at
                    FROM ghost_records""")
                cursor.execute("DROP TABLE ghost_records")
                cursor.execute("ALTER TABLE ghost_records_new RENAME TO ghost_records")
                conn.commit()
                logging.info("Migrated ghost_records: removed UNIQUE constraint on user_id")
        except Exception as e:
            logging.error(f"Error migrating ghost_records: {e}")
            conn.rollback()

    # Add schedule columns to templates (new feature — safe to run repeatedly)
    if db_type == 'postgresql':
        for col_ddl in [
            "ADD COLUMN IF NOT EXISTS schedule_day TEXT DEFAULT NULL",
            "ADD COLUMN IF NOT EXISTS schedule_time TEXT DEFAULT NULL",
            "ADD COLUMN IF NOT EXISTS schedule_enabled BOOLEAN DEFAULT FALSE",
            "ADD COLUMN IF NOT EXISTS last_scheduled_date TEXT DEFAULT NULL",
        ]:
            try:
                cursor.execute(f"ALTER TABLE templates {col_ddl}")
                conn.commit()
            except Exception:
                conn.rollback()
    else:
        for col, defval in [
            ("schedule_day", "NULL"),
            ("schedule_time", "NULL"),
            ("schedule_enabled", "0"),
            ("last_scheduled_date", "NULL"),
        ]:
            try:
                cursor.execute(f"ALTER TABLE templates ADD COLUMN {col} TEXT DEFAULT {defval}")
                conn.commit()
            except Exception:
                conn.rollback()  # column already exists — safe to ignore

    # Add recurrence_type to templates
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE templates ADD COLUMN IF NOT EXISTS recurrence_type TEXT DEFAULT 'weekly'")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE templates ADD COLUMN recurrence_type TEXT DEFAULT 'weekly'")
            conn.commit()
        except Exception:
            conn.rollback()

    # Add panel_msg_id to rollcalls for cross-restart panel recovery
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE rollcalls ADD COLUMN IF NOT EXISTS panel_msg_id BIGINT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE rollcalls ADD COLUMN panel_msg_id INTEGER DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # Ensure admin_actions table exists (for databases created before this feature)
    if db_type == 'postgresql':
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    admin_id BIGINT NOT NULL,
                    admin_name TEXT,
                    action_type TEXT NOT NULL,
                    target_name TEXT,
                    rollcall_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admin_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    admin_id INTEGER NOT NULL,
                    admin_name TEXT,
                    action_type TEXT NOT NULL,
                    target_name TEXT,
                    rollcall_id INTEGER,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

    # Add in_pos/out_pos/wait_pos to users and proxy_users (added for join-order preservation)
    # Add proxy_owner_id to proxy_users (added for proxy ownership tracking)
    for tbl in ("users", "proxy_users"):
        if db_type == 'postgresql':
            for col in ("in_pos", "out_pos", "wait_pos"):
                try:
                    cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} INTEGER DEFAULT NULL")
                    conn.commit()
                except Exception:
                    conn.rollback()
        else:
            for col in ("in_pos", "out_pos", "wait_pos"):
                try:
                    cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INTEGER DEFAULT NULL")
                    conn.commit()
                except Exception:
                    conn.rollback()  # column already exists — safe to ignore

    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE proxy_users ADD COLUMN IF NOT EXISTS proxy_owner_id BIGINT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE proxy_users ADD COLUMN proxy_owner_id INTEGER DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # Add web_token to rollcalls for magic-link web voting
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE rollcalls ADD COLUMN IF NOT EXISTS web_token TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS rollcalls_web_token_unique ON rollcalls(web_token) WHERE web_token IS NOT NULL"
            )
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE rollcalls ADD COLUMN web_token TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # Add group_web_token to chats for permanent per-group bookmarkable URL
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS group_web_token TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS chats_group_web_token_unique ON chats(group_web_token) WHERE group_web_token IS NOT NULL"
            )
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN group_web_token TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # PostgreSQL: replace COALESCE expression constraint on ghost_records with partial unique indexes
    # (the expression constraint caused ON CONFLICT clauses to fail at runtime)
    if db_type == 'postgresql':
        try:
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ghost_records_proxy_unique
                ON ghost_records(chat_id, proxy_name) WHERE proxy_name IS NOT NULL
            """)
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ghost_records_user_unique
                ON ghost_records(chat_id, user_id) WHERE proxy_name IS NULL
            """)
            conn.commit()
        except Exception:
            conn.rollback()
        # Drop old COALESCE expression constraint if it still exists
        try:
            cursor.execute("""
                SELECT conname FROM pg_constraint
                WHERE conrelid = 'ghost_records'::regclass AND contype = 'u'
                AND conname LIKE '%coalesce%'
            """)
            rows = cursor.fetchall()
            for row in rows:
                conname = row[0] if not isinstance(row, dict) else row["conname"]
                cursor.execute(f"ALTER TABLE ghost_records DROP CONSTRAINT IF EXISTS {conname}")
            conn.commit()
        except Exception:
            conn.rollback()

    # Add group_name column for capturing Telegram chat titles
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN IF NOT EXISTS group_name TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE chats ADD COLUMN group_name TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # web_view_stats — persistent total page-view counter per group token
    if db_type == 'postgresql':
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS web_view_stats (
                    group_token TEXT PRIMARY KEY,
                    view_count  BIGINT NOT NULL DEFAULT 0,
                    last_viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS web_view_stats (
                    group_token TEXT PRIMARY KEY,
                    view_count  INTEGER NOT NULL DEFAULT 0,
                    last_viewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

    # system_config — arbitrary key/value store (VAPID keys etc.)
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    # push_subscriptions — web-push subscriber endpoints per group
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                group_token TEXT NOT NULL,
                endpoint   TEXT NOT NULL UNIQUE,
                p256dh     TEXT NOT NULL,
                auth       TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active     INTEGER NOT NULL DEFAULT 1
            )
        """ if db_type != 'postgresql' else """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id         SERIAL PRIMARY KEY,
                group_token TEXT NOT NULL,
                endpoint   TEXT NOT NULL UNIQUE,
                p256dh     TEXT NOT NULL,
                auth       TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active     BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()
    try:
        idx = "CREATE INDEX IF NOT EXISTS push_subscriptions_group_token ON push_subscriptions(group_token)"
        cursor.execute(idx)
        conn.commit()
    except Exception:
        conn.rollback()

    # tg_user_id on push_subscriptions — links a verified Telegram identity to a push endpoint
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE push_subscriptions ADD COLUMN IF NOT EXISTS tg_user_id BIGINT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE push_subscriptions ADD COLUMN tg_user_id INTEGER DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # web_admins — Telegram users granted web-admin rights for a chat (cached from /weblink caller)
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS web_admins (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                tg_user_id  INTEGER NOT NULL,
                tg_name     TEXT,
                added_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(chat_id, tg_user_id)
            )
        """ if db_type != 'postgresql' else """
            CREATE TABLE IF NOT EXISTS web_admins (
                id          SERIAL PRIMARY KEY,
                chat_id     BIGINT NOT NULL,
                tg_user_id  BIGINT NOT NULL,
                tg_name     TEXT,
                added_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(chat_id, tg_user_id)
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS web_admins_chat ON web_admins(chat_id)")
        conn.commit()
    except Exception:
        conn.rollback()

    # web_verify_tokens — one-time codes for the Telegram deep-link identity bridge
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS web_verify_tokens (
                code         TEXT PRIMARY KEY,
                tg_user_id   INTEGER DEFAULT NULL,
                tg_name      TEXT DEFAULT NULL,
                tg_username  TEXT DEFAULT NULL,
                expires_at   TEXT NOT NULL,
                used_at      TEXT DEFAULT NULL,
                created_at   TEXT DEFAULT (datetime('now'))
            )
        """ if db_type != 'postgresql' else """
            CREATE TABLE IF NOT EXISTS web_verify_tokens (
                code         TEXT PRIMARY KEY,
                tg_user_id   BIGINT DEFAULT NULL,
                tg_name      TEXT DEFAULT NULL,
                tg_username  TEXT DEFAULT NULL,
                expires_at   TIMESTAMPTZ NOT NULL,
                used_at      TIMESTAMPTZ DEFAULT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    # Migrate existing web_verify_tokens tables that predate tg_username column
    try:
        if db_type == 'postgresql':
            cursor.execute(
                "ALTER TABLE web_verify_tokens ADD COLUMN IF NOT EXISTS tg_username TEXT DEFAULT NULL"
            )
        else:
            cursor.execute("ALTER TABLE web_verify_tokens ADD COLUMN tg_username TEXT DEFAULT NULL")
        conn.commit()
    except Exception:
        conn.rollback()  # column already exists — safe to ignore

    # member_login_tokens — persistent personal login codes (/mytoken).
    # One active code per user, stored as a SHA-256 hash: redeeming it on the
    # web maps to the tg_user_id and mints the same signed id_token the
    # deep-link / Login Widget flows issue. Telegram-independent self-serve.
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS member_login_tokens (
                user_id      INTEGER PRIMARY KEY,
                token_hash   TEXT NOT NULL UNIQUE,
                first_name   TEXT DEFAULT NULL,
                username     TEXT DEFAULT NULL,
                created_at   TEXT DEFAULT (datetime('now')),
                last_used_at TEXT DEFAULT NULL
            )
        """ if db_type != 'postgresql' else """
            CREATE TABLE IF NOT EXISTS member_login_tokens (
                user_id      BIGINT PRIMARY KEY,
                token_hash   TEXT NOT NULL UNIQUE,
                first_name   TEXT DEFAULT NULL,
                username     TEXT DEFAULT NULL,
                created_at   TIMESTAMPTZ DEFAULT NOW(),
                last_used_at TIMESTAMPTZ DEFAULT NULL
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    # scheduled_rollcalls — one-shot web-scheduled rollcalls (fire at a specific datetime)
    if db_type == 'postgresql':
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_rollcalls (
                    id              SERIAL PRIMARY KEY,
                    chat_id         BIGINT NOT NULL,
                    title           TEXT NOT NULL,
                    scheduled_at    TEXT NOT NULL,
                    created_by_uid  BIGINT NOT NULL,
                    created_by_name TEXT NOT NULL,
                    is_fired        BOOLEAN NOT NULL DEFAULT FALSE,
                    fired_at        TEXT DEFAULT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_rollcalls (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id         INTEGER NOT NULL,
                    title           TEXT NOT NULL,
                    scheduled_at    TEXT NOT NULL,
                    created_by_uid  INTEGER NOT NULL,
                    created_by_name TEXT NOT NULL,
                    is_fired        INTEGER NOT NULL DEFAULT 0,
                    fired_at        TEXT DEFAULT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()


    # ── Dues & Treasury tables ────────────────────────────────────────────────
    # game_closures: one row per financially closed game. UNIQUE(rollcall_id)
    # is the double-close guard. dues_entries / fund_transactions are
    # APPEND-ONLY ledgers — corrections are compensating entries, never
    # UPDATE/DELETE, so the full money history is always reconstructable.
    if db_type == 'postgresql':
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS game_closures (
                    id              SERIAL PRIMARY KEY,
                    chat_id         BIGINT NOT NULL,
                    rollcall_id     INTEGER NOT NULL UNIQUE,
                    title           TEXT,
                    ground_cost     INTEGER NOT NULL,
                    in_count        INTEGER NOT NULL,
                    subsidy         INTEGER NOT NULL DEFAULT 0,
                    per_head        INTEGER NOT NULL,
                    rounding_step   INTEGER NOT NULL,
                    remainder       INTEGER NOT NULL DEFAULT 0,
                    collector_uid   BIGINT DEFAULT NULL,
                    collector_name  TEXT DEFAULT NULL,
                    collector_paid_ground INTEGER DEFAULT 0,
                    collector_upi   TEXT DEFAULT NULL,
                    closed_by_uid   BIGINT NOT NULL,
                    closed_by_name  TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dues_entries (
                    id              SERIAL PRIMARY KEY,
                    chat_id         BIGINT NOT NULL,
                    rollcall_id     INTEGER DEFAULT NULL,
                    user_id         BIGINT DEFAULT NULL,
                    member_name     TEXT NOT NULL,
                    entry_type      TEXT NOT NULL,
                    amount          INTEGER NOT NULL,
                    memo            TEXT DEFAULT NULL,
                    created_by_uid  BIGINT NOT NULL,
                    created_by_name TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_dues_entries_chat ON dues_entries(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_dues_entries_rollcall ON dues_entries(rollcall_id)")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fund_transactions (
                    id              SERIAL PRIMARY KEY,
                    chat_id         BIGINT NOT NULL,
                    rollcall_id     INTEGER DEFAULT NULL,
                    txn_type        TEXT NOT NULL,
                    amount          INTEGER NOT NULL,
                    description     TEXT DEFAULT NULL,
                    created_by_uid  BIGINT NOT NULL,
                    created_by_name TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fund_transactions_chat ON fund_transactions(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fund_transactions_rollcall ON fund_transactions(rollcall_id)")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS penalty_tiers (
                    id                      SERIAL PRIMARY KEY,
                    chat_id                 BIGINT NOT NULL,
                    name                    TEXT NOT NULL,
                    amount                  INTEGER NOT NULL,
                    description             TEXT DEFAULT NULL,
                    late_minutes_threshold  INTEGER DEFAULT NULL,
                    is_ditch                INTEGER DEFAULT 0,
                    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(chat_id, name)
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS game_closures (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id         INTEGER NOT NULL,
                    rollcall_id     INTEGER NOT NULL UNIQUE,
                    title           TEXT,
                    ground_cost     INTEGER NOT NULL,
                    in_count        INTEGER NOT NULL,
                    subsidy         INTEGER NOT NULL DEFAULT 0,
                    per_head        INTEGER NOT NULL,
                    rounding_step   INTEGER NOT NULL,
                    remainder       INTEGER NOT NULL DEFAULT 0,
                    collector_uid   INTEGER DEFAULT NULL,
                    collector_name  TEXT DEFAULT NULL,
                    collector_paid_ground INTEGER DEFAULT 0,
                    collector_upi   TEXT DEFAULT NULL,
                    closed_by_uid   INTEGER NOT NULL,
                    closed_by_name  TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS dues_entries (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id         INTEGER NOT NULL,
                    rollcall_id     INTEGER DEFAULT NULL,
                    user_id         INTEGER DEFAULT NULL,
                    member_name     TEXT NOT NULL,
                    entry_type      TEXT NOT NULL,
                    amount          INTEGER NOT NULL,
                    memo            TEXT DEFAULT NULL,
                    created_by_uid  INTEGER NOT NULL,
                    created_by_name TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_dues_entries_chat ON dues_entries(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_dues_entries_rollcall ON dues_entries(rollcall_id)")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS fund_transactions (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id         INTEGER NOT NULL,
                    rollcall_id     INTEGER DEFAULT NULL,
                    txn_type        TEXT NOT NULL,
                    amount          INTEGER NOT NULL,
                    description     TEXT DEFAULT NULL,
                    created_by_uid  INTEGER NOT NULL,
                    created_by_name TEXT NOT NULL,
                    created_at      TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fund_transactions_chat ON fund_transactions(chat_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fund_transactions_rollcall ON fund_transactions(rollcall_id)")
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS penalty_tiers (
                    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id                 INTEGER NOT NULL,
                    name                    TEXT NOT NULL,
                    amount                  INTEGER NOT NULL,
                    description             TEXT DEFAULT NULL,
                    late_minutes_threshold  INTEGER DEFAULT NULL,
                    is_ditch                INTEGER DEFAULT 0,
                    created_at              TEXT NOT NULL,
                    UNIQUE(chat_id, name)
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

    # is_cancelled — marks rollcalls cancelled before they happened (weather, venue, etc.)
    # Cancelled rollcalls are excluded from attendance rate, streak, and session counts.
    if db_type == 'postgresql':
        try:
            cursor.execute(
                "ALTER TABLE rollcalls ADD COLUMN IF NOT EXISTS is_cancelled BOOLEAN DEFAULT FALSE"
            )
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute(
                "ALTER TABLE rollcalls ADD COLUMN is_cancelled INTEGER DEFAULT 0"
            )
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore

    # web_direct_login_tokens — admin-issued single-use login URLs for Telegram-down scenarios
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS web_direct_login_tokens (
                token           TEXT PRIMARY KEY,
                chat_id         INTEGER NOT NULL,
                tg_user_id      INTEGER NOT NULL,
                tg_name         TEXT NOT NULL,
                created_by_uid  INTEGER NOT NULL,
                created_by_name TEXT NOT NULL,
                expires_at      TEXT NOT NULL,
                used_at         TEXT DEFAULT NULL,
                created_at      TEXT DEFAULT (datetime('now'))
            )
        """ if db_type != 'postgresql' else """
            CREATE TABLE IF NOT EXISTS web_direct_login_tokens (
                token           TEXT PRIMARY KEY,
                chat_id         BIGINT NOT NULL,
                tg_user_id      BIGINT NOT NULL,
                tg_name         TEXT NOT NULL,
                created_by_uid  BIGINT NOT NULL,
                created_by_name TEXT NOT NULL,
                expires_at      TIMESTAMPTZ NOT NULL,
                used_at         TIMESTAMPTZ DEFAULT NULL,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        conn.commit()
    except Exception:
        conn.rollback()

    # Add collector_upi to game_closures (per-game collector UPI, distinct from treasury_upi)
    if db_type == 'postgresql':
        try:
            cursor.execute("ALTER TABLE game_closures ADD COLUMN IF NOT EXISTS collector_upi TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()
    else:
        try:
            cursor.execute("ALTER TABLE game_closures ADD COLUMN collector_upi TEXT DEFAULT NULL")
            conn.commit()
        except Exception:
            conn.rollback()  # column already exists — safe to ignore


def get_or_create_chat(chat_id: int) -> Dict:
    """Get or create chat settings"""
    import uuid as _uuid
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        if db_type == 'postgresql':
            cursor.execute(
                "SELECT * FROM chats WHERE chat_id = %s",
                (chat_id,)
            )
        else:
            cursor.execute(
                "SELECT * FROM chats WHERE chat_id = ?",
                (chat_id,)
            )
        row = cursor.fetchone()
        if row:
            result = dict(row)
        else:
            # Create new chat
            if db_type == 'postgresql':
                cursor.execute(
                    """INSERT INTO chats (chat_id, shh_mode, admin_rights, timezone, absent_limit, ghost_tracking_enabled)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                    (chat_id, False, False, 'Asia/Kolkata', 1, True)
                )
                result = dict(cursor.fetchone())
            else:
                cursor.execute(
                    """INSERT INTO chats (chat_id, shh_mode, admin_rights, timezone, absent_limit, ghost_tracking_enabled)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (chat_id, 0, 0, 'Asia/Kolkata', 1, 1)
                )
                cursor.execute(
                    "SELECT * FROM chats WHERE chat_id = ?",
                    (chat_id,)
                )
                result = dict(cursor.fetchone())
            conn.commit()
            logging.info(f"Created new chat: {chat_id}")

        # Lazily generate group_web_token for existing chats that predate this column.
        if not result.get('group_web_token'):
            token = _uuid.uuid4().hex
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"UPDATE chats SET group_web_token = {ph} WHERE chat_id = {ph}",
                (token, chat_id)
            )
            conn.commit()
            result['group_web_token'] = token

        return result
    except Exception as e:
        conn.rollback()
        logging.error(f"Error in get_or_create_chat: {e}")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def update_chat_group_name(chat_id: int, name: str) -> None:
    """Persist the Telegram group title so it can be displayed in the admin UI."""
    if not name:
        return
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(
            f"UPDATE chats SET group_name = {ph} WHERE chat_id = {ph} AND (group_name IS NULL OR group_name != {ph})",
            (name, chat_id, name),
        )
        conn.commit()
    except Exception:
        logging.exception(f"update_chat_group_name({chat_id})")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_chat_by_group_web_token(token: str) -> Optional[Dict]:
    """Look up a chat by its permanent group web token."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"SELECT * FROM chats WHERE group_web_token = {ph}", (token,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error in get_chat_by_group_web_token: {e}")
        return None


_VALID_CHAT_FIELDS = {
    'shh_mode', 'admin_rights', 'timezone', 'absent_limit',
    'ghost_tracking_enabled', 'group_name', 'group_web_token',
    'upi_vpa', 'treasury_upi', 'dues_round_step',
    'penalty_late_t1', 'penalty_late_t2', 'penalty_late_t3', 'penalty_ditch',
    'dues_enabled', 'dues_self_paid_mode',
    'auto_buzz_hours', 'dues_weekly_nudge', 'dues_report_enabled', 'last_idle_nudge',
    'collector_rotation', 'last_collector_uid', 'dues_epoch',
}

def update_chat_settings(chat_id: int, **kwargs) -> bool:
    """Update chat settings"""
    for key in kwargs:
        if key not in _VALID_CHAT_FIELDS:
            raise ValueError(f"update_chat_settings: invalid field '{key}'")
    try:
        with _cursor(commit=True) as cursor:

            # Build UPDATE query dynamically
            fields = []
            values = []

            for key, value in kwargs.items():
                fields.append(f"{key} = %s" if db_type == 'postgresql' else f"{key} = ?")
                # Convert boolean to int for SQLite
                if db_type == 'sqlite' and isinstance(value, bool):
                    value = 1 if value else 0
                values.append(value)
        
            if not fields:
                return True
        
            values.append(chat_id)
            query = f"UPDATE chats SET {', '.join(fields)} WHERE chat_id = {'%s' if db_type == 'postgresql' else '?'}"
        
            cursor.execute(query, values)
            logging.info(f"Updated chat settings for {chat_id}: {kwargs}")
            return True
    except Exception as e:
        logging.error(f"Error updating chat settings: {e}")
        return False

def create_rollcall(chat_id: int, title: str, timezone: str = 'Asia/Kolkata', web_token: Optional[str] = None) -> int:
    """Create a new rollcall and return its ID"""
    try:
        with _cursor(commit=True) as cursor:

            # Ensure chat exists
            get_or_create_chat(chat_id)

            if db_type == 'postgresql':
                cursor.execute(
                    """INSERT INTO rollcalls (chat_id, title, timezone, web_token)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (chat_id, title, timezone, web_token)
                )
                rollcall_id = cursor.fetchone()[0]
            else:
                cursor.execute(
                    """INSERT INTO rollcalls (chat_id, title, timezone, web_token)
                       VALUES (?, ?, ?, ?)""",
                    (chat_id, title, timezone, web_token)
                )
                rollcall_id = cursor.lastrowid
        
            logging.info(f"Created rollcall {rollcall_id} for chat {chat_id}: {title}")
            return rollcall_id
    except Exception as e:
        logging.error(f"Error creating rollcall: {e}")
        raise

def ensure_rollcall_stats(rollcall_id: int) -> None:
    """
    Ensure a rollcall_stats row exists for this rollcall.
    Called once at rollcall creation so increment_rollcall_stat never fails silently.
    """
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """
                    INSERT INTO rollcall_stats (rollcall_id, total_in, total_out, total_maybe)
                    VALUES (%s, 0, 0, 0)
                    ON CONFLICT (rollcall_id) DO NOTHING
                    """,
                    (rollcall_id,),
                )
            else:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO rollcall_stats (rollcall_id, total_in, total_out, total_maybe)
                    VALUES (?, 0, 0, 0)
                    """,
                    (rollcall_id,),
                )
            logging.info(f"Ensured rollcall_stats row for rollcall {rollcall_id}")
    except Exception as e:
        logging.error(f"Error ensuring rollcall_stats: {e}")


def get_rollcall(rollcall_id: int) -> Optional[Dict]:
    """Get rollcall by ID"""
    try:
        with _cursor() as cursor:
        
            if db_type == 'postgresql':
                cursor.execute(
                    "SELECT * FROM rollcalls WHERE id = %s",
                    (rollcall_id,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM rollcalls WHERE id = ?",
                    (rollcall_id,)
                )
        
            row = cursor.fetchone()
        
            if row:
                return dict(row)
            return None
    except Exception as e:
        logging.error(f"Error getting rollcall: {e}")
        return None

def get_rollcall_by_web_token(token: str) -> Optional[Dict]:
    """Get an active rollcall by its magic-link web_token."""
    try:
        with _cursor() as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    "SELECT * FROM rollcalls WHERE web_token = %s AND is_active = TRUE",
                    (token,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM rollcalls WHERE web_token = ? AND is_active = 1",
                    (token,)
                )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error looking up rollcall by web_token: {e}")
        return None


_VALID_ROLLCALL_FIELDS = {
    'chat_id', 'title', 'is_active', 'finalize_date', 'location',
    'event_fee', 'in_list_limit', 'panel_msg_id', 'web_token',
    'timezone', 'reminder_hours', 'template_name', 'created_at',
    'is_cancelled',
    'collector_uid', 'collector_name', 'collector_paid_ground', 'collector_upi',
    'auto_buzz_sent',
}

def update_rollcall(rollcall_id: int, **kwargs) -> bool:
    """Update rollcall fields"""
    for key in kwargs:
        if key not in _VALID_ROLLCALL_FIELDS:
            raise ValueError(f"update_rollcall: invalid field '{key}'")
    try:
        with _cursor(commit=True) as cursor:

            # Build UPDATE query dynamically
            fields = []
            values = []

            for key, value in kwargs.items():
                fields.append(f"{key} = %s" if db_type == 'postgresql' else f"{key} = ?")
                values.append(value)
        
            if not fields:
                return True
        
            values.append(rollcall_id)
            query = f"UPDATE rollcalls SET {', '.join(fields)} WHERE id = {'%s' if db_type == 'postgresql' else '?'}"
        
            cursor.execute(query, values)
            logging.info(f"Updated rollcall {rollcall_id}: {kwargs}")
            return True
    except Exception as e:
        logging.error(f"Error updating rollcall: {e}")
        return False

def get_active_rollcalls(chat_id: int) -> List[Dict]:
    """Get all active rollcalls for a chat"""
    try:
        with _cursor() as cursor:
        
            if db_type == 'postgresql':
                cursor.execute(
                    """SELECT * FROM rollcalls 
                       WHERE chat_id = %s AND is_active = TRUE
                       ORDER BY created_at ASC""",
                    (chat_id,)
                )
            else:
                cursor.execute(
                    """SELECT * FROM rollcalls 
                       WHERE chat_id = ? AND is_active = 1
                       ORDER BY created_at ASC""",
                    (chat_id,)
                )
        
            rows = cursor.fetchall()
            result = []
            for row in rows:
                result.append(dict(row))
        
            return result
    except Exception as e:
        logging.error(f"Error getting active rollcalls: {e}")
        return []

def create_or_update_template(
    chatid: int,
    name: str,
    title: Optional[str] = None,
    inlistlimit: Optional[int] = None,
    location: Optional[str] = None,
    eventfee: Optional[str] = None,
    offsetdays: Optional[int] = None,
    offsethours: Optional[int] = None,
    offsetminutes: Optional[int] = None,
    event_day: Optional[str] = None,
    event_time: Optional[str] = None,
) -> bool:
    """
    Create or update a template for a chat.
    Uniqueness is (chatid, name).
    """
    try:
        with _cursor(commit=True) as cursor:
            if db_type == "postgresql":
                cursor.execute(
                    """
                    INSERT INTO templates
                        (chatid, name, title, inlistlimit, location, eventfee,
                         offsetdays, offsethours, offsetminutes,event_day, event_time)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chatid, name) DO UPDATE SET
                        title = EXCLUDED.title,
                        inlistlimit = EXCLUDED.inlistlimit,
                        location = EXCLUDED.location,
                        eventfee = EXCLUDED.eventfee,
                        offsetdays = EXCLUDED.offsetdays,
                        offsethours = EXCLUDED.offsethours,
                        offsetminutes = EXCLUDED.offsetminutes,
                        event_day = EXCLUDED.event_day,
                        event_time = EXCLUDED.event_time
                    """,
                    (
                        chatid,
                        name,
                        title,
                        inlistlimit,
                        location,
                        eventfee,
                        offsetdays,
                        offsethours,
                        offsetminutes,
                        event_day, 
                        event_time
                    ),
                )
            else:
                # SQLite: preserve existing schedule columns (INSERT OR REPLACE would reset them to NULL)
                cursor.execute(
                    "SELECT id, schedule_day, schedule_time, schedule_enabled, last_scheduled_date, recurrence_type "
                    "FROM templates WHERE chatid = ? AND name = ?",
                    (chatid, name)
                )
                existing_row = cursor.fetchone()
                if existing_row:
                    existing_row = dict(existing_row)
                    row_id        = existing_row['id']
                    sched_day     = existing_row['schedule_day']
                    sched_time    = existing_row['schedule_time']
                    sched_enabled = existing_row['schedule_enabled']
                    sched_last    = existing_row['last_scheduled_date']
                    sched_recur   = existing_row['recurrence_type'] or 'weekly'
                else:
                    row_id = sched_day = sched_time = sched_last = None
                    sched_enabled = 0
                    sched_recur = 'weekly'
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO templates
                    (
                        id, chatid, name, title, inlistlimit, location, eventfee,
                        offsetdays, offsethours, offsetminutes, event_day, event_time,
                        schedule_day, schedule_time, schedule_enabled, last_scheduled_date, recurrence_type
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        chatid, name, title, inlistlimit, location, eventfee,
                        offsetdays, offsethours, offsetminutes, event_day, event_time,
                        sched_day, sched_time, sched_enabled, sched_last, sched_recur
                    ),
                )
        
            return True
    except Exception as e:
        logging.error(f"Error creating/updating template: {e}")
        return False


def clear_rollcall_reminder(rollcall_id: int) -> bool:
    """Persist `reminder_hours = NULL` for a rollcall — call this RIGHT AFTER
    the pre-close reminder is sent so a bot restart doesn't re-fire it.

    Before this helper existed, the reminder fire path set
    `rollcall.reminder = None` in memory only. On bot restart, models.RollCall
    reloads reminder_hours from the DB row (still set), and the freshly-
    started check loop sees `now >= reminder_time` and sends the reminder
    AGAIN. This persists the clear so restart can't double-fire.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(
            f"UPDATE rollcalls SET reminder_hours = NULL WHERE id = {ph}",
            (rollcall_id,),
        )
        conn.commit()
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"Error clearing rollcall reminder: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def end_rollcall(rollcall_id: int) -> bool:
    """Mark a rollcall as ended"""
    try:
        with _cursor(commit=True) as cursor:
        
            if db_type == 'postgresql':
                cursor.execute(
                    """UPDATE rollcalls SET
                       is_active = FALSE,
                       ended_at = CURRENT_TIMESTAMP
                       WHERE id = %s""",
                    (rollcall_id,)
                )
            else:
                cursor.execute(
                    """UPDATE rollcalls SET
                       is_active = 0,
                       ended_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (rollcall_id,)
                )
        
            logging.info(f"Ended rollcall {rollcall_id}")
            return True
    except Exception as e:
        logging.error(f"Error ending rollcall: {e}")
        return False


def get_all_chat_ids() -> List[int]:
    """Return all known chat IDs from the chats table."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM chats")
        return [row['chat_id'] for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error fetching all chat IDs: {e}")
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql' and conn:
            release_connection(conn)


_VALID_STATUSES = {'in', 'out', 'maybe', 'waitlist'}


def _next_pos_with_cursor(cursor, rollcall_id: int, status: str) -> int:
    """Return next position using the caller's cursor — avoids a second connection and eliminates
    the TOCTOU race between the MAX query and the subsequent INSERT on PostgreSQL."""
    col = {'in': 'in_pos', 'out': 'out_pos', 'waitlist': 'wait_pos'}.get(status)
    if col is None:
        return 0
    ph = '%s' if db_type == 'postgresql' else '?'
    cursor.execute(
        f"SELECT COALESCE(MAX({col}), 0) FROM users WHERE rollcall_id = {ph} AND status = {ph}",
        (rollcall_id, status)
    )
    max_real = int(cursor.fetchone()[0] or 0)
    cursor.execute(
        f"SELECT COALESCE(MAX({col}), 0) FROM proxy_users WHERE rollcall_id = {ph} AND status = {ph}",
        (rollcall_id, status)
    )
    max_proxy = int(cursor.fetchone()[0] or 0)
    return max(max_real, max_proxy) + 1


def add_or_update_user(rollcall_id: int, user_id: int, first_name: str, username: str, status: str, comment: str = '') -> bool:
    """Insert or update a regular user. Position assigned once per bucket, preserved on re-entry."""
    if status not in _VALID_STATUSES:
        logging.error(f"add_or_update_user: invalid status '{status}' for user {user_id}")
        return False
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'

            # Fetch existing positions and current status
            cursor.execute(
                f"SELECT in_pos, out_pos, wait_pos, status FROM users WHERE rollcall_id = {ph} AND user_id = {ph}",
                (rollcall_id, user_id)
            )
            existing = cursor.fetchone()

            if existing:
                existing = dict(existing)
                prev_status = existing['status']
                in_pos   = existing['in_pos']
                out_pos  = existing['out_pos']
                wait_pos = existing['wait_pos']
                # Reset the position of any bucket the user is leaving so re-entry
                # later assigns a fresh position at the END of that bucket. This
                # ensures fair FIFO ordering — in particular, a user promoted
                # WAITLIST→IN who later returns to the waitlist goes to the back.
                if prev_status == 'in' and status != 'in':
                    in_pos = None
                if prev_status == 'out' and status != 'out':
                    out_pos = None
                if prev_status == 'waitlist' and status != 'waitlist':
                    wait_pos = None
                # Assign NEW position when entering a bucket for the first time
                # (or re-entering after having left). Use the same cursor so the
                # MAX query and the INSERT share a connection and avoid a TOCTOU race.
                if status == 'in' and in_pos is None:
                    in_pos = _next_pos_with_cursor(cursor, rollcall_id, 'in')
                elif status == 'out' and out_pos is None:
                    out_pos = _next_pos_with_cursor(cursor, rollcall_id, 'out')
                elif status == 'waitlist' and wait_pos is None:
                    wait_pos = _next_pos_with_cursor(cursor, rollcall_id, 'waitlist')
            else:
                # Brand new user
                in_pos = out_pos = wait_pos = None
                if status == 'in':
                    in_pos = _next_pos_with_cursor(cursor, rollcall_id, 'in')
                elif status == 'out':
                    out_pos = _next_pos_with_cursor(cursor, rollcall_id, 'out')
                elif status == 'waitlist':
                    wait_pos = _next_pos_with_cursor(cursor, rollcall_id, 'waitlist')

            if db_type == 'postgresql':
                cursor.execute("""
                    INSERT INTO users (rollcall_id, user_id, first_name, username, status, comment, in_pos, out_pos, wait_pos)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (rollcall_id, user_id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        username   = EXCLUDED.username,
                        status     = EXCLUDED.status,
                        comment    = EXCLUDED.comment,
                        in_pos     = EXCLUDED.in_pos,
                        out_pos    = EXCLUDED.out_pos,
                        wait_pos   = EXCLUDED.wait_pos,
                        updated_at = CURRENT_TIMESTAMP
                """, (rollcall_id, user_id, first_name, username, status, comment, in_pos, out_pos, wait_pos))
            else:
                cursor.execute("""
                    INSERT INTO users (rollcall_id, user_id, first_name, username, status, comment, in_pos, out_pos, wait_pos)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rollcall_id, user_id) DO UPDATE SET
                        first_name = excluded.first_name,
                        username   = excluded.username,
                        status     = excluded.status,
                        comment    = excluded.comment,
                        in_pos     = excluded.in_pos,
                        out_pos    = excluded.out_pos,
                        wait_pos   = excluded.wait_pos,
                        updated_at = CURRENT_TIMESTAMP
                """, (rollcall_id, user_id, first_name, username, status, comment, in_pos, out_pos, wait_pos))

            return True
    except Exception as e:
        logging.error(f"Error add/update user: {e}")
        raise


def add_or_update_proxy_user(rollcall_id: int, name: str, status: str, comment: str = '', proxy_owner_id: Optional[int] = None) -> bool:
    """Add or update a proxy user with position tracking."""
    if status not in _VALID_STATUSES:
        logging.error(f"add_or_update_proxy_user: invalid status '{status}' for proxy '{name}'")
        return False
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'

            # Fetch existing positions and current status
            cursor.execute(
                f"SELECT in_pos, out_pos, wait_pos, status FROM proxy_users WHERE rollcall_id = {ph} AND name = {ph}",
                (rollcall_id, name)
            )
            existing = cursor.fetchone()

            if existing:
                existing = dict(existing)
                prev_status = existing['status']
                in_pos   = existing['in_pos']
                out_pos  = existing['out_pos']
                wait_pos = existing['wait_pos']
                # Reset the position of any bucket the proxy is leaving so re-entry
                # later assigns a fresh position at the END of that bucket. This
                # ensures fair FIFO ordering — in particular, a proxy promoted
                # WAITLIST→IN who later returns to the waitlist goes to the back.
                if prev_status == 'in' and status != 'in':
                    in_pos = None
                if prev_status == 'out' and status != 'out':
                    out_pos = None
                if prev_status == 'waitlist' and status != 'waitlist':
                    wait_pos = None
                # Assign NEW position when entering a bucket for the first time
                # (or re-entering after having left). Use the same cursor so the
                # MAX query and the INSERT share a connection and avoid a TOCTOU race.
                if status == 'in' and in_pos is None:
                    in_pos = _next_pos_with_cursor(cursor, rollcall_id, 'in')
                elif status == 'out' and out_pos is None:
                    out_pos = _next_pos_with_cursor(cursor, rollcall_id, 'out')
                elif status == 'waitlist' and wait_pos is None:
                    wait_pos = _next_pos_with_cursor(cursor, rollcall_id, 'waitlist')
            else:
                # Brand new proxy
                in_pos = out_pos = wait_pos = None
                if status == 'in':
                    in_pos = _next_pos_with_cursor(cursor, rollcall_id, 'in')
                elif status == 'out':
                    out_pos = _next_pos_with_cursor(cursor, rollcall_id, 'out')
                elif status == 'waitlist':
                    wait_pos = _next_pos_with_cursor(cursor, rollcall_id, 'waitlist')

            if db_type == 'postgresql':
                cursor.execute("""
                    INSERT INTO proxy_users (rollcall_id, name, status, comment, proxy_owner_id, in_pos, out_pos, wait_pos, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (rollcall_id, name) DO UPDATE SET
                        status         = EXCLUDED.status,
                        comment        = EXCLUDED.comment,
                        proxy_owner_id = EXCLUDED.proxy_owner_id,
                        in_pos         = EXCLUDED.in_pos,
                        out_pos        = EXCLUDED.out_pos,
                        wait_pos       = EXCLUDED.wait_pos,
                        updated_at     = CURRENT_TIMESTAMP
                """, (rollcall_id, name, status, comment, proxy_owner_id, in_pos, out_pos, wait_pos))
            else:
                cursor.execute("""
                    INSERT INTO proxy_users (rollcall_id, name, status, comment, proxy_owner_id, in_pos, out_pos, wait_pos, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(rollcall_id, name) DO UPDATE SET
                        status         = excluded.status,
                        comment        = excluded.comment,
                        proxy_owner_id = excluded.proxy_owner_id,
                        in_pos         = excluded.in_pos,
                        out_pos        = excluded.out_pos,
                        wait_pos       = excluded.wait_pos,
                        updated_at     = excluded.updated_at
                """, (rollcall_id, name, status, comment, proxy_owner_id, in_pos, out_pos, wait_pos))

            return True
    except Exception as e:
        logging.error(f"Error adding/updating proxy user: {e}")
        return False

def get_all_users(rollcall_id: int):
    """
    Get all users (real + proxy) for a rollcall.
    Ordering:
    - Grouped by status (in, out, maybe, waitlist).
    - Within IN/OUT/WAITLIST, ordered by their per-state position.
    - For MAYBE (no positions), fall back to created_at.
    """
    try:
        with _cursor() as cursor:
            if db_type == "postgresql":
                cursor.execute(
                    """
                    SELECT id, rollcall_id, user_id, first_name, username,
                           status, comment, in_pos, out_pos, wait_pos,
                           created_at, updated_at
                    FROM users WHERE rollcall_id = %s
                    ORDER BY
                        CASE status
                            WHEN 'in'       THEN 1
                            WHEN 'out'      THEN 2
                            WHEN 'maybe'    THEN 3
                            WHEN 'waitlist' THEN 4
                            ELSE 5
                        END,
                        CASE status
                            WHEN 'in'       THEN COALESCE(in_pos, 0)
                            WHEN 'out'      THEN COALESCE(out_pos, 0)
                            WHEN 'waitlist' THEN COALESCE(wait_pos, 0)
                            ELSE 0
                        END,
                        created_at ASC
                    """,
                    (rollcall_id,),
                )
            else:
                cursor.execute(
                    """
                    SELECT id, rollcall_id, user_id, first_name, username,
                           status, comment, in_pos, out_pos, wait_pos,
                           created_at, updated_at
                    FROM users WHERE rollcall_id = ?
                    ORDER BY
                        CASE status
                            WHEN 'in'       THEN 1
                            WHEN 'out'      THEN 2
                            WHEN 'maybe'    THEN 3
                            WHEN 'waitlist' THEN 4
                            ELSE 5
                        END,
                        CASE status
                            WHEN 'in'       THEN COALESCE(in_pos, 0)
                            WHEN 'out'      THEN COALESCE(out_pos, 0)
                            WHEN 'waitlist' THEN COALESCE(wait_pos, 0)
                            ELSE 0
                        END,
                        created_at ASC
                    """,
                    (rollcall_id,),
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logging.error(f"Error getting all users: {e}")
        return []


def get_proxy_users_by_status(rollcall_id: int, status: str) -> List[Dict]:
    """Get proxy users by status ordered by position"""
    try:
        with _cursor() as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """
                    SELECT * FROM proxy_users
                    WHERE rollcall_id = %s AND status = %s
                    ORDER BY
                        CASE status
                            WHEN 'in'       THEN COALESCE(in_pos, 0)
                            WHEN 'out'      THEN COALESCE(out_pos, 0)
                            WHEN 'waitlist' THEN COALESCE(wait_pos, 0)
                            ELSE 0
                        END ASC,
                        created_at ASC
                    """,
                    (rollcall_id, status)
                )
            else:
                cursor.execute(
                    """
                    SELECT * FROM proxy_users
                    WHERE rollcall_id = ? AND status = ?
                    ORDER BY
                        CASE status
                            WHEN 'in'       THEN COALESCE(in_pos, 0)
                            WHEN 'out'      THEN COALESCE(out_pos, 0)
                            WHEN 'waitlist' THEN COALESCE(wait_pos, 0)
                            ELSE 0
                        END ASC,
                        created_at ASC
                    """,
                    (rollcall_id, status)
                )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        logging.error(f"Error getting proxy users: {e}")
        return []


def delete_template(chatid: int, name: str) -> bool:
    """
    Delete a template for a chat by name.
    """
    try:
        with _cursor(commit=True) as cursor:
            if db_type == "postgresql":
                cursor.execute(
                    "DELETE FROM templates WHERE chatid = %s AND name = %s",
                    (chatid, name),
                )
            else:
                cursor.execute(
                    "DELETE FROM templates WHERE chatid = ? AND name = ?",
                    (chatid, name),
                )
            return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error deleting template: {e}")
        return False


def set_template_schedule(chatid: int, name: str, schedule_day: Optional[str], schedule_time: str, recurrence_type: str = 'weekly', schedule_expires_at: Optional[str] = None) -> bool:
    """Set schedule day/time and enable auto-start for a template.
    schedule_day is None for daily recurrence (no weekday to match).
    schedule_expires_at ("YYYY-MM-DD") is when this auto-disables itself —
    see check_template_schedules in check_reminders.py."""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            enabled = True if db_type == "postgresql" else 1
            cursor.execute(
                f"UPDATE templates SET schedule_day = {ph}, schedule_time = {ph}, "
                f"schedule_enabled = {ph}, last_scheduled_date = NULL, recurrence_type = {ph}, "
                f"schedule_expires_at = {ph} "
                f"WHERE chatid = {ph} AND name = {ph}",
                (schedule_day, schedule_time, enabled, recurrence_type, schedule_expires_at, chatid, name),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error setting template schedule: {e}")
        return False


def disable_template_schedule(chatid: int, name: str) -> bool:
    """Disable auto-start scheduling for a template."""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            disabled = False if db_type == "postgresql" else 0
            cursor.execute(
                f"UPDATE templates SET schedule_enabled = {ph} WHERE chatid = {ph} AND name = {ph}",
                (disabled, chatid, name),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error disabling template schedule: {e}")
        return False


def enable_template_schedule(chatid: int, name: str) -> bool:
    """Re-enable scheduling for a template using its previously saved schedule parameters."""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            enabled = True if db_type == "postgresql" else 1
            cursor.execute(
                f"UPDATE templates SET schedule_enabled = {ph} WHERE chatid = {ph} AND name = {ph}",
                (enabled, chatid, name),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error enabling template schedule: {e}")
        return False


def update_template_last_scheduled_date(chatid: int, name: str, date_str: str) -> bool:
    """Record the date (YYYY-MM-DD) when a template was last auto-started."""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"UPDATE templates SET last_scheduled_date = {ph} WHERE chatid = {ph} AND name = {ph}",
                (date_str, chatid, name),
            )
            return cursor.rowcount > 0
    except Exception as e:
        logging.error(f"Error updating last_scheduled_date: {e}")
        return False


def get_all_scheduled_templates() -> List[Dict]:
    """Return all templates with schedule_enabled=True across all chats."""
    try:
        with _cursor() as cursor:
            if db_type == "postgresql":
                cursor.execute("SELECT * FROM templates WHERE schedule_enabled = TRUE")
            else:
                cursor.execute("SELECT * FROM templates WHERE schedule_enabled = 1")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logging.error(f"Error fetching scheduled templates: {e}")
        return []


def delete_user_by_name(rollcall_id: int, name: str) -> bool:
    """Delete a user by name — checks proxy_users first, then real users.
    Matches @username uniquely; first_name is only used when it identifies
    exactly one user (otherwise we refuse to delete to avoid wiping the
    wrong account when two real users share a first name)."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'

            # Try proxy_users FIRST (named proxy should be removed before real user)
            cursor.execute(
                f"DELETE FROM proxy_users WHERE rollcall_id = {ph} AND name = {ph}",
                (rollcall_id, name)
            )
            rows_deleted = cursor.rowcount

            # When a proxy is deleted, also purge their ghost_records row so they
            # no longer appear on the /absent_stats leaderboard.
            if rows_deleted > 0:
                cursor.execute(
                    f"""DELETE FROM ghost_records
                        WHERE proxy_name = {ph}
                        AND chat_id = (SELECT chat_id FROM rollcalls WHERE id = {ph})""",
                    (name, rollcall_id)
                )

            # Only try real users if no proxy was deleted
            if rows_deleted == 0:
                clean_name = name.lstrip('@')

                # Username is unique within a rollcall, so try it first.
                cursor.execute(
                    f"DELETE FROM users WHERE rollcall_id = {ph} AND username = {ph}",
                    (rollcall_id, clean_name)
                )
                rows_deleted = cursor.rowcount

                if rows_deleted == 0:
                    # Fall back to first_name — but only when it uniquely identifies one user.
                    cursor.execute(
                        f"SELECT user_id FROM users WHERE rollcall_id = {ph} AND first_name = {ph}",
                        (rollcall_id, clean_name)
                    )
                    matches = cursor.fetchall()
                    if len(matches) == 1:
                        uid = matches[0][0] if not isinstance(matches[0], dict) else matches[0]['user_id']
                        cursor.execute(
                            f"DELETE FROM users WHERE rollcall_id = {ph} AND user_id = {ph}",
                            (rollcall_id, uid)
                        )
                        rows_deleted = cursor.rowcount
                    elif len(matches) > 1:
                        logging.warning(
                            f"delete_user_by_name: '{clean_name}' matches {len(matches)} users in rollcall {rollcall_id}; refusing to delete"
                        )

            return rows_deleted > 0

    except Exception as e:
        logging.error(f"Error deleting user: {e}")
        return False


def delete_user_by_id(rollcall_id: int, user_id) -> bool:
    """Delete a real user (int user_id) or proxy user (str user_id) by exact id.
    Used by /set_status which knows the precise user from the in-memory cache."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            rows_deleted = 0
            if isinstance(user_id, int):
                cursor.execute(
                    f"DELETE FROM users WHERE rollcall_id = {ph} AND user_id = {ph}",
                    (rollcall_id, user_id)
                )
                rows_deleted = cursor.rowcount
            else:
                cursor.execute(
                    f"DELETE FROM proxy_users WHERE rollcall_id = {ph} AND name = {ph}",
                    (rollcall_id, str(user_id))
                )
                rows_deleted = cursor.rowcount
            return rows_deleted > 0
    except Exception as e:
        logging.error(f"Error deleting user by id: {e}")
        return False

def close_db():
    """Close database connections"""
    global db_pool, db_conn
    
    if db_type == 'postgresql' and db_pool:
        db_pool.closeall()
        logging.info("PostgreSQL connection pool closed")
    elif db_type == 'sqlite' and db_conn:
        db_conn.close()
        logging.info("SQLite connection closed")


def increment_user_stat(chat_id: int, user_id: int, field: str) -> None:
    """Increment a single numeric field in user_stats."""
    if field not in VALID_USER_STAT_FIELDS:
        raise ValueError(f"Invalid stat field: {field}")
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """
                    INSERT INTO user_stats (chat_id, user_id, {field})
                    VALUES (%s, %s, 1)
                    ON CONFLICT (chat_id, user_id) DO UPDATE
                    SET {field} = user_stats.{field} + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """.format(field=field),
                    (chat_id, user_id),
                )
            else:
                cursor.execute(
                    f"""
                    INSERT OR IGNORE INTO user_stats (chat_id, user_id)
                    VALUES (?, ?)
                    """,
                    (chat_id, user_id),
                )
                cursor.execute(
                    f"""
                    UPDATE user_stats
                    SET {field} = {field} + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (chat_id, user_id),
                )
    except Exception as e:
        logging.error(f"Error incrementing user stat {field}: {e}")

def increment_rollcall_stat(rollcall_id: int, field: str) -> None:
    """Increment a single numeric field in rollcall_stats."""
    if field not in VALID_ROLLCALL_STAT_FIELDS:
        raise ValueError(f"Invalid rollcall stat field: {field}")
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """
                    INSERT INTO rollcall_stats (rollcall_id, {field})
                    VALUES (%s, 1)
                    ON CONFLICT (rollcall_id) DO UPDATE
                    SET {field} = rollcall_stats.{field} + 1,
                        updated_at = CURRENT_TIMESTAMP
                    """.format(field=field),
                    (rollcall_id,),
                )
            else:
                cursor.execute(
                    f"""
                    INSERT OR IGNORE INTO rollcall_stats (rollcall_id)
                    VALUES (?)
                    """,
                    (rollcall_id,),
                )
                cursor.execute(
                    f"""
                    UPDATE rollcall_stats
                    SET {field} = {field} + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE rollcall_id = ?
                    """,
                    (rollcall_id,),
                )
    except Exception as e:
        logging.error(f"Error incrementing rollcall stat {field}: {e}")


def get_next_position(rollcall_id: int, status: str) -> int:
    """Return next position index across both users and proxy_users tables."""
    with _cursor() as cursor:
        if status == 'in':
            col = 'in_pos'
        elif status == 'out':
            col = 'out_pos'
        elif status == 'waitlist':
            col = 'wait_pos'
        else:
            return 0

        ph = '%s' if db_type == 'postgresql' else '?'

        cursor.execute(
            f"SELECT COALESCE(MAX({col}), 0) FROM users WHERE rollcall_id = {ph} AND status = {ph}",
            (rollcall_id, status)
        )
        max_real = int(cursor.fetchone()[0] or 0)

        cursor.execute(
            f"SELECT COALESCE(MAX({col}), 0) FROM proxy_users WHERE rollcall_id = {ph} AND status = {ph}",
            (rollcall_id, status)
        )
        max_proxy = int(cursor.fetchone()[0] or 0)

        return max(max_real, max_proxy) + 1



def get_templates(chatid: int) -> List[Dict]:
    """
    Get all templates for a chat.
    """
    try:
        with _cursor() as cursor:
            if db_type == "postgresql":
                cursor.execute(
                    "SELECT * FROM templates WHERE chatid = %s ORDER BY name ASC",
                    (chatid,),
                )
            else:
                cursor.execute(
                    "SELECT * FROM templates WHERE chatid = ? ORDER BY name ASC",
                    (chatid,),
                )
            rows = cursor.fetchall()
            if db_type == "postgresql":
                return [dict(r) for r in rows]
            else:
                return [dict(r) for r in rows]
    except Exception as e:
        logging.error(f"Error getting templates: {e}")
        return []

def db_ping():
    """Lightweight database connectivity check."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

        if db_type == 'postgresql':
            cursor.execute("SELECT 1")
        else:
            cursor.execute("SELECT 1")

        cursor.fetchone()
        return True
    except Exception as e:
        logging.error(f"Database ping failed: {e}")
        return False
    finally:
        if cursor:
            if cursor is not None:
                cursor.close()
        if db_type == 'postgresql' and conn:
            release_connection(conn)

def get_template(chatid: int, name: str) -> Optional[Dict]:
    """
    Get a single template for a chat by name.
    """
    try:
        with _cursor() as cursor:
            if db_type == "postgresql":
                cursor.execute(
                    "SELECT * FROM templates WHERE chatid = %s AND name = %s",
                    (chatid, name),
                )
            else:
                cursor.execute(
                    "SELECT * FROM templates WHERE chatid = ? AND name = ?",
                    (chatid, name),
                )
            row = cursor.fetchone()
            if row:
                if db_type == "postgresql":
                    return dict(row)
                else:
                    return dict(row)
            return None
    except Exception as e:
        logging.error(f"Error getting template: {e}")
        return None


# ---------------------------------------------------------------------------
# Ghost tracking functions
# ---------------------------------------------------------------------------

def get_ghost_count(chat_id: int, user_id: int) -> int:
    """Return the ghost count for a user in a chat (0 if no record)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT ghost_count FROM ghost_records WHERE chat_id = {ph} AND user_id = {ph}",
                (chat_id, user_id)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logging.error(f"Error getting ghost count: {e}")
        return 0


def get_ghost_count_by_proxy_name(chat_id: int, proxy_name: str) -> int:
    """Return the ghost count for a proxy user in a chat (0 if no record)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT ghost_count FROM ghost_records WHERE chat_id = {ph} AND proxy_name = {ph}",
                (chat_id, proxy_name)
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logging.error(f"Error getting ghost count by proxy name: {e}")
        return 0


def increment_ghost_count(chat_id: int, user_id: int, user_name: str, proxy_name: str = None) -> bool:
    """Increment ghost count for a user or proxy user, inserting a record if one does not exist.
    
    For proxy users (added via /sif), pass user_id=-1 and the proxy_name.
    """
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                if proxy_name:
                    cursor.execute(
                        """INSERT INTO ghost_records (chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at)
                           VALUES (%s, %s, %s, %s, 1, CURRENT_TIMESTAMP)
                           ON CONFLICT (chat_id, proxy_name) WHERE proxy_name IS NOT NULL DO UPDATE
                           SET ghost_count = ghost_records.ghost_count + 1,
                               user_name = EXCLUDED.user_name,
                               last_ghosted_at = CURRENT_TIMESTAMP""",
                        (chat_id, user_id, proxy_name, user_name)
                    )
                else:
                    cursor.execute(
                        """INSERT INTO ghost_records (chat_id, user_id, user_name, ghost_count, last_ghosted_at)
                           VALUES (%s, %s, %s, 1, CURRENT_TIMESTAMP)
                           ON CONFLICT (chat_id, user_id) WHERE proxy_name IS NULL DO UPDATE
                           SET ghost_count = ghost_records.ghost_count + 1,
                               user_name = EXCLUDED.user_name,
                               last_ghosted_at = CURRENT_TIMESTAMP""",
                        (chat_id, user_id, user_name)
                    )
            else:
                # SQLite: For proxy users, look up by proxy_name; for real users, look up by user_id
                if proxy_name:
                    cursor.execute(
                        "SELECT id, ghost_count FROM ghost_records WHERE chat_id = ? AND proxy_name = ?",
                        (chat_id, proxy_name)
                    )
                else:
                    cursor.execute(
                        "SELECT id, ghost_count FROM ghost_records WHERE chat_id = ? AND user_id = ?",
                        (chat_id, user_id)
                    )
                existing = cursor.fetchone()
                if existing:
                    if proxy_name:
                        cursor.execute(
                            """UPDATE ghost_records SET ghost_count = ghost_count + 1, user_name = ?, last_ghosted_at = CURRENT_TIMESTAMP
                               WHERE chat_id = ? AND proxy_name = ?""",
                            (user_name, chat_id, proxy_name)
                        )
                    else:
                        cursor.execute(
                            """UPDATE ghost_records SET ghost_count = ghost_count + 1, user_name = ?, last_ghosted_at = CURRENT_TIMESTAMP
                               WHERE chat_id = ? AND user_id = ?""",
                            (user_name, chat_id, user_id)
                        )
                else:
                    cursor.execute(
                        """INSERT INTO ghost_records (chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at)
                           VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)""",
                        (chat_id, user_id, proxy_name, user_name)
                    )
            logging.info(f"Incremented ghost count for user {user_id}/{proxy_name} in chat {chat_id}")
            return True
    except Exception as e:
        logging.error(f"Error incrementing ghost count: {e}")
        return False


def reset_ghost_count(chat_id: int, user_id: int, proxy_name: str = None) -> bool:
    """Reset ghost count to 0 for a user or proxy user (admin clear).

    For proxy users, pass user_id=-1 and the proxy_name.
    """
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if proxy_name:
                cursor.execute(
                    f"UPDATE ghost_records SET ghost_count = 0, last_ghosted_at = NULL WHERE chat_id = {ph} AND proxy_name = {ph}",
                    (chat_id, proxy_name)
                )
            else:
                cursor.execute(
                    f"UPDATE ghost_records SET ghost_count = 0, last_ghosted_at = NULL WHERE chat_id = {ph} AND user_id = {ph}",
                    (chat_id, user_id)
                )
            logging.info(f"Reset ghost count for user {user_id}/{proxy_name} in chat {chat_id}")
            return True
    except Exception as e:
        logging.error(f"Error resetting ghost count: {e}")
        return False


def decrement_ghost_count(chat_id: int, user_id: int, proxy_name: str = None) -> bool:
    """Decrement ghost count by 1, floored at 0. No-op if no record exists.

    Called from the /mark_absent finalize step for every IN user who was NOT
    selected as a ghost — i.e. they actually attended. The count never goes
    negative; when it lands at 0, last_ghosted_at is cleared too so the
    leaderboard and reconf threshold treat them as fresh.
    """
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            sql = (
                "UPDATE ghost_records SET "
                "ghost_count = CASE WHEN ghost_count > 0 THEN ghost_count - 1 ELSE 0 END, "
                "last_ghosted_at = CASE WHEN ghost_count > 1 THEN last_ghosted_at ELSE NULL END "
                f"WHERE chat_id = {ph} AND "
            )
            if proxy_name:
                cursor.execute(sql + f"proxy_name = {ph}", (chat_id, proxy_name))
            else:
                cursor.execute(sql + f"user_id = {ph}", (chat_id, user_id))
            return True
    except Exception as e:
        logging.error(f"Error decrementing ghost count: {e}")
        return False


def get_ghost_leaderboard(chat_id: int) -> List[Dict]:
    """Return all users with ghost_count > 0 for a chat, combined across
    any merged identity aliases (ghost_count summed, last_ghosted_at is the
    most recent across the group), sorted descending."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""SELECT user_id, proxy_name, user_name, ghost_count, last_ghosted_at
                    FROM ghost_records
                    WHERE chat_id = {ph} AND ghost_count > 0""",
                (chat_id,)
            )
            raw_rows = [dict(row) for row in cursor.fetchall()]

        from services import identity as identity_svc
        collapsed: Dict[tuple, Dict] = {}
        for row in raw_rows:
            is_proxy = row.get('proxy_name') is not None
            canonical = (identity_svc.resolve_canonical(chat_id, proxy_name=row['proxy_name']) if is_proxy
                         else identity_svc.resolve_canonical(chat_id, user_id=row['user_id']))
            if canonical['kind'] == 'user':
                key = ('user', canonical['user_id'])
            else:
                key = ('proxy', (canonical['proxy_name'] or '').lower())

            if key not in collapsed:
                collapsed[key] = {
                    'user_id': canonical['user_id'] if canonical['kind'] == 'user' else -1,
                    'proxy_name': canonical['proxy_name'],
                    'user_name': None,
                    'ghost_count': 0,
                    'last_ghosted_at': None,
                }
            bucket = collapsed[key]
            bucket['ghost_count'] += row.get('ghost_count') or 0
            last = row.get('last_ghosted_at')
            if last and (bucket['last_ghosted_at'] is None or last > bucket['last_ghosted_at']):
                bucket['last_ghosted_at'] = last
            # Prefer the canonical identity's OWN row's stored name over an
            # alias's — only fall back to a synthesized name below if the
            # canonical never had its own ghost_records row (it only exists
            # in this leaderboard because an alias points at it).
            is_canonical_own_row = (
                (canonical['kind'] == 'user' and not is_proxy and row['user_id'] == canonical['user_id'])
                or (canonical['kind'] == 'proxy' and is_proxy
                    and (row['proxy_name'] or '').lower() == (canonical['proxy_name'] or '').lower())
            )
            if row.get('user_name') and (bucket['user_name'] is None or is_canonical_own_row):
                bucket['user_name'] = row['user_name']

        result = list(collapsed.values())
        for bucket in result:
            if not bucket['user_name']:
                bucket['user_name'] = (bucket['proxy_name'] if bucket['proxy_name']
                                        else _member_display_name(chat_id, bucket['user_id']))
        result.sort(key=lambda r: (r['ghost_count'] or 0, r['last_ghosted_at'] or ''), reverse=True)
        return result
    except Exception as e:
        logging.error(f"Error getting ghost leaderboard: {e}")
        return []


def get_identity_link(chat_id: int, alias_proxy_name: str) -> Optional[Dict]:
    """The active 'linked' row for this alias (case-insensitive), or None
    if this proxy name isn't currently merged into anything."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""SELECT * FROM identity_links
                    WHERE chat_id = {ph} AND LOWER(alias_proxy_name) = LOWER({ph}) AND status = 'linked'""",
                (chat_id, alias_proxy_name)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error getting identity link: {e}")
        return None


def get_links_by_canonical(chat_id: int, canonical_user_id: int = None,
                            canonical_proxy_name: str = None) -> List[Dict]:
    """Every active 'linked' row whose canonical target is this identity —
    i.e. every alias currently pointing here."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if canonical_user_id is not None:
                cursor.execute(
                    f"""SELECT * FROM identity_links
                        WHERE chat_id = {ph} AND canonical_user_id = {ph} AND status = 'linked'""",
                    (chat_id, canonical_user_id)
                )
            else:
                cursor.execute(
                    f"""SELECT * FROM identity_links
                        WHERE chat_id = {ph} AND LOWER(canonical_proxy_name) = LOWER({ph}) AND status = 'linked'""",
                    (chat_id, canonical_proxy_name or "")
                )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting links by canonical: {e}")
        return []


def upsert_identity_link(chat_id: int, alias_proxy_name: str, *,
                          canonical_user_id: int = None,
                          canonical_proxy_name: str = None,
                          created_by: int = None, created_by_name: str = None) -> Dict:
    """Create (or repoint, if already linked) the active link for this
    alias. Exactly one of canonical_user_id/canonical_proxy_name should be
    given — enforced by services/identity.py, not here."""
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """INSERT INTO identity_links
                           (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                            status, created_by, created_by_name, created_at)
                       VALUES (%s, %s, %s, %s, 'linked', %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (chat_id, LOWER(alias_proxy_name)) WHERE status = 'linked'
                       DO UPDATE SET canonical_user_id = EXCLUDED.canonical_user_id,
                                     canonical_proxy_name = EXCLUDED.canonical_proxy_name,
                                     created_by = EXCLUDED.created_by,
                                     created_by_name = EXCLUDED.created_by_name,
                                     created_at = CURRENT_TIMESTAMP
                       RETURNING *""",
                    (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                     created_by, created_by_name)
                )
                return dict(cursor.fetchone())
            else:
                cursor.execute(
                    "SELECT id FROM identity_links WHERE chat_id = ? AND LOWER(alias_proxy_name) = LOWER(?) AND status = 'linked'",
                    (chat_id, alias_proxy_name)
                )
                existing = cursor.fetchone()
                if existing:
                    cursor.execute(
                        """UPDATE identity_links
                           SET canonical_user_id = ?, canonical_proxy_name = ?,
                               created_by = ?, created_by_name = ?, created_at = CURRENT_TIMESTAMP
                           WHERE id = ?""",
                        (canonical_user_id, canonical_proxy_name, created_by, created_by_name, existing["id"])
                    )
                else:
                    cursor.execute(
                        """INSERT INTO identity_links
                               (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                                status, created_by, created_by_name, created_at)
                           VALUES (?, ?, ?, ?, 'linked', ?, ?, CURRENT_TIMESTAMP)""",
                        (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                         created_by, created_by_name)
                    )
                cursor.execute(
                    "SELECT * FROM identity_links WHERE chat_id = ? AND LOWER(alias_proxy_name) = LOWER(?) AND status = 'linked'",
                    (chat_id, alias_proxy_name)
                )
                return dict(cursor.fetchone())
    except Exception:
        logging.exception("upsert_identity_link failed")
        raise


def repoint_links(chat_id: int, from_proxy_name: str, *,
                   to_user_id: int = None, to_proxy_name: str = None) -> int:
    """Cascade step: repoint every active alias currently targeting
    from_proxy_name (i.e. from_proxy_name was itself a merge target with
    its own aliases) to the new final target. Returns rows affected."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""UPDATE identity_links
                    SET canonical_user_id = {ph}, canonical_proxy_name = {ph}
                    WHERE chat_id = {ph} AND status = 'linked'
                      AND LOWER(canonical_proxy_name) = LOWER({ph})""",
                (to_user_id, to_proxy_name, chat_id, from_proxy_name)
            )
            return cursor.rowcount or 0
    except Exception:
        logging.exception("repoint_links failed")
        raise


def delete_identity_link(chat_id: int, alias_proxy_name: str) -> bool:
    """Unmerge: delete this alias's active link row. Returns True if a row
    was deleted, False if it wasn't linked to begin with."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""DELETE FROM identity_links
                    WHERE chat_id = {ph} AND LOWER(alias_proxy_name) = LOWER({ph}) AND status = 'linked'""",
                (chat_id, alias_proxy_name)
            )
            return (cursor.rowcount or 0) > 0
    except Exception:
        logging.exception("delete_identity_link failed")
        raise


def discard_identity_name(chat_id: int, alias_proxy_name: str, *,
                           created_by: int = None, created_by_name: str = None) -> None:
    """Mark a proxy name as invalid/garbage (a stray "2" or "]" from a
    typo'd /sif) so it stops showing up in suggestions, the merge picker,
    and the identities list — without touching its historical proxy_users
    rows (past attendance isn't deleted, just hidden from future merge
    bookkeeping). Idempotent. Reversible via undiscard_identity_name."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""SELECT id FROM identity_links
                    WHERE chat_id = {ph} AND LOWER(alias_proxy_name) = LOWER({ph}) AND status = 'discarded'""",
                (chat_id, alias_proxy_name)
            )
            if cursor.fetchone() is not None:
                return
            cursor.execute(
                f"""INSERT INTO identity_links
                        (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                         status, created_by, created_by_name, created_at)
                    VALUES ({ph}, {ph}, NULL, NULL, 'discarded', {ph}, {ph}, CURRENT_TIMESTAMP)""",
                (chat_id, alias_proxy_name, created_by, created_by_name)
            )
    except Exception:
        logging.exception("discard_identity_name failed")
        raise


def undiscard_identity_name(chat_id: int, alias_proxy_name: str) -> bool:
    """Reverse discard_identity_name. Returns True if a row was removed."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""DELETE FROM identity_links
                    WHERE chat_id = {ph} AND LOWER(alias_proxy_name) = LOWER({ph}) AND status = 'discarded'""",
                (chat_id, alias_proxy_name)
            )
            return (cursor.rowcount or 0) > 0
    except Exception:
        logging.exception("undiscard_identity_name failed")
        raise


def list_identity_links(chat_id: int, status: str = 'linked') -> List[Dict]:
    """All rows for a chat with the given status."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT * FROM identity_links WHERE chat_id = {ph} AND status = {ph}",
                (chat_id, status)
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error listing identity links: {e}")
        return []


def insert_dismissed_suggestion(chat_id: int, alias_proxy_name: str, *,
                                 candidate_user_id: int = None,
                                 candidate_proxy_name: str = None,
                                 created_by: int = None, created_by_name: str = None) -> None:
    """Record that this specific (alias, candidate) pairing was reviewed and
    rejected, so list_suggestions doesn't keep proposing it. Idempotent —
    a repeat dismissal of the same pair is a silent no-op."""
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """INSERT INTO identity_links
                           (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                            status, created_by, created_by_name, created_at)
                       VALUES (%s, %s, %s, %s, 'dismissed', %s, %s, CURRENT_TIMESTAMP)
                       ON CONFLICT (chat_id, LOWER(alias_proxy_name),
                                    COALESCE(CAST(canonical_user_id AS TEXT), LOWER(canonical_proxy_name)))
                       WHERE status = 'dismissed' DO NOTHING""",
                    (chat_id, alias_proxy_name, candidate_user_id, candidate_proxy_name,
                     created_by, created_by_name)
                )
            else:
                cursor.execute(
                    """SELECT id FROM identity_links
                       WHERE chat_id = ? AND LOWER(alias_proxy_name) = LOWER(?) AND status = 'dismissed'
                         AND COALESCE(CAST(canonical_user_id AS TEXT), LOWER(canonical_proxy_name))
                             = COALESCE(?, LOWER(?))""",
                    (chat_id, alias_proxy_name,
                     str(candidate_user_id) if candidate_user_id is not None else None,
                     candidate_proxy_name)
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        """INSERT INTO identity_links
                               (chat_id, alias_proxy_name, canonical_user_id, canonical_proxy_name,
                                status, created_by, created_by_name, created_at)
                           VALUES (?, ?, ?, ?, 'dismissed', ?, ?, CURRENT_TIMESTAMP)""",
                        (chat_id, alias_proxy_name, candidate_user_id, candidate_proxy_name,
                         created_by, created_by_name)
                    )
    except Exception:
        logging.exception("insert_dismissed_suggestion failed")
        raise


def get_all_proxy_names(chat_id: int) -> List[str]:
    """Every distinct proxy name ever recorded in this chat (active or
    ended rollcalls) — powers the merge picker and suggestion engine.
    (find_proxy_in_chat only checks existence of ONE specific name;
    get_group_attendance_totals only returns a COUNT — neither returns
    the actual name list.)"""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""SELECT DISTINCT pu.name FROM proxy_users pu
                    JOIN rollcalls r ON pu.rollcall_id = r.id
                    WHERE r.chat_id = {ph}
                    ORDER BY pu.name""",
                (chat_id,)
            )
            return [row["name"] for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting all proxy names: {e}")
        return []


def get_identity_last_activity(chat_id: int, user_id: int = None,
                                proxy_name: str = None) -> Optional[str]:
    """updated_at from user_stats/proxy_stats for one identity — powers the
    'current_streak belongs to whichever alias was most recently active'
    merge heuristic. Returns None if the identity has no stats row yet."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if user_id is not None:
                cursor.execute(
                    f"SELECT updated_at FROM user_stats WHERE chat_id = {ph} AND user_id = {ph}",
                    (chat_id, user_id)
                )
            else:
                cursor.execute(
                    f"SELECT updated_at FROM proxy_stats WHERE chat_id = {ph} AND proxy_name = {ph}",
                    (chat_id, proxy_name)
                )
            row = cursor.fetchone()
            return row["updated_at"] if row else None
    except Exception as e:
        logging.error(f"Error getting identity last activity: {e}")
        return None


def get_user_ghost_count_by_name(chat_id: int, user_name: str) -> Optional[Dict]:
    """Find a ghost record by user_name or proxy_name for a chat (for admin /clear_absent by name)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT user_id, proxy_name, user_name, ghost_count FROM ghost_records WHERE chat_id = {ph} AND (user_name = {ph} OR proxy_name = {ph})",
                (chat_id, user_name, user_name)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error looking up ghost record by name: {e}")
        return None


def mark_rollcall_absent_done(rollcall_id: int) -> bool:
    """Mark a rollcall's absent selection as completed."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            val = True if db_type == 'postgresql' else 1
            cursor.execute(
                f"UPDATE rollcalls SET absent_marked = {ph} WHERE id = {ph}",
                (val, rollcall_id)
            )
            logging.info(f"Marked rollcall {rollcall_id} absent_marked=True")
            return True
    except Exception as e:
        logging.error(f"Error marking rollcall absent done: {e}")
        return False


def get_unprocessed_rollcalls(chat_id: int, days: int = 30) -> List[Dict]:
    """
    Return ended roll calls that still need absent marking:
      - is_active = FALSE (ended)
      - absent_marked = FALSE (not yet processed)
      - ended_at within the last `days` days
      - had at least one user with status='in'
    """
    try:
        with _cursor() as cursor:
            if db_type == 'postgresql':
                cursor.execute(
                    """SELECT r.id, r.title, r.ended_at
                       FROM rollcalls r
                       WHERE r.chat_id = %s
                         AND r.is_active = FALSE
                         AND r.absent_marked = FALSE
                         AND r.ended_at >= NOW() - (%s * INTERVAL '1 day')
                         AND EXISTS (
                             SELECT 1 FROM users u
                             WHERE u.rollcall_id = r.id AND u.status = 'in'
                         )
                       ORDER BY r.ended_at DESC""",
                    (chat_id, days)
                )
            else:
                cursor.execute(
                    """SELECT r.id, r.title, r.ended_at
                       FROM rollcalls r
                       WHERE r.chat_id = ?
                         AND r.is_active = 0
                         AND r.absent_marked = 0
                         AND r.ended_at >= datetime('now', ? || ' days')
                         AND EXISTS (
                             SELECT 1 FROM users u
                             WHERE u.rollcall_id = r.id AND u.status = 'in'
                         )
                       ORDER BY r.ended_at DESC""",
                    (chat_id, f'-{days}')
                )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting unprocessed rollcalls: {e}")
        return []


def add_ghost_event(rollcall_id: int, chat_id: int, user_id: int = None, user_name: str = None, proxy_name: str = None) -> bool:
    """Record an individual ghost event for audit trail."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"""INSERT INTO ghost_events (rollcall_id, chat_id, user_id, proxy_name, user_name)
                    VALUES ({ph}, {ph}, {ph}, {ph}, {ph})""",
                (rollcall_id, chat_id, user_id, proxy_name, user_name)
            )
            return True
    except Exception as e:
        logging.error(f"Error adding ghost event: {e}")
        return False


def get_rollcall_in_users(rollcall_id: int) -> List[Dict]:
    """Return all users (real + proxy) with status='in' for a given rollcall.

    Real users (signed in via /in or the panel) have an integer ``user_id``.
    Proxy users (added via /sif) have ``user_id=None`` and a ``proxy_name`` key.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'

            # Real Telegram users
            cursor.execute(
                f"""SELECT user_id, first_name, username
                    FROM users
                    WHERE rollcall_id = {ph} AND status = 'in'
                    ORDER BY in_pos ASC""",
                (rollcall_id,)
            )
            real_rows = [dict(row) for row in cursor.fetchall()]

            # Proxy users added via /sif (no Telegram user_id)
            cursor.execute(
                f"""SELECT name, proxy_owner_id
                    FROM proxy_users
                    WHERE rollcall_id = {ph} AND status = 'in'
                    ORDER BY in_pos ASC""",
                (rollcall_id,)
            )
            proxy_rows = [
                {'user_id': None, 'first_name': row['name'], 'username': None,
                 'proxy_name': row['name'], 'proxy_owner_id': row['proxy_owner_id']}
                for row in cursor.fetchall()
            ]

            return real_rows + proxy_rows
    except Exception as e:
        logging.error(f"Error getting rollcall IN users: {e}")
        return []


# Ghost selection persistence: save/load selections to DB
def save_ghost_selections(chat_id: int, rc_db_id: int, selected_ids: set) -> bool:
    """Save ghost selections to database for crash recovery"""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == 'postgresql' else "?"
            ts = "NOW()" if db_type == 'postgresql' else "CURRENT_TIMESTAMP"
        
            # Upsert selections
            cursor.execute(
                f"""INSERT INTO ghost_selections (chat_id, rc_db_id, selected_ids, updated_at)
                   VALUES ({ph}, {ph}, {ph}, {ts})
                   ON CONFLICT (chat_id, rc_db_id) 
                   DO UPDATE SET selected_ids = {ph}, updated_at = {ts}""",
                (chat_id, rc_db_id, json.dumps(list(selected_ids)), json.dumps(list(selected_ids)))
            )
            return True
    except Exception as e:
        logging.error(f"Error saving ghost selections: {e}")
        return False


def load_ghost_selections(chat_id: int, rc_db_id: int) -> Optional[set]:
    """Load ghost selections from database for crash recovery"""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == 'postgresql' else "?"
        
            cursor.execute(
                f"""SELECT selected_ids FROM ghost_selections 
                   WHERE chat_id = {ph} AND rc_db_id = {ph}""",
                (chat_id, rc_db_id)
            )
            row = cursor.fetchone()
            if row and row['selected_ids']:
                return set(json.loads(row['selected_ids']))
            return None
    except Exception as e:
        logging.error(f"Error loading ghost selections: {e}")
        return None


def create_ghost_selections_table() -> None:
    """Create ghost_selections table if not exists"""
    try:
        with _cursor(commit=True) as cursor:
            if db_type == 'postgresql':
                cursor.execute("""CREATE TABLE IF NOT EXISTS ghost_selections (
                    chat_id BIGINT NOT NULL,
                    rc_db_id INTEGER NOT NULL,
                    selected_ids JSONB DEFAULT '[]',
                    updated_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (chat_id, rc_db_id)
                )""")
            else:
                cursor.execute("""CREATE TABLE IF NOT EXISTS ghost_selections (
                    chat_id INTEGER NOT NULL,
                    rc_db_id INTEGER NOT NULL,
                    selected_ids TEXT DEFAULT '[]',
                    updated_at TIMESTAMP,
                    PRIMARY KEY (chat_id, rc_db_id)
                )""")
    except Exception as e:
        logging.error(f"Error creating ghost_selections table: {e}")


def update_streak_on_checkin(chat_id: int, user_id: int) -> None:
    """Increment current_streak by 1 for a user at rollcall end; update best_streak if exceeded."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    INSERT INTO user_stats (chat_id, user_id, current_streak, best_streak)
                    VALUES ({ph}, {ph}, 1, 1)
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        current_streak = user_stats.current_streak + 1,
                        best_streak    = GREATEST(user_stats.best_streak, user_stats.current_streak + 1),
                        updated_at     = CURRENT_TIMESTAMP
                """, (chat_id, user_id))
            else:
                cursor.execute(f"""
                    INSERT OR IGNORE INTO user_stats (chat_id, user_id) VALUES ({ph}, {ph})
                """, (chat_id, user_id))
                cursor.execute(f"""
                    UPDATE user_stats
                    SET current_streak = current_streak + 1,
                        best_streak    = MAX(best_streak, current_streak + 1),
                        updated_at     = CURRENT_TIMESTAMP
                    WHERE chat_id = {ph} AND user_id = {ph}
                """, (chat_id, user_id))
    except Exception as e:
        logging.error(f"Error updating streak on checkin: {e}")


def reset_user_streak(chat_id: int, user_id: int) -> None:
    """Reset current_streak to 0. Called when a user breaks a streak — either
    by being ghost-marked, or by ending a session as OUT / MAYBE rather than IN."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(f"""
            UPDATE user_stats SET current_streak = 0, updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = {ph} AND user_id = {ph}
        """, (chat_id, user_id))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"Error resetting streak: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def update_proxy_streak_on_checkin(chat_id: int, proxy_name: str) -> None:
    """Increment current_streak by 1 for a proxy at rollcall end; update
    best_streak if exceeded. Mirrors update_streak_on_checkin for real users
    but keyed on proxy_name and stored in the parallel proxy_stats table."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        if db_type == 'postgresql':
            cursor.execute(f"""
                INSERT INTO proxy_stats (chat_id, proxy_name, current_streak, best_streak)
                VALUES ({ph}, {ph}, 1, 1)
                ON CONFLICT (chat_id, proxy_name) DO UPDATE SET
                    current_streak = proxy_stats.current_streak + 1,
                    best_streak    = GREATEST(proxy_stats.best_streak, proxy_stats.current_streak + 1),
                    updated_at     = CURRENT_TIMESTAMP
            """, (chat_id, proxy_name))
        else:
            cursor.execute(f"""
                INSERT OR IGNORE INTO proxy_stats (chat_id, proxy_name) VALUES ({ph}, {ph})
            """, (chat_id, proxy_name))
            cursor.execute(f"""
                UPDATE proxy_stats
                SET current_streak = current_streak + 1,
                    best_streak    = MAX(best_streak, current_streak + 1),
                    updated_at     = CURRENT_TIMESTAMP
                WHERE chat_id = {ph} AND proxy_name = {ph}
            """, (chat_id, proxy_name))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"Error updating proxy streak on checkin: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def reset_proxy_streak(chat_id: int, proxy_name: str) -> None:
    """Reset a proxy's current_streak to 0 — called when a proxy's final
    status at /erc is OUT or MAYBE (mirrors reset_user_streak for real
    users)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(f"""
            UPDATE proxy_stats SET current_streak = 0, updated_at = CURRENT_TIMESTAMP
            WHERE chat_id = {ph} AND proxy_name = {ph}
        """, (chat_id, proxy_name))
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logging.error(f"Error resetting proxy streak: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_user_streaks(chat_id: int, user_id: int) -> Dict:
    """Return {current_streak, best_streak} for a real user, 0s if no stats row."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                SELECT current_streak, best_streak
                FROM user_stats WHERE chat_id = {ph} AND user_id = {ph}
            """, (chat_id, user_id))
            row = cursor.fetchone()
            if row is None:
                return {'current_streak': 0, 'best_streak': 0}
            if isinstance(row, dict):
                return {
                    'current_streak': int(row.get('current_streak') or 0),
                    'best_streak':    int(row.get('best_streak') or 0),
                }
            return {'current_streak': int(row[0] or 0), 'best_streak': int(row[1] or 0)}
    except Exception as e:
        logging.error(f"Error fetching user streaks: {e}")
        return {'current_streak': 0, 'best_streak': 0}


def get_proxy_streaks(chat_id: int, proxy_name: str) -> Dict:
    """Return {current_streak, best_streak} for a proxy. Both default to 0
    if the proxy has no proxy_stats row yet (i.e. hasn't been through an
    /erc since proxy_stats was introduced)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                SELECT current_streak, best_streak
                FROM proxy_stats WHERE chat_id = {ph} AND proxy_name = {ph}
            """, (chat_id, proxy_name))
            row = cursor.fetchone()
            if row is None:
                return {'current_streak': 0, 'best_streak': 0}
            if isinstance(row, dict):
                return {
                    'current_streak': int(row.get('current_streak') or 0),
                    'best_streak':    int(row.get('best_streak') or 0),
                }
            return {'current_streak': int(row[0] or 0), 'best_streak': int(row[1] or 0)}
    except Exception as e:
        logging.error(f"Error fetching proxy streaks: {e}")
        return {'current_streak': 0, 'best_streak': 0}


def get_chat_ended_rollcall_count(chat_id: int) -> int:
    """Return the total number of ENDED rollcalls in this chat.

    Used as the denominator for Voting% and Attendance% in /stats — both
    rates measure each user against ALL ended sessions, not just sessions
    they participated in. That's the only way "voting %" means engagement
    rather than "100% trivially because they voted at least once."
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(
                f"SELECT COUNT(*) FROM rollcalls WHERE chat_id = {ph} AND is_active = {active_false}",
                (chat_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return 0
            if isinstance(row, dict):
                return int(next(iter(row.values())) or 0)
            return int(row[0] or 0)
    except Exception as e:
        logging.error(f"Error counting ended rollcalls: {e}")
        return 0


def get_user_attendance_count(chat_id: int, user_id: int) -> int:
    """Return the number of ENDED rollcalls in this chat where the user's
    final status was IN. This is the authoritative attendance number —
    user_stats.total_in counts every IN VOTE (which inflates if a user flips
    IN→OUT→IN within one session), so it must not be used for attendance %.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT COUNT(*) FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND u.user_id = {ph}
                  AND u.status = 'in' AND r.is_active = {active_false}
            """, (chat_id, user_id))
            row = cursor.fetchone()
            if row is None:
                return 0
            if isinstance(row, dict):
                return int(next(iter(row.values())) or 0)
            return int(row[0] or 0)
    except Exception as e:
        logging.error(f"Error counting attendance: {e}")
        return 0


def get_leaderboard_by_attendance(chat_id: int, limit: int = 10) -> List[Dict]:
    """Return top-N PARTICIPANTS (real users + proxies) ordered by actual
    attendance count (final-IN in ended rollcalls), tiebreak by
    participation count ASC (rewards consistency — fewer sessions to attend
    the same number of times means higher attendance %).

    Each row: kind ('real' or 'proxy'), user_id (int or None), proxy_name
    (str or None), display_name (best-known label), attended,
    total_rollcalls (sessions participated in), total_in/out/maybe (vote
    breakdown — proxy rows count per-session, real-user rows count per-vote
    via user_stats).
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'

            # Real users — same query as before
            cursor.execute(f"""
                SELECT us.user_id,
                       COALESCE(att.attended, 0) AS attended,
                       COALESCE(us.total_rollcalls, 0) AS total_rc,
                       COALESCE(us.total_in, 0) AS total_in,
                       COALESCE(us.total_out, 0) AS total_out,
                       COALESCE(us.total_maybe, 0) AS total_maybe
                FROM user_stats us
                LEFT JOIN (
                    SELECT u.user_id, COUNT(*) AS attended
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = {ph} AND u.status = 'in' AND r.is_active = {active_false}
                    GROUP BY u.user_id
                ) att ON att.user_id = us.user_id
                WHERE us.chat_id = {ph}
            """, (chat_id, chat_id))
            real_rows = cursor.fetchall()

            # Proxies — derived entirely from proxy_users because they have no
            # user_stats counter row. Per-row metrics: attended = COUNT where
            # status='in', total_rc = COUNT (any status), in/out/maybe = COUNT
            # of each. proxy_users is UNIQUE(rollcall_id, name) so each row
            # counts at most once per rollcall.
            cursor.execute(f"""
                SELECT pu.name,
                       SUM(CASE WHEN pu.status = 'in'    THEN 1 ELSE 0 END) AS attended,
                       COUNT(*) AS total_rc,
                       SUM(CASE WHEN pu.status = 'in'    THEN 1 ELSE 0 END) AS total_in,
                       SUM(CASE WHEN pu.status = 'out'   THEN 1 ELSE 0 END) AS total_out,
                       SUM(CASE WHEN pu.status = 'maybe' THEN 1 ELSE 0 END) AS total_maybe
                FROM proxy_users pu
                JOIN rollcalls r ON pu.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND r.is_active = {active_false}
                GROUP BY pu.name
            """, (chat_id,))
            proxy_rows = cursor.fetchall()

            # Real-user display names — latest first_name/username per user_id
            # from ended rollcalls. Filtered by is_active so we don't pull
            # display data from in-progress sessions.
            name_map = {}
            if real_rows:
                uids = [
                    (r['user_id'] if isinstance(r, dict) else r[0])
                    for r in real_rows
                ]
                placeholders = ",".join([ph] * len(uids))
                if db_type == 'postgresql':
                    cursor.execute(f"""
                        SELECT DISTINCT ON (u.user_id) u.user_id, u.first_name, u.username
                        FROM users u
                        JOIN rollcalls r ON u.rollcall_id = r.id
                        WHERE r.chat_id = {ph} AND r.is_active = {active_false}
                          AND u.user_id IN ({placeholders})
                        ORDER BY u.user_id, u.updated_at DESC
                    """, (chat_id, *uids))
                else:
                    cursor.execute(f"""
                        SELECT u.user_id, u.first_name, u.username
                        FROM users u
                        JOIN rollcalls r ON u.rollcall_id = r.id
                        WHERE r.chat_id = {ph} AND r.is_active = {active_false}
                          AND u.user_id IN ({placeholders})
                        ORDER BY u.user_id, u.updated_at ASC
                    """, (chat_id, *uids))
                for ur in cursor.fetchall():
                    if isinstance(ur, dict):
                        name_map[ur['user_id']] = (ur.get('first_name'), ur.get('username'))
                    else:
                        name_map[ur[0]] = (ur[1], ur[2])

            # Materialize unified rows
            unified = []
            for r in real_rows:
                uid = r['user_id'] if isinstance(r, dict) else r[0]
                first_name, username = name_map.get(uid, (None, None))
                unified.append({
                    'kind':            'real',
                    'user_id':         uid,
                    'proxy_name':      None,
                    'display_name':    first_name or username or f"User {uid}",
                    'username':        username,
                    'attended':        int((r['attended']    if isinstance(r, dict) else r[1]) or 0),
                    'total_rollcalls': int((r['total_rc']    if isinstance(r, dict) else r[2]) or 0),
                    'total_in':        int((r['total_in']    if isinstance(r, dict) else r[3]) or 0),
                    'total_out':       int((r['total_out']   if isinstance(r, dict) else r[4]) or 0),
                    'total_maybe':     int((r['total_maybe'] if isinstance(r, dict) else r[5]) or 0),
                })
            for r in proxy_rows:
                name = r['name'] if isinstance(r, dict) else r[0]
                unified.append({
                    'kind':            'proxy',
                    'user_id':         None,
                    'proxy_name':      name,
                    'display_name':    name,
                    'username':        None,
                    'attended':        int((r['attended']    if isinstance(r, dict) else r[1]) or 0),
                    'total_rollcalls': int((r['total_rc']    if isinstance(r, dict) else r[2]) or 0),
                    'total_in':        int((r['total_in']    if isinstance(r, dict) else r[3]) or 0),
                    'total_out':       int((r['total_out']   if isinstance(r, dict) else r[4]) or 0),
                    'total_maybe':     int((r['total_maybe'] if isinstance(r, dict) else r[5]) or 0),
                })

            # Fold merged identity aliases into one combined entry before
            # ranking — a proxy merged into a real user (or into another
            # proxy) must show as a single participant, not two. See
            # services/identity.py.
            from services import identity as identity_svc
            collapsed: Dict[tuple, Dict] = {}
            for entry in unified:
                canonical = (identity_svc.resolve_canonical(chat_id, user_id=entry['user_id'])
                             if entry['kind'] == 'real'
                             else identity_svc.resolve_canonical(chat_id, proxy_name=entry['proxy_name']))
                if canonical['kind'] == 'user':
                    key = ('real', canonical['user_id'])
                else:
                    key = ('proxy', (canonical['proxy_name'] or '').lower())

                if key not in collapsed:
                    if canonical['kind'] == 'user':
                        display_name = _member_display_name(chat_id, canonical['user_id'])
                        username = name_map.get(canonical['user_id'], (None, None))[1]
                    else:
                        display_name = canonical['proxy_name']
                        username = None
                    collapsed[key] = {
                        'kind': canonical['kind'] if canonical['kind'] == 'proxy' else 'real',
                        'user_id': canonical['user_id'], 'proxy_name': canonical['proxy_name'],
                        'display_name': display_name, 'username': username,
                        'attended': 0, 'total_rollcalls': 0, 'total_in': 0, 'total_out': 0, 'total_maybe': 0,
                    }
                bucket = collapsed[key]
                bucket['attended'] += entry['attended']
                bucket['total_rollcalls'] += entry['total_rollcalls']
                bucket['total_in'] += entry['total_in']
                bucket['total_out'] += entry['total_out']
                bucket['total_maybe'] += entry['total_maybe']
            unified = list(collapsed.values())

            # Sort by attended DESC, total_rollcalls ASC (rewards consistency),
            # then deterministic tiebreak by display_name ASC.
            unified.sort(key=lambda x: (-x['attended'], x['total_rollcalls'], x['display_name'] or ''))
            return unified[:limit]
    except Exception as e:
        logging.error(f"Error fetching attendance leaderboard: {e}")
        return []


def get_proxy_attendance_count(chat_id: int, proxy_name: str) -> int:
    """Same idea as get_user_attendance_count, but for proxy users."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT COUNT(*) FROM proxy_users pu
                JOIN rollcalls r ON pu.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND pu.name = {ph}
                  AND pu.status = 'in' AND r.is_active = {active_false}
            """, (chat_id, proxy_name))
            row = cursor.fetchone()
            if row is None:
                return 0
            if isinstance(row, dict):
                return int(next(iter(row.values())) or 0)
            return int(row[0] or 0)
    except Exception as e:
        logging.error(f"Error counting proxy attendance: {e}")
        return 0


def get_proxy_stats(chat_id: int, proxy_name: str) -> Dict:
    """Return a single proxy's aggregate stats: total rollcalls participated
    in (any status), attended (final-IN), and per-status vote breakdown.
    For proxies the per-status breakdown is per-rollcall (proxy_users is
    UNIQUE(rollcall_id, name) so each row = one final status per session)
    — not per-vote like the real-user total_in counter."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT COUNT(*) AS total_rc,
                       SUM(CASE WHEN pu.status = 'in'    THEN 1 ELSE 0 END) AS total_in,
                       SUM(CASE WHEN pu.status = 'out'   THEN 1 ELSE 0 END) AS total_out,
                       SUM(CASE WHEN pu.status = 'maybe' THEN 1 ELSE 0 END) AS total_maybe
                FROM proxy_users pu
                JOIN rollcalls r ON pu.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND pu.name = {ph} AND r.is_active = {active_false}
            """, (chat_id, proxy_name))
            row = cursor.fetchone()
            if row is None:
                return {'total_rollcalls': 0, 'attended': 0, 'total_in': 0, 'total_out': 0, 'total_maybe': 0}
            if isinstance(row, dict):
                return {
                    'total_rollcalls': int(row.get('total_rc') or 0),
                    'attended':        int(row.get('total_in') or 0),
                    'total_in':        int(row.get('total_in') or 0),
                    'total_out':       int(row.get('total_out') or 0),
                    'total_maybe':     int(row.get('total_maybe') or 0),
                }
            return {
                'total_rollcalls': int(row[0] or 0),
                'attended':        int(row[1] or 0),
                'total_in':        int(row[1] or 0),
                'total_out':       int(row[2] or 0),
                'total_maybe':     int(row[3] or 0),
            }
    except Exception as e:
        logging.error(f"Error fetching proxy stats: {e}")
        return {'total_rollcalls': 0, 'attended': 0, 'total_in': 0, 'total_out': 0, 'total_maybe': 0}


def get_group_attendance_totals(chat_id: int) -> Dict:
    """Aggregate group-level attendance stats including BOTH real users and
    proxies. Returns: total_rollcalls (ended), real_attendance_slots,
    proxy_attendance_slots, real_participants (distinct user_id),
    proxy_participants (distinct proxy name), real_vote_in/out/maybe (from
    user_stats — per-vote counts), proxy_in/out/maybe (per-session counts
    from proxy_users), waitlist_promotions (from user_stats)."""
    conn = get_connection()
    cursor = None
    out = {
        'total_rollcalls': 0,
        'real_attendance_slots': 0, 'proxy_attendance_slots': 0,
        'real_participants': 0,     'proxy_participants': 0,
        'real_vote_in': 0, 'real_vote_out': 0, 'real_vote_maybe': 0,
        'proxy_in': 0,     'proxy_out': 0,     'proxy_maybe': 0,
        'waitlist_promotions': 0,
    }
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        active_false = 'FALSE' if db_type == 'postgresql' else '0'

        cursor.execute(
            f"SELECT COUNT(*) FROM rollcalls WHERE chat_id = {ph} AND is_active = {active_false}",
            (chat_id,),
        )
        row = cursor.fetchone()
        out['total_rollcalls'] = int((row[0] if not isinstance(row, dict) else next(iter(row.values()))) or 0)

        cursor.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT u.user_id)
            FROM users u
            JOIN rollcalls r ON u.rollcall_id = r.id
            WHERE r.chat_id = {ph} AND u.status = 'in' AND r.is_active = {active_false}
        """, (chat_id,))
        row = cursor.fetchone()
        if row is not None:
            if isinstance(row, dict):
                vals = list(row.values())
                out['real_attendance_slots'] = int(vals[0] or 0)
                out['real_participants']     = int(vals[1] or 0)
            else:
                out['real_attendance_slots'] = int(row[0] or 0)
                out['real_participants']     = int(row[1] or 0)

        cursor.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT pu.name)
            FROM proxy_users pu
            JOIN rollcalls r ON pu.rollcall_id = r.id
            WHERE r.chat_id = {ph} AND pu.status = 'in' AND r.is_active = {active_false}
        """, (chat_id,))
        row = cursor.fetchone()
        if row is not None:
            if isinstance(row, dict):
                vals = list(row.values())
                out['proxy_attendance_slots'] = int(vals[0] or 0)
                out['proxy_participants']     = int(vals[1] or 0)
            else:
                out['proxy_attendance_slots'] = int(row[0] or 0)
                out['proxy_participants']     = int(row[1] or 0)

        cursor.execute(f"""
            SELECT SUM(total_in), SUM(total_out), SUM(total_maybe), SUM(total_waiting_to_in)
            FROM user_stats WHERE chat_id = {ph}
        """, (chat_id,))
        row = cursor.fetchone()
        if row is not None:
            if isinstance(row, dict):
                vals = list(row.values())
                out['real_vote_in']        = int(vals[0] or 0)
                out['real_vote_out']       = int(vals[1] or 0)
                out['real_vote_maybe']     = int(vals[2] or 0)
                out['waitlist_promotions'] = int(vals[3] or 0)
            else:
                out['real_vote_in']        = int(row[0] or 0)
                out['real_vote_out']       = int(row[1] or 0)
                out['real_vote_maybe']     = int(row[2] or 0)
                out['waitlist_promotions'] = int(row[3] or 0)

        cursor.execute(f"""
            SELECT SUM(CASE WHEN pu.status = 'in'    THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pu.status = 'out'   THEN 1 ELSE 0 END),
                   SUM(CASE WHEN pu.status = 'maybe' THEN 1 ELSE 0 END)
            FROM proxy_users pu
            JOIN rollcalls r ON pu.rollcall_id = r.id
            WHERE r.chat_id = {ph} AND r.is_active = {active_false}
        """, (chat_id,))
        row = cursor.fetchone()
        if row is not None:
            if isinstance(row, dict):
                vals = list(row.values())
                out['proxy_in']    = int(vals[0] or 0)
                out['proxy_out']   = int(vals[1] or 0)
                out['proxy_maybe'] = int(vals[2] or 0)
            else:
                out['proxy_in']    = int(row[0] or 0)
                out['proxy_out']   = int(row[1] or 0)
                out['proxy_maybe'] = int(row[2] or 0)

        return out
    except Exception as e:
        logging.error(f"Error fetching group attendance totals: {e}")
        return out
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_bot_attendance_totals() -> Dict:
    """Bot-wide aggregate of real-attendance slots + active group counts.
    Companion to build_bot_stats_text — does NOT scope to a single chat."""
    conn = get_connection()
    cursor = None
    out = {'real_attendance_slots': 0, 'proxy_attendance_slots': 0,
           'ended_rollcalls': 0, 'real_participants': 0, 'proxy_participants': 0}
    try:
        cursor = conn.cursor()
        active_false = 'FALSE' if db_type == 'postgresql' else '0'

        cursor.execute(f"SELECT COUNT(*) FROM rollcalls WHERE is_active = {active_false}")
        row = cursor.fetchone()
        out['ended_rollcalls'] = int((row[0] if not isinstance(row, dict) else next(iter(row.values()))) or 0)

        cursor.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT u.user_id)
            FROM users u JOIN rollcalls r ON u.rollcall_id = r.id
            WHERE u.status = 'in' AND r.is_active = {active_false}
        """)
        row = cursor.fetchone()
        if row is not None:
            if isinstance(row, dict):
                vals = list(row.values())
                out['real_attendance_slots'] = int(vals[0] or 0)
                out['real_participants']     = int(vals[1] or 0)
            else:
                out['real_attendance_slots'] = int(row[0] or 0)
                out['real_participants']     = int(row[1] or 0)

        cursor.execute(f"""
            SELECT COUNT(*), COUNT(DISTINCT pu.name)
            FROM proxy_users pu JOIN rollcalls r ON pu.rollcall_id = r.id
            WHERE pu.status = 'in' AND r.is_active = {active_false}
        """)
        row = cursor.fetchone()
        if row is not None:
            if isinstance(row, dict):
                vals = list(row.values())
                out['proxy_attendance_slots'] = int(vals[0] or 0)
                out['proxy_participants']     = int(vals[1] or 0)
            else:
                out['proxy_attendance_slots'] = int(row[0] or 0)
                out['proxy_participants']     = int(row[1] or 0)

        return out
    except Exception as e:
        logging.error(f"Error fetching bot attendance totals: {e}")
        return out
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def find_proxy_in_chat(chat_id: int, name: str) -> bool:
    """Return True if a proxy named `name` exists in any rollcall of this chat.
    Used by resolve_user_for_stats to fall through to proxy lookup."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                SELECT 1 FROM proxy_users pu
                JOIN rollcalls r ON pu.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND pu.name = {ph}
                LIMIT 1
            """, (chat_id, name))
            return cursor.fetchone() is not None
    except Exception as e:
        logging.error(f"Error finding proxy in chat: {e}")
        return False


def find_user_by_username_for_stats(chat_id: int, username: str) -> Optional[Dict]:
    """Look up a real user by @username in this chat's ENDED rollcalls (for
    /stats <@user> resolution). Restricting to ended rollcalls keeps
    in-progress sessions from shadowing real history."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT DISTINCT u.user_id, u.first_name FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND u.username = {ph}
                  AND r.is_active = {active_false}
                ORDER BY u.user_id LIMIT 1
            """, (chat_id, username))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error finding user by username for stats: {e}")
        return None


def find_users_by_name_for_stats(chat_id: int, name: str) -> List[Dict]:
    """Look up real users by display name in this chat's ENDED rollcalls
    (for /stats <name> resolution). May return multiple rows if several
    users share the name — caller decides how to handle ambiguity."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT u.user_id, MAX(u.first_name) AS first_name,
                       MAX(u.updated_at) AS latest_seen
                FROM users u
                JOIN rollcalls r ON u.rollcall_id = r.id
                WHERE r.chat_id = {ph} AND u.first_name = {ph}
                  AND r.is_active = {active_false}
                GROUP BY u.user_id
                ORDER BY latest_seen DESC
            """, (chat_id, name))
            return [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error finding users by name for stats: {e}")
        return []


def get_user_stats_row(chat_id: int, user_id: int) -> Optional[Dict]:
    """Raw user_stats row (vote totals, streaks) for personal_stats."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                SELECT total_in, total_out, total_maybe, total_rollcalls,
                       total_waiting_to_in, best_streak, current_streak
                FROM user_stats WHERE chat_id = {ph} AND user_id = {ph}
            """, (chat_id, user_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error fetching user_stats row: {e}")
        return None


def get_rollcall_history(chat_id: int, limit: int = 10, offset: int = 0) -> List[Dict]:
    """Return ended rollcalls for a chat with participant counts, supporting pagination."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    SELECT r.id, r.title, r.ended_at, r.finalize_date, r.created_at,
                        (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'in') +
                        (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'in') AS in_count,
                        (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'out') +
                        (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'out') AS out_count,
                        (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'maybe') +
                        (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'maybe') AS maybe_count,
                        (SELECT COUNT(*) FROM ghost_events g WHERE g.rollcall_id = r.id) AS ghost_count
                    FROM rollcalls r
                    WHERE r.chat_id = {ph} AND r.is_active = FALSE
                    ORDER BY r.ended_at DESC
                    LIMIT {ph} OFFSET {ph}
                """, (chat_id, limit, offset))
            else:
                cursor.execute(f"""
                    SELECT r.id, r.title, r.ended_at, r.finalize_date, r.created_at,
                        (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'in') +
                        (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'in') AS in_count,
                        (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'out') +
                        (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'out') AS out_count,
                        (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'maybe') +
                        (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'maybe') AS maybe_count,
                        (SELECT COUNT(*) FROM ghost_events g WHERE g.rollcall_id = r.id) AS ghost_count
                    FROM rollcalls r
                    WHERE r.chat_id = {ph} AND r.is_active = 0
                    ORDER BY r.ended_at DESC
                    LIMIT {ph} OFFSET {ph}
                """, (chat_id, limit, offset))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting rollcall history: {e}")
        return []


def get_user_session_history(chat_id: int, user_id: int, limit: int = 15) -> List[Dict]:
    """Return recent ended rollcalls with the user's status for each (NULL = did not vote)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cancel_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT r.id, r.title, r.ended_at, r.finalize_date, r.created_at,
                       CASE WHEN COALESCE(r.is_cancelled, {cancel_false}) != {cancel_false}
                            THEN 'cancelled'
                            ELSE COALESCE(u.status, 'miss')
                       END AS status
                FROM rollcalls r
                LEFT JOIN users u ON u.rollcall_id = r.id AND u.user_id = {ph}
                WHERE r.chat_id = {ph} AND r.is_active = {active_false}
                ORDER BY r.ended_at DESC
                LIMIT {ph}
            """, (user_id, chat_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error("Error getting user session history: %s", e)
        return []


def get_user_voted_chats(tg_user_id: int) -> List[Dict]:
    """Return all chats where tg_user_id has voting history.

    Each entry: chat_id, group_name, timezone, group_web_token,
    sessions_attended, total_sessions, total_voted, current_streak,
    best_streak, ghost_count.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cancel_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT
                    us.chat_id,
                    c.group_name,
                    COALESCE(c.timezone, 'Asia/Kolkata') AS timezone,
                    c.group_web_token,
                    COALESCE(us.current_streak, 0)    AS current_streak,
                    COALESCE(us.best_streak, 0)        AS best_streak,
                    COALESCE(us.total_rollcalls, 0)    AS total_voted,
                    (SELECT COUNT(*) FROM users u2
                     JOIN rollcalls r2 ON u2.rollcall_id = r2.id
                     WHERE r2.chat_id = us.chat_id AND u2.user_id = us.user_id
                       AND u2.status = 'in' AND r2.is_active = {active_false}
                       AND COALESCE(r2.is_cancelled, {cancel_false}) = {cancel_false}
                    ) AS sessions_attended,
                    (SELECT COUNT(*) FROM rollcalls r3
                     WHERE r3.chat_id = us.chat_id AND r3.is_active = {active_false}
                     AND COALESCE(r3.is_cancelled, {cancel_false}) = {cancel_false}
                    ) AS total_sessions,
                    COALESCE((SELECT gr.ghost_count FROM ghost_records gr
                     WHERE gr.chat_id = us.chat_id AND gr.user_id = us.user_id
                       AND gr.proxy_name IS NULL LIMIT 1), 0) AS ghost_count
                FROM user_stats us
                JOIN chats c ON c.chat_id = us.chat_id
                WHERE us.user_id = {ph}
                ORDER BY sessions_attended DESC
            """, (tg_user_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error("Error in get_user_voted_chats: %s", e)
        return []


def get_user_rank_in_chat(chat_id: int, user_id: int) -> Optional[int]:
    """Return 1-based leaderboard rank for user in this chat (by final-IN count)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_false = 'FALSE' if db_type == 'postgresql' else '0'
            cancel_false = 'FALSE' if db_type == 'postgresql' else '0'
            cursor.execute(f"""
                SELECT COUNT(*) + 1 AS rank FROM (
                    SELECT u.user_id, COUNT(*) AS attended
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = {ph} AND u.status = 'in'
                      AND r.is_active = {active_false}
                      AND COALESCE(r.is_cancelled, {cancel_false}) = {cancel_false}
                      AND u.user_id IS NOT NULL
                    GROUP BY u.user_id
                ) sub
                WHERE sub.attended > (
                    SELECT COUNT(*) FROM users u2
                    JOIN rollcalls r2 ON u2.rollcall_id = r2.id
                    WHERE r2.chat_id = {ph} AND u2.user_id = {ph}
                      AND u2.status = 'in' AND r2.is_active = {active_false}
                      AND COALESCE(r2.is_cancelled, {cancel_false}) = {cancel_false}
                )
            """, (chat_id, chat_id, user_id))
            row = cursor.fetchone()
            if row is None:
                return None
            val = row[0] if not isinstance(row, dict) else row.get('rank', 1)
            return int(val) if val is not None else None
    except Exception as e:
        logging.error("Error in get_user_rank_in_chat: %s", e)
        return None


def get_user_upcoming_scheduled_rollcalls(tg_user_id: int, limit: int = 10) -> List[Dict]:
    """Return upcoming (unfired) scheduled rollcalls from all groups the user has voted in."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    SELECT sr.id, sr.chat_id, sr.title, sr.scheduled_at,
                           c.group_name, c.group_web_token
                    FROM scheduled_rollcalls sr
                    JOIN chats c ON c.chat_id = sr.chat_id
                    WHERE sr.is_fired = FALSE
                      AND sr.chat_id IN (
                          SELECT chat_id FROM user_stats WHERE user_id = {ph}
                      )
                    ORDER BY sr.scheduled_at ASC
                    LIMIT {ph}
                """, (tg_user_id, limit))
            else:
                cursor.execute(f"""
                    SELECT sr.id, sr.chat_id, sr.title, sr.scheduled_at,
                           c.group_name, c.group_web_token
                    FROM scheduled_rollcalls sr
                    JOIN chats c ON c.chat_id = sr.chat_id
                    WHERE sr.is_fired = 0
                      AND sr.chat_id IN (
                          SELECT chat_id FROM user_stats WHERE user_id = {ph}
                      )
                    ORDER BY sr.scheduled_at ASC
                    LIMIT {ph}
                """, (tg_user_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error("Error in get_user_upcoming_scheduled_rollcalls: %s", e)
        return []


def log_admin_action(
    chat_id: int,
    admin_id: int,
    admin_name: str,
    action_type: str,
    target_name: str = None,
    rollcall_id: int = None,
    details: str = None,
) -> None:
    """Record an admin action in the audit log."""
    conn = get_connection()
    # Guard against UnboundLocalError when conn.cursor() raises — if cursor
    # was never assigned, finally would otherwise leak the NameError out and
    # surface as "Something went wrong" to callers like /buzz.
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(
            f"INSERT INTO admin_actions (chat_id, admin_id, admin_name, action_type, target_name, rollcall_id, details) "
            f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
            (chat_id, admin_id, admin_name, action_type, target_name, rollcall_id, details),
        )
        conn.commit()
    except Exception as e:
        logging.error(f"Error logging admin action: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_admin_audit_log(chat_id: int, limit: int = 15, offset: int = 0) -> List[Dict]:
    """Return admin/command actions for a chat with pagination support."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT id, admin_name, action_type, target_name, rollcall_id, details, created_at "
                f"FROM admin_actions WHERE chat_id = {ph} ORDER BY created_at DESC LIMIT {ph} OFFSET {ph}",
                (chat_id, limit, offset),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error fetching admin audit log: {e}")
        return []


def count_admin_audit_log(chat_id: int) -> int:
    """Return total number of recorded actions for a chat."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT COUNT(*) FROM admin_actions WHERE chat_id = {ph}",
                (chat_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logging.error(f"Error counting admin audit log: {e}")
        return 0


def upsert_chat_member(chat_id: int, user_id: int, first_name: str, username: str = None) -> None:
    """Insert or update a chat member record.

    Called every time a real Telegram user votes so that display names stay
    fresh and the member is (re-)marked active.
    """
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            now = _utcnow_naive()
            if db_type == 'postgresql':
                cursor.execute(f"""
                    INSERT INTO chat_members (chat_id, user_id, first_name, username, is_active, last_seen)
                    VALUES ({ph}, {ph}, {ph}, {ph}, TRUE, {ph})
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        first_name = EXCLUDED.first_name,
                        username   = EXCLUDED.username,
                        is_active  = TRUE,
                        last_seen  = EXCLUDED.last_seen
                """, (chat_id, user_id, first_name, username, now))
            else:
                cursor.execute(f"""
                    INSERT INTO chat_members (chat_id, user_id, first_name, username, is_active, last_seen)
                    VALUES ({ph}, {ph}, {ph}, {ph}, 1, {ph})
                    ON CONFLICT (chat_id, user_id) DO UPDATE SET
                        first_name = excluded.first_name,
                        username   = excluded.username,
                        is_active  = 1,
                        last_seen  = excluded.last_seen
                """, (chat_id, user_id, first_name, username, now))
    except Exception as e:
        logging.error(f"Error upserting chat member: {e}")


def mark_member_inactive(chat_id: int, user_id: int) -> None:
    """Mark a member as no longer in the group (left or kicked)."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_val = False if db_type == 'postgresql' else 0
            cursor.execute(f"""
                UPDATE chat_members SET is_active = {ph}
                WHERE chat_id = {ph} AND user_id = {ph}
            """, (active_val, chat_id, user_id))
    except Exception as e:
        logging.error(f"Error marking member inactive: {e}")


def get_active_members(chat_id: int) -> List[Dict]:
    """Return all members currently marked active for a chat.

    These are real Telegram users (not proxy users) who have voted at least
    once and have not been detected as having left the group.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_val = True if db_type == 'postgresql' else 1
            cursor.execute(f"""
                SELECT user_id, first_name, username
                FROM chat_members
                WHERE chat_id = {ph} AND is_active = {ph}
                ORDER BY last_seen DESC
            """, (chat_id, active_val))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting active members: {e}")
        return []


def get_member_display_info(chat_id: int, user_id: int) -> Optional[Dict]:
    """Return {'first_name': ..., 'username': ...} for a verified user in a chat, or None.

    Used by the web voting layer to enforce the canonical Telegram display name
    and username so that name conflicts are resolved the same way as in-bot voting.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT first_name, username FROM chat_members WHERE chat_id = {ph} AND user_id = {ph}",
                (chat_id, user_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return {'first_name': row['first_name'], 'username': row.get('username')}
            return {'first_name': row[0], 'username': row[1]}
    except Exception as e:
        logging.error(f"Error getting member display info: {e}")
        return None


# ────────────────────────────────────────────────────────────────────────
# api_tokens CRUD (REST API auth — PR 3)
# ────────────────────────────────────────────────────────────────────────

import hashlib  # noqa: E402
import secrets  # noqa: E402


def _hash_token(token: str) -> str:
    """SHA-256 hex digest of an API token. The plaintext is never stored;
    callers verify by hashing the inbound token and looking up by hash.
    Token entropy is high enough (>=128 bits) that no salt is needed.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_token() -> str:
    """Generate a new opaque API token. Format: `rc_<32 hex>` (132 bits
    of entropy from secrets). Plaintext is shown to the issuer once."""
    return f"rc_{secrets.token_hex(16)}"


def insert_api_token(
    token_hash: str,
    chat_id: int,
    scopes: str,
    label: str | None = None,
    issued_by_user_id: int | None = None,
    expires_at=None,
) -> None:
    """Persist an issued token's hash, scopes, and metadata. The plaintext
    must NOT be passed here — it's the caller's responsibility to hash."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                INSERT INTO api_tokens (token_hash, chat_id, issued_by_user_id,
                                        scopes, label, expires_at)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            """, (token_hash, chat_id, issued_by_user_id, scopes, label, expires_at))
    except Exception as e:
        logging.exception("insert_api_token: failed to persist token for chat %s: %s", chat_id, e)
        raise


def lookup_api_token(token_hash: str) -> Optional[Dict]:
    """Look up a token by its hash. Returns a dict with chat_id, scopes
    (parsed to a list), label, expires_at, revoked_at — or None if no
    matching token, the token is revoked, or it has expired.

    Also bumps `last_used_at` as a side effect when a hit is returned, so
    operators can audit token activity via the same row."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                SELECT chat_id, issued_by_user_id, scopes, label,
                       created_at, expires_at, last_used_at, revoked_at
                FROM api_tokens
                WHERE token_hash = {ph}
            """, (token_hash,))
            row = cursor.fetchone()
            if row is None:
                return None

            d = dict(row)
            # Revoked or expired tokens act as non-existent for auth purposes.
            if d.get("revoked_at") is not None:
                return None
            expires_at = d.get("expires_at")
            if expires_at is not None:
                # PG returns datetime; SQLite returns string. Coerce to compare.
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                try:
                    if isinstance(expires_at, str):
                        # SQLite stores datetimes as strings. Callers that pass a
                        # tz-aware expires_at (e.g. api/routes/auth.py's Mini App
                        # token: datetime.now(timezone.utc) + timedelta(...)) get
                        # str()'d with a "+00:00" offset suffix — none of the
                        # naive strptime formats below match that, so try
                        # fromisoformat first (handles the offset directly; needs
                        # "T" not " " before Python 3.11, but this repo targets
                        # 3.12 where either separator works).
                        parsed = None
                        try:
                            parsed = datetime.fromisoformat(expires_at)
                            if parsed.tzinfo is not None:
                                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                        except ValueError:
                            # SQLite stores naive datetimes as strings (datetime.__str__
                            # gives "YYYY-MM-DD HH:MM:SS.ffffff"). Try all plausible formats.
                            for fmt in (
                                "%Y-%m-%d %H:%M:%S.%f",
                                "%Y-%m-%d %H:%M:%S",
                                "%Y-%m-%dT%H:%M:%SZ",
                                "%Y-%m-%dT%H:%M:%S",
                                "%Y-%m-%dT%H:%M:%S.%fZ",
                                "%Y-%m-%dT%H:%M:%S.%f",
                            ):
                                try:
                                    parsed = datetime.strptime(expires_at, fmt).replace(tzinfo=None)
                                    break
                                except ValueError:
                                    continue
                        # Unparseable expiry → treat as expired (safe default).
                        if parsed is None:
                            return None
                    else:
                        parsed = expires_at
                    if parsed is not None and parsed < now:
                        return None
                except Exception:
                    logging.exception("api_token expiry parse failed; treating as expired")
                    return None

            # Bump last_used_at. Best-effort — don't fail the lookup if it fails.
            try:
                cursor.execute(f"""
                    UPDATE api_tokens SET last_used_at = CURRENT_TIMESTAMP
                    WHERE token_hash = {ph}
                """, (token_hash,))
            except Exception:
                logging.exception("api_token last_used_at update failed")

            d["scopes"] = [s.strip() for s in (d.get("scopes") or "").split(",") if s.strip()]
            return d
    except Exception:
        logging.exception("lookup_api_token failed")
        return None


def list_api_tokens(chat_id: int) -> List[Dict]:
    """List all tokens issued for a chat (active + revoked + expired).
    Useful for the admin token-management surface. token_hash is included
    so revocation by hash works."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                SELECT token_hash, issued_by_user_id, scopes, label,
                       created_at, expires_at, last_used_at, revoked_at
                FROM api_tokens
                WHERE chat_id = {ph}
                ORDER BY created_at DESC
            """, (chat_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        logging.exception("list_api_tokens failed")
        return []


def revoke_api_token(token_hash: str) -> bool:
    """Mark a token as revoked (sets revoked_at). Returns True if a row
    was modified, False if no such token (or already revoked)."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"""
                UPDATE api_tokens
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE token_hash = {ph} AND revoked_at IS NULL
            """, (token_hash,))
            affected = cursor.rowcount
            return bool(affected)
    except Exception:
        logging.exception("revoke_api_token failed")
        return False


# ── Web presence / view-count helpers ────────────────────────────────────────

def increment_group_view_count(group_token: str) -> int:
    """Upsert a view-count row for group_token and return the new total."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    INSERT INTO web_view_stats (group_token, view_count, last_viewed_at)
                    VALUES ({ph}, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT (group_token)
                    DO UPDATE SET view_count = web_view_stats.view_count + 1,
                                  last_viewed_at = CURRENT_TIMESTAMP
                    RETURNING view_count
                """, (group_token,))
                row = cursor.fetchone()
                count = int(row[0] if row else 1)
            else:
                cursor.execute(f"""
                    INSERT INTO web_view_stats (group_token, view_count, last_viewed_at)
                    VALUES ({ph}, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT (group_token)
                    DO UPDATE SET view_count = view_count + 1,
                                  last_viewed_at = CURRENT_TIMESTAMP
                """, (group_token,))
                cursor.execute(f"SELECT view_count FROM web_view_stats WHERE group_token = {ph}", (group_token,))
                row = cursor.fetchone()
                count = int(row[0] if row else 1)
            return count
    except Exception:
        logging.exception("increment_group_view_count failed")
        return 0


def get_group_view_count(group_token: str) -> int:
    """Return the total view count for group_token (0 if none recorded yet)."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT view_count FROM web_view_stats WHERE group_token = {ph}",
                (group_token,),
            )
            row = cursor.fetchone()
            return int(row[0] if row else 0)
    except Exception:
        logging.exception("get_group_view_count failed")
        return 0


def get_system_config(key: str) -> Optional[str]:
    """Return a value from system_config, or None if not set."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"SELECT value FROM system_config WHERE key = {ph}", (key,))
            row = cursor.fetchone()
            if row is None:
                return None
            return row[0] if not isinstance(row, dict) else row['value']
    except Exception:
        logging.exception("get_system_config failed for key=%s", key)
        return None


def set_system_config(key: str, value: str) -> None:
    """Upsert a key/value pair in system_config."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    INSERT INTO system_config (key, value, updated_at)
                    VALUES ({ph}, {ph}, CURRENT_TIMESTAMP)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
                """, (key, value))
            else:
                cursor.execute(f"""
                    INSERT OR REPLACE INTO system_config (key, value, updated_at)
                    VALUES ({ph}, {ph}, CURRENT_TIMESTAMP)
                """, (key, value))
    except Exception:
        logging.exception("set_system_config failed for key=%s", key)


def save_push_subscription(group_token: str, endpoint: str, p256dh: str, auth: str, tg_user_id: Optional[int] = None) -> None:
    """Upsert a push subscription for a group. Re-activates if previously unsubscribed."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    INSERT INTO push_subscriptions (group_token, endpoint, p256dh, auth, active, tg_user_id)
                    VALUES ({ph}, {ph}, {ph}, {ph}, TRUE, {ph})
                    ON CONFLICT (endpoint) DO UPDATE SET
                        group_token = EXCLUDED.group_token,
                        p256dh      = EXCLUDED.p256dh,
                        auth        = EXCLUDED.auth,
                        active      = TRUE,
                        tg_user_id  = COALESCE(EXCLUDED.tg_user_id, push_subscriptions.tg_user_id),
                        created_at  = CURRENT_TIMESTAMP
                """, (group_token, endpoint, p256dh, auth, tg_user_id))
            else:
                cursor.execute(f"""
                    INSERT OR REPLACE INTO push_subscriptions (group_token, endpoint, p256dh, auth, active, tg_user_id)
                    VALUES ({ph}, {ph}, {ph}, {ph}, 1, {ph})
                """, (group_token, endpoint, p256dh, auth, tg_user_id))
    except Exception:
        logging.exception("save_push_subscription failed")


def get_push_subscriptions(group_token: str) -> List[Dict]:
    """Return all active push subscriptions for a group."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            active_val = 'TRUE' if db_type == 'postgresql' else '1'
            cursor.execute(
                f"SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE group_token = {ph} AND active = {active_val}",
                (group_token,)
            )
            rows = cursor.fetchall()
            result = []
            for row in rows:
                if isinstance(row, dict):
                    result.append({'endpoint': row['endpoint'], 'p256dh': row['p256dh'], 'auth': row['auth']})
                else:
                    result.append({'endpoint': row[0], 'p256dh': row[1], 'auth': row[2]})
            return result
    except Exception:
        logging.exception("get_push_subscriptions failed")
        return []


def delete_push_subscription(endpoint: str) -> None:
    """Mark a push subscription inactive (expired or unsubscribed)."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"UPDATE push_subscriptions SET active = {'FALSE' if db_type == 'postgresql' else '0'} WHERE endpoint = {ph}",
                (endpoint,)
            )
    except Exception:
        logging.exception("delete_push_subscription failed")


def create_web_verify_token(code: str, expires_at: "datetime") -> None:
    """Store a new one-time verification code."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"INSERT INTO web_verify_tokens (code, expires_at) VALUES ({ph}, {ph})",
                (code, expires_at.isoformat()),
            )
    except Exception:
        logging.exception("create_web_verify_token failed")


def mark_web_verify_token(code: str, tg_user_id: int, tg_name: str, tg_username: Optional[str] = None) -> bool:
    """Bot calls this once the user opens the deep link. Returns True if code was found and unmarked."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            now = _utcnow_naive().isoformat()
            cursor.execute(
                f"UPDATE web_verify_tokens SET tg_user_id={ph}, tg_name={ph}, tg_username={ph} "
                f"WHERE code={ph} AND tg_user_id IS NULL AND used_at IS NULL AND expires_at > {ph}",
                (tg_user_id, tg_name, tg_username, code, now),
            )
            updated = cursor.rowcount > 0
            return updated
    except Exception:
        logging.exception("mark_web_verify_token failed")
        return False


def get_web_verify_token(code: str) -> Optional[Dict]:
    """Return the token row if it exists and is not expired, else None."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            now = _utcnow_naive().isoformat()
            cursor.execute(
                f"SELECT code, tg_user_id, tg_name, tg_username, used_at FROM web_verify_tokens "
                f"WHERE code={ph} AND expires_at > {ph}",
                (code, now),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if isinstance(row, dict):
                return dict(row)
            return {'code': row[0], 'tg_user_id': row[1], 'tg_name': row[2], 'tg_username': row[3], 'used_at': row[4]}
    except Exception:
        logging.exception("get_web_verify_token failed")
        return None


def consume_web_verify_token(code: str) -> Optional[Dict]:
    """Mark the token used and return {tg_user_id, tg_name}, or None if not ready."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        now = _utcnow_naive().isoformat()
        cursor.execute(
            f"UPDATE web_verify_tokens SET used_at={ph} "
            f"WHERE code={ph} AND tg_user_id IS NOT NULL AND used_at IS NULL AND expires_at > {ph}",
            (now, code, now),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return None
        conn.commit()
        cursor.execute(
            f"SELECT tg_user_id, tg_name, tg_username FROM web_verify_tokens WHERE code={ph}",
            (code,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return {'tg_user_id': row['tg_user_id'], 'tg_name': row['tg_name'], 'tg_username': row.get('tg_username')}
        return {'tg_user_id': row[0], 'tg_name': row[1], 'tg_username': row[2]}
    except Exception:
        conn.rollback()
        logging.exception("consume_web_verify_token failed")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def upsert_member_login_token(user_id: int, token_hash: str,
                              first_name: Optional[str] = None,
                              username: Optional[str] = None) -> bool:
    """Store (or replace) a user's persistent login code hash — /mytoken
    reissues by overwriting, so exactly one code is active per user."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(
                    f"INSERT INTO member_login_tokens (user_id, token_hash, first_name, username) "
                    f"VALUES ({ph},{ph},{ph},{ph}) "
                    f"ON CONFLICT (user_id) DO UPDATE SET token_hash = EXCLUDED.token_hash, "
                    f"first_name = EXCLUDED.first_name, username = EXCLUDED.username, "
                    f"created_at = NOW(), last_used_at = NULL",
                    (user_id, token_hash, first_name, username),
                )
            else:
                cursor.execute(
                    f"INSERT OR REPLACE INTO member_login_tokens "
                    f"(user_id, token_hash, first_name, username) VALUES ({ph},{ph},{ph},{ph})",
                    (user_id, token_hash, first_name, username),
                )
        return True
    except Exception:
        logging.exception("upsert_member_login_token failed")
        return False


def get_member_login_token_by_hash(token_hash: str) -> Optional[Dict]:
    """Look up a login-code hash. Returns {user_id, first_name, username} or None."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT user_id, first_name, username FROM member_login_tokens "
                f"WHERE token_hash = {ph}",
                (token_hash,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            if isinstance(row, dict):
                return {'user_id': row['user_id'], 'first_name': row['first_name'],
                        'username': row['username']}
            return {'user_id': row[0], 'first_name': row[1], 'username': row[2]}
    except Exception:
        logging.exception("get_member_login_token_by_hash failed")
        return None


def touch_member_login_token(user_id: int) -> None:
    """Stamp last_used_at on a successful code redemption."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"UPDATE member_login_tokens SET last_used_at = {ph} WHERE user_id = {ph}",
                (_utcnow_naive().isoformat(), user_id),
            )
    except Exception:
        logging.exception("touch_member_login_token failed")


def delete_member_login_token(user_id: int) -> bool:
    """Revoke a user's login code (/mytoken off). True if one existed."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"DELETE FROM member_login_tokens WHERE user_id = {ph}",
                (user_id,),
            )
            return cursor.rowcount > 0
    except Exception:
        logging.exception("delete_member_login_token failed")
        return False


def set_web_admin(chat_id: int, tg_user_id: int, tg_name: str) -> None:
    """Upsert a web admin for a chat (called when admin runs /weblink)."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(
                    f"INSERT INTO web_admins (chat_id, tg_user_id, tg_name) VALUES ({ph},{ph},{ph}) "
                    f"ON CONFLICT (chat_id, tg_user_id) DO UPDATE SET tg_name = EXCLUDED.tg_name, added_at = NOW()",
                    (chat_id, tg_user_id, tg_name),
                )
            else:
                cursor.execute(
                    f"INSERT OR REPLACE INTO web_admins (chat_id, tg_user_id, tg_name) VALUES ({ph},{ph},{ph})",
                    (chat_id, tg_user_id, tg_name),
                )
    except Exception:
        logging.exception("set_web_admin failed")


def get_web_admin_chats(tg_user_id: int) -> List[int]:
    """Return every chat_id where this Telegram user is a cached web admin.

    Used to offer a group picker on Telegram-based admin console sign-in
    without needing a live Telegram call per candidate chat — the picker
    list itself is allowed to trust the cache; the actual admin grant is
    still live-verified (see api/web_admin.py) once a chat is selected.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(f"SELECT chat_id FROM web_admins WHERE tg_user_id={ph}", (tg_user_id,))
            return [r[0] if not isinstance(r, dict) else r['chat_id'] for r in cursor.fetchall()]
    except Exception:
        logging.exception("get_web_admin_chats failed")
        return []


def is_web_admin(chat_id: int, tg_user_id: int) -> bool:
    """Return True if the user is a cached web admin for this chat."""
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"SELECT 1 FROM web_admins WHERE chat_id={ph} AND tg_user_id={ph}",
                (chat_id, tg_user_id),
            )
            return cursor.fetchone() is not None
    except Exception:
        logging.exception("is_web_admin failed")
        return False


def revoke_web_admin(chat_id: int, tg_user_id: int) -> None:
    """Clear a cached web admin — called when a live Telegram admin-status
    recheck finds the user is no longer an admin/creator of the chat. Without
    this, web-admin status (granted correctly at the time) never expired even
    after the person lost their real Telegram admin role."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"DELETE FROM web_admins WHERE chat_id={ph} AND tg_user_id={ph}",
                (chat_id, tg_user_id),
            )
    except Exception:
        logging.exception("revoke_web_admin failed")


def get_response_time_leaderboard(chat_id: int, limit: int = 10) -> List[Dict]:
    """
    Return per-user average and best response time (seconds from rollcall start
    to first vote) across ended rollcalls in this chat.

    Uses users.created_at (insert time = first vote) minus rollcalls.created_at.
    Only ended rollcalls with positive response times are included.
    Ordered fastest-first.
    """
    try:
        with _cursor() as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            if db_type == 'postgresql':
                cursor.execute(f"""
                    SELECT
                        u.user_id,
                        u.first_name AS display_name,
                        u.username,
                        AVG(EXTRACT(EPOCH FROM (u.created_at - r.created_at)))::bigint AS avg_seconds,
                        MIN(EXTRACT(EPOCH FROM (u.created_at - r.created_at)))::bigint AS best_seconds,
                        COUNT(*)::int AS rollcall_count
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = {ph}
                      AND r.ended_at IS NOT NULL
                      AND u.created_at > r.created_at
                    GROUP BY u.user_id, u.first_name, u.username
                    HAVING COUNT(*) >= 1
                    ORDER BY avg_seconds ASC
                    LIMIT {ph}
                """, (chat_id, limit))
            else:
                cursor.execute(f"""
                    SELECT
                        u.user_id,
                        u.first_name AS display_name,
                        u.username,
                        CAST(AVG((julianday(u.created_at) - julianday(r.created_at)) * 86400) AS INTEGER) AS avg_seconds,
                        CAST(MIN((julianday(u.created_at) - julianday(r.created_at)) * 86400) AS INTEGER) AS best_seconds,
                        COUNT(*) AS rollcall_count
                    FROM users u
                    JOIN rollcalls r ON u.rollcall_id = r.id
                    WHERE r.chat_id = {ph}
                      AND r.ended_at IS NOT NULL
                      AND u.created_at > r.created_at
                    GROUP BY u.user_id, u.first_name, u.username
                    HAVING COUNT(*) >= 1
                    ORDER BY avg_seconds ASC
                    LIMIT {ph}
                """, (chat_id, limit))
            rows = cursor.fetchall()
            result = []
            for row in rows:
                if isinstance(row, dict):
                    r = row
                else:
                    r = {
                        'user_id': row[0], 'display_name': row[1], 'username': row[2],
                        'avg_seconds': row[3], 'best_seconds': row[4], 'rollcall_count': row[5],
                    }
                result.append({
                    'user_id': int(r['user_id']),
                    'display_name': r['display_name'] or '',
                    'username': r['username'] or '',
                    'avg_response_seconds': int(r['avg_seconds'] or 0),
                    'best_response_seconds': int(r['best_seconds'] or 0),
                    'rollcall_count': int(r['rollcall_count'] or 0),
                })
            return result
    except Exception:
        logging.exception("get_response_time_leaderboard failed")
        return []


# ── Scheduled rollcalls ────────────────────────────────────────────────────────

def create_scheduled_rollcall(
    chat_id: int,
    title: str,
    scheduled_at: str,  # ISO datetime string (UTC)
    created_by_uid: int,
    created_by_name: str,
) -> int:
    """Create a one-shot scheduled rollcall. Returns the new row id."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        now = _utcnow_naive().strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor.execute(
            f"INSERT INTO scheduled_rollcalls (chat_id, title, scheduled_at, created_by_uid, created_by_name, created_at)"
            f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph})",
            (chat_id, title, scheduled_at, created_by_uid, created_by_name, now),
        )
        conn.commit()
        if db_type == "postgresql":
            cursor.execute("SELECT lastval()")
        else:
            cursor.execute("SELECT last_insert_rowid()")
        return cursor.fetchone()[0]
    except Exception:
        logging.exception("create_scheduled_rollcall failed")
        conn.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def get_pending_scheduled_rollcalls() -> List[Dict]:
    """Return all unfired scheduled rollcalls whose fire time has passed (UTC)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            now = _utcnow_naive().strftime("%Y-%m-%dT%H:%M:%SZ")
            if db_type == "postgresql":
                cursor.execute(
                    "SELECT * FROM scheduled_rollcalls WHERE is_fired = FALSE AND scheduled_at <= %s ORDER BY scheduled_at",
                    (now,),
                )
            else:
                cursor.execute(
                    "SELECT * FROM scheduled_rollcalls WHERE is_fired = 0 AND scheduled_at <= ? ORDER BY scheduled_at",
                    (now,),
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception:
        logging.exception("get_pending_scheduled_rollcalls failed")
        return []


def get_upcoming_scheduled_rollcalls(chat_id: int) -> List[Dict]:
    """Return unfired future scheduled rollcalls for a chat, sorted by fire time."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            if db_type == "postgresql":
                cursor.execute(
                    "SELECT * FROM scheduled_rollcalls WHERE chat_id = %s AND is_fired = FALSE ORDER BY scheduled_at",
                    (chat_id,),
                )
            else:
                cursor.execute(
                    "SELECT * FROM scheduled_rollcalls WHERE chat_id = ? AND is_fired = 0 ORDER BY scheduled_at",
                    (chat_id,),
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception:
        logging.exception("get_upcoming_scheduled_rollcalls failed")
        return []


def mark_scheduled_rollcall_fired(row_id: int) -> None:
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            now = _utcnow_naive().strftime("%Y-%m-%dT%H:%M:%SZ")
            if db_type == "postgresql":
                cursor.execute(
                    "UPDATE scheduled_rollcalls SET is_fired = TRUE, fired_at = %s WHERE id = %s",
                    (now, row_id),
                )
            else:
                cursor.execute(
                    "UPDATE scheduled_rollcalls SET is_fired = 1, fired_at = ? WHERE id = ?",
                    (now, row_id),
                )
    except Exception:
        logging.exception("mark_scheduled_rollcall_fired failed")


def delete_scheduled_rollcall(row_id: int, chat_id: int) -> bool:
    """Delete an unfired scheduled rollcall. Returns True if a row was deleted."""
    try:
        with _cursor(commit=True) as cursor:
            if db_type == "postgresql":
                cursor.execute(
                    "DELETE FROM scheduled_rollcalls WHERE id = %s AND chat_id = %s AND is_fired = FALSE",
                    (row_id, chat_id),
                )
            else:
                cursor.execute(
                    "DELETE FROM scheduled_rollcalls WHERE id = ? AND chat_id = ? AND is_fired = 0",
                    (row_id, chat_id),
                )
            deleted = cursor.rowcount > 0
            return deleted
    except Exception:
        logging.exception("delete_scheduled_rollcall failed")
        return False


# ── Dues & Treasury ──────────────────────────────────────────────────────────
# dues_entries and fund_transactions are APPEND-ONLY: the only writers are the
# INSERT helpers below. Corrections must be compensating entries so the money
# history stays fully reconstructable (see CLAUDE.md).

def _dues_now() -> str:
    return _utcnow_naive().strftime("%Y-%m-%dT%H:%M:%SZ")


def create_game_closure(
    chat_id: int,
    rollcall_id: int,
    title: str,
    ground_cost: int,
    in_count: int,
    subsidy: int,
    per_head: int,
    rounding_step: int,
    remainder: int,
    closed_by_uid: int,
    closed_by_name: str,
    collector_uid: int = None,
    collector_name: str = None,
    collector_paid_ground: int = 0,
    collector_upi: str = None,
) -> int:
    """Create the financial-close record for a game. Raises on duplicate
    rollcall_id (UNIQUE constraint) — that is the double-close guard."""
    try:
        with _cursor(commit=True) as cur:
            ph = "%s" if db_type == "postgresql" else "?"
            cur.execute(
                f"INSERT INTO game_closures (chat_id, rollcall_id, title, ground_cost, in_count,"
                f" subsidy, per_head, rounding_step, remainder, collector_uid, collector_name,"
                f" collector_paid_ground, collector_upi, closed_by_uid, closed_by_name, created_at)"
                f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (chat_id, rollcall_id, title, ground_cost, in_count, subsidy, per_head,
                 rounding_step, remainder, collector_uid, collector_name,
                 collector_paid_ground, collector_upi, closed_by_uid, closed_by_name, _dues_now()),
            )
            if db_type == "postgresql":
                cur.execute("SELECT lastval()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            return cur.fetchone()[0]
    except Exception:
        logging.exception("create_game_closure failed")
        raise


def write_game_closure_batch(
    closure: Dict,
    dues_entries: List[Dict],
    fund_transactions: List[Dict],
) -> int:
    """Atomically write a game's financial close: the closure row, every
    member share/reimbursement dues_entries row, and every fund_transactions
    row, in ONE transaction/commit. A failure at any point rolls back
    everything — a game is never left "closed" (game_closures row present,
    blocking re-close via the UNIQUE(rollcall_id) constraint) with some or
    all of its dues rows missing.

    closure: dict with the same fields as create_game_closure's params
      (chat_id, rollcall_id, title, ground_cost, in_count, subsidy, per_head,
      rounding_step, remainder, closed_by_uid, closed_by_name, and optional
      collector_uid/collector_name/collector_paid_ground/collector_upi).
    dues_entries: list of dicts, each with add_dues_entry's params (chat_id,
      rollcall_id, user_id, member_name, entry_type, amount, memo,
      created_by_uid, created_by_name).
    fund_transactions: list of dicts, each with add_fund_transaction's params
      (chat_id, rollcall_id, txn_type, amount, description, created_by_uid,
      created_by_name).

    All rows share one created_at timestamp (computed once) — they represent
    a single atomic financial event. Returns the new game_closures.id.
    """
    try:
        with _cursor(commit=True) as cur:
            ph = "%s" if db_type == "postgresql" else "?"
            now = _dues_now()

            cur.execute(
                f"INSERT INTO game_closures (chat_id, rollcall_id, title, ground_cost, in_count,"
                f" subsidy, per_head, rounding_step, remainder, collector_uid, collector_name,"
                f" collector_paid_ground, collector_upi, closed_by_uid, closed_by_name, created_at)"
                f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (closure["chat_id"], closure["rollcall_id"], closure["title"], closure["ground_cost"],
                 closure["in_count"], closure["subsidy"], closure["per_head"], closure["rounding_step"],
                 closure["remainder"], closure.get("collector_uid"), closure.get("collector_name"),
                 closure.get("collector_paid_ground", 0), closure.get("collector_upi"),
                 closure["closed_by_uid"], closure["closed_by_name"], now),
            )
            if db_type == "postgresql":
                cur.execute("SELECT lastval()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            closure_id = cur.fetchone()[0]

            for e in dues_entries:
                cur.execute(
                    f"INSERT INTO dues_entries (chat_id, rollcall_id, user_id, member_name,"
                    f" entry_type, amount, memo, created_by_uid, created_by_name, created_at)"
                    f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                    (e["chat_id"], e["rollcall_id"], e["user_id"], e["member_name"],
                     e["entry_type"], e["amount"], e.get("memo"), e["created_by_uid"],
                     e["created_by_name"], now),
                )

            for t in fund_transactions:
                cur.execute(
                    f"INSERT INTO fund_transactions (chat_id, rollcall_id, txn_type, amount,"
                    f" description, created_by_uid, created_by_name, created_at)"
                    f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                    (t["chat_id"], t["rollcall_id"], t["txn_type"], t["amount"],
                     t["description"], t["created_by_uid"], t["created_by_name"], now),
                )

            return closure_id
    except Exception:
        logging.exception("write_game_closure_batch failed")
        raise


def get_game_closure(rollcall_id: int) -> Optional[Dict]:
    """Return the closure row for a rollcall, or None if not financially closed."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM game_closures WHERE rollcall_id = {ph}",
                (rollcall_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_game_closure failed")
        return None


def get_nth_game_closure(chat_id: int, n: int = 0) -> Optional[Dict]:
    """Return the Nth most recent closure for a chat (0 = latest, 1 = second most recent).

    Used by /cancel_game_dues ::N to target a specific past game by position.
    """
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM game_closures WHERE chat_id = {ph}"
                f" ORDER BY id DESC LIMIT 1 OFFSET {ph}",
                (chat_id, n),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_nth_game_closure failed")
        return None


def get_latest_game_closure(chat_id: int) -> Optional[Dict]:
    """Return the most recent closure for a chat (for /add_adhoc and defaults)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM game_closures WHERE chat_id = {ph} ORDER BY id DESC LIMIT 1",
                (chat_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_latest_game_closure failed")
        return None


def has_ever_been_collector(chat_id: int, user_id: int) -> bool:
    """True if user_id was the collector on ANY past game closure for this chat.

    No time/count bound by design — once you've collected for this chat,
    you can keep marking payments (mirrors real-world treasurer handoffs
    where the group, not a rotation, decides who's still trusted).
    """
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT 1 FROM game_closures WHERE chat_id = {ph} AND collector_uid = {ph} LIMIT 1",
                (chat_id, user_id),
            )
            return cursor.fetchone() is not None
    except Exception:
        logging.exception("has_ever_been_collector failed")
        return False


def update_game_closure_collector(
    rollcall_id: int, collector_uid: int, collector_name: str,
    collector_paid_ground: int = None, collector_upi: str = None,
) -> bool:
    """Set/replace the collector on an existing closure (post-close /set_collector).
    Not a money row — game_closures records metadata; ledgers stay append-only."""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            sets = [f"collector_uid = {ph}", f"collector_name = {ph}"]
            params: list = [collector_uid, collector_name]
            if collector_paid_ground is not None:
                sets.append(f"collector_paid_ground = {ph}")
                params.append(collector_paid_ground)
            if collector_upi is not None:
                sets.append(f"collector_upi = {ph}")
                params.append(collector_upi)
            params.append(rollcall_id)
            cursor.execute(
                f"UPDATE game_closures SET {', '.join(sets)} WHERE rollcall_id = {ph}",
                params,
            )
            updated = cursor.rowcount > 0
            return updated
    except Exception:
        logging.exception("update_game_closure_collector failed")
        return False


def delete_game_closure(rollcall_id: int) -> bool:
    """Remove a game closure row so the rollcall becomes eligible for re-close.

    game_closures is NOT append-only (metadata, not money rows), so deletion
    is permitted.  The compensating dues_entries and fund_transactions written
    by cancel_game_credit remain for a complete audit trail.
    """
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"DELETE FROM game_closures WHERE rollcall_id = {ph}",
                (rollcall_id,),
            )
            deleted = cursor.rowcount > 0
            return deleted
    except Exception:
        logging.exception("delete_game_closure failed")
        return False


def add_dues_entry(
    chat_id: int,
    rollcall_id: int,
    user_id: int,
    member_name: str,
    entry_type: str,
    amount: int,
    memo: str,
    created_by_uid: int,
    created_by_name: str,
) -> int:
    """Append one dues ledger entry. Positive amount = member owes; negative = credit."""
    try:
        with _cursor(commit=True) as cur:
            ph = "%s" if db_type == "postgresql" else "?"
            cur.execute(
                f"INSERT INTO dues_entries (chat_id, rollcall_id, user_id, member_name,"
                f" entry_type, amount, memo, created_by_uid, created_by_name, created_at)"
                f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (chat_id, rollcall_id, user_id, member_name, entry_type, amount, memo,
                 created_by_uid, created_by_name, _dues_now()),
            )
            if db_type == "postgresql":
                cur.execute("SELECT lastval()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            return cur.fetchone()[0]
    except Exception:
        logging.exception("add_dues_entry failed")
        raise


# Member key: entries for real users aggregate on user_id; entries for unowned
# proxies (user_id NULL) aggregate on LOWER(member_name).
_DUES_MEMBER_KEY = "COALESCE(CAST(user_id AS TEXT), LOWER(member_name))"


def _dues_identity_group(chat_id: int, user_id: int = None, member_name: str = None) -> List[tuple]:
    """Every (user_id, member_name) key to query for one dues identity,
    expanded through any active merge (services/identity.py) — canonical +
    every alias. Returns just [that one identity] when unmerged. Lazy
    import: services/identity.py imports db, so db.py can't import it back
    at module level."""
    from services import identity as identity_svc
    group = identity_svc.get_alias_group(chat_id, user_id=user_id, proxy_name=member_name)
    keys = [(group["user_id"], None)] if group["kind"] == "user" else [(None, group["proxy_name"])]
    keys.extend((None, alias) for alias in group["aliases"])
    return keys


def _member_display_name(chat_id: int, user_id: int) -> str:
    info = get_member_display_info(chat_id, user_id)
    if info:
        return info.get("first_name") or info.get("username") or str(user_id)
    return str(user_id)


def get_dues_balance(chat_id: int, user_id: int = None, member_name: str = None) -> int:
    """Balance for one member's full alias group (canonical + every merged
    alias, if any): SUM(amount). Positive = owes."""
    try:
        ph = "%s" if db_type == "postgresql" else "?"
        total = 0
        for uid, mname in _dues_identity_group(chat_id, user_id=user_id, member_name=member_name):
            with _cursor() as cursor:
                if uid is not None:
                    cursor.execute(
                        f"SELECT COALESCE(SUM(amount), 0) FROM dues_entries"
                        f" WHERE chat_id = {ph} AND user_id = {ph}",
                        (chat_id, uid),
                    )
                else:
                    cursor.execute(
                        f"SELECT COALESCE(SUM(amount), 0) FROM dues_entries"
                        f" WHERE chat_id = {ph} AND user_id IS NULL AND LOWER(member_name) = {ph}",
                        (chat_id, (mname or "").lower()),
                    )
                row = cursor.fetchone()
                total += int(row[0] or 0)
        return total
    except Exception:
        logging.exception("get_dues_balance failed")
        return 0


def get_all_dues_balances(chat_id: int, nonzero_only: bool = False) -> List[Dict]:
    """Per-member balances for a chat, combined across any merged identity
    aliases. Each row: user_id, member_name (canonical display name), balance.

    Groups on the raw per-identity key first (as before), then folds rows
    sharing a canonical identity in Python — mirrors get_leaderboard_by_
    attendance's existing two-step SQL-then-Python-merge precedent.
    nonzero_only is applied AFTER the fold, not via SQL HAVING, since two
    aliases whose raw balances net to zero must not survive as two
    separate nonzero-looking rows.
    """
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT MAX(user_id) AS user_id, MAX(member_name) AS member_name,"
                f" SUM(amount) AS balance"
                f" FROM dues_entries WHERE chat_id = {ph}"
                f" GROUP BY {_DUES_MEMBER_KEY}",
                (chat_id,),
            )
            raw_rows = [dict(r) for r in cursor.fetchall()]

        from services import identity as identity_svc
        merged: Dict[tuple, Dict] = {}
        for row in raw_rows:
            uid = row.get("user_id")
            mname = row.get("member_name")
            canonical = (identity_svc.resolve_canonical(chat_id, user_id=uid) if uid is not None
                         else identity_svc.resolve_canonical(chat_id, proxy_name=mname))
            if canonical["kind"] == "user":
                key = ("user", canonical["user_id"])
            else:
                key = ("proxy", (canonical["proxy_name"] or "").lower())
            existing = merged.get(key)
            if existing is None:
                existing = merged[key] = {
                    "user_id": canonical["user_id"], "member_name": None,
                    "balance": 0,
                }
            existing["balance"] = (existing["balance"] or 0) + (row.get("balance") or 0)
            # Prefer the canonical identity's OWN raw row's stored
            # member_name over an alias's — a real user's actual ledger
            # name (or a canonical proxy's own name) beats a synthesized
            # fallback, which only kicks in below if the canonical never
            # had its own dues_entries row (it only appears here because
            # an alias points at it).
            is_canonical_own_row = (
                (canonical["kind"] == "user" and uid == canonical["user_id"])
                or (canonical["kind"] == "proxy" and uid is None
                    and (mname or "").lower() == (canonical["proxy_name"] or "").lower())
            )
            if mname and (existing["member_name"] is None or is_canonical_own_row):
                existing["member_name"] = mname

        result = list(merged.values())
        for row in result:
            if not row["member_name"]:
                row["member_name"] = _member_display_name(chat_id, row["user_id"])
        if nonzero_only:
            result = [r for r in result if (r["balance"] or 0) != 0]
        result.sort(key=lambda r: r["balance"] or 0, reverse=True)
        return result
    except Exception:
        logging.exception("get_all_dues_balances failed")
        return []


def get_dues_entries(
    chat_id: int, user_id: int = None, member_name: str = None,
    limit: int = 15, offset: int = 0,
) -> List[Dict]:
    """Paginated ledger lines, newest first. When a member key is given,
    combines the full alias group (canonical + every merged alias) into
    one interleaved, correctly-paginated ledger."""
    try:
        ph = "%s" if db_type == "postgresql" else "?"
        if user_id is None and member_name is None:
            with _cursor() as cursor:
                cursor.execute(
                    f"SELECT * FROM dues_entries WHERE chat_id = {ph}"
                    f" ORDER BY id DESC LIMIT {ph} OFFSET {ph}",
                    (chat_id, limit, offset),
                )
                return [dict(r) for r in cursor.fetchall()]

        rows = []
        for uid, mname in _dues_identity_group(chat_id, user_id=user_id, member_name=member_name):
            with _cursor() as cursor:
                if uid is not None:
                    cursor.execute(
                        f"SELECT * FROM dues_entries WHERE chat_id = {ph} AND user_id = {ph}"
                        f" ORDER BY id DESC",
                        (chat_id, uid),
                    )
                else:
                    cursor.execute(
                        f"SELECT * FROM dues_entries WHERE chat_id = {ph} AND user_id IS NULL"
                        f" AND LOWER(member_name) = {ph} ORDER BY id DESC",
                        (chat_id, (mname or "").lower()),
                    )
                rows.extend(dict(r) for r in cursor.fetchall())
        rows.sort(key=lambda r: r["id"], reverse=True)
        return rows[offset:offset + limit]
    except Exception:
        logging.exception("get_dues_entries failed")
        return []


def get_dues_entries_for_rollcall(rollcall_id: int) -> List[Dict]:
    """All ledger lines attached to one game (for cancel-credit reversal)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM dues_entries WHERE rollcall_id = {ph} ORDER BY id ASC",
                (rollcall_id,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception:
        logging.exception("get_dues_entries_for_rollcall failed")
        return []


def get_proxy_owner_uid(chat_id: int, member_name: str) -> Optional[int]:
    """Return the Telegram user_id of the proxy owner for member_name, if recorded.

    Looks at the most recent dues_entry for member_name whose memo matches the
    format "owner:{uid}:{name}" written by close_game for owned proxies.
    Returns None for unowned proxies or if the memo format is older.
    """
    import re as _re
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT memo FROM dues_entries WHERE chat_id = {ph}"
                f" AND LOWER(member_name) = LOWER({ph})"
                f" AND memo LIKE 'owner:%'"
                f" ORDER BY id DESC LIMIT 1",
                (chat_id, member_name),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            m = _re.match(r"owner:(\d+):", row[0])
            return int(m.group(1)) if m else None
    except Exception:
        logging.exception("get_proxy_owner_uid failed")
        return None


def count_dues_entries(chat_id: int) -> int:
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(f"SELECT COUNT(*) FROM dues_entries WHERE chat_id = {ph}", (chat_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception:
        logging.exception("count_dues_entries failed")
        return 0


def add_fund_transaction(
    chat_id: int,
    rollcall_id: int,
    txn_type: str,
    amount: int,
    description: str,
    created_by_uid: int,
    created_by_name: str,
) -> int:
    """Append one fund ledger entry. Positive = into fund; negative = out."""
    try:
        with _cursor(commit=True) as cur:
            ph = "%s" if db_type == "postgresql" else "?"
            cur.execute(
                f"INSERT INTO fund_transactions (chat_id, rollcall_id, txn_type, amount,"
                f" description, created_by_uid, created_by_name, created_at)"
                f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (chat_id, rollcall_id, txn_type, amount, description,
                 created_by_uid, created_by_name, _dues_now()),
            )
            if db_type == "postgresql":
                cur.execute("SELECT lastval()")
            else:
                cur.execute("SELECT last_insert_rowid()")
            return cur.fetchone()[0]
    except Exception:
        logging.exception("add_fund_transaction failed")
        raise


def get_fund_balance(chat_id: int) -> int:
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM fund_transactions WHERE chat_id = {ph}",
                (chat_id,),
            )
            row = cursor.fetchone()
            return int(row[0] or 0)
    except Exception:
        logging.exception("get_fund_balance failed")
        return 0


def get_fund_transactions(chat_id: int, limit: int = 15, offset: int = 0) -> List[Dict]:
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM fund_transactions WHERE chat_id = {ph}"
                f" ORDER BY id DESC LIMIT {ph} OFFSET {ph}",
                (chat_id, limit, offset),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception:
        logging.exception("get_fund_transactions failed")
        return []


def get_fund_transactions_for_rollcall(rollcall_id: int) -> List[Dict]:
    """All fund transactions attached to one rollcall (for cancellation reversal)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM fund_transactions WHERE rollcall_id = {ph} ORDER BY id ASC",
                (rollcall_id,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except Exception:
        logging.exception("get_fund_transactions_for_rollcall failed")
        return []


def count_fund_transactions(chat_id: int) -> int:
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(f"SELECT COUNT(*) FROM fund_transactions WHERE chat_id = {ph}", (chat_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception:
        logging.exception("count_fund_transactions failed")
        return 0


def get_latest_closeable_rollcall(chat_id: int) -> Optional[Dict]:
    """Most recent ended, not-cancelled rollcall with no financial closure yet
    (dues-epoch filtered — see _dues_epoch_clause)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            active_false = "FALSE" if db_type == "postgresql" else "0"
            epoch_sql, epoch_params = _dues_epoch_clause(chat_id, ph)
            cursor.execute(
                f"""SELECT r.* FROM rollcalls r
                    LEFT JOIN game_closures gc ON gc.rollcall_id = r.id
                    WHERE r.chat_id = {ph}
                      AND r.is_active = {active_false}
                      AND COALESCE(r.is_cancelled, {active_false}) = {active_false}
                      AND gc.id IS NULL{epoch_sql}
                    ORDER BY r.ended_at IS NULL ASC, r.ended_at DESC, r.id DESC LIMIT 1""",
                (chat_id, *epoch_params),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_latest_closeable_rollcall failed")
        return None


def get_last_collector_upi(chat_id: int, user_id: int) -> Optional[str]:
    """The UPI this member used the last time they collected, or None.
    Source of the collector-UPI memory: most recent closure wins, so
    correcting the UPI once self-heals every future suggestion."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"""SELECT collector_upi FROM game_closures
                    WHERE chat_id = {ph} AND collector_uid = {ph}
                      AND collector_upi IS NOT NULL
                    ORDER BY id DESC LIMIT 1""",
                (chat_id, user_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return row["collector_upi"] if isinstance(row, dict) else row[0]
    except Exception:
        logging.exception("get_last_collector_upi failed")
        return None


# ── penalty_tiers ─────────────────────────────────────────────────────────────

def upsert_penalty_tier(
    chat_id: int,
    name: str,
    amount: int,
    description: Optional[str] = None,
    late_minutes_threshold: Optional[int] = None,
    is_ditch: bool = False,
) -> bool:
    """Insert or replace a penalty tier for a chat. name is unique per chat.

    If is_ditch=True, clears the is_ditch flag from all other tiers for this
    chat first (only one ditch tier per group).
    """
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            now = _utcnow_naive().isoformat()
            clean_name = name.strip().lower()
            ditch_int = 1 if is_ditch else 0

            if is_ditch:
                cursor.execute(
                    f"UPDATE penalty_tiers SET is_ditch = 0 WHERE chat_id = {ph} AND name != {ph}",
                    (chat_id, clean_name),
                )

            if db_type == "postgresql":
                cursor.execute(
                    f"INSERT INTO penalty_tiers"
                    f" (chat_id, name, amount, description, late_minutes_threshold, is_ditch, created_at)"
                    f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})"
                    f" ON CONFLICT (chat_id, name) DO UPDATE SET"
                    f"  amount=EXCLUDED.amount,"
                    f"  description=EXCLUDED.description,"
                    f"  late_minutes_threshold=EXCLUDED.late_minutes_threshold,"
                    f"  is_ditch=EXCLUDED.is_ditch",
                    (chat_id, clean_name, amount, description,
                     late_minutes_threshold, ditch_int, now),
                )
            else:
                cursor.execute(
                    f"INSERT OR REPLACE INTO penalty_tiers"
                    f" (chat_id, name, amount, description, late_minutes_threshold, is_ditch, created_at)"
                    f" VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                    (chat_id, clean_name, amount, description,
                     late_minutes_threshold, ditch_int, now),
                )
            return True
    except Exception:
        logging.exception("upsert_penalty_tier failed")
        return False


def _dues_epoch_clause(chat_id: int, ph: str):
    """(sql_fragment, params) restricting closeable-game queries to games ended
    after the chat's dues epoch. The epoch is stamped when dues is (re-)enabled
    or a season reset runs — games from before it (pre-dues history, or games
    played while dues was disabled) must never surface as 'unsettled', or an
    admin could retroactively charge members for games settled outside the
    system. NULL epoch (legacy groups) = no restriction."""
    epoch = (get_or_create_chat(chat_id) or {}).get("dues_epoch")
    if not epoch:
        return "", ()
    return f" AND r.ended_at > {ph}", (epoch,)


def get_unsettled_rollcalls(chat_id: int, limit: int = 10) -> List[Dict]:
    """All ended, not-cancelled rollcalls with no financial closure yet and
    ended after the dues epoch, newest first. Same query as
    get_latest_closeable_rollcall minus the LIMIT 1 — used by /settle_dues so
    an admin can reach an older unsettled game, not just the latest one."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            active_false = "FALSE" if db_type == "postgresql" else "0"
            epoch_sql, epoch_params = _dues_epoch_clause(chat_id, ph)
            cursor.execute(
                f"""SELECT r.* FROM rollcalls r
                    LEFT JOIN game_closures gc ON gc.rollcall_id = r.id
                    WHERE r.chat_id = {ph}
                      AND r.is_active = {active_false}
                      AND COALESCE(r.is_cancelled, {active_false}) = {active_false}
                      AND gc.id IS NULL{epoch_sql}
                    ORDER BY r.ended_at IS NULL ASC, r.ended_at DESC, r.id DESC LIMIT {ph}""",
                (chat_id, *epoch_params, limit),
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception:
        logging.exception("get_unsettled_rollcalls failed")
        return []


def get_penalty_tiers(chat_id: int) -> List[Dict]:
    """Return all penalty tiers for a chat, ordered by amount ascending."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM penalty_tiers WHERE chat_id = {ph} ORDER BY amount ASC",
                (chat_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_penalty_tiers failed")
        return []


def get_penalty_tier(chat_id: int, name: str) -> Optional[Dict]:
    """Return a single penalty tier by name (case-insensitive)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"SELECT * FROM penalty_tiers WHERE chat_id = {ph} AND name = {ph}",
                (chat_id, name.strip().lower()),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_penalty_tier failed")
        return None


def get_tier_for_minutes(chat_id: int, minutes: int) -> Optional[Dict]:
    """Find the best matching late tier for a given number of minutes late.

    Returns the tier with the highest late_minutes_threshold that is still
    <= minutes.  Returns None when no configured tier covers this duration.
    """
    tiers = get_penalty_tiers(chat_id)
    candidates = [
        t for t in tiers
        if t.get("late_minutes_threshold") is not None
        and t["late_minutes_threshold"] <= minutes
    ]
    return max(candidates, key=lambda t: t["late_minutes_threshold"]) if candidates else None


def get_ditch_tier(chat_id: int) -> Optional[Dict]:
    """Return the tier flagged as is_ditch=1 for this chat, or None."""
    for t in get_penalty_tiers(chat_id):
        if t.get("is_ditch"):
            return t
    return None


def delete_penalty_tier(chat_id: int, name: str) -> bool:
    """Delete a penalty tier by name. Returns True if a row was deleted."""
    try:
        with _cursor(commit=True) as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"DELETE FROM penalty_tiers WHERE chat_id = {ph} AND name = {ph}",
                (chat_id, name.strip().lower()),
            )
            deleted = cursor.rowcount > 0
            return deleted
    except Exception:
        logging.exception("delete_penalty_tier failed")
        return False


# ── web_direct_login_tokens helpers ──────────────────────────────────────────

def create_web_direct_login_token(
    token: str,
    chat_id: int,
    tg_user_id: int,
    tg_name: str,
    created_by_uid: int,
    created_by_name: str,
    expires_at: "datetime",
) -> None:
    """Store a single-use admin-issued web login token."""
    try:
        with _cursor(commit=True) as cursor:
            ph = '%s' if db_type == 'postgresql' else '?'
            cursor.execute(
                f"INSERT INTO web_direct_login_tokens "
                f"(token, chat_id, tg_user_id, tg_name, created_by_uid, created_by_name, expires_at) "
                f"VALUES ({ph},{ph},{ph},{ph},{ph},{ph},{ph})",
                (token, chat_id, tg_user_id, tg_name, created_by_uid, created_by_name, expires_at.isoformat()),
            )
    except Exception:
        logging.exception("create_web_direct_login_token failed")
        raise


def consume_web_direct_login_token(token: str) -> Optional[Dict]:
    """Atomically mark the token as used and return its payload, or None if invalid/expired/used."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        now = _utcnow_naive().isoformat()
        cursor.execute(
            f"UPDATE web_direct_login_tokens SET used_at={ph} "
            f"WHERE token={ph} AND used_at IS NULL AND expires_at > {ph}",
            (now, token, now),
        )
        if cursor.rowcount == 0:
            conn.commit()
            return None
        conn.commit()
        cursor.execute(
            f"SELECT chat_id, tg_user_id, tg_name FROM web_direct_login_tokens WHERE token={ph}",
            (token,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        return {'chat_id': row[0], 'tg_user_id': row[1], 'tg_name': row[2]}
    except Exception:
        conn.rollback()
        logging.exception("consume_web_direct_login_token failed")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


# ── Digest / periodic-job helpers ─────────────────────────────────────────────

def get_latest_ended_rollcall(chat_id: int) -> Optional[Dict]:
    """Most recent ended, not-cancelled rollcall — full row (for /repeat)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            active_false = "FALSE" if db_type == "postgresql" else "0"
            cursor.execute(
                f"""SELECT * FROM rollcalls
                    WHERE chat_id = {ph}
                      AND is_active = {active_false}
                      AND COALESCE(is_cancelled, {active_false}) = {active_false}
                    ORDER BY ended_at IS NULL ASC, ended_at DESC, id DESC LIMIT 1""",
                (chat_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_latest_ended_rollcall failed")
        return None


def get_rollcalls_between(chat_id: int, start_utc: str, end_utc: str) -> List[Dict]:
    """Ended rollcalls in a UTC window with IN counts (for /summary and wrap-up).

    start_utc/end_utc are 'YYYY-MM-DD HH:MM:SS' strings compared against
    ended_at (CURRENT_TIMESTAMP, UTC in both dialects).
    """
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            active_false = "FALSE" if db_type == "postgresql" else "0"
            cursor.execute(
                f"""SELECT r.id, r.title, r.ended_at,
                    (SELECT COUNT(*) FROM users u WHERE u.rollcall_id = r.id AND u.status = 'in') +
                    (SELECT COUNT(*) FROM proxy_users p WHERE p.rollcall_id = r.id AND p.status = 'in') AS in_count
                    FROM rollcalls r
                    WHERE r.chat_id = {ph}
                      AND r.is_active = {active_false}
                      AND COALESCE(r.is_cancelled, {active_false}) = {active_false}
                      AND r.ended_at IS NOT NULL
                      AND r.ended_at >= {ph} AND r.ended_at < {ph}
                    ORDER BY r.ended_at DESC""",
                (chat_id, start_utc, end_utc),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_rollcalls_between failed")
        return []


def get_attendance_between(chat_id: int, start_utc: str, end_utc: str) -> List[Dict]:
    """Per-member IN counts for rollcalls ended in a UTC window.

    Returns [{'name': ..., 'attended': N}] combining real users and proxies,
    ordered most-attended first.
    """
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            active_false = "FALSE" if db_type == "postgresql" else "0"
            window = (
                f"r.chat_id = {ph} AND r.is_active = {active_false} "
                f"AND COALESCE(r.is_cancelled, {active_false}) = {active_false} "
                f"AND r.ended_at IS NOT NULL AND r.ended_at >= {ph} AND r.ended_at < {ph}"
            )
            cursor.execute(
                f"""SELECT name, SUM(cnt) AS attended FROM (
                        SELECT u.first_name AS name, COUNT(*) AS cnt
                        FROM users u JOIN rollcalls r ON r.id = u.rollcall_id
                        WHERE {window} AND u.status = 'in'
                        GROUP BY u.first_name
                        UNION ALL
                        SELECT p.name AS name, COUNT(*) AS cnt
                        FROM proxy_users p JOIN rollcalls r ON r.id = p.rollcall_id
                        WHERE {window} AND p.status = 'in'
                        GROUP BY p.name
                    ) t
                    WHERE name IS NOT NULL
                    GROUP BY name ORDER BY attended DESC""",
                (chat_id, start_utc, end_utc, chat_id, start_utc, end_utc),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_attendance_between failed")
        return []


def get_fund_transactions_between(chat_id: int, start_utc: str, end_utc: str) -> List[Dict]:
    """Fund transactions in a UTC window (for the monthly statement)."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"""SELECT txn_type, amount, description, created_at
                    FROM fund_transactions
                    WHERE chat_id = {ph} AND created_at >= {ph} AND created_at < {ph}
                    ORDER BY created_at""",
                (chat_id, start_utc, end_utc),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_fund_transactions_between failed")
        return []


def get_idle_chats(cutoff_utc: str) -> List[Dict]:
    """Chats with at least one template whose most recent rollcall predates cutoff.

    Returns [{'chat_id', 'last_rc_at', 'last_idle_nudge', 'group_name'}].
    Only group chats (negative ids) — private chats can't hold rollcall games.
    """
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"""SELECT c.chat_id, c.group_name, c.last_idle_nudge,
                           MAX(r.created_at) AS last_rc_at
                    FROM chats c
                    JOIN rollcalls r ON r.chat_id = c.chat_id
                    WHERE c.chat_id < 0
                      AND EXISTS (SELECT 1 FROM templates t WHERE t.chatid = c.chat_id)
                    GROUP BY c.chat_id, c.group_name, c.last_idle_nudge
                    HAVING MAX(r.created_at) < {ph}""",
                (cutoff_utc,),
            )
            return [dict(row) for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_idle_chats failed")
        return []


def get_last_admin_actor(chat_id: int) -> Optional[Dict]:
    """Most recent admin actor for a chat from the audit log — {'admin_id', 'admin_name'}."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"""SELECT admin_id, admin_name FROM admin_actions
                    WHERE chat_id = {ph} ORDER BY id DESC LIMIT 1""",
                (chat_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception:
        logging.exception("get_last_admin_actor failed")
        return None


def get_all_chat_ids_with_dues() -> List[int]:
    """Chat ids where dues is enabled and the weekly auto-nudge is on."""
    try:
        with _cursor() as cursor:
            true_val = "TRUE" if db_type == "postgresql" else "1"
            cursor.execute(
                f"""SELECT chat_id FROM chats
                    WHERE dues_enabled = {true_val} AND dues_weekly_nudge = 1"""
            )
            return [row["chat_id"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_all_chat_ids_with_dues failed")
        return []


def get_all_chat_ids_with_dues_report() -> List[int]:
    """Chat ids where dues is enabled and weekly dues report is on."""
    try:
        with _cursor() as cursor:
            true_val = "TRUE" if db_type == "postgresql" else "1"
            cursor.execute(
                f"""SELECT chat_id FROM chats
                    WHERE dues_enabled = {true_val} AND dues_report_enabled = 1"""
            )
            return [row["chat_id"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_all_chat_ids_with_dues_report failed")
        return []


def get_active_group_chat_ids(since_utc: str) -> List[int]:
    """Group chats with at least one rollcall created since the given UTC timestamp."""
    try:
        with _cursor() as cursor:
            ph = "%s" if db_type == "postgresql" else "?"
            cursor.execute(
                f"""SELECT DISTINCT chat_id FROM rollcalls
                    WHERE chat_id < 0 AND created_at >= {ph}""",
                (since_utc,),
            )
            return [row["chat_id"] if isinstance(row, dict) else row[0] for row in cursor.fetchall()]
    except Exception:
        logging.exception("get_active_group_chat_ids failed")
        return []


# Initialize database on import
init_db()
