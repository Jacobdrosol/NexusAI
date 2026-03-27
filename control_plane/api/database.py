"""Database engineer API endpoints for schema management."""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Request

from control_plane.audit.utils import record_audit_event
from control_plane.database.database_engineer import DatabaseEngineer
from control_plane.database.schema_manager import ColumnDefinition, TableDefinition
from control_plane.security.guards import enforce_body_size, enforce_rate_limit

router = APIRouter(prefix="/v1/database", tags=["database"])
logger = logging.getLogger(__name__)

_db_engineer: Optional[DatabaseEngineer] = None


def get_database_engineer() -> DatabaseEngineer:
    """Get or create the database engineer singleton."""
    global _db_engineer
    if _db_engineer is None:
        _db_engineer = DatabaseEngineer()
    return _db_engineer


def _schema_validation_errors(errors: List[str]) -> List[Dict[str, Any]]:
    """Format schema validation errors for API response."""
    return [
        {
            "field_path": "schema",
            "message": error,
            "invalid_value": None,
        }
        for error in errors
    ]


@router.get("/schema")
async def get_schema() -> Dict[str, Any]:
    """Get current database schema structure."""
    enforce_rate_limit("schema_read", 60)
    engineer = get_database_engineer()
    return await engineer.get_schema()


@router.get("/schema/table/{table_name}")
async def get_table_info(table_name: str) -> List[Dict[str, Any]]:
    """Get column information for a specific table."""
    enforce_rate_limit("schema_read", 60)
    engineer = get_database_engineer()

    if not await engineer.table_exists(table_name):
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    return await engineer.get_table_info(table_name)


@router.post("/migrations/plan")
async def plan_migration(
    request: Request,
    plan_id: str = Body(..., description="Unique identifier for this migration"),
    description: str = Body(..., description="Human-readable description of changes"),
    tables: Optional[List[Dict[str, Any]]] = Body(None, description="Tables to create"),
    columns: Optional[Dict[str, List[Dict[str, Any]]]] = Body(
        None, description="Columns to add by table"
    ),
    indexes: Optional[List[str]] = Body(None, description="Index SQL statements"),
) -> Dict[str, Any]:
    """
    Create a migration plan for additive schema changes.

    This endpoint validates that all proposed changes are safe (additive only).
    No changes are applied - use POST /v1/migrations/apply to execute.
    """
    enforce_rate_limit("migration_plan", 10)
    enforce_body_size(request, max_size=65536)

    engineer = get_database_engineer()

    # Convert dict specs to proper objects
    table_defs = []
    if tables:
        for t in tables:
            cols = [
                ColumnDefinition(
                    name=c["name"],
                    type=c.get("type", "TEXT"),
                    nullable=c.get("nullable", True),
                    default=c.get("default"),
                    primary_key=c.get("primary_key", False),
                    unique=c.get("unique", False),
                    references=c.get("references"),
                )
                for c in t.get("columns", [])
            ]
            table_defs.append(TableDefinition(name=t["name"], columns=cols, indexes=t.get("indexes", [])))

    column_defs = {}
    if columns:
        for table_name, col_list in columns.items():
            column_defs[table_name] = [
                ColumnDefinition(
                    name=c["name"],
                    type=c.get("type", "TEXT"),
                    nullable=c.get("nullable", True),
                    default=c.get("default"),
                )
                for c in col_list
            ]

    plan = await engineer.plan_migration(
        plan_id=plan_id,
        description=description,
        tables=table_defs if table_defs else None,
        columns=column_defs if column_defs else None,
        indexes=indexes,
    )

    return {
        "plan_id": plan.id,
        "description": plan.description,
        "is_safe": plan.is_safe,
        "tables_to_create": [t.name for t in plan.tables_to_create],
        "columns_to_add": {k: [c.name for c in v] for k, v in plan.columns_to_add.items()},
        "indexes_to_create": plan.indexes_to_create,
        "validation_errors": plan.validation_errors,
    }


@router.post("/migrations/apply")
async def apply_migration(
    request: Request,
    plan_id: str = Body(..., description="ID of the migration plan to apply"),
    description: str = Body("", description="Optional description override"),
    tables: Optional[List[Dict[str, Any]]] = Body(None),
    columns: Optional[Dict[str, List[Dict[str, Any]]]] = Body(None),
    indexes: Optional[List[str]] = Body(None),
) -> Dict[str, Any]:
    """
    Apply a migration plan.

    The plan is re-validated before execution. Only safe, additive changes are allowed.
    """
    enforce_rate_limit("migration_apply", 5)
    enforce_body_size(request, max_size=65536)

    engineer = get_database_engineer()

    # Reconstruct the plan
    table_defs = []
    if tables:
        for t in tables:
            cols = [
                ColumnDefinition(
                    name=c["name"],
                    type=c.get("type", "TEXT"),
                    nullable=c.get("nullable", True),
                    default=c.get("default"),
                    primary_key=c.get("primary_key", False),
                    unique=c.get("unique", False),
                    references=c.get("references"),
                )
                for c in t.get("columns", [])
            ]
            table_defs.append(TableDefinition(name=t["name"], columns=cols, indexes=t.get("indexes", [])))

    column_defs = {}
    if columns:
        for table_name, col_list in columns.items():
            column_defs[table_name] = [
                ColumnDefinition(
                    name=c["name"],
                    type=c.get("type", "TEXT"),
                    nullable=c.get("nullable", True),
                    default=c.get("default"),
                )
                for c in col_list
            ]

    plan = await engineer.plan_migration(
        plan_id=plan_id,
        description=description,
        tables=table_defs if table_defs else None,
        columns=column_defs if column_defs else None,
        indexes=indexes,
    )

    if not plan.is_safe:
        await record_audit_event(
            actor="database_api",
            action="migration_apply_rejected",
            resource=f"migration:{plan_id}",
            status="rejected",
            details={"reason": "validation_failed", "errors": plan.validation_errors},
        )
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "migration_unsafe",
                "errors": plan.validation_errors,
            },
        )

    result = await engineer.apply_migration(plan)

    await record_audit_event(
        actor="database_api",
        action="migration_apply",
        resource=f"migration:{plan_id}",
        status="success" if result.success else "error",
        details={
            "changes_applied": result.changes_applied,
            "errors": result.errors,
        },
    )

    if not result.success:
        raise HTTPException(
            status_code=500,
            detail={
                "reason": "migration_failed",
                "errors": result.errors,
            },
        )

    return {
        "success": True,
        "migration_id": result.migration_id,
        "changes_applied": result.changes_applied,
    }


@router.get("/migrations/history")
async def get_migration_history(limit: int = Query(50, ge=1, le=500)) -> List[Dict[str, Any]]:
    """Get history of applied migrations."""
    enforce_rate_limit("migration_history", 30)
    engineer = get_database_engineer()
    history = await engineer.get_migration_history()
    return history[:limit]


@router.post("/sql/validate")
async def validate_sql(
    request: Request,
    sql: str = Body(..., description="SQL statement to validate"),
) -> Dict[str, Any]:
    """
    Validate a SQL statement for safety.

    Returns whether the statement is safe and any validation errors.
    """
    enforce_rate_limit("sql_validate", 60)
    enforce_body_size(request, max_size=8192)

    engineer = get_database_engineer()
    is_safe, errors = engineer.validate_sql_statement(sql)

    return {
        "is_safe": is_safe,
        "errors": errors,
        "sql_preview": sql[:200] + "..." if len(sql) > 200 else sql,
    }


@router.post("/sql/execute")
async def execute_sql(
    request: Request,
    sql: str = Body(..., description="SQL statement to execute"),
    params: Optional[List[Any]] = Body(None, description="Parameters for prepared statement"),
) -> Dict[str, Any]:
    """
    Execute a safe SQL statement.

    Only additive operations (CREATE TABLE, ALTER TABLE ADD COLUMN, CREATE INDEX)
    and read operations (SELECT, PRAGMA) are allowed without special approval.
    """
    enforce_rate_limit("sql_execute", 30)
    enforce_body_size(request, max_size=16384)

    engineer = get_database_engineer()
    result = await engineer.execute_safe_sql(
        sql,
        params=tuple(params) if params else None,
    )

    await record_audit_event(
        actor="database_api",
        action="sql_execute",
        resource="database",
        status="success" if result.get("success") else "error",
        details={
            "sql_preview": sql[:200],
            "result": {k: v for k, v in result.items() if k != "data"},
        },
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result,
        )

    return result


@router.get("/connections")
async def list_connections(
    enabled_only: bool = Query(False, description="Only return enabled connections"),
) -> List[Dict[str, Any]]:
    """List all database connections."""
    enforce_rate_limit("connection_list", 60)
    engineer = get_database_engineer()
    return await engineer.list_connections(enabled_only=enabled_only)


@router.post("/connections")
async def create_connection(
    request: Request,
    name: str = Body(..., description="Connection name"),
    kind: str = Body(..., description="Connection kind (sqlite, postgresql, mysql)"),
    description: str = Body("", description="Optional description"),
    connection_string: Optional[str] = Body(None, description="Full connection string"),
    config: Optional[Dict[str, Any]] = Body(None, description="Connection configuration"),
    credentials_ref: Optional[str] = Body(None, description="Reference to stored credentials"),
) -> Dict[str, Any]:
    """Create a new database connection."""
    enforce_rate_limit("connection_create", 10)
    enforce_body_size(request, max_size=8192)

    engineer = get_database_engineer()

    # Validate config
    config_dict = config or {"kind": kind}
    is_valid, errors = engineer.validate_database_config(config_dict)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail={
                "reason": "invalid_config",
                "errors": errors,
            },
        )

    connection = await engineer.create_connection(
        name=name,
        kind=kind,
        description=description,
        connection_string=connection_string,
        config=config,
        credentials_ref=credentials_ref,
    )

    await record_audit_event(
        actor="database_api",
        action="connection_create",
        resource=f"connection:{connection['id']}",
        status="success",
        details={"name": name, "kind": kind},
    )

    return connection


@router.get("/connections/{connection_id}")
async def get_connection(connection_id: str) -> Dict[str, Any]:
    """Get a specific database connection."""
    enforce_rate_limit("connection_get", 60)
    engineer = get_database_engineer()
    connection = await engineer.get_connection(connection_id)

    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    return connection


@router.put("/connections/{connection_id}")
async def update_connection(
    request: Request,
    connection_id: str,
    name: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    config: Optional[Dict[str, Any]] = Body(None),
    credentials_ref: Optional[str] = Body(None),
    enabled: Optional[bool] = Body(None),
) -> Dict[str, Any]:
    """Update a database connection."""
    enforce_rate_limit("connection_update", 10)
    enforce_body_size(request, max_size=8192)

    engineer = get_database_engineer()
    connection = await engineer.update_connection(
        connection_id=connection_id,
        name=name,
        description=description,
        config=config,
        credentials_ref=credentials_ref,
        enabled=enabled,
    )

    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    await record_audit_event(
        actor="database_api",
        action="connection_update",
        resource=f"connection:{connection_id}",
        status="success",
        details={"updates": {k: v for k, v in locals().items() if k != "connection_id" and v is not None}},
    )

    return connection


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: str) -> Dict[str, Any]:
    """Delete a database connection."""
    enforce_rate_limit("connection_delete", 10)

    engineer = get_database_engineer()
    deleted = await engineer.delete_connection(connection_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Connection not found")

    await record_audit_event(
        actor="database_api",
        action="connection_delete",
        resource=f"connection:{connection_id}",
        status="success",
    )

    return {"success": True, "deleted_id": connection_id}


@router.post("/connections/{connection_id}/test")
async def test_connection(connection_id: str) -> Dict[str, Any]:
    """Test a database connection."""
    enforce_rate_limit("connection_test", 10)

    engineer = get_database_engineer()
    result = await engineer.test_connection(connection_id)

    await record_audit_event(
        actor="database_api",
        action="connection_test",
        resource=f"connection:{connection_id}",
        status="success" if result.get("success") else "error",
        details={"result": {k: v for k, v in result.items() if k != "details"}},
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result,
        )

    return result


@router.get("/connections/{connection_id}/schema")
async def get_connection_schema(connection_id: str) -> Dict[str, Any]:
    """Get stored schema snapshot for a connection."""
    enforce_rate_limit("schema_read", 30)

    engineer = get_database_engineer()
    snapshot = await engineer._connection_repo.get_schema_snapshot(connection_id)

    if snapshot is None:
        connection = await engineer.get_connection(connection_id)
        if not connection:
            raise HTTPException(status_code=404, detail="Connection not found")
        return {"schema": None, "note": "No schema snapshot captured yet"}

    return {"schema": snapshot}


@router.get("/schema/snapshot")
async def capture_schema_snapshot() -> Dict[str, Any]:
    """Capture current database schema as a snapshot."""
    enforce_rate_limit("schema_snapshot", 10)
    engineer = get_database_engineer()
    return await engineer.capture_schema_snapshot()
