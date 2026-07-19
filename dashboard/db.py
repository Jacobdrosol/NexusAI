"""Database initialisation and session management."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dashboard.models import Base, Connection

_DATA_DIR = Path(__file__).parent.parent / "data"
_DB_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{_DATA_DIR / 'nexusai.db'}",
)

engine = create_engine(
    _DB_URL,
    connect_args={"check_same_thread": False} if _DB_URL.startswith("sqlite") else {},
    echo=False,
)

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)

_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _create_all_tables() -> None:
    """Create all metadata tables with a tolerance for SQLite init races."""
    try:
        Base.metadata.create_all(bind=engine)
    except OperationalError as exc:
        # Multiple workers may race on first startup with SQLite.
        if "already exists" not in str(exc).lower():
            raise


def _migrate_connection_secrets() -> bool:
    """Encrypt legacy connection secrets once the connections table is available."""
    from dashboard.connections_service import normalize_auth_payload, normalize_connection_config

    if not inspect(engine).has_table(Connection.__tablename__):
        # A sibling process can still be creating tables after this process lost a
        # SQLite CREATE TABLE race. Leave initialization retryable in that case.
        return False

    session = SessionLocal()
    try:
        changed = False
        for row in session.query(Connection).all():
            try:
                config = json.loads(row.config_json or "{}")
            except Exception:
                config = {}
            try:
                auth = json.loads(row.auth_json or "{}")
            except Exception:
                auth = {}
            normalized_config = normalize_connection_config(config if isinstance(config, dict) else {})
            normalized_auth = normalize_auth_payload(auth if isinstance(auth, dict) else {})
            if normalized_config != config:
                row.config_json = json.dumps(normalized_config)
                changed = True
            if normalized_auth != auth:
                row.auth_json = json.dumps(normalized_auth)
                changed = True
        if changed:
            session.commit()
    finally:
        session.close()
    return True


def init_db() -> None:
    """Create all tables if they don't exist."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        _create_all_tables()
        if not _migrate_connection_secrets():
            return
        _INITIALIZED = True


def get_db() -> Session:
    """Return a new database session. Caller must close it."""
    return SessionLocal()
