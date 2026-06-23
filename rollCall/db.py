"""
Database layer for RollCall bot
Supports both PostgreSQL and SQLite
"""
from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone
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
                    group_name TEXT DEFAULT NULL
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
                    group_name TEXT DEFAULT NULL
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


def _run_migrations(conn, cursor):

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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(f"SELECT * FROM chats WHERE group_web_token = {ph}", (token,))
        row = cursor.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logging.error(f"Error in get_chat_by_group_web_token: {e}")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


_VALID_CHAT_FIELDS = {
    'shh_mode', 'admin_rights', 'timezone', 'absent_limit',
    'ghost_tracking_enabled', 'group_name', 'group_web_token',
}

def update_chat_settings(chat_id: int, **kwargs) -> bool:
    """Update chat settings"""
    for key in kwargs:
        if key not in _VALID_CHAT_FIELDS:
            raise ValueError(f"update_chat_settings: invalid field '{key}'")
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

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
        conn.commit()
        logging.info(f"Updated chat settings for {chat_id}: {kwargs}")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error updating chat settings: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

def create_rollcall(chat_id: int, title: str, timezone: str = 'Asia/Kolkata', web_token: Optional[str] = None) -> int:
    """Create a new rollcall and return its ID"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

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
        
        conn.commit()
        logging.info(f"Created rollcall {rollcall_id} for chat {chat_id}: {title}")
        return rollcall_id
    except Exception as e:
        conn.rollback()
        logging.error(f"Error creating rollcall: {e}")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

def ensure_rollcall_stats(rollcall_id: int) -> None:
    """
    Ensure a rollcall_stats row exists for this rollcall.
    Called once at rollcall creation so increment_rollcall_stat never fails silently.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        logging.info(f"Ensured rollcall_stats row for rollcall {rollcall_id}")
    except Exception as e:
        conn.rollback()
        logging.error(f"Error ensuring rollcall_stats: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_rollcall(rollcall_id: int) -> Optional[Dict]:
    """Get rollcall by ID"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

def get_rollcall_by_web_token(token: str) -> Optional[Dict]:
    """Get an active rollcall by its magic-link web_token."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


_VALID_ROLLCALL_FIELDS = {
    'chat_id', 'title', 'is_active', 'finalize_date', 'location',
    'event_fee', 'in_list_limit', 'panel_msg_id', 'web_token',
    'timezone', 'reminder_hours', 'template_name', 'created_at',
}

def update_rollcall(rollcall_id: int, **kwargs) -> bool:
    """Update rollcall fields"""
    for key in kwargs:
        if key not in _VALID_ROLLCALL_FIELDS:
            raise ValueError(f"update_rollcall: invalid field '{key}'")
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()

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
        conn.commit()
        logging.info(f"Updated rollcall {rollcall_id}: {kwargs}")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error updating rollcall: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

def get_active_rollcalls(chat_id: int) -> List[Dict]:
    """Get all active rollcalls for a chat"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error creating/updating template: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        
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
        
        conn.commit()
        logging.info(f"Ended rollcall {rollcall_id}")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error ending rollcall: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error add/update user: {e}")
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def add_or_update_proxy_user(rollcall_id: int, name: str, status: str, comment: str = '', proxy_owner_id: Optional[int] = None) -> bool:
    """Add or update a proxy user with position tracking."""
    if status not in _VALID_STATUSES:
        logging.error(f"add_or_update_proxy_user: invalid status '{status}' for proxy '{name}'")
        return False
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error adding/updating proxy user: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

def get_all_users(rollcall_id: int):
    """
    Get all users (real + proxy) for a rollcall.
    Ordering:
    - Grouped by status (in, out, maybe, waitlist).
    - Within IN/OUT/WAITLIST, ordered by their per-state position.
    - For MAYBE (no positions), fall back to created_at.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def get_proxy_users_by_status(rollcall_id: int, status: str) -> List[Dict]:
    """Get proxy users by status ordered by position"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def delete_template(chatid: int, name: str) -> bool:
    """
    Delete a template for a chat by name.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        logging.error(f"Error deleting template: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def set_template_schedule(chatid: int, name: str, schedule_day: str, schedule_time: str, recurrence_type: str = 'weekly') -> bool:
    """Set schedule day/time and enable auto-start for a template."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        enabled = True if db_type == "postgresql" else 1
        cursor.execute(
            f"UPDATE templates SET schedule_day = {ph}, schedule_time = {ph}, "
            f"schedule_enabled = {ph}, last_scheduled_date = NULL, recurrence_type = {ph} "
            f"WHERE chatid = {ph} AND name = {ph}",
            (schedule_day, schedule_time, enabled, recurrence_type, chatid, name),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        logging.error(f"Error setting template schedule: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def disable_template_schedule(chatid: int, name: str) -> bool:
    """Disable auto-start scheduling for a template."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        disabled = False if db_type == "postgresql" else 0
        cursor.execute(
            f"UPDATE templates SET schedule_enabled = {ph} WHERE chatid = {ph} AND name = {ph}",
            (disabled, chatid, name),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        logging.error(f"Error disabling template schedule: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def enable_template_schedule(chatid: int, name: str) -> bool:
    """Re-enable scheduling for a template using its previously saved schedule parameters."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        enabled = True if db_type == "postgresql" else 1
        cursor.execute(
            f"UPDATE templates SET schedule_enabled = {ph} WHERE chatid = {ph} AND name = {ph}",
            (enabled, chatid, name),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        logging.error(f"Error enabling template schedule: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def update_template_last_scheduled_date(chatid: int, name: str, date_str: str) -> bool:
    """Record the date (YYYY-MM-DD) when a template was last auto-started."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = "%s" if db_type == "postgresql" else "?"
        cursor.execute(
            f"UPDATE templates SET last_scheduled_date = {ph} WHERE chatid = {ph} AND name = {ph}",
            (date_str, chatid, name),
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        conn.rollback()
        logging.error(f"Error updating last_scheduled_date: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def get_all_scheduled_templates() -> List[Dict]:
    """Return all templates with schedule_enabled=True across all chats."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute("SELECT * FROM templates WHERE schedule_enabled = TRUE")
        else:
            cursor.execute("SELECT * FROM templates WHERE schedule_enabled = 1")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logging.error(f"Error fetching scheduled templates: {e}")
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


def delete_user_by_name(rollcall_id: int, name: str) -> bool:
    """Delete a user by name — checks proxy_users first, then real users.
    Matches @username uniquely; first_name is only used when it identifies
    exactly one user (otherwise we refuse to delete to avoid wiping the
    wrong account when two real users share a first name)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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

        conn.commit()
        return rows_deleted > 0

    except Exception as e:
        conn.rollback()
        logging.error(f"Error deleting user: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def delete_user_by_id(rollcall_id: int, user_id) -> bool:
    """Delete a real user (int user_id) or proxy user (str user_id) by exact id.
    Used by /set_status which knows the precise user from the in-memory cache."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        return rows_deleted > 0
    except Exception as e:
        conn.rollback()
        logging.error(f"Error deleting user by id: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error incrementing user stat {field}: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)

def increment_rollcall_stat(rollcall_id: int, field: str) -> None:
    """Increment a single numeric field in rollcall_stats."""
    if field not in VALID_ROLLCALL_STAT_FIELDS:
        raise ValueError(f"Invalid rollcall stat field: {field}")
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error incrementing rollcall stat {field}: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_next_position(rollcall_id: int, status: str) -> int:
    """Return next position index across both users and proxy_users tables."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)



def get_templates(chatid: int) -> List[Dict]:
    """
    Get all templates for a chat.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)

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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == "postgresql":
            release_connection(conn)


# ---------------------------------------------------------------------------
# Ghost tracking functions
# ---------------------------------------------------------------------------

def get_ghost_count(chat_id: int, user_id: int) -> int:
    """Return the ghost count for a user in a chat (0 if no record)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_ghost_count_by_proxy_name(chat_id: int, proxy_name: str) -> int:
    """Return the ghost count for a proxy user in a chat (0 if no record)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def increment_ghost_count(chat_id: int, user_id: int, user_name: str, proxy_name: str = None) -> bool:
    """Increment ghost count for a user or proxy user, inserting a record if one does not exist.
    
    For proxy users (added via /sif), pass user_id=-1 and the proxy_name.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        logging.info(f"Incremented ghost count for user {user_id}/{proxy_name} in chat {chat_id}")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error incrementing ghost count: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def reset_ghost_count(chat_id: int, user_id: int, proxy_name: str = None) -> bool:
    """Reset ghost count to 0 for a user or proxy user (admin clear).

    For proxy users, pass user_id=-1 and the proxy_name.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        logging.info(f"Reset ghost count for user {user_id}/{proxy_name} in chat {chat_id}")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error resetting ghost count: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def decrement_ghost_count(chat_id: int, user_id: int, proxy_name: str = None) -> bool:
    """Decrement ghost count by 1, floored at 0. No-op if no record exists.

    Called from the /mark_absent finalize step for every IN user who was NOT
    selected as a ghost — i.e. they actually attended. The count never goes
    negative; when it lands at 0, last_ghosted_at is cleared too so the
    leaderboard and reconf threshold treat them as fresh.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error decrementing ghost count: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_ghost_leaderboard(chat_id: int) -> List[Dict]:
    """Return all users with ghost_count > 0 for a chat, sorted descending."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(
            f"""SELECT user_id, proxy_name, user_name, ghost_count, last_ghosted_at
                FROM ghost_records
                WHERE chat_id = {ph} AND ghost_count > 0
                ORDER BY ghost_count DESC, last_ghosted_at DESC""",
            (chat_id,)
        )
        return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error getting ghost leaderboard: {e}")
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_user_ghost_count_by_name(chat_id: int, user_name: str) -> Optional[Dict]:
    """Find a ghost record by user_name or proxy_name for a chat (for admin /clear_absent by name)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def mark_rollcall_absent_done(rollcall_id: int) -> bool:
    """Mark a rollcall's absent selection as completed."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        val = True if db_type == 'postgresql' else 1
        cursor.execute(
            f"UPDATE rollcalls SET absent_marked = {ph} WHERE id = {ph}",
            (val, rollcall_id)
        )
        conn.commit()
        logging.info(f"Marked rollcall {rollcall_id} absent_marked=True")
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error marking rollcall absent done: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_unprocessed_rollcalls(chat_id: int, days: int = 30) -> List[Dict]:
    """
    Return ended roll calls that still need absent marking:
      - is_active = FALSE (ended)
      - absent_marked = FALSE (not yet processed)
      - ended_at within the last `days` days
      - had at least one user with status='in'
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def add_ghost_event(rollcall_id: int, chat_id: int, user_id: int = None, user_name: str = None, proxy_name: str = None) -> bool:
    """Record an individual ghost event for audit trail."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(
            f"""INSERT INTO ghost_events (rollcall_id, chat_id, user_id, proxy_name, user_name)
                VALUES ({ph}, {ph}, {ph}, {ph}, {ph})""",
            (rollcall_id, chat_id, user_id, proxy_name, user_name)
        )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error adding ghost event: {e}")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_rollcall_in_users(rollcall_id: int) -> List[Dict]:
    """Return all users (real + proxy) with status='in' for a given rollcall.

    Real users (signed in via /in or the panel) have an integer ``user_id``.
    Proxy users (added via /sif) have ``user_id=None`` and a ``proxy_name`` key.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
            f"""SELECT name
                FROM proxy_users
                WHERE rollcall_id = {ph} AND status = 'in'
                ORDER BY in_pos ASC""",
            (rollcall_id,)
        )
        proxy_rows = [
            {'user_id': None, 'first_name': row['name'], 'username': None, 'proxy_name': row['name']}
            for row in cursor.fetchall()
        ]

        return real_rows + proxy_rows
    except Exception as e:
        logging.error(f"Error getting rollcall IN users: {e}")
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


# Ghost selection persistence: save/load selections to DB
def save_ghost_selections(chat_id: int, rc_db_id: int, selected_ids: set) -> bool:
    """Save ghost selections to database for crash recovery"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        return True
    except Exception as e:
        logging.error(f"Error saving ghost selections: {e}")
        conn.rollback()
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def load_ghost_selections(chat_id: int, rc_db_id: int) -> Optional[set]:
    """Load ghost selections from database for crash recovery"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def create_ghost_selections_table() -> None:
    """Create ghost_selections table if not exists"""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
    except Exception as e:
        logging.error(f"Error creating ghost_selections table: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def update_streak_on_checkin(chat_id: int, user_id: int) -> None:
    """Increment current_streak by 1 for a user at rollcall end; update best_streak if exceeded."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error updating streak on checkin: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


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


def get_proxy_streaks(chat_id: int, proxy_name: str) -> Dict:
    """Return {current_streak, best_streak} for a proxy. Both default to 0
    if the proxy has no proxy_stats row yet (i.e. hasn't been through an
    /erc since proxy_stats was introduced)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_chat_ended_rollcall_count(chat_id: int) -> int:
    """Return the total number of ENDED rollcalls in this chat.

    Used as the denominator for Voting% and Attendance% in /stats — both
    rates measure each user against ALL ended sessions, not just sessions
    they participated in. That's the only way "voting %" means engagement
    rather than "100% trivially because they voted at least once."
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_user_attendance_count(chat_id: int, user_id: int) -> int:
    """Return the number of ENDED rollcalls in this chat where the user's
    final status was IN. This is the authoritative attendance number —
    user_stats.total_in counts every IN VOTE (which inflates if a user flips
    IN→OUT→IN within one session), so it must not be used for attendance %.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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

        # Sort by attended DESC, total_rollcalls ASC (rewards consistency),
        # then deterministic tiebreak by display_name ASC.
        unified.sort(key=lambda x: (-x['attended'], x['total_rollcalls'], x['display_name'] or ''))
        return unified[:limit]
    except Exception as e:
        logging.error(f"Error fetching attendance leaderboard: {e}")
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_proxy_attendance_count(chat_id: int, proxy_name: str) -> int:
    """Same idea as get_user_attendance_count, but for proxy users."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_proxy_stats(chat_id: int, proxy_name: str) -> Dict:
    """Return a single proxy's aggregate stats: total rollcalls participated
    in (any status), attended (final-IN), and per-status vote breakdown.
    For proxies the per-status breakdown is per-rollcall (proxy_users is
    UNIQUE(rollcall_id, name) so each row = one final status per session)
    — not per-vote like the real-user total_in counter."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_rollcall_history(chat_id: int, limit: int = 10, offset: int = 0) -> List[Dict]:
    """Return ended rollcalls for a chat with participant counts, supporting pagination."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        if db_type == 'postgresql':
            cursor.execute(f"""
                SELECT r.id, r.title, r.ended_at,
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
                SELECT r.id, r.title, r.ended_at,
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def count_admin_audit_log(chat_id: int) -> int:
    """Return total number of recorded actions for a chat."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def upsert_chat_member(chat_id: int, user_id: int, first_name: str, username: str = None) -> None:
    """Insert or update a chat member record.

    Called every time a real Telegram user votes so that display names stay
    fresh and the member is (re-)marked active.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
    except Exception as e:
        logging.error(f"Error upserting chat member: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def mark_member_inactive(chat_id: int, user_id: int) -> None:
    """Mark a member as no longer in the group (left or kicked)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        active_val = False if db_type == 'postgresql' else 0
        cursor.execute(f"""
            UPDATE chat_members SET is_active = {ph}
            WHERE chat_id = {ph} AND user_id = {ph}
        """, (active_val, chat_id, user_id))
        conn.commit()
    except Exception as e:
        logging.error(f"Error marking member inactive: {e}")
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_active_members(chat_id: int) -> List[Dict]:
    """Return all members currently marked active for a chat.

    These are real Telegram users (not proxy users) who have voted at least
    once and have not been detected as having left the group.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


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
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(f"""
            INSERT INTO api_tokens (token_hash, chat_id, issued_by_user_id,
                                    scopes, label, expires_at)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
        """, (token_hash, chat_id, issued_by_user_id, scopes, label, expires_at))
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.exception("insert_api_token: failed to persist token for chat %s: %s", chat_id, e)
        raise
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def lookup_api_token(token_hash: str) -> Optional[Dict]:
    """Look up a token by its hash. Returns a dict with chat_id, scopes
    (parsed to a list), label, expires_at, revoked_at — or None if no
    matching token, the token is revoked, or it has expired.

    Also bumps `last_used_at` as a side effect when a hit is returned, so
    operators can audit token activity via the same row."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
                    parsed = _parse_db_datetime(expires_at)
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
            conn.commit()
        except Exception:
            logging.exception("api_token last_used_at update failed")

        d["scopes"] = [s.strip() for s in (d.get("scopes") or "").split(",") if s.strip()]
        return d
    except Exception:
        logging.exception("lookup_api_token failed")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def list_api_tokens(chat_id: int) -> List[Dict]:
    """List all tokens issued for a chat (active + revoked + expired).
    Useful for the admin token-management surface. token_hash is included
    so revocation by hash works."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def revoke_api_token(token_hash: str) -> bool:
    """Mark a token as revoked (sets revoked_at). Returns True if a row
    was modified, False if no such token (or already revoked)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'
        cursor.execute(f"""
            UPDATE api_tokens
            SET revoked_at = CURRENT_TIMESTAMP
            WHERE token_hash = {ph} AND revoked_at IS NULL
        """, (token_hash,))
        affected = cursor.rowcount
        conn.commit()
        return bool(affected)
    except Exception:
        logging.exception("revoke_api_token failed")
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


# ── Web presence / view-count helpers ────────────────────────────────────────

def increment_group_view_count(group_token: str) -> int:
    """Upsert a view-count row for group_token and return the new total."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
        conn.commit()
        return count
    except Exception:
        logging.exception("increment_group_view_count failed")
        return 0
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_group_view_count(group_token: str) -> int:
    """Return the total view count for group_token (0 if none recorded yet)."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


def get_response_time_leaderboard(chat_id: int, limit: int = 10) -> List[Dict]:
    """
    Return per-user average and best response time (seconds from rollcall start
    to first vote) across ended rollcalls in this chat.

    Uses users.created_at (insert time = first vote) minus rollcalls.created_at.
    Only ended rollcalls with positive response times are included.
    Ordered fastest-first.
    """
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
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
    finally:
        if cursor is not None:
            cursor.close()
        if db_type == 'postgresql':
            release_connection(conn)


# Initialize database on import
init_db()
