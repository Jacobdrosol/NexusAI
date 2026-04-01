# Database — Control Plane

The `database` package provides schema management, safe SQL execution, and external connection tracking for the NexusAI control plane. It is composed of three modules:

| Module | Class | Responsibility |
|---|---|---|
| `database_engineer.py` | `DatabaseEngineer` | Top-level service; orchestrates schema and connection operations |
| `schema_manager.py` | `SchemaManager` | Additive-only migrations, introspection, migration history |
| `connection_repository.py` | `ConnectionRepository` | CRUD for external DB connection metadata |

---

## DatabaseEngineer (`database_engineer.py`)

`DatabaseEngineer` is the primary public interface for database operations from the rest of the control plane. It owns a `SchemaManager` and a `ConnectionRepository` instance and delegates to each as appropriate.

### Constructor and Initialization

```python
DatabaseEngineer(db_path: Optional[str] = None)
```

Resolves `db_path` the same way as other control-plane services: explicit argument -> `DATABASE_URL` env var (`sqlite:///...`) -> `data/nexusai.db`.

```python
await engineer.initialize()
```

Must be called before use. Delegates to `_schema_manager._ensure_db()` and `_connection_repo._ensure_db()` to create all required tables.

---

## SQL Safety Model

`DatabaseEngineer` enforces a two-layer safety check on all SQL statements.

### Class-Level Policy Sets

```python
SAFE_STATEMENTS = frozenset({"SELECT", "PRAGMA", "INSERT", "UPDATE", "DELETE"})

DANGEROUS_STATEMENTS = frozenset({"DROP", "TRUNCATE", "RENAME", "DELETE FROM"})

ALLOWED_OPERATIONS = frozenset({
    "CREATE TABLE",
    "ALTER TABLE",
    "CREATE INDEX",
    "INSERT INTO",
    "UPDATE",
    "SELECT",
    "PRAGMA",
})
```

### `validate_sql_statement(sql: str) -> Tuple[bool, List[str]]`

Performs two checks:

1. **Substring scan for `DANGEROUS_STATEMENTS`**: if any dangerous keyword appears anywhere in the uppercased SQL, an error is appended.
2. **Prefix check against `ALLOWED_OPERATIONS`**: the statement must begin with one of the allowed prefixes (case-insensitive).
3. **Additional explicit checks**: `DROP TABLE`, `DROP INDEX`, and `DETACH` each produce a dedicated error.

Returns `(is_safe: bool, errors: List[str])`.

### `execute_safe_sql(sql, params=None, require_approval=True) -> Dict[str, Any]`

Calls `validate_sql_statement` first. If validation fails, returns:

```json
{"success": false, "error": "Statement failed safety validation", "validation_errors": [...]}
```

If safe, executes under `_lock`. For `SELECT` and `PRAGMA` statements returns:

```json
{"success": true, "data": [...], "row_count": N}
```

For write statements, commits and returns:

```json
{"success": true, "rows_affected": N}
```

Any exception is caught and returned as `{"success": false, "error": "<message>"}`.

Note: `require_approval` is accepted as a parameter but has no effect on behaviour — the same validation is always applied.

---

## Schema Introspection

| Method | Returns | Description |
|---|---|---|
| `get_schema()` | `Dict[str, Any]` | Lists all tables and their columns. Delegates to `SchemaManager.get_current_schema()`. |
| `table_exists(table_name)` | `bool` | Checks cache (or refreshes it) via `SchemaManager`. |
| `column_exists(table_name, col_name)` | `bool` | Checks cache via `SchemaManager`. |
| `get_table_info(table_name)` | `List[Dict]` | Runs `PRAGMA table_info(table_name)` directly; returns column metadata rows. |

---

## Migration Planning and Execution

All schema changes are **additive only** — new tables and new columns are supported; `DROP`, `TRUNCATE`, and `RENAME` are rejected.

### `plan_migration(plan_id, description, tables, columns, indexes) -> MigrationPlan`

Delegates to `SchemaManager.create_migration_plan`. Validates that:
- Tables to be created do not already exist.
- Columns to be added are for existing tables and do not already exist.

Returns a `MigrationPlan` dataclass. If any validation error is found, `plan.is_safe` is set to `False` and `plan.validation_errors` is populated.

### `apply_migration(plan: MigrationPlan) -> MigrationResult`

Rejects plans where `plan.is_safe is False` immediately. Otherwise delegates to `SchemaManager.apply_migration`. On success, logs the applied changes. On failure, logs errors.

### `add_column_if_not_exists(table_name, column_name, column_type, nullable, default) -> Tuple[bool, str]`

Convenience wrapper: checks existence, builds a `ColumnDefinition`, plans a migration, and applies it. Returns `(True, message)` on success or `(False, error)`.

### `create_table_if_not_exists(table_name, columns, indexes) -> Tuple[bool, str]`

Convenience wrapper: checks existence, builds a `TableDefinition`, plans, and applies.

---

## Schema Snapshots

### `capture_schema_snapshot() -> Dict[str, Any]`

Captures current schema as a serialisable snapshot:

```json
{
    "captured_at": "2024-01-01T00:00:00+00:00",
    "tables": {
        "cp_bots": {"columns": [...], "column_count": 2}
    }
}
```

### `compare_schemas(snapshot1, snapshot2) -> Dict[str, Any]`

Returns `{"added_tables": [...], "removed_tables": [...], "common_tables": [...]}`. Does **not** compare column-level differences within common tables.

---

## Database Config Validation

### `validate_database_config(config: Dict) -> Tuple[bool, List[str]]`

Validates connection config dicts by `kind`:

| Kind | Required fields |
|---|---|
| `sqlite` | `database` or `path` |
| `postgresql` / `postgres` | `host`, `database` |
| `mysql` | `host`, `database` |

Also validates `port` if present (integer, 1-65535).

---

## SchemaManager (`schema_manager.py`)

`SchemaManager` manages the `schema_migrations` tracking table and performs the actual DDL execution.

### Migration Tracking Table

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  TEXT NOT NULL,    -- ISO-8601 UTC
    checksum    TEXT              -- SHA-256[:16] of plan id + description + structure
)
```

### Data Classes

#### `ColumnDefinition`

```python
@dataclass
class ColumnDefinition:
    name: str
    type: str
    nullable: bool = True
    default: Optional[str] = None
    primary_key: bool = False
    unique: bool = False
    references: Optional[str] = None   # e.g. "other_table(id)"
```

#### `TableDefinition`

```python
@dataclass
class TableDefinition:
    name: str
    columns: List[ColumnDefinition] = field(default_factory=list)
    indexes: List[str] = field(default_factory=list)  # raw CREATE INDEX SQL
```

#### `MigrationPlan`

```python
@dataclass
class MigrationPlan:
    id: str
    description: str
    tables_to_create: List[TableDefinition]
    columns_to_add: Dict[str, List[ColumnDefinition]]   # table_name -> columns
    indexes_to_create: List[str]
    is_safe: bool = True
    validation_errors: List[str]
```

#### `MigrationResult`

```python
@dataclass
class MigrationResult:
    success: bool
    migration_id: str
    changes_applied: List[str]   # human-readable description of each DDL statement run
    errors: List[str]
```

### Schema Cache

`SchemaManager` holds `_schema_cache: Dict[str, Set[str]]` (table -> set of column names). The cache is populated lazily by `get_current_schema()` and invalidated (set to `{}`) after every successful `apply_migration`. `table_exists` and `column_exists` will trigger a cache refresh if the cache is empty.

### Migration Execution Flow (`apply_migration`)

1. Check for validation errors; abort if any.
2. Acquire `_lock`.
3. Open SQLite connection.
4. **Create tables**: generate `CREATE TABLE (col_defs...)` SQL from `TableDefinition`; run per-table indexes.
5. **Add columns**: generate `ALTER TABLE ... ADD COLUMN` SQL for each `ColumnDefinition`.
6. **Create standalone indexes**: execute each raw SQL string in `indexes_to_create`.
7. **Record migration**: `INSERT INTO schema_migrations (id, description, applied_at, checksum)`.
8. Commit.
9. Invalidate `_schema_cache`.

### SQL Validation (`validate_sql`)

A standalone method that mirrors the logic in `DatabaseEngineer.validate_sql_statement` but with slightly different keyword patterns (checks `"DROP "`, `"DROP\t"`, `"TRUNCATE "`, `"TRUNCATE\t"`, `"RENAME "`; also checks `ALTER TABLE ... DROP COLUMN` explicitly).

### `get_migration_history() -> List[Dict[str, Any]]`

Queries `schema_migrations` ordered by `applied_at DESC`. Returns all columns.

---

## ConnectionRepository (`connection_repository.py`)

Stores metadata about external database connections (e.g., PostgreSQL, MySQL, SQLite). **Credentials are never stored directly** — only a `credentials_ref` pointer (e.g., a secret name) is held.

### SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS database_connections (
    id               TEXT PRIMARY KEY,          -- "db_conn_{name}_{timestamp}"
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,             -- "sqlite", "postgresql", "mysql", etc.
    description      TEXT NOT NULL DEFAULT '',
    connection_string TEXT,                     -- optional DSN
    config_json      TEXT NOT NULL DEFAULT '{}', -- JSON config blob
    credentials_ref  TEXT,                      -- pointer to secret store
    schema_snapshot  TEXT,                      -- JSON snapshot of remote schema
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_tested_at   TEXT,
    last_test_result TEXT                       -- JSON result of most recent test
)
```

Two indexes are created:

- `idx_database_connections_name` on `(name)`
- `idx_database_connections_enabled` on `(enabled, name)`

### Initialization and Column Migration

`_ensure_db()` calls `_ensure_connection_columns(db)` after table creation. This method reads `PRAGMA table_info(database_connections)` and issues `ALTER TABLE ... ADD COLUMN` for any column that does not exist. This allows the schema to be extended without formal migrations for the connections table specifically.

### ID Format

Connection IDs are generated as:

```python
f"db_conn_{name.lower().replace(' ', '_')}_{int(datetime.now().timestamp())}"
```

This means IDs are not UUIDs and can collide if two connections with the same name are created within the same second.

### Public Methods

| Method | Signature | Description |
|---|---|---|
| `create` | `(name, kind, description, connection_string, config, credentials_ref) -> Dict` | Creates and persists a new connection. Returns the full connection dict. |
| `get` | `(connection_id: str) -> Optional[Dict]` | Returns connection from in-memory cache, or `None`. |
| `list` | `(enabled_only: bool = False) -> List[Dict]` | Returns all connections sorted by name. Filter by `enabled` if requested. |
| `update` | `(connection_id, name, description, config, credentials_ref, enabled) -> Optional[Dict]` | Partial update. Returns updated connection or `None` if not found. |
| `delete` | `(connection_id: str) -> bool` | Deletes connection. Returns `False` if not found. |
| `test_connection` | `(connection_id: str) -> Dict` | Attempts a connectivity test (see below). |
| `get_schema_snapshot` | `(connection_id: str) -> Optional[Dict]` | Returns stored JSON schema snapshot for the connection. |
| `update_schema_snapshot` | `(connection_id: str, snapshot: Dict) -> bool` | Stores a schema snapshot; updates `updated_at`. |

### Connection Testing

`test_connection` behaviour varies by `kind`:

| Kind | Actual test |
|---|---|
| `sqlite` | Opens the SQLite file and runs `SELECT 1` |
| `postgresql` | Returns `success: True` with a note that full testing requires `asyncpg` — **does not actually connect** |
| `mysql` | Returns `success: True` with a note that full testing requires `aiomysql` — **does not actually connect** |
| *(other)* | Returns `success: False`, `error: "Unknown connection kind: ..."` |

Test results are written back to `last_tested_at` and `last_test_result` in both the DB and the in-memory cache.

---

## Known Issues / Refactor Notes

### DatabaseEngineer

- **`DELETE` is blocked by `execute_safe_sql`**: `DELETE` appears in `SAFE_STATEMENTS` but not in `ALLOWED_OPERATIONS`. The prefix check (`sql_upper.startswith(op)`) will fail for any `DELETE` statement (which starts with `"DELETE"`, not `"DELETE FROM"` which is only in `DANGEROUS_STATEMENTS`). The result is that `DELETE` statements can never be executed via `execute_safe_sql`, even though they are listed as safe. This is a logic inconsistency between the two sets.
- **`require_approval` parameter is unused**: the `execute_safe_sql` method accepts `require_approval: bool` but never branches on it. The parameter is misleading.
- **Duplicate validation logic**: `DatabaseEngineer.validate_sql_statement` and `SchemaManager.validate_sql` are two separate implementations of nearly identical logic with slightly different dangerous-keyword patterns. They should be unified.
- **`DBActionPolicy` not present**: the architecture implies a per-bot or per-request policy model (with fields like `allow_schema_introspection`, `allow_additive_schema_changes`), but no such class exists in these files. The closest analogue is the class-level `ALLOWED_OPERATIONS` / `DANGEROUS_STATEMENTS` frozensets, which are global and non-configurable.

### SchemaManager

- **`is_safe` is sticky-false**: once `MigrationPlan.is_safe` is set to `False` (due to one validation error), it is never reset. A plan with one already-existing table will be rejected entirely, even if the other requested changes are valid. There is no partial-application mode.
- **Stale cache risk**: `table_exists` and `column_exists` only refresh the cache if `_schema_cache` is falsy (empty dict). If the schema is changed by an external process between calls, the cached view will be incorrect until the next migration or explicit `get_current_schema()` call.
- **`create_migration_plan` marks plan unsafe but continues**: when a table already exists, `plan.is_safe = False` is set and processing continues. Validation errors accumulate for all requested tables/columns, but the partially-valid operations (e.g., valid column additions) are still added to the plan and then silently discarded because `apply_migration` checks `is_safe` first.
- **Checksum covers only structure counts, not column names**: `_compute_checksum` hashes `len(t.columns)` per table, not the actual column names or types. Two different migrations with the same table count will produce the same checksum, undermining the integrity check.

### ConnectionRepository

- **ID collision risk**: connection IDs are `db_conn_{name}_{unix_seconds}`. Two connections with the same name created in the same second will have the same ID, causing the second `INSERT` to fail with a primary key violation.
- **`_ensure_connection_columns` has dead branching**: lines 111-115 have `if default_match: ... else: ...` where both branches execute the identical `ALTER TABLE` statement. The `if/else` is meaningless.
- **`test_connection` returns false success for PostgreSQL and MySQL**: callers receive `success: True` for non-SQLite databases even though no connection was attempted. This can lead operators to believe a misconfigured connection is valid.
- **`config_json` in memory vs DB**: on load, `config_json` is parsed from JSON into a dict and stored as a dict in `_connections`. On update, the dict is passed directly to `json.dumps()` for persistence. Callers must always pass `config` as a dict, not a JSON string.
- **No unique constraint on `name`**: two connections with the same `name` can be created. The index `idx_database_connections_name` speeds up name lookups but does not enforce uniqueness.
