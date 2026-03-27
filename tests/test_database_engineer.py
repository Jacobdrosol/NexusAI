"""Tests for the database engineer service."""
import asyncio
import os
import tempfile
from typing import Any, Dict, List

import pytest

from control_plane.database.database_engineer import DatabaseEngineer
from control_plane.database.schema_manager import ColumnDefinition, TableDefinition, SchemaManager
from control_plane.database.connection_repository import ConnectionRepository


@pytest.fixture
def temp_db_path() -> str:
    """Create a temporary database file for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def database_engineer(temp_db_path: str) -> DatabaseEngineer:
    """Create a database engineer instance for testing."""
    return DatabaseEngineer(db_path=temp_db_path)


@pytest.fixture
def schema_manager(temp_db_path: str) -> SchemaManager:
    """Create a schema manager instance for testing."""
    return SchemaManager(db_path=temp_db_path)


@pytest.fixture
def connection_repository(temp_db_path: str) -> ConnectionRepository:
    """Create a connection repository instance for testing."""
    return ConnectionRepository(db_path=temp_db_path)


class TestSchemaManager:
    """Tests for SchemaManager."""

    @pytest.mark.asyncio
    async def test_get_current_schema_empty(self, schema_manager: SchemaManager) -> None:
        """Test getting schema from empty database."""
        schema = await schema_manager.get_current_schema()
        assert isinstance(schema, dict)

    @pytest.mark.asyncio
    async def test_table_exists_check(self, schema_manager: SchemaManager, temp_db_path: str) -> None:
        """Test checking if a table exists."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE test_table (id TEXT PRIMARY KEY)")
            await db.commit()

        assert await schema_manager.table_exists("test_table") is True
        assert await schema_manager.table_exists("nonexistent_table") is False

    @pytest.mark.asyncio
    async def test_column_exists_check(self, schema_manager: SchemaManager, temp_db_path: str) -> None:
        """Test checking if a column exists."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE test_table (id TEXT PRIMARY KEY, name TEXT)")
            await db.commit()

        await schema_manager.get_current_schema()
        assert await schema_manager.column_exists("test_table", "id") is True
        assert await schema_manager.column_exists("test_table", "name") is True
        assert await schema_manager.column_exists("test_table", "nonexistent") is False

    @pytest.mark.asyncio
    async def test_create_migration_plan_for_new_table(self, schema_manager: SchemaManager) -> None:
        """Test creating a migration plan for a new table."""
        plan = await schema_manager.create_migration_plan(
            plan_id="test_create_table",
            description="Create test table",
            tables=[TableDefinition(
                name="test_table",
                columns=[
                    ColumnDefinition(name="id", type="TEXT", primary_key=True),
                    ColumnDefinition(name="name", type="TEXT", nullable=False),
                    ColumnDefinition(name="created_at", type="TEXT"),
                ],
            )],
        )

        assert plan.id == "test_create_table"
        assert plan.description == "Create test table"
        assert plan.is_safe is True
        assert len(plan.tables_to_create) == 1
        assert len(plan.validation_errors) == 0

    @pytest.mark.asyncio
    async def test_create_migration_plan_for_existing_table(
        self, schema_manager: SchemaManager, temp_db_path: str
    ) -> None:
        """Test that creating a migration for existing table fails validation."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE existing_table (id TEXT)")
            await db.commit()

        await schema_manager.get_current_schema()
        plan = await schema_manager.create_migration_plan(
            plan_id="test_existing_table",
            description="Try to create existing table",
            tables=[TableDefinition(
                name="existing_table",
                columns=[ColumnDefinition(name="id", type="TEXT")],
            )],
        )

        assert plan.is_safe is False
        assert len(plan.validation_errors) > 0

    @pytest.mark.asyncio
    async def test_create_migration_plan_for_new_column(self, schema_manager: SchemaManager, temp_db_path: str) -> None:
        """Test creating a migration plan for adding a column."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE test_table (id TEXT PRIMARY KEY)")
            await db.commit()

        await schema_manager.get_current_schema()
        plan = await schema_manager.create_migration_plan(
            plan_id="test_add_column",
            description="Add column to test_table",
            columns={
                "test_table": [ColumnDefinition(name="new_column", type="TEXT", nullable=True)]
            },
        )

        assert plan.is_safe is True
        assert "test_table" in plan.columns_to_add
        assert len(plan.columns_to_add["test_table"]) == 1

    @pytest.mark.asyncio
    async def test_apply_migration_create_table(self, schema_manager: SchemaManager) -> None:
        """Test applying a migration to create a table."""
        plan = await schema_manager.create_migration_plan(
            plan_id="apply_test_create",
            description="Create table via migration",
            tables=[TableDefinition(
                name="migrated_table",
                columns=[
                    ColumnDefinition(name="id", type="TEXT", primary_key=True),
                    ColumnDefinition(name="data", type="TEXT"),
                ],
            )],
        )

        result = await schema_manager.apply_migration(plan)

        assert result.success is True
        assert result.migration_id == "apply_test_create"
        assert any("migrated_table" in change for change in result.changes_applied)

    @pytest.mark.asyncio
    async def test_apply_migration_add_column(self, schema_manager: SchemaManager, temp_db_path: str) -> None:
        """Test applying a migration to add a column."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE test_table (id TEXT PRIMARY KEY)")
            await db.commit()

        await schema_manager.get_current_schema()
        plan = await schema_manager.create_migration_plan(
            plan_id="apply_test_add_column",
            description="Add column via migration",
            columns={
                "test_table": [ColumnDefinition(name="added_column", type="TEXT", default="''")]
            },
        )

        result = await schema_manager.apply_migration(plan)

        assert result.success is True
        assert any("added_column" in change for change in result.changes_applied)

    @pytest.mark.asyncio
    async def test_migration_history(self, schema_manager: SchemaManager) -> None:
        """Test migration history tracking."""
        plan = await schema_manager.create_migration_plan(
            plan_id="history_test",
            description="Test migration for history",
            tables=[TableDefinition(
                name="history_table",
                columns=[ColumnDefinition(name="id", type="TEXT", primary_key=True)],
            )],
        )

        await schema_manager.apply_migration(plan)
        history = await schema_manager.get_migration_history()

        assert len(history) > 0
        assert any(h["id"] == "history_test" for h in history)

    @pytest.mark.asyncio
    async def test_validate_sql_safe(self, schema_manager: SchemaManager) -> None:
        """Test SQL validation for safe statements."""
        safe_statements = [
            "SELECT * FROM users",
            "CREATE TABLE test (id TEXT)",
            "ALTER TABLE users ADD COLUMN email TEXT",
            "CREATE INDEX idx_email ON users(email)",
            "PRAGMA table_info(users)",
        ]

        for sql in safe_statements:
            is_safe, errors = schema_manager.validate_sql(sql)
            assert is_safe is True, f"Expected '{sql}' to be safe, got errors: {errors}"

    @pytest.mark.asyncio
    async def test_validate_sql_unsafe(self, schema_manager: SchemaManager) -> None:
        """Test SQL validation for unsafe statements."""
        unsafe_statements = [
            "DROP TABLE users",
            "DROP INDEX idx_test",
            "TRUNCATE TABLE users",
            "ALTER TABLE users DROP COLUMN email",
        ]

        for sql in unsafe_statements:
            is_safe, errors = schema_manager.validate_sql(sql)
            assert is_safe is False, f"Expected '{sql}' to be unsafe"
            assert len(errors) > 0


class TestConnectionRepository:
    """Tests for ConnectionRepository."""

    @pytest.mark.asyncio
    async def test_create_connection(self, connection_repository: ConnectionRepository) -> None:
        """Test creating a new connection."""
        connection = await connection_repository.create(
            name="Test SQLite DB",
            kind="sqlite",
            description="Test database connection",
            config={"database": "/tmp/test.db"},
        )

        assert connection["name"] == "Test SQLite DB"
        assert connection["kind"] == "sqlite"
        assert connection["enabled"] == 1
        assert "id" in connection

    @pytest.mark.asyncio
    async def test_get_connection(self, connection_repository: ConnectionRepository) -> None:
        """Test getting a connection by ID."""
        created = await connection_repository.create(
            name="Get Test",
            kind="sqlite",
            config={"database": "/tmp/get_test.db"},
        )

        retrieved = await connection_repository.get(created["id"])

        assert retrieved is not None
        assert retrieved["id"] == created["id"]
        assert retrieved["name"] == "Get Test"

    @pytest.mark.asyncio
    async def test_list_connections(self, connection_repository: ConnectionRepository) -> None:
        """Test listing connections."""
        await connection_repository.create(name="Conn 1", kind="sqlite", config={})
        await connection_repository.create(name="Conn 2", kind="sqlite", config={})

        connections = await connection_repository.list()

        assert len(connections) >= 2
        names = [c["name"] for c in connections]
        assert "Conn 1" in names
        assert "Conn 2" in names

    @pytest.mark.asyncio
    async def test_update_connection(self, connection_repository: ConnectionRepository) -> None:
        """Test updating a connection."""
        created = await connection_repository.create(
            name="Update Test",
            kind="sqlite",
            config={},
        )

        updated = await connection_repository.update(
            connection_id=created["id"],
            name="Updated Name",
            description="Updated description",
        )

        assert updated is not None
        assert updated["name"] == "Updated Name"
        assert updated["description"] == "Updated description"

    @pytest.mark.asyncio
    async def test_delete_connection(self, connection_repository: ConnectionRepository) -> None:
        """Test deleting a connection."""
        created = await connection_repository.create(
            name="Delete Test",
            kind="sqlite",
            config={},
        )

        deleted = await connection_repository.delete(created["id"])
        assert deleted is True

        retrieved = await connection_repository.get(created["id"])
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_test_connection_sqlite(self, connection_repository: ConnectionRepository, temp_db_path: str) -> None:
        """Test testing a SQLite connection."""
        created = await connection_repository.create(
            name="Testable SQLite",
            kind="sqlite",
            config={"database": temp_db_path},
        )

        result = await connection_repository.test_connection(created["id"])

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_schema_snapshot(self, connection_repository: ConnectionRepository) -> None:
        """Test schema snapshot storage and retrieval."""
        created = await connection_repository.create(
            name="Snapshot Test",
            kind="sqlite",
            config={},
        )

        snapshot = {"tables": ["users", "posts"], "version": "1.0"}
        result = await connection_repository.update_schema_snapshot(created["id"], snapshot)

        assert result is True

        retrieved = await connection_repository.get_schema_snapshot(created["id"])
        assert retrieved is not None
        assert retrieved["tables"] == ["users", "posts"]


class TestDatabaseEngineer:
    """Tests for DatabaseEngineer."""

    @pytest.mark.asyncio
    async def test_initialize(self, database_engineer: DatabaseEngineer) -> None:
        """Test initializing the database engineer."""
        await database_engineer.initialize()
        # Should complete without errors

    @pytest.mark.asyncio
    async def test_get_schema(self, database_engineer: DatabaseEngineer, temp_db_path: str) -> None:
        """Test getting schema structure."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE schema_test (id TEXT PRIMARY KEY, name TEXT)")
            await db.commit()

        await database_engineer.initialize()
        schema = await database_engineer.get_schema()

        assert "tables" in schema
        assert "columns_by_table" in schema

    @pytest.mark.asyncio
    async def test_table_exists(self, database_engineer: DatabaseEngineer, temp_db_path: str) -> None:
        """Test checking if table exists."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE exists_check (id TEXT)")
            await db.commit()

        await database_engineer.initialize()
        assert await database_engineer.table_exists("exists_check") is True
        assert await database_engineer.table_exists("nonexistent") is False

    @pytest.mark.asyncio
    async def test_add_column_if_not_exists(self, database_engineer: DatabaseEngineer, temp_db_path: str) -> None:
        """Test adding a column if it doesn't exist."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE add_col_test (id TEXT PRIMARY KEY)")
            await db.commit()

        await database_engineer.initialize()

        success, message = await database_engineer.add_column_if_not_exists(
            "add_col_test",
            "new_col",
            "TEXT",
            nullable=True,
        )

        assert success is True
        assert "new_col" in message

        # Second attempt should fail (column already exists)
        success2, message2 = await database_engineer.add_column_if_not_exists(
            "add_col_test",
            "new_col",
            "TEXT",
        )

        assert success2 is False

    @pytest.mark.asyncio
    async def test_create_table_if_not_exists(self, database_engineer: DatabaseEngineer) -> None:
        """Test creating a table if it doesn't exist."""
        await database_engineer.initialize()

        success, message = await database_engineer.create_table_if_not_exists(
            "create_test_table",
            columns=[
                ColumnDefinition(name="id", type="TEXT", primary_key=True),
                ColumnDefinition(name="data", type="TEXT"),
            ],
        )

        assert success is True
        assert "create_test_table" in message

        # Second attempt should fail (table already exists)
        success2, message2 = await database_engineer.create_table_if_not_exists(
            "create_test_table",
            columns=[ColumnDefinition(name="id", type="TEXT")],
        )

        assert success2 is False

    @pytest.mark.asyncio
    async def test_validate_sql_statement_safe(self, database_engineer: DatabaseEngineer) -> None:
        """Test validating safe SQL statements."""
        safe_statements = [
            "SELECT * FROM users",
            "CREATE TABLE test (id TEXT)",
            "ALTER TABLE users ADD COLUMN email TEXT",
            "CREATE INDEX idx ON users(name)",
        ]

        for sql in safe_statements:
            is_safe, errors = database_engineer.validate_sql_statement(sql)
            assert is_safe is True, f"'{sql}' should be safe"

    @pytest.mark.asyncio
    async def test_validate_sql_statement_unsafe(self, database_engineer: DatabaseEngineer) -> None:
        """Test validating unsafe SQL statements."""
        unsafe_statements = [
            "DROP TABLE users",
            "TRUNCATE TABLE logs",
            "ALTER TABLE users DROP COLUMN email",
        ]

        for sql in unsafe_statements:
            is_safe, errors = database_engineer.validate_sql_statement(sql)
            assert is_safe is False, f"'{sql}' should be unsafe"
            assert len(errors) > 0

    @pytest.mark.asyncio
    async def test_execute_safe_sql_select(self, database_engineer: DatabaseEngineer, temp_db_path: str) -> None:
        """Test executing safe SELECT statement."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE select_test (id TEXT, value TEXT)")
            await db.execute("INSERT INTO select_test VALUES ('1', 'a'), ('2', 'b')")
            await db.commit()

        await database_engineer.initialize()
        result = await database_engineer.execute_safe_sql("SELECT * FROM select_test")

        assert result["success"] is True
        assert result["row_count"] == 2

    @pytest.mark.asyncio
    async def test_execute_safe_sql_rejects_drop(self, database_engineer: DatabaseEngineer) -> None:
        """Test that DROP statements are rejected."""
        await database_engineer.initialize()
        result = await database_engineer.execute_safe_sql("DROP TABLE users")

        assert result["success"] is False
        assert "validation_errors" in result

    @pytest.mark.asyncio
    async def test_capture_schema_snapshot(self, database_engineer: DatabaseEngineer, temp_db_path: str) -> None:
        """Test capturing schema snapshot."""
        import aiosqlite
        async with aiosqlite.connect(temp_db_path) as db:
            await db.execute("CREATE TABLE snapshot_table (id TEXT PRIMARY KEY, data TEXT)")
            await db.commit()

        await database_engineer.initialize()
        snapshot = await database_engineer.capture_schema_snapshot()

        assert "captured_at" in snapshot
        assert "tables" in snapshot
        assert "snapshot_table" in snapshot["tables"]

    @pytest.mark.asyncio
    async def test_compare_schemas(self, database_engineer: DatabaseEngineer) -> None:
        """Test comparing two schema snapshots."""
        snapshot1 = {
            "tables": {"users": {"columns": [], "column_count": 0}},
        }
        snapshot2 = {
            "tables": {
                "users": {"columns": [], "column_count": 0},
                "posts": {"columns": [], "column_count": 0},
            },
        }

        comparison = await database_engineer.compare_schemas(snapshot1, snapshot2)

        assert "added_tables" in comparison
        assert "removed_tables" in comparison
        assert "common_tables" in comparison
        assert "posts" in comparison["added_tables"]
        assert "users" in comparison["common_tables"]

    @pytest.mark.asyncio
    async def test_validate_database_config(self, database_engineer: DatabaseEngineer) -> None:
        """Test validating database configuration."""
        valid_sqlite = {"kind": "sqlite", "database": "/tmp/test.db"}
        is_valid, errors = database_engineer.validate_database_config(valid_sqlite)
        assert is_valid is True

        invalid_sqlite = {"kind": "sqlite"}
        is_valid2, errors2 = database_engineer.validate_database_config(invalid_sqlite)
        assert is_valid2 is False
        assert len(errors2) > 0

        invalid_port = {"kind": "postgresql", "host": "localhost", "database": "test", "port": "invalid"}
        is_valid3, errors3 = database_engineer.validate_database_config(invalid_port)
        assert is_valid3 is False


class TestDatabaseEngineerIntegration:
    """Integration tests for the database engineer service."""

    @pytest.mark.asyncio
    async def test_full_migration_workflow(self, database_engineer: DatabaseEngineer) -> None:
        """Test complete migration planning and execution workflow."""
        await database_engineer.initialize()

        # Plan migration
        plan = await database_engineer.plan_migration(
            plan_id="integration_test_migration",
            description="Create users table with columns",
            tables=[TableDefinition(
                name="integration_users",
                columns=[
                    ColumnDefinition(name="id", type="TEXT", primary_key=True),
                    ColumnDefinition(name="email", type="TEXT", nullable=False, unique=True),
                    ColumnDefinition(name="created_at", type="TEXT"),
                ],
            )],
        )

        assert plan.is_safe is True
        assert len(plan.validation_errors) == 0

        # Apply migration
        result = await database_engineer.apply_migration(plan)
        assert result.success is True

        # Verify table was created
        assert await database_engineer.table_exists("integration_users") is True

        # Add column via migration
        success, message = await database_engineer.add_column_if_not_exists(
            "integration_users",
            "last_login",
            "TEXT",
            nullable=True,
        )
        assert success is True

        # Verify column was added
        table_info = await database_engineer.get_table_info("integration_users")
        column_names = [col["name"] for col in table_info]
        assert "last_login" in column_names

    @pytest.mark.asyncio
    async def test_connection_with_schema_snapshot(self, database_engineer: DatabaseEngineer, temp_db_path: str) -> None:
        """Test connection management with schema snapshots."""
        await database_engineer.initialize()

        # Create connection
        connection = await database_engineer.create_connection(
            name="Integration Test DB",
            kind="sqlite",
            config={"database": temp_db_path},
        )

        # Capture and store schema snapshot
        snapshot = await database_engineer.capture_schema_snapshot()
        await database_engineer._connection_repo.update_schema_snapshot(
            connection["id"],
            snapshot,
        )

        # Retrieve and verify
        retrieved = await database_engineer._connection_repo.get_schema_snapshot(connection["id"])
        assert retrieved is not None

    @pytest.mark.asyncio
    async def test_migration_history_tracking(self, database_engineer: DatabaseEngineer) -> None:
        """Test that migration history is properly tracked."""
        await database_engineer.initialize()

        # Get initial history
        initial_history = await database_engineer.get_migration_history()

        # Apply a migration
        plan = await database_engineer.plan_migration(
            plan_id="history_tracking_test",
            description="Test history tracking",
            tables=[TableDefinition(
                name="history_tracking_table",
                columns=[ColumnDefinition(name="id", type="TEXT", primary_key=True)],
            )],
        )
        await database_engineer.apply_migration(plan)

        # Verify history was updated
        final_history = await database_engineer.get_migration_history()
        assert len(final_history) > len(initial_history)
        assert any(h["id"] == "history_tracking_test" for h in final_history)
