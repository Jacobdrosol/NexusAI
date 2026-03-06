"""Database initialisation and session management."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dashboard.models import Base

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
        _INITIALIZED = True


def get_db() -> Session:
    """Return a new database session. Caller must close it."""
    return SessionLocal()
