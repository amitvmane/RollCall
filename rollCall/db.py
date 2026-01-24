"""
Database layer for RollCall bot
Supports both PostgreSQL and SQLite
"""

import os
import json
import logging
from datetime import datetime
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_users (
                    id SERIAL PRIMARY KEY,
                    rollcall_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    comment TEXT,
                    proxy_owner_id BIGINT,  -- NEW: Telegram user_id of proxy creator
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id, name)
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
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS proxy_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rollcall_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    comment TEXT,
                    proxy_owner_id INTEGER,  -- NEW: Telegram user_id of proxy creator
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (rollcall_id) REFERENCES rollcalls(id) ON DELETE CASCADE,
                    UNIQUE(rollcall_id, name)
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
                    FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
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
        
        conn.commit()
        logging.debug("Database tables created successfully")
    except Exception as e:
        conn.rollback()
        logging.error(f"Error creating tables: {e}")
        raise
    finally:
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

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
            if db_type == 'postgresql':
                result = dict(row)
            else:
                result = dict(row)
        else:
            # Create new chat
            if db_type == 'postgresql':
                cursor.execute(
                    """INSERT INTO chats (chat_id, shh_mode, admin_rights, timezone)
                       VALUES (%s, %s, %s, %s) RETURNING *""",
                    (chat_id, False, False, 'Asia/Calcutta')
                )
                result = dict(cursor.fetchone())
            else:
                cursor.execute(
                    """INSERT INTO chats (chat_id, shh_mode, admin_rights, timezone)
                       VALUES (?, ?, ?, ?)""",
                    (chat_id, 0, 0, 'Asia/Calcutta')
                )
                result = {
                    'chat_id': chat_id,
                    'shh_mode': False,
                    'admin_rights': False,
                    'timezone': 'Asia/Calcutta'
                }
            
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

def add_or_update_user(rollcall_id: int, user_id: int, first_name: str, username: str, status: str, comment: str = '') -> bool:
    """
        Insert or update a regular user row and maintain per-state positions.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()

        # Determine positions based on new status
        in_pos = out_pos = wait_pos = None
        if status == "in":
            in_pos = get_next_position(rollcall_id, "in")
        elif status == "out":
            out_pos = get_next_position(rollcall_id, "out")
        elif status == "waitlist":
            wait_pos = get_next_position(rollcall_id, "waitlist")

        if db_type == "postgresql":
            cursor.execute(
                """
                INSERT INTO users (
                    rollcall_id, user_id, first_name, username, status, comment,
                    in_pos, out_pos, wait_pos
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (rollcall_id, user_id)
                DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    username   = EXCLUDED.username,
                    status     = EXCLUDED.status,
                    comment    = EXCLUDED.comment,
                    in_pos     = EXCLUDED.in_pos,
                    out_pos    = EXCLUDED.out_pos,
                    wait_pos   = EXCLUDED.wait_pos,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    rollcall_id,
                    user_id,
                    first_name,
                    username,
                    status,
                    comment,
                    in_pos,
                    out_pos,
                    wait_pos,
                ),
            )
        else:
            # SQLite with UPSERT syntax
            cursor.execute(
                """
                INSERT INTO users (
                    rollcall_id, user_id, first_name, username, status, comment,
                    in_pos, out_pos, wait_pos
                )
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
                """,
                (
                    rollcall_id,
                    user_id,
                    first_name,
                    username,
                    status,
                    comment,
                    in_pos,
                    out_pos,
                    wait_pos,
                ),
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Error add/update user: {e}")
        raise
    finally:
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)

def add_or_update_proxy_user(rollcall_id: int, name: str, status: str, comment: str = '', proxy_owner_id: Optional[int] = None) -> bool:
    """Add or update a proxy user"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        if db_type == 'postgresql':
            cursor.execute(
                """
                INSERT INTO proxy_users (rollcall_id, name, status, comment, proxy_owner_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (rollcall_id, name)
                DO UPDATE SET status = EXCLUDED.status,
                            comment = EXCLUDED.comment,
                            proxy_owner_id = EXCLUDED.proxy_owner_id,
                            updated_at = CURRENT_TIMESTAMP
                """,
                (rollcall_id, name, status, comment, proxy_owner_id)
            )
        else:
            cursor.execute(
                """
                INSERT OR REPLACE INTO proxy_users (rollcall_id, name, status, comment, proxy_owner_id, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (rollcall_id, name, status, comment, proxy_owner_id)
            )
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logging.error(f"Error adding/updating proxy user: {e}")
        return False
    finally:
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def get_all_users(rollcall_id: int):
    """
    Get all regular users for a rollcall.

    Ordering:
    - Grouped by status (in, out, maybe, waitlist) for convenience.
    - Within IN/OUT/WAITING, ordered by their per-state position.
    - For MAYBE (no positions), fall back to created_at.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if db_type == "postgresql":
            cursor.execute(
                """
                SELECT
                    id,
                    rollcall_id,
                    user_id,
                    first_name,
                    username,
                    status,
                    comment,
                    in_pos,
                    out_pos,
                    wait_pos,
                    created_at,
                    updated_at
                FROM users
                WHERE rollcall_id = %s
                ORDER BY
                    CASE status
                        WHEN 'in' THEN 1
                        WHEN 'out' THEN 2
                        WHEN 'maybe' THEN 3
                        WHEN 'waitlist' THEN 4
                        ELSE 5
                    END,
                    CASE status
                        WHEN 'in' THEN COALESCE(in_pos, 0)
                        WHEN 'out' THEN COALESCE(out_pos, 0)
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
                SELECT
                    id,
                    rollcall_id,
                    user_id,
                    first_name,
                    username,
                    status,
                    comment,
                    in_pos,
                    out_pos,
                    wait_pos,
                    created_at,
                    updated_at
                FROM users
                WHERE rollcall_id = ?
                ORDER BY
                    CASE status
                        WHEN 'in' THEN 1
                        WHEN 'out' THEN 2
                        WHEN 'maybe' THEN 3
                        WHEN 'waitlist' THEN 4
                        ELSE 5
                    END,
                    CASE status
                        WHEN 'in' THEN COALESCE(in_pos, 0)
                        WHEN 'out' THEN COALESCE(out_pos, 0)
                        WHEN 'waitlist' THEN COALESCE(wait_pos, 0)
                        ELSE 0
                    END,
                    created_at ASC
                """,
                (rollcall_id,),
            )
        rows = cursor.fetchall()
        if db_type == "postgresql":
            return [dict(r) for r in rows]
        else:
            return [dict(r) for r in rows]
    finally:
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)

def get_proxy_users_by_status(rollcall_id: int, status: str) -> List[Dict]:
    """Get proxy users by status"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        if db_type == 'postgresql':
            cursor.execute(
                "SELECT * FROM proxy_users WHERE rollcall_id = %s AND status = %s ORDER BY created_at ASC",
                (rollcall_id, status)
            )
        else:
            cursor.execute(
                "SELECT * FROM proxy_users WHERE rollcall_id = ? AND status = ? ORDER BY created_at ASC",
                (rollcall_id, status)
            )
        
        rows = cursor.fetchall()
        result = []
        for row in rows:
            result.append(dict(row))
        
        return result
    except Exception as e:
        logging.error(f"Error getting proxy users: {e}")
        return []
    finally:
        if db_type == 'postgresql':
            cursor.close()
            release_connection(conn)

def delete_user_by_name(rollcall_id: int, name: str) -> bool:
    """Delete a user by name (supports both regular and proxy users)"""
    conn = get_connection()
    try:
        cursor = conn.cursor()
        
        # Try to delete from users table (by first_name)
        if db_type == 'postgresql':
            cursor.execute(
                "DELETE FROM users WHERE rollcall_id = %s AND first_name = %s",
                (rollcall_id, name)
            )
        else:
            cursor.execute(
                "DELETE FROM users WHERE rollcall_id = ? AND first_name = ?",
                (rollcall_id, name)
            )
        
        rows_deleted = cursor.rowcount
        
        # If no rows deleted, try proxy_users table
        if rows_deleted == 0:
            if db_type == 'postgresql':
                cursor.execute(
                    "DELETE FROM proxy_users WHERE rollcall_id = %s AND name = %s",
                    (rollcall_id, name)
                )
            else:
                cursor.execute(
                    "DELETE FROM proxy_users WHERE rollcall_id = ? AND name = ?",
                    (rollcall_id, name)
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
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if db_type == 'postgresql':
            cursor.execute(
                """
                INSERT INTO user_stats (chat_id, user_id, {field})
                VALUES (%s, %s, 1)
                ON CONFLICT (chat_id, user_id)
                DO UPDATE SET {field} = user_stats.{field} + 1,
                              updated_at = CURRENT_TIMESTAMP
                """.format(field=field),
                (chat_id, user_id),
            )
        else:
            cursor.execute(
                f"""
                INSERT OR IGNORE INTO user_stats (chat_id, user_id, {field})
                VALUES (?, ?, 0)
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
    conn = get_connection()
    try:
        cursor = conn.cursor()

        if db_type == 'postgresql':
            cursor.execute(
                """
                INSERT INTO rollcall_stats (rollcall_id, {field})
                VALUES (%s, 1)
                ON CONFLICT (rollcall_id)
                DO UPDATE SET {field} = rollcall_stats.{field} + 1,
                              updated_at = CURRENT_TIMESTAMP
                """.format(field=field),
                (rollcall_id,),
            )
        else:
            cursor.execute(
                f"""
                INSERT OR IGNORE INTO rollcall_stats (rollcall_id, {field})
                VALUES (?, 0)
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
    """
    Return next position index for given status in users table.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        if status == "in":
            col = "in_pos"
        elif status == "out":
            col = "out_pos"
        elif status == "waitlist":
            col = "wait_pos"
        else:
            return 0  # maybe or unknown, no ordering

        if db_type == "postgresql":
            cursor.execute(
                f"SELECT COALESCE(MAX({col}), 0) FROM users WHERE rollcall_id = %s AND status = %s",
                (rollcall_id, status),
            )
        else:
            cursor.execute(
                f"SELECT COALESCE(MAX({col}), 0) FROM users WHERE rollcall_id = ? AND status = ?",
                (rollcall_id, status),
            )
        row = cursor.fetchone()
        max_pos = row[0] if row else 0
        return int(max_pos) + 1
    finally:
        if db_type == "postgresql":
            cursor.close()
            release_connection(conn)


# Initialize database on import
init_db()
