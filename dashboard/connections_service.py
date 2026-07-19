"""Helpers for bot connections: secret handling, OpenAPI parsing, and tests."""
from __future__ import annotations

import shlex
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url

from shared.connection_secrets import (
    mask_auth_payload,
    mask_connection_config,
    normalize_auth_payload,
    normalize_connection_config,
    resolve_auth_payload,
    resolve_connection_config,
)
from shared.connection_runtime import parse_openapi_actions, test_http_connection

__all__ = (
    "mask_auth_payload",
    "mask_connection_config",
    "normalize_auth_payload",
    "normalize_connection_config",
    "resolve_auth_payload",
    "resolve_connection_config",
    "parse_openapi_actions",
    "test_database_connection",
    "test_http_connection",
)


def _mask_dsn_password(dsn: str) -> str:
    raw = str(dsn or "").strip()
    if not raw:
        return ""
    try:
        url = make_url(raw)
        if url.password:
            return url.render_as_string(hide_password=True)
        return raw
    except Exception:
        return raw


def test_database_connection(*, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Run a DB connectivity/query test using SQLAlchemy engine."""
    dsn = normalize_database_dsn(str(config.get("dsn") or "").strip())
    if not dsn:
        return {"ok": False, "error": "dsn is required"}
    readonly = bool(config.get("readonly", True))
    query = str(payload.get("query") or "SELECT 1").strip()
    qlower = query.lower()
    if readonly and not (qlower.startswith("select") or qlower.startswith("with")):
        return {"ok": False, "error": "readonly connection only allows SELECT/WITH queries"}

    engine = create_engine(dsn)
    with engine.connect() as conn:
        result = conn.execute(text(query))
        if result.returns_rows:
            rows = [dict(r._mapping) for r in result.fetchmany(25)]
            return {"ok": True, "rows": rows, "row_count": len(rows)}
        conn.commit()
        return {"ok": True, "rows": [], "row_count": 0}


def inspect_database_schema(*, config: dict[str, Any]) -> dict[str, Any]:
    """Return a schema snapshot suitable for project-level context ingestion."""
    dsn = normalize_database_dsn(str(config.get("dsn") or "").strip())
    if not dsn:
        return {"ok": False, "error": "dsn is required"}

    engine = create_engine(dsn)
    inspector = inspect(engine)
    default_schema = getattr(inspector, "default_schema_name", None)
    try:
        schema_names = inspector.get_schema_names()
    except Exception:
        schema_names = [default_schema] if default_schema else [None]

    blocked = {"information_schema", "pg_catalog", "pg_toast", "mysql", "performance_schema", "sys"}
    selected_schemas: list[str | None] = []
    for schema in schema_names or [None]:
        if schema and schema.lower() in blocked:
            continue
        selected_schemas.append(schema)
    if not selected_schemas:
        selected_schemas = [default_schema] if default_schema else [None]

    snapshot: dict[str, Any] = {
        "ok": True,
        "dialect": engine.dialect.name,
        "default_schema": default_schema,
        "schemas": [],
    }
    totals = {"tables": 0, "views": 0, "columns": 0, "foreign_keys": 0}

    with engine.connect():
        for schema in selected_schemas:
            schema_entry: dict[str, Any] = {
                "name": schema or default_schema or "default",
                "tables": [],
                "views": [],
            }
            table_names = inspector.get_table_names(schema=schema)
            view_names = inspector.get_view_names(schema=schema)

            for table_name in table_names:
                columns = inspector.get_columns(table_name, schema=schema)
                pk = inspector.get_pk_constraint(table_name, schema=schema) or {}
                foreign_keys = inspector.get_foreign_keys(table_name, schema=schema) or []
                indexes = inspector.get_indexes(table_name, schema=schema) or []
                schema_entry["tables"].append(
                    {
                        "name": table_name,
                        "columns": [
                            {
                                "name": str(column.get("name") or ""),
                                "type": str(column.get("type") or ""),
                                "nullable": bool(column.get("nullable", True)),
                                "default": column.get("default"),
                            }
                            for column in columns
                        ],
                        "primary_key": list(pk.get("constrained_columns") or []),
                        "foreign_keys": [
                            {
                                "constrained_columns": list(fk.get("constrained_columns") or []),
                                "referred_schema": fk.get("referred_schema"),
                                "referred_table": fk.get("referred_table"),
                                "referred_columns": list(fk.get("referred_columns") or []),
                            }
                            for fk in foreign_keys
                        ],
                        "indexes": [
                            {
                                "name": idx.get("name"),
                                "columns": list(idx.get("column_names") or []),
                                "unique": bool(idx.get("unique", False)),
                            }
                            for idx in indexes
                        ],
                    }
                )
                totals["tables"] += 1
                totals["columns"] += len(columns)
                totals["foreign_keys"] += len(foreign_keys)

            for view_name in view_names:
                schema_entry["views"].append({"name": view_name})
                totals["views"] += 1

            snapshot["schemas"].append(schema_entry)

    snapshot["totals"] = totals
    return snapshot


def render_database_schema_document(*, connection_name: str, snapshot: dict[str, Any]) -> str:
    """Render a schema snapshot into a readable vault document."""
    lines = [
        f"# Database Schema Snapshot: {connection_name}",
        "",
        f"Dialect: {snapshot.get('dialect') or 'unknown'}",
        f"Default schema: {snapshot.get('default_schema') or 'default'}",
    ]
    totals = snapshot.get("totals") if isinstance(snapshot.get("totals"), dict) else {}
    lines.extend(
        [
            "",
            "## Totals",
            f"- Tables: {int(totals.get('tables') or 0)}",
            f"- Views: {int(totals.get('views') or 0)}",
            f"- Columns: {int(totals.get('columns') or 0)}",
            f"- Foreign keys: {int(totals.get('foreign_keys') or 0)}",
        ]
    )
    schemas = snapshot.get("schemas") if isinstance(snapshot.get("schemas"), list) else []
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        lines.extend(["", f"## Schema: {schema.get('name') or 'default'}"])
        for table in schema.get("tables") or []:
            if not isinstance(table, dict):
                continue
            lines.extend(["", f"### Table: {table.get('name') or 'unknown'}"])
            primary_key = table.get("primary_key") or []
            if primary_key:
                lines.append(f"- Primary key: {', '.join(str(col) for col in primary_key)}")
            columns = table.get("columns") if isinstance(table.get("columns"), list) else []
            if columns:
                lines.append("- Columns:")
                for column in columns:
                    if not isinstance(column, dict):
                        continue
                    default = column.get("default")
                    default_text = f", default={default}" if default is not None else ""
                    lines.append(
                        "  - "
                        + f"{column.get('name')}: {column.get('type')} "
                        + ("NULL" if column.get("nullable", True) else "NOT NULL")
                        + default_text
                    )
            foreign_keys = table.get("foreign_keys") if isinstance(table.get("foreign_keys"), list) else []
            if foreign_keys:
                lines.append("- Foreign keys:")
                for fk in foreign_keys:
                    if not isinstance(fk, dict):
                        continue
                    source_cols = ", ".join(str(col) for col in fk.get("constrained_columns") or [])
                    target_cols = ", ".join(str(col) for col in fk.get("referred_columns") or [])
                    target_table = fk.get("referred_table") or "unknown"
                    target_schema = fk.get("referred_schema")
                    target_ref = f"{target_schema}.{target_table}" if target_schema else str(target_table)
                    lines.append(f"  - ({source_cols}) -> {target_ref} ({target_cols})")
        views = schema.get("views") if isinstance(schema.get("views"), list) else []
        if views:
            lines.append("")
            lines.append("### Views")
            for view in views:
                if isinstance(view, dict):
                    lines.append(f"- {view.get('name') or 'unknown'}")
    return "\n".join(lines).strip() + "\n"


def _parse_key_value_dsn(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    if ";" in raw and "=" in raw:
        chunks = [chunk.strip() for chunk in raw.split(";") if chunk.strip()]
    else:
        chunks = shlex.split(raw)
    for chunk in chunks:
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        pairs[key.strip().lower()] = value.strip().strip("'").strip('"')
    return pairs


def _normalize_postgres_sslmode(raw: str) -> str:
    value = str(raw or "").strip().lower().replace("_", "-")
    mapping = {
        "disable": "disable",
        "allow": "allow",
        "prefer": "prefer",
        "require": "require",
        "verifyca": "verify-ca",
        "verify-ca": "verify-ca",
        "verifyfull": "verify-full",
        "verify-full": "verify-full",
    }
    return mapping.get(value, value)


def normalize_database_dsn(raw: str) -> str:
    """Accept common DB connection-string variants and return a SQLAlchemy DSN."""
    dsn = str(raw or "").strip()
    if not dsn:
        return ""

    if dsn.startswith("postgres://"):
        dsn = "postgresql+psycopg2://" + dsn[len("postgres://"):]
    elif dsn.startswith("postgresql://"):
        dsn = "postgresql+psycopg2://" + dsn[len("postgresql://"):]

    try:
        make_url(dsn)
        return dsn
    except Exception:
        pass

    parts = _parse_key_value_dsn(dsn)
    if parts:
        server = parts.get("server") or parts.get("host")
        if server and server.startswith("tcp:"):
            server = server[4:]
        database = parts.get("database") or parts.get("dbname")
        username = parts.get("user") or parts.get("uid") or parts.get("user id") or parts.get("username")
        password = parts.get("password") or parts.get("pwd")
        port_raw = parts.get("port")
        port = int(port_raw) if port_raw and str(port_raw).isdigit() else None
        sslmode = _normalize_postgres_sslmode(parts.get("sslmode") or parts.get("ssl mode") or "")
        trust_server_certificate = str(
            parts.get("trust server certificate") or parts.get("trustservercertificate") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}

        if server and database:
            query: dict[str, str] = {}
            if sslmode:
                query["sslmode"] = sslmode
            if trust_server_certificate and sslmode in {"verify-ca", "verify-full"}:
                # Npgsql-style trust_server_certificate disables certificate verification.
                query["sslmode"] = "require"
            url = URL.create(
                "postgresql+psycopg2",
                username=username or None,
                password=password or None,
                host=server or None,
                port=port,
                database=database or None,
                query=query,
            )
            return url.render_as_string(hide_password=False)

    raise ValueError(
        "Could not parse database connection string. Use a SQLAlchemy URL or PostgreSQL key=value string."
    )
