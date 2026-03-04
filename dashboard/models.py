"""Database models and initialization for the NexusAI dashboard.

Manages SQLite tables: User, Worker, Settings.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH: Path = Path(__file__).parent.parent / "data" / "nexusai.db"


def get_db() -> sqlite3.Connection:
    """Return a SQLite connection with row_factory set to Row."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all required tables if they do not already exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS user (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                email     TEXT    NOT NULL UNIQUE,
                password  TEXT    NOT NULL,
                role      TEXT    NOT NULL DEFAULT 'admin',
                created_at DATETIME NOT NULL
            );

            CREATE TABLE IF NOT EXISTS worker (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                host      TEXT    NOT NULL,
                port      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at DATETIME NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def admin_exists() -> bool:
    """Return True if at least one admin user exists in the database."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM user WHERE role='admin' LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def create_user(email: str, hashed_password: str, role: str = "admin") -> None:
    """Insert a new user row into the database."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO user (email, password, role, created_at) VALUES (?, ?, ?, ?)",
            (email, hashed_password, role, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def create_worker(name: str, host: str, port: int) -> None:
    """Insert a new worker row into the database."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO worker (name, host, port) VALUES (?, ?, ?)",
            (name, host, port),
        )
        conn.commit()
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    """Upsert a key/value pair in the settings table."""
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return the value for *key* from the settings table, or *default*."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default
    except sqlite3.OperationalError:
        return default
    finally:
        conn.close()
