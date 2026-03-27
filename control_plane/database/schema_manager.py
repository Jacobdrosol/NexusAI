"""Schema manager for safe database migrations."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")


@dataclass
class ColumnDefinition:
    """Definition of a table column."""
    name: str
    type: str
    nullable: bool = True
    default: Optional[str] = None
    primary_key: bool = False
    unique: bool = False
    references: Optional[str] = None


@dataclass
class TableDefinition:
    """Definition of a database table."""
    name: str
    columns: List[ColumnDefinition] = field(default_factory=list)
    indexes: List[str] = field(default_factory=list)


@dataclass
class MigrationPlan:
    """A planned migration with additive changes only."""
    id: str
    description: str
    tables_to_create: List[TableDefinition] = field(default_factory=list)
    columns_to_add: Dict[str, List[ColumnDefinition]] = field(default_factory=dict)
    indexes_to_create: List[str] = field(default_factory=list)
    is_safe: bool = True
    validation_errors: List[str] = field(default_factory=list)

    def add_table(self, table: TableDefinition) -> None:
        """Add a table to be created."""
        self.tables_to_create.append(table)

    def add_column(self, table_name: str, column: ColumnDefinition) -> None:
        """Add a column to an existing table."""
        if table_name not in self.columns_to_add:
            self.columns_to_add[table_name] = []
        self.columns_to_add[table_name].append(column)

    def add_index(self, index_sql: str) -> None:
        """Add an index to be created."""
        self.indexes_to_create.append(index_sql)


@dataclass
class MigrationResult:
    """Result of a migration execution."""
    success: bool
    migration_id: str
    changes_applied: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class SchemaManager:
    """Manages database schema introspection and additive migrations."""

    # Safe statement types for database engineer operations
    SAFE_STATEMENTS = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    DANGEROUS_STATEMENTS = {"DROP", "TRUNCATE", "RENAME", "ALTER TABLE ... DROP"}

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path if db_path else _DEFAULT_DB_PATH
        self._lock = asyncio.Lock()
        self._schema_cache: Dict[str, Set[str]] = {}
        self._migration_history: List[str] = []

    async def _ensure_db(self) -> None:
        """Ensure the schema tracking table exists."""
        async with open_sqlite(self._db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    checksum TEXT
                )
            """)
            await db.commit()

    async def get_current_schema(self) -> Dict[str, Set[str]]:
        """Get current table/column structure."""
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ) as cursor:
                tables = [row["name"] for row in await cursor.fetchall()]

            schema: Dict[str, Set[str]] = {}
            for table in tables:
                async with db.execute(f"PRAGMA table_info({table})") as cursor:
                    columns = {row["name"] for row in await cursor.fetchall()}
                    schema[table] = columns

            self._schema_cache = schema
            return schema

    async def table_exists(self, table_name: str) -> bool:
        """Check if a table exists."""
        if not self._schema_cache:
            await self.get_current_schema()
        return table_name in self._schema_cache

    async def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table."""
        if not self._schema_cache:
            await self.get_current_schema()
        return column_name in self._schema_cache.get(table_name, set())

    async def create_migration_plan(
        self,
        plan_id: str,
        description: str,
        tables: Optional[List[TableDefinition]] = None,
        columns: Optional[Dict[str, List[ColumnDefinition]]] = None,
        indexes: Optional[List[str]] = None,
    ) -> MigrationPlan:
        """Create and validate a migration plan."""
        await self.get_current_schema()

        plan = MigrationPlan(id=plan_id, description=description)

        validation_errors: List[str] = []

        # Validate tables to create
        for table in (tables or []):
            if await self.table_exists(table.name):
                validation_errors.append(f"Table '{table.name}' already exists")
                plan.is_safe = False
            else:
                plan.add_table(table)

        # Validate columns to add
        for table_name, cols in (columns or {}).items():
            if not await self.table_exists(table_name):
                validation_errors.append(f"Table '{table_name}' does not exist")
                plan.is_safe = False
            else:
                existing_cols = self._schema_cache.get(table_name, set())
                for col in cols:
                    if col.name in existing_cols:
                        validation_errors.append(
                            f"Column '{col.name}' already exists in '{table_name}'"
                        )
                    else:
                        plan.add_column(table_name, col)

        # Add indexes
        for index_sql in (indexes or []):
            plan.add_index(index_sql)

        plan.validation_errors = validation_errors
        return plan

    async def apply_migration(self, plan: MigrationPlan) -> MigrationResult:
        """Apply a validated migration plan."""
        result = MigrationResult(success=False, migration_id=plan.id)

        if plan.validation_errors:
            result.errors = plan.validation_errors
            return result

        # Ensure schema_migrations table exists
        await self._ensure_db()

        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                try:
                    # Create tables
                    for table in plan.tables_to_create:
                        columns_def = []
                        for col in table.columns:
                            col_def = f"{col.name} {col.type}"
                            if col.primary_key:
                                col_def += " PRIMARY KEY"
                            if not col.nullable:
                                col_def += " NOT NULL"
                            if col.default:
                                col_def += f" DEFAULT {col.default}"
                            if col.unique and not col.primary_key:
                                col_def += " UNIQUE"
                            if col.references:
                                col_def += f" REFERENCES {col.references}"
                            columns_def.append(col_def)

                        create_sql = f"CREATE TABLE {table.name} ({', '.join(columns_def)})"
                        await db.execute(create_sql)
                        result.changes_applied.append(f"Created table '{table.name}'")

                        # Create indexes for this table
                        for index_sql in table.indexes:
                            await db.execute(index_sql)
                            result.changes_applied.append(f"Created index on '{table.name}'")

                    # Add columns
                    for table_name, cols in plan.columns_to_add.items():
                        for col in cols:
                            col_def = f"{col.name} {col.type}"
                            if not col.nullable and col.default:
                                col_def += f" DEFAULT {col.default}"
                            elif not col.nullable:
                                col_def += " DEFAULT ''"

                            alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {col_def}"
                            await db.execute(alter_sql)
                            result.changes_applied.append(
                                f"Added column '{col.name}' to '{table_name}'"
                            )

                    # Create standalone indexes
                    for index_sql in plan.indexes_to_create:
                        await db.execute(index_sql)
                        result.changes_applied.append(f"Created index")

                    # Record migration
                    from datetime import datetime, timezone
                    await db.execute("""
                        INSERT INTO schema_migrations (id, description, applied_at, checksum)
                        VALUES (?, ?, ?, ?)
                    """, (
                        plan.id,
                        plan.description,
                        datetime.now(timezone.utc).isoformat(),
                        self._compute_checksum(plan),
                    ))
                    await db.commit()
                    result.success = True
                    self._migration_history.append(plan.id)
                    # Invalidate cache so subsequent checks see new schema
                    self._schema_cache = {}

                except Exception as e:
                    result.errors.append(str(e))
                    logger.error("Migration failed: %s", e)

        return result

    def _compute_checksum(self, plan: MigrationPlan) -> str:
        """Compute a checksum for the migration plan."""
        import hashlib
        content = f"{plan.id}:{plan.description}"
        content += str([(t.name, len(t.columns)) for t in plan.tables_to_create])
        content += str([(k, len(v)) for k, v in sorted(plan.columns_to_add.items())])
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    async def get_migration_history(self) -> List[Dict[str, Any]]:
        """Get list of applied migrations."""
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT id, description, applied_at, checksum FROM schema_migrations ORDER BY applied_at DESC"
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    def validate_sql(self, sql: str) -> tuple[bool, List[str]]:
        """Validate SQL for safety (additive only)."""
        sql_upper = sql.upper().strip()
        errors: List[str] = []

        # Check for dangerous statements - check DROP/RENAME/TRUNCATE anywhere in statement
        dangerous_keywords = ["DROP ", "DROP\t", "TRUNCATE ", "TRUNCATE\t", "RENAME "]
        for dangerous in dangerous_keywords:
            if dangerous in sql_upper:
                errors.append(f"Dangerous statement type detected: {dangerous.strip()}")

        # Check for ALTER TABLE DROP COLUMN
        if "ALTER TABLE" in sql_upper and "DROP COLUMN" in sql_upper:
            errors.append("Dangerous statement type detected: DROP COLUMN")

        # Only allow additive operations
        allowed_starts = [
            "CREATE TABLE",
            "ALTER TABLE",
            "CREATE INDEX",
            "INSERT INTO",
            "UPDATE ",
            "SELECT ",
            "PRAGMA ",
        ]

        is_allowed = any(sql_upper.startswith(p) for p in allowed_starts)
        if not is_allowed and sql_upper:
            errors.append("Statement not in allowed additive patterns")

        return len(errors) == 0, errors
