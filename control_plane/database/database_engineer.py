"""Database Engineer service for safe schema operations."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from control_plane.database.schema_manager import (
    SchemaManager,
    MigrationPlan,
    MigrationResult,
    ColumnDefinition,
    TableDefinition,
)
from control_plane.database.connection_repository import ConnectionRepository
from control_plane.sqlite_helpers import open_sqlite

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")


class DatabaseEngineer:
    """
    Database engineer service for managing schema changes and external connections.

    This service provides:
    - Safe, additive-only schema migrations
    - External database connection management
    - Schema introspection and validation
    - Migration history tracking
    """

    # SQL statement types that are considered safe (read-only or additive)
    SAFE_STATEMENTS = frozenset({"SELECT", "PRAGMA", "INSERT", "UPDATE", "DELETE"})

    # Statement types that require explicit approval
    DANGEROUS_STATEMENTS = frozenset({"DROP", "TRUNCATE", "RENAME", "DELETE FROM"})

    # Allowed additive operations
    ALLOWED_OPERATIONS = frozenset({
        "CREATE TABLE",
        "ALTER TABLE",
        "CREATE INDEX",
        "INSERT INTO",
        "UPDATE",
        "SELECT",
        "PRAGMA",
    })

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path if db_path else _DEFAULT_DB_PATH
        self._schema_manager = SchemaManager(db_path=self._db_path)
        self._connection_repo = ConnectionRepository(db_path=self._db_path)
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the database engineer service."""
        await self._schema_manager._ensure_db()
        await self._connection_repo._ensure_db()
        logger.info("Database engineer service initialized")

    # -------------------------------------------------------------------------
    # Schema Introspection
    # -------------------------------------------------------------------------

    async def get_schema(self) -> Dict[str, Any]:
        """Get current database schema structure."""
        schema = await self._schema_manager.get_current_schema()
        return {"tables": list(schema.keys()), "columns_by_table": {k: list(v) for k, v in schema.items()}}

    async def table_exists(self, table_name: str) -> bool:
        """Check if a table exists."""
        return await self._schema_manager.table_exists(table_name)

    async def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table."""
        return await self._schema_manager.column_exists(table_name, column_name)

    async def get_table_info(self, table_name: str) -> List[Dict[str, Any]]:
        """Get column information for a table."""
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    # -------------------------------------------------------------------------
    # Migration Planning and Execution
    # -------------------------------------------------------------------------

    async def plan_migration(
        self,
        plan_id: str,
        description: str,
        tables: Optional[List[TableDefinition]] = None,
        columns: Optional[Dict[str, List[ColumnDefinition]]] = None,
        indexes: Optional[List[str]] = None,
    ) -> MigrationPlan:
        """
        Create a migration plan for additive schema changes.

        Args:
            plan_id: Unique identifier for this migration
            description: Human-readable description of changes
            tables: List of new tables to create
            columns: Dict of table_name -> list of columns to add
            indexes: List of CREATE INDEX statements

        Returns:
            MigrationPlan with validation results
        """
        return await self._schema_manager.create_migration_plan(
            plan_id=plan_id,
            description=description,
            tables=tables,
            columns=columns,
            indexes=indexes,
        )

    async def apply_migration(self, plan: MigrationPlan) -> MigrationResult:
        """
        Apply a validated migration plan.

        Args:
            plan: A validated MigrationPlan

        Returns:
            MigrationResult with success status and details
        """
        if not plan.is_safe:
            return MigrationResult(
                success=False,
                migration_id=plan.id,
                errors=plan.validation_errors,
            )

        result = await self._schema_manager.apply_migration(plan)

        if result.success:
            logger.info("Migration %s applied successfully: %s", plan.id, result.changes_applied)
        else:
            logger.error("Migration %s failed: %s", plan.id, result.errors)

        return result

    async def get_migration_history(self) -> List[Dict[str, Any]]:
        """Get history of applied migrations."""
        return await self._schema_manager.get_migration_history()

    # -------------------------------------------------------------------------
    # Schema Change Helpers
    # -------------------------------------------------------------------------

    async def add_column_if_not_exists(
        self,
        table_name: str,
        column_name: str,
        column_type: str,
        nullable: bool = True,
        default: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Add a column to a table if it doesn't exist.

        Returns:
            (success, message) tuple
        """
        if await self._schema_manager.column_exists(table_name, column_name):
            return False, f"Column '{column_name}' already exists in '{table_name}'"

        plan = await self.plan_migration(
            plan_id=f"add_{table_name}_{column_name}_{int(datetime.now().timestamp())}",
            description=f"Add column {column_name} to {table_name}",
            columns={table_name: [ColumnDefinition(
                name=column_name,
                type=column_type,
                nullable=nullable,
                default=default,
            )]},
        )

        result = await self.apply_migration(plan)
        if result.success:
            return True, f"Added column '{column_name}' to '{table_name}'"
        return False, result.errors[0] if result.errors else "Unknown error"

    async def create_table_if_not_exists(
        self,
        table_name: str,
        columns: List[ColumnDefinition],
        indexes: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """
        Create a table if it doesn't exist.

        Returns:
            (success, message) tuple
        """
        if await self._schema_manager.table_exists(table_name):
            return False, f"Table '{table_name}' already exists"

        plan = await self.plan_migration(
            plan_id=f"create_{table_name}_{int(datetime.now().timestamp())}",
            description=f"Create table {table_name}",
            tables=[TableDefinition(name=table_name, columns=columns, indexes=indexes or [])],
        )

        result = await self.apply_migration(plan)
        if result.success:
            return True, f"Created table '{table_name}'"
        return False, result.errors[0] if result.errors else "Unknown error"

    # -------------------------------------------------------------------------
    # SQL Validation and Execution
    # -------------------------------------------------------------------------

    def validate_sql_statement(self, sql: str) -> Tuple[bool, List[str]]:
        """
        Validate a SQL statement for safety.

        Returns:
            (is_safe, errors) tuple
        """
        sql_upper = sql.strip().upper()
        errors: List[str] = []

        # Check for dangerous patterns
        for dangerous in self.DANGEROUS_STATEMENTS:
            if dangerous in sql_upper:
                errors.append(f"Dangerous pattern detected: {dangerous}")

        # Check if statement starts with allowed operation
        is_allowed = any(sql_upper.startswith(op) for op in self.ALLOWED_OPERATIONS)

        if not is_allowed and sql_upper:
            errors.append(f"Statement type not in allowed operations: {self.ALLOWED_OPERATIONS}")

        # Additional pattern checks
        if "DROP TABLE" in sql_upper or "DROP INDEX" in sql_upper:
            errors.append("DROP operations are not allowed")

        if "DETACH" in sql_upper:
            errors.append("DETACH operations are not allowed")

        return len(errors) == 0, errors

    async def execute_safe_sql(
        self,
        sql: str,
        params: Optional[tuple] = None,
        require_approval: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute a safe SQL statement.

        Args:
            sql: SQL statement to execute
            params: Optional parameters for prepared statement
            require_approval: If True, only additive operations allowed

        Returns:
            Result dict with success status and data/errors
        """
        is_safe, errors = self.validate_sql_statement(sql)

        if not is_safe:
            return {
                "success": False,
                "error": "Statement failed safety validation",
                "validation_errors": errors,
            }

        try:
            async with self._lock:
                async with open_sqlite(self._db_path) as db:
                    db.row_factory = aiosqlite.Row
                    if params:
                        async with db.execute(sql, params) as cursor:
                            rows = await cursor.fetchall()
                    else:
                        async with db.execute(sql) as cursor:
                            rows = await cursor.fetchall()

                    # Check if it's a SELECT-like statement returning data
                    if sql.strip().upper().startswith(("SELECT", "PRAGMA")):
                        return {
                            "success": True,
                            "data": [dict(row) for row in rows],
                            "row_count": len(rows),
                        }
                    else:
                        await db.commit()
                        return {
                            "success": True,
                            "rows_affected": db.total_changes if hasattr(db, "total_changes") else 0,
                        }

        except Exception as e:
            logger.error("SQL execution failed: %s", e)
            return {
                "success": False,
                "error": str(e),
            }

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------

    async def create_connection(
        self,
        name: str,
        kind: str,
        description: str = "",
        connection_string: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        credentials_ref: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new database connection."""
        return await self._connection_repo.create(
            name=name,
            kind=kind,
            description=description,
            connection_string=connection_string,
            config=config,
            credentials_ref=credentials_ref,
        )

    async def get_connection(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """Get a connection by ID."""
        return await self._connection_repo.get(connection_id)

    async def list_connections(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """List database connections."""
        return await self._connection_repo.list(enabled_only=enabled_only)

    async def update_connection(
        self,
        connection_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        credentials_ref: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a database connection."""
        return await self._connection_repo.update(
            connection_id=connection_id,
            name=name,
            description=description,
            config=config,
            credentials_ref=credentials_ref,
            enabled=enabled,
        )

    async def delete_connection(self, connection_id: str) -> bool:
        """Delete a database connection."""
        return await self._connection_repo.delete(connection_id)

    async def test_connection(self, connection_id: str) -> Dict[str, Any]:
        """Test a database connection."""
        return await self._connection_repo.test_connection(connection_id)

    # -------------------------------------------------------------------------
    # Schema Snapshot and Comparison
    # -------------------------------------------------------------------------

    async def capture_schema_snapshot(self) -> Dict[str, Any]:
        """Capture current database schema as a snapshot."""
        schema = await self._schema_manager.get_current_schema()
        snapshot = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "tables": {},
        }

        for table_name in schema:
            columns = await self.get_table_info(table_name)
            snapshot["tables"][table_name] = {
                "columns": columns,
                "column_count": len(columns),
            }

        return snapshot

    async def compare_schemas(
        self,
        snapshot1: Dict[str, Any],
        snapshot2: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compare two schema snapshots."""
        tables1 = set(snapshot1.get("tables", {}).keys())
        tables2 = set(snapshot2.get("tables", {}).keys())

        return {
            "added_tables": list(tables2 - tables1),
            "removed_tables": list(tables1 - tables2),
            "common_tables": list(tables1 & tables2),
        }

    # -------------------------------------------------------------------------
    # Configuration Validation
    # -------------------------------------------------------------------------

    def validate_database_config(self, config: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        Validate database configuration.

        Returns:
            (is_valid, errors) tuple
        """
        errors: List[str] = []

        # Check for required fields based on connection kind
        kind = config.get("kind", "").lower()

        if kind == "sqlite":
            if "database" not in config and "path" not in config:
                errors.append("SQLite connection requires 'database' or 'path'")
        elif kind in ("postgresql", "postgres"):
            required = ["host", "database"]
            for field in required:
                if field not in config:
                    errors.append(f"PostgreSQL connection requires '{field}'")
        elif kind == "mysql":
            required = ["host", "database"]
            for field in required:
                if field not in config:
                    errors.append(f"MySQL connection requires '{field}'")

        # Validate port if provided
        if "port" in config:
            try:
                port = int(config["port"])
                if port < 1 or port > 65535:
                    errors.append("Port must be between 1 and 65535")
            except (ValueError, TypeError):
                errors.append("Port must be a valid integer")

        return len(errors) == 0, errors
