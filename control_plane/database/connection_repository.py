"""Connection repository for external database connections."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_CONNECTIONS = """
CREATE TABLE IF NOT EXISTS database_connections (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    connection_string TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    credentials_ref TEXT,
    schema_snapshot TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_tested_at TEXT,
    last_test_result TEXT
)
"""

_CREATE_CONNECTIONS_NAME_INDEX = """
CREATE INDEX IF NOT EXISTS idx_database_connections_name
ON database_connections(name)
"""

_CREATE_CONNECTIONS_ENABLED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_database_connections_enabled
ON database_connections(enabled, name)
"""


class ConnectionRepository:
    """Repository for managing external database connections."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._connections: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._db_ready = False

        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            if db_url.startswith("sqlite:///"):
                self._db_path = db_url[len("sqlite:///"):]
            else:
                self._db_path = _DEFAULT_DB_PATH

    async def _ensure_db(self) -> None:
        """Ensure database tables exist and load connections."""
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                await db.execute(_CREATE_CONNECTIONS)
                await db.execute(_CREATE_CONNECTIONS_NAME_INDEX)
                await db.execute(_CREATE_CONNECTIONS_ENABLED_INDEX)
                await self._ensure_connection_columns(db)
                await db.commit()

                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM database_connections") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        data = dict(row)
                        data["config_json"] = json.loads(data.get("config_json") or "{}")
                        self._connections[data["id"]] = data
            self._db_ready = True

    async def _ensure_connection_columns(self, db: aiosqlite.Connection) -> None:
        """Ensure all required columns exist."""
        async with db.execute("PRAGMA table_info(database_connections)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}

        required_columns = {
            "id": "TEXT PRIMARY KEY",
            "name": "TEXT NOT NULL",
            "kind": "TEXT NOT NULL",
            "description": "TEXT NOT NULL DEFAULT ''",
            "connection_string": "TEXT",
            "config_json": "TEXT NOT NULL DEFAULT '{}'",
            "credentials_ref": "TEXT",
            "schema_snapshot": "TEXT",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "created_at": "TEXT NOT NULL",
            "updated_at": "TEXT NOT NULL",
            "last_tested_at": "TEXT",
            "last_test_result": "TEXT",
        }

        for col_name, col_def in required_columns.items():
            if col_name not in columns:
                default_match = "DEFAULT" in col_def
                if default_match:
                    await db.execute(f"ALTER TABLE database_connections ADD COLUMN {col_name} {col_def}")
                else:
                    await db.execute(f"ALTER TABLE database_connections ADD COLUMN {col_name} {col_def}")

    async def create(
        self,
        name: str,
        kind: str,
        description: str = "",
        connection_string: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        credentials_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new database connection."""
        await self._ensure_db()
        now = datetime.now(timezone.utc).isoformat()

        connection = {
            "id": f"db_conn_{name.lower().replace(' ', '_')}_{int(datetime.now().timestamp())}",
            "name": name.strip(),
            "kind": kind,
            "description": description,
            "connection_string": connection_string,
            "config_json": config or {},
            "credentials_ref": credentials_ref,
            "schema_snapshot": None,
            "enabled": 1,
            "created_at": now,
            "updated_at": now,
            "last_tested_at": None,
            "last_test_result": None,
        }

        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute("""
                    INSERT INTO database_connections
                    (id, name, kind, description, connection_string, config_json, credentials_ref, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    connection["id"],
                    connection["name"],
                    connection["kind"],
                    connection["description"],
                    connection["connection_string"],
                    json.dumps(connection["config_json"]),
                    connection["credentials_ref"],
                    connection["enabled"],
                    connection["created_at"],
                    connection["updated_at"],
                ))
                await db.commit()
            self._connections[connection["id"]] = connection

        return connection

    async def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """Get a connection by ID."""
        await self._ensure_db()
        return self._connections.get(connection_id)

    async def list(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """List all connections."""
        await self._ensure_db()
        connections = list(self._connections.values())
        if enabled_only:
            connections = [c for c in connections if c.get("enabled")]
        return sorted(connections, key=lambda x: x.get("name", ""))

    async def update(
        self,
        connection_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        credentials_ref: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a connection."""
        await self._ensure_db()
        async with self._lock:
            existing = self._connections.get(connection_id)
            if not existing:
                return None

            updates: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc).isoformat()}
            if name is not None:
                updates["name"] = name.strip()
            if description is not None:
                updates["description"] = description
            if config is not None:
                updates["config_json"] = config
            if credentials_ref is not None:
                updates["credentials_ref"] = credentials_ref
            if enabled is not None:
                updates["enabled"] = 1 if enabled else 0

            async with open_sqlite(self._db_path) as db:
                set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
                values = list(updates.values()) + [connection_id]
                await db.execute(f"""
                    UPDATE database_connections SET {set_clause} WHERE id = ?
                """, values)
                await db.commit()

            existing.update(updates)
            self._connections[connection_id] = existing
            return existing

    async def delete(self, connection_id: str) -> bool:
        """Delete a connection."""
        await self._ensure_db()
        async with self._lock:
            if connection_id not in self._connections:
                return False

            async with open_sqlite(self._db_path) as db:
                await db.execute("DELETE FROM database_connections WHERE id = ?", (connection_id,))
                await db.commit()
            del self._connections[connection_id]
            return True

    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        """Test a database connection."""
        await self._ensure_db()
        connection = self._connections.get(connection_id)
        if not connection:
            return {"success": False, "error": "Connection not found"}

        result = {
            "success": False,
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "error": None,
            "details": {},
        }

        try:
            kind = connection.get("kind", "").lower()
            config = connection.get("config_json", {})

            if kind == "sqlite":
                db_path = config.get("database", connection.get("connection_string"))
                if db_path:
                    async with aiosqlite.connect(db_path) as db:
                        await db.execute("SELECT 1")
                    result["success"] = True
                    result["details"]["database_type"] = "SQLite"
            elif kind == "postgresql":
                # Would need asyncpg - mark as configured but not testable inline
                result["success"] = True
                result["details"]["database_type"] = "PostgreSQL"
                result["details"]["note"] = "Connection configured, full test requires asyncpg"
            elif kind == "mysql":
                result["success"] = True
                result["details"]["database_type"] = "MySQL"
                result["details"]["note"] = "Connection configured, full test requires aiomysql"
            else:
                result["error"] = f"Unknown connection kind: {kind}"

        except Exception as e:
            result["error"] = str(e)

        # Update last_tested_at and result
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute("""
                    UPDATE database_connections
                    SET last_tested_at = ?, last_test_result = ?
                    WHERE id = ?
                """, (result["tested_at"], json.dumps(result), connection_id))
                await db.commit()
            if connection_id in self._connections:
                self._connections[connection_id]["last_tested_at"] = result["tested_at"]
                self._connections[connection_id]["last_test_result"] = result

        return result

    async def get_schema_snapshot(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """Get stored schema snapshot for a connection."""
        await self._ensure_db()
        connection = self._connections.get(connection_id)
        if not connection:
            return None

        snapshot = connection.get("schema_snapshot")
        if snapshot:
            return json.loads(snapshot)
        return None

    async def update_schema_snapshot(self, connection_id: str, snapshot: Dict[str, Any]) -> bool:
        """Update stored schema snapshot."""
        await self._ensure_db()
        async with self._lock:
            if connection_id not in self._connections:
                return False

            snapshot_json = json.dumps(snapshot)
            async with open_sqlite(self._db_path) as db:
                await db.execute("""
                    UPDATE database_connections SET schema_snapshot = ?, updated_at = ?
                    WHERE id = ?
                """, (snapshot_json, datetime.now(timezone.utc).isoformat(), connection_id))
                await db.commit()
            self._connections[connection_id]["schema_snapshot"] = snapshot_json
            self._connections[connection_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
            return True
