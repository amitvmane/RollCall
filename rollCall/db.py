"""
Database layer for RollCall bot
Supports both PostgreSQL and SQLite
"""

import os
import json
import logging
#from datetime import datetime
from typing import Dict, List, Optional, Any

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

# Database connection pool/connection
db_pool = None
db_conn = None
db_type = None
# Allowlists for safe SQL field interpolation
VALID_USER_STAT_FIELDS = {
    'total_in', 'total_out', 'total_maybe', 'total_waiting_to_in',
    'total_rollcalls', 'total_response_seconds', 'best_streak', 'current_streak'
}
VALID_ROLLCALL_STAT_FIELDS = {'total_in', 'total_out', 'total_maybe'}


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
    logging.debug("Database initialized successfully")

def init_postgresql():
    """Initialize PostgreSQL connection pool"""
    global db_pool
    try:
        db_pool = SimpleConnectionPool(
            minconn=1,
            maxconn=5,
            dsn=DATABASE_URL
        )
        logging.debug("PostgreSQL connection pool created")
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
    """Get database connection"""
    if db_type == 'postgresql':
        return db_pool.getconn()
    else:
        return db_conn

def release_connection(conn):
    """Release database connection"""
    if db_type == 'postgresql':
        db_pool.putconn(conn)

def create_tables():
    """Create database tables if they don't exist"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        if db_type == 'postgresql':
            # PostgreSQL table definitions
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id BIGINT PRIMARY KEY,
                    shh_mode BOOLEAN DEFAULT FALSE,
                    admin_rights BOOLEAN DEFAULT FALSE,
                    timezone VARCHAR(100) DEFAULT 'Asia/Calcutta',
                    absent_limit INTEGER DEFAULT 1,
                    ghost_tracking_enabled BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    timezone VARCHAR(100) DEFAULT 'Asia/Calcutta',
                    location TEXT,
                    event_fee TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE,
                    ended_at TIMESTAMP,
                    absent_marked BOOLEAN DEFAULT FALSE,
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
                    timezone TEXT DEFAULT 'Asia/Calcutta',
                    absent_limit INTEGER DEFAULT 1,
                    ghost_tracking_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    timezone TEXT DEFAULT 'Asia/Calcutta',
                    location TEXT,
                    event_fee TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1,
                    ended_at TIMESTAMP,
                    absent_marked INTEGER DEFAULT 0,
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
                    last_ghosted_at TIMESTAMP,
                    UNIQUE(chat_id, COALESCE(proxy_name, user_id::text))
                )
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

        conn.commit()
        logging.debug("Database tables created successfully")

        # Migrate existing databases to add new columns if needed
        _migrate_schema(conn)

    except Exception as e:
        conn.rollback()
        logging.error(f"Error creating tables: {e}")
        raise
    finally:
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def _migrate_schema(conn):
    """Add new columns to existing tables for databases created before ghost tracking."""
    migrations = []
    if db_type == 'postgresql':
        migrations = [
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS absent_limit INTEGER DEFAULT 1",
            "ALTER TABLE chats ADD COLUMN IF NOT EXISTS ghost_tracking_enabled BOOLEAN DEFAULT TRUE",
            "ALTER TABLE rollcalls ADD COLUMN IF NOT EXISTS absent_marked BOOLEAN DEFAULT FALSE",
            "ALTER TABLE ghost_records ADD COLUMN IF NOT EXISTS proxy_name TEXT",
        ]
    else:
        migrations = [
            ("ALTER TABLE chats ADD COLUMN absent_limit INTEGER DEFAULT 1"),
            ("ALTER TABLE chats ADD COLUMN ghost_tracking_enabled INTEGER DEFAULT 1"),
            ("ALTER TABLE rollcalls ADD COLUMN absent_marked INTEGER DEFAULT 0"),
            ("ALTER TABLE ghost_records ADD COLUMN proxy_name TEXT"),
        ]

    cursor = conn.cursor()
    for sql in migrations:
        try:
            cursor.execute(sql)
            conn.commit()
        except Exception:
            conn.rollback()

    # Stamp all pre-existing rollcalls as already processed so the ghost
    # tracking prompt never fires for roll calls that started before this
    # deployment.  New rollcalls get absent_marked = FALSE (the DB default)
    # and are therefore fully eligible for ghost tracking.
    try:
        if db_type == 'postgresql':
            cursor.execute("UPDATE rollcalls SET absent_marked = TRUE WHERE absent_marked = FALSE")
        else:
            cursor.execute("UPDATE rollcalls SET absent_marked = 1 WHERE absent_marked = 0")
        conn.commit()
    except Exception:
        conn.rollback()

    if db_type == 'postgresql':
        cursor.close()


def get_or_create_chat(chat_id: int) -> Dict:
    """Get or create chat settings"""
    conn = get_connection()
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
            return dict(row)
        else:
            # Create new chat
            if db_type == 'postgresql':
                cursor.execute(
                    """INSERT INTO chats (chat_id, shh_mode, admin_rights, timezone, absent_limit, ghost_tracking_enabled)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                    (chat_id, False, False, 'Asia/Calcutta', 1, True)
                )
                result = dict(cursor.fetchone())
            else:
                cursor.execute(
                    """INSERT INTO chats (chat_id, shh_mode, admin_rights, timezone, absent_limit, ghost_tracking_enabled)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (chat_id, 0, 0, 'Asia/Calcutta', 1, 1)
                )
                # Re-query to get actual DB values instead of hardcoding
                cursor.execute(
                    "SELECT * FROM chats WHERE chat_id = ?",
                    (chat_id,)
                )
                result = dict(cursor.fetchone())
            conn.commit()
            logging.info(f"Created new chat: {chat_id}")
            return result
    except Exception as e:
        conn.rollback()
        logging.error(f"Error in get_or_create_chat: {e}")
        raise
    finally:
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def update_chat_settings(chat_id: int, **kwargs) -> bool:
    """Update chat settings"""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def create_rollcall(chat_id: int, title: str, timezone: str = 'Asia/Calcutta') -> int:
    """Create a new rollcall and return its ID"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Ensure chat exists
        get_or_create_chat(chat_id)
        
        if db_type == 'postgresql':
            cursor.execute(
                """INSERT INTO rollcalls (chat_id, title, timezone)
                   VALUES (%s, %s, %s) RETURNING id""",
                (chat_id, title, timezone)
            )
            rollcall_id = cursor.fetchone()[0]
        else:
            cursor.execute(
                """INSERT INTO rollcalls (chat_id, title, timezone)
                   VALUES (?, ?, ?)""",
                (chat_id, title, timezone)
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def ensure_rollcall_stats(rollcall_id: int) -> None:
    """
    Ensure a rollcall_stats row exists for this rollcall.
    Called once at rollcall creation so increment_rollcall_stat never fails silently.
    """
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def get_rollcall(rollcall_id: int) -> Optional[Dict]:
    """Get rollcall by ID"""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def update_rollcall(rollcall_id: int, **kwargs) -> bool:
    """Update rollcall fields"""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def get_active_rollcalls(chat_id: int) -> List[Dict]:
    """Get all active rollcalls for a chat"""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
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
            # SQLite: emulate upsert with INSERT OR REPLACE on id
            cursor.execute(
                """
                INSERT OR REPLACE INTO templates
                (
                    id, chatid, name, title, inlistlimit, location, eventfee,
                    offsetdays, offsethours, offsetminutes, event_day, event_time
                )
                VALUES (
                    COALESCE(
                        (SELECT id FROM templates WHERE chatid = ? AND name = ?),
                        NULL
                    ),
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    chatid, name,
                    chatid, name, title, inlistlimit, location, eventfee,
                    offsetdays, offsethours, offsetminutes, event_day, event_time
                ),
            )
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error creating/updating template: {e}")
        return False
    finally:
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)


def end_rollcall(rollcall_id: int) -> bool:
    """Mark a rollcall as ended"""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def get_all_chat_ids() -> List[int]:
    """Return all known chat IDs from the chats table."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        if db_type == 'postgresql':
            cursor.execute("SELECT chat_id FROM chats")
        else:
            cursor.execute("SELECT chat_id FROM chats")
        return [row['chat_id'] for row in cursor.fetchall()]
    except Exception as e:
        logging.error(f"Error fetching all chat IDs: {e}")
        return []
    finally:
        if cursor is not None and db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def add_or_update_user(rollcall_id: int, user_id: int, first_name: str, username: str, status: str, comment: str = '') -> bool:
    """Insert or update a regular user. Position assigned once per bucket, preserved on re-entry."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'

        # Fetch existing positions
        cursor.execute(
            f"SELECT in_pos, out_pos, wait_pos FROM users WHERE rollcall_id = {ph} AND user_id = {ph}",
            (rollcall_id, user_id)
        )
        existing = cursor.fetchone()

        if existing:
            existing = dict(existing)
            in_pos   = existing['in_pos']
            out_pos  = existing['out_pos']
            wait_pos = existing['wait_pos']
            # Only assign NEW position if entering this bucket for the FIRST TIME
            if status == 'in' and in_pos is None:
                in_pos = get_next_position(rollcall_id, 'in')
            elif status == 'out' and out_pos is None:
                out_pos = get_next_position(rollcall_id, 'out')
            elif status == 'waitlist' and wait_pos is None:
                wait_pos = get_next_position(rollcall_id, 'waitlist')
        else:
            # Brand new user
            in_pos = out_pos = wait_pos = None
            if status == 'in':
                in_pos = get_next_position(rollcall_id, 'in')
            elif status == 'out':
                out_pos = get_next_position(rollcall_id, 'out')
            elif status == 'waitlist':
                wait_pos = get_next_position(rollcall_id, 'waitlist')

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
    except Exception as e:
        conn.rollback()
        logging.error(f"Error add/update user: {e}")
        raise
    finally:
        if cursor is not None and db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def add_or_update_proxy_user(rollcall_id: int, name: str, status: str, comment: str = '', proxy_owner_id: Optional[int] = None) -> bool:
    """Add or update a proxy user with position tracking."""
    conn = get_connection()
    cursor = None
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'

        # Fetch existing positions
        cursor.execute(
            f"SELECT in_pos, out_pos, wait_pos FROM proxy_users WHERE rollcall_id = {ph} AND name = {ph}",
            (rollcall_id, name)
        )
        existing = cursor.fetchone()

        if existing:
            existing = dict(existing)
            in_pos   = existing['in_pos']
            out_pos  = existing['out_pos']
            wait_pos = existing['wait_pos']
            # Only assign NEW position if entering this bucket for the FIRST TIME
            if status == 'in' and in_pos is None:
                in_pos = get_next_position(rollcall_id, 'in')
            elif status == 'out' and out_pos is None:
                out_pos = get_next_position(rollcall_id, 'out')
            elif status == 'waitlist' and wait_pos is None:
                wait_pos = get_next_position(rollcall_id, 'waitlist')
        else:
            # Brand new proxy
            in_pos = out_pos = wait_pos = None
            if status == 'in':
                in_pos = get_next_position(rollcall_id, 'in')
            elif status == 'out':
                out_pos = get_next_position(rollcall_id, 'out')
            elif status == 'waitlist':
                wait_pos = get_next_position(rollcall_id, 'waitlist')

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
        if cursor is not None and db_type == 'postgresql':
            cursor.close()
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
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)


def get_proxy_users_by_status(rollcall_id: int, status: str) -> List[Dict]:
    """Get proxy users by status ordered by position"""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def delete_template(chatid: int, name: str) -> bool:
    """
    Delete a template for a chat by name.
    """
    conn = get_connection()
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
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)


def delete_user_by_name(rollcall_id: int, name: str) -> bool:
    """Delete a user by name — checks proxy_users first, then real users.
    Supports matching by first_name OR username (with or without @).
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        ph = '%s' if db_type == 'postgresql' else '?'

        # Try proxy_users FIRST (named proxy should be removed before real user)
        cursor.execute(
            f"DELETE FROM proxy_users WHERE rollcall_id = {ph} AND name = {ph}",
            (rollcall_id, name)
        )
        rows_deleted = cursor.rowcount

        # Only try real users if no proxy was deleted
        if rows_deleted == 0:
            # Strip @ if admin passed @username format
            clean_name = name.lstrip('@')
            cursor.execute(
                f"DELETE FROM users WHERE rollcall_id = {ph} AND (first_name = {ph} OR username = {ph})",
                (rollcall_id, clean_name, clean_name)
            )
            rows_deleted = cursor.rowcount

        conn.commit()
        return rows_deleted > 0

    except Exception as e:
        conn.rollback()
        logging.error(f"Error deleting user: {e}")
        return False
    finally:
        if db_type == 'postgresql':
            cursor.close()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def increment_rollcall_stat(rollcall_id: int, field: str) -> None:
    """Increment a single numeric field in rollcall_stats."""
    if field not in VALID_ROLLCALL_STAT_FIELDS:
        raise ValueError(f"Invalid rollcall stat field: {field}")
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def get_next_position(rollcall_id: int, status: str) -> int:
    """Return next position index across both users and proxy_users tables."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)



def get_templates(chatid: int) -> List[Dict]:
    """
    Get all templates for a chat.
    """
    conn = get_connection()
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
        if db_type == "postgresql":
            cursor.close()
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
            cursor.close()
        if db_type == 'postgresql' and conn:
            release_connection(conn)

def get_template(chatid: int, name: str) -> Optional[Dict]:
    """
    Get a single template for a chat by name.
    """
    conn = get_connection()
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
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)


# ---------------------------------------------------------------------------
# Ghost tracking functions
# ---------------------------------------------------------------------------

def get_ghost_count(chat_id: int, user_id: int) -> int:
    """Return the ghost count for a user in a chat (0 if no record)."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def get_ghost_count_by_proxy_name(chat_id: int, proxy_name: str) -> int:
    """Return the ghost count for a proxy user in a chat (0 if no record)."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
        release_connection(conn)


def increment_ghost_count(chat_id: int, user_id: int, user_name: str, proxy_name: str = None) -> bool:
    """Increment ghost count for a user or proxy user, inserting a record if one does not exist.
    
    For proxy users (added via /sif), pass user_id=-1 and the proxy_name.
    """
    conn = get_connection()
try:
        cursor = conn.cursor()
        if db_type == 'postgresql':
            if proxy_name:
                cursor.execute(
                    """INSERT INTO ghost_records (chat_id, user_id, proxy_name, user_name, ghost_count, last_ghosted_at)
                       VALUES (%s, %s, %s, %s, 1, CURRENT_TIMESTAMP)
                       ON CONFLICT (chat_id, proxy_name) DO UPDATE
                       SET ghost_count = ghost_records.ghost_count + 1,
                           user_name = EXCLUDED.user_name,
                           last_ghosted_at = CURRENT_TIMESTAMP""",
                    (chat_id, user_id, proxy_name, user_name)
                )
            else:
                cursor.execute(
                    """INSERT INTO ghost_records (chat_id, user_id, user_name, ghost_count, last_ghosted_at)
                       VALUES (%s, %s, %s, 1, CURRENT_TIMESTAMP)
                       ON CONFLICT (chat_id, user_id) DO UPDATE
                       SET ghost_count = ghost_records.ghost_count + 1,
                           user_name = EXCLUDED.user_name,
                           last_ghosted_at = CURRENT_TIMESTAMP""",
                    (chat_id, user_id, user_name)
                )
        else:
            cursor.execute(
                "SELECT ghost_count FROM ghost_records WHERE chat_id = ? AND COALESCE(proxy_name, '') = COALESCE(?, '')",
                (chat_id, proxy_name)
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    """UPDATE ghost_records SET ghost_count = ghost_count + 1, user_name = ?, last_ghosted_at = CURRENT_TIMESTAMP
                       WHERE chat_id = ? AND COALESCE(proxy_name, '') = COALESCE(?, '')""",
                    (user_name, chat_id, proxy_name)
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
        if db_type == 'postgresql':
            cursor.close()
        release_connection(conn)


def reset_ghost_count(chat_id: int, user_id: int, proxy_name: str = None) -> bool:
    """Reset ghost count to 0 for a user or proxy user (admin clear).
    
    For proxy users, pass user_id=-1 and the proxy_name.
    """
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
        release_connection(conn)


def get_ghost_leaderboard(chat_id: int) -> List[Dict]:
    """Return all users with ghost_count > 0 for a chat, sorted descending."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def get_user_ghost_count_by_name(chat_id: int, user_name: str) -> Optional[Dict]:
    """Find a ghost record by user_name or proxy_name for a chat (for admin /clear_absent by name)."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
        release_connection(conn)


def mark_rollcall_absent_done(rollcall_id: int) -> bool:
    """Mark a rollcall's absent selection as completed."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
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
    try:
        cursor = conn.cursor()
        if db_type == 'postgresql':
            cursor.execute(
                """SELECT r.id, r.title, r.ended_at
                   FROM rollcalls r
                   WHERE r.chat_id = %s
                     AND r.is_active = FALSE
                     AND r.absent_marked = FALSE
                     AND r.ended_at >= NOW() - INTERVAL '%s days'
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


def add_ghost_event(rollcall_id: int, chat_id: int, user_id: int = None, user_name: str = None, proxy_name: str = None) -> bool:
    """Record an individual ghost event for audit trail."""
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
        release_connection(conn)


def get_rollcall_in_users(rollcall_id: int) -> List[Dict]:
    """Return all users (real + proxy) with status='in' for a given rollcall.

    Real users (signed in via /in or the panel) have an integer ``user_id``.
    Proxy users (added via /sif) have ``user_id=None`` and a ``proxy_name`` key.
    """
    conn = get_connection()
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
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)


# Initialize database on import
init_db()
