"""SQLAlchemy ORM models for NexusAI dashboard."""
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
