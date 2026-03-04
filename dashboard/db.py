"""Database initialisation and session management."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
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


def init_db() -> None:
    """Create all tables if they don't exist."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    """Return a new database session. Caller must close it."""
    return SessionLocal()
