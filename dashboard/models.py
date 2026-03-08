"""SQLAlchemy ORM models and onboarding helpers for NexusAI dashboard.

ORM models: User, Worker, Bot, Task, Setting.
Onboarding helpers: init_db, admin_exists, create_user, create_worker,
set_setting, get_setting.
"""
import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


class User(Base):
    """Dashboard user account."""
    __tablename__ = "users"

    id: Mapped[int] = Column(Integer, primary_key=True)
    email: Mapped[str] = Column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = Column(String(255), nullable=False)
    role: Mapped[str] = Column(String(50), nullable=False, default="user")
    is_active: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    def get_id(self) -> str:
        """Return string ID for flask-login."""
        return str(self.id)

    @property
    def is_authenticated(self) -> bool:
        """Return True if user is authenticated."""
        return True

    @property
    def is_anonymous(self) -> bool:
        """Return False; anonymous users are not supported."""
        return False


class Worker(Base):
    """Registered compute worker."""
    __tablename__ = "workers"

    id: Mapped[int] = Column(Integer, primary_key=True)
    name: Mapped[str] = Column(String(255), nullable=False)
    host: Mapped[str] = Column(String(255), nullable=False)
    port: Mapped[int] = Column(Integer, nullable=False, default=8001)
    status: Mapped[str] = Column(String(50), nullable=False, default="offline")
    capabilities: Mapped[str] = Column(Text, nullable=False, default="[]")
    metrics: Mapped[str] = Column(Text, nullable=False, default="{}")
    enabled: Mapped[bool] = Column(Boolean, nullable=False, default=True)

    def capabilities_as_dict(self) -> Any:
        """Parse capabilities JSON."""
        try:
            return json.loads(self.capabilities)
        except (json.JSONDecodeError, TypeError):
            return []

    def metrics_as_dict(self) -> Any:
        """Parse metrics JSON."""
        try:
            return json.loads(self.metrics)
        except (json.JSONDecodeError, TypeError):
            return {}


class Bot(Base):
    """Configured AI bot."""
    __tablename__ = "bots"

    id: Mapped[int] = Column(Integer, primary_key=True)
    name: Mapped[str] = Column(String(255), nullable=False)
    role: Mapped[str] = Column(String(255), nullable=False, default="")
    priority: Mapped[int] = Column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    backends: Mapped[str] = Column(Text, nullable=False, default="[]")
    routing_rules: Mapped[str] = Column(Text, nullable=False, default="{}")

    def backends_as_list(self) -> Any:
        """Parse backends JSON."""
        try:
            return json.loads(self.backends)
        except (json.JSONDecodeError, TypeError):
            return []


class Connection(Base):
    """Reusable external connection definition (HTTP/API or database)."""
    __tablename__ = "connections"

    id: Mapped[int] = Column(Integer, primary_key=True)
    name: Mapped[str] = Column(String(255), nullable=False)
    kind: Mapped[str] = Column(String(50), nullable=False, default="http")
    description: Mapped[str] = Column(Text, nullable=False, default="")
    config_json: Mapped[str] = Column(Text, nullable=False, default="{}")
    auth_json: Mapped[str] = Column(Text, nullable=False, default="{}")
    schema_text: Mapped[str] = Column(Text, nullable=False, default="")
    enabled: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class BotConnection(Base):
    """Many-to-many map of bot references to connection IDs."""
    __tablename__ = "bot_connections"

    id: Mapped[int] = Column(Integer, primary_key=True)
    bot_ref: Mapped[str] = Column(String(255), nullable=False)
    connection_id: Mapped[int] = Column(Integer, nullable=False)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ProjectConnection(Base):
    """Many-to-many map of project references to connection IDs."""
    __tablename__ = "project_connections"

    id: Mapped[int] = Column(Integer, primary_key=True)
    project_ref: Mapped[str] = Column(String(255), nullable=False)
    connection_id: Mapped[int] = Column(Integer, nullable=False)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Task(Base):
    """Dispatched task record."""
    __tablename__ = "tasks"

    id: Mapped[int] = Column(Integer, primary_key=True)
    bot_id: Mapped[int] = Column(Integer, nullable=False)
    payload: Mapped[str] = Column(Text, nullable=False, default="{}")
    metadata_json: Mapped[str] = Column(Text, nullable=False, default="{}")
    status: Mapped[str] = Column(String(50), nullable=False, default="queued")
    result: Mapped[Optional[str]] = Column(Text, nullable=True)
    error: Mapped[Optional[str]] = Column(Text, nullable=True)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def payload_as_dict(self) -> Any:
        """Parse payload JSON."""
        try:
            return json.loads(self.payload)
        except (json.JSONDecodeError, TypeError):
            return {}


class Setting(Base):
    """Key-value configuration store (used by the onboarding wizard)."""
    __tablename__ = "settings"

    key: Mapped[str] = Column(String(255), primary_key=True)
    value: Mapped[Optional[str]] = Column(Text, nullable=True)
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Onboarding helper functions (used by the first-run wizard)
# Lazy imports of dashboard.db avoid a circular-import with db.py which
# itself imports Base from this module.
# ---------------------------------------------------------------------------


def init_db() -> None:
    """Create all required tables if they do not already exist."""
    from dashboard.db import init_db as _db_init
    _db_init()


def admin_exists() -> bool:
    """Return True if at least one admin user exists in the database."""
    from dashboard.db import get_db
    db = get_db()
    try:
        return db.query(User).filter_by(role="admin").first() is not None
    except Exception:
        return False
    finally:
        db.close()


def create_user(email: str, hashed_password: str, role: str = "admin") -> None:
    """Insert a new user row into the database."""
    from dashboard.db import get_db
    db = get_db()
    try:
        normalized_email = (email or "").strip().lower()
        db.add(User(
            email=normalized_email,
            password_hash=hashed_password,
            role=role,
            is_active=True,
            created_at=datetime.now(timezone.utc),
        ))
        db.commit()
    finally:
        db.close()


def create_worker(name: str, host: str, port: int) -> None:
    """Insert a new worker row into the database."""
    from dashboard.db import get_db
    db = get_db()
    try:
        db.add(Worker(name=name, host=host, port=port))
        db.commit()
    finally:
        db.close()


def set_setting(key: str, value: str) -> None:
    """Upsert a key/value pair in the settings table."""
    from dashboard.db import get_db
    db = get_db()
    try:
        row = db.query(Setting).filter_by(key=key).first()
        if row:
            row.value = value
            row.updated_at = datetime.now(timezone.utc)
        else:
            db.add(Setting(key=key, value=value, updated_at=datetime.now(timezone.utc)))
        db.commit()
    finally:
        db.close()


def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return the value for *key* from the settings table, or *default*."""
    from dashboard.db import get_db
    db = get_db()
    try:
        row = db.query(Setting).filter_by(key=key).first()
        return row.value if row else default
    except Exception:
        return default
    finally:
        db.close()
