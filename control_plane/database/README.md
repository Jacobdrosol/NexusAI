# Database

The database module provides schema introspection, additive-only migrations, and external database connection management for the control plane and bot workflows.

---

## Files

| File | Class | Purpose |
|------|-------|---------|
| `database_engineer.py` | `DatabaseEngineer` | Orchestrates schema ops and connection access |
| `connection_repository.py` | `ConnectionRepository` | CRUD for external DB connection configs |
| `schema_manager.py` | `SchemaManager` | DDL helpers, table inspection, migration history |

---

## DatabaseEngineer (`database_engineer.py`)

The high-level service used by the `pm-database-engineer` bot stage and the API layer.

### Permitted Operations

| Category | Allowed | Blocked |
|----------|---------|---------|
| Safe read | `SELECT`, `PRAGMA` | — |
| Additive writes | `INSERT`, `UPDATE`, `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE ADD COLUMN` | — |
| Destructive | — | `DROP`, `TRUNCATE`, `RENAME`, `DELETE FROM` (whole table), destructive `ALTER TABLE` |

**Note**: The code defines `SAFE_STATEMENTS` and `DANGEROUS_STATEMENTS` frozensets, but enforcement is done by `SchemaManager` and the bot's `DBActionPolicy` — the frozensets in `DatabaseEngineer` are informational.

### DBActionPolicy (from `shared/models.py`)

Bots with `execution_policy.can_apply_db_actions=True` can request DB operations, subject to the policy:

| Field | Default | Description |
|-------|---------|-------------|
| `allow_schema_introspection` | `True` | Allow `PRAGMA` / schema reads |
| `allow_additive_schema_changes` | `False` | Allow `CREATE TABLE`, `ALTER TABLE ADD COLUMN`, `CREATE INDEX` |
| `allow_migration_apply` | `False` | Allow running a migration plan |
| `allow_migration_mark_applied` | `False` | Allow marking a migration as applied without running |
| `allow_safe_metadata_updates` | `False` | Allow `INSERT`/`UPDATE` on metadata tables |
| `denied_statement_types` | `[]` | Additional statement types to block |
| `denied_sql_patterns` | `[]` | Regex patterns for additional blocking |

### Key Methods

| Method | Description |
|--------|-------------|
| `initialize()` | Ensures DB tables exist |
| `get_schema()` | Returns `{tables: [...], columns_by_table: {...}}` |
| `table_exists(name)` | Boolean check |
| `add_connection(...)` | Adds external DB connection |
| `list_connections()` | All enabled connections |
| `test_connection(id)` | Validates connectivity |
| `apply_migration(plan)` | Runs a `MigrationPlan` against the internal or external DB |

---

## ConnectionRepository (`connection_repository.py`)

Manages external database connections (PostgreSQL, MySQL, etc.) used by bots.

### SQLite Table: `database_connections`

| Column | Description |
|--------|-------------|
| `id` | UUID |
| `name` | Human-readable name |
| `kind` | Connection type (e.g., `postgresql`, `mysql`, `sqlite`) |
| `description` | Free text |
| `connection_string` | DSN (sensitive — not encrypted at rest in this table) |
| `config_json` | JSON extra config |
| `credentials_ref` | Optional key vault reference |
| `schema_snapshot` | Last-known schema JSON |
| `enabled` | Boolean |
| `created_at` / `updated_at` | ISO 8601 UTC |
| `last_tested_at` | Timestamp of last connection test |
| `last_test_result` | Pass/fail/error from last test |

**Known issue**: Connection strings are stored unencrypted. Consider referencing `KeyVault` for sensitive credentials.

---

## SchemaManager (`schema_manager.py`)

Low-level DDL helpers:

| Method | Description |
|--------|-------------|
| `get_current_schema()` | Returns `{table_name: [column_names]}` for all user tables |
| `table_exists(name)` | Boolean |
| `get_migration_history()` | List of applied migration records |
| `apply_migration(plan)` | Executes a `MigrationPlan`; records result in migration history |

### MigrationPlan

```python
@dataclass
class MigrationPlan:
    id: str           # unique migration ID
    description: str
    sql_statements: List[str]  # DDL/DML statements to run in order
    is_reversible: bool = False
    rollback_sql: Optional[List[str]] = None
```

Only additive statements are permitted. The schema manager checks each statement against `ALLOWED_OPERATIONS` before executing.

---

## Known Issues

- Connection strings stored in plaintext (no Fernet encryption like `KeyVault`).
- `DatabaseEngineer.SAFE_STATEMENTS` / `DANGEROUS_STATEMENTS` frozensets are informational only — actual policy enforcement is in `DBActionPolicy`.
- No migration locking — concurrent migrations could conflict.
- `schema_manager` migration history table name is not documented in the code; need to verify DDL.
