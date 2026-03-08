"""Helpers for bot connections: secret handling, OpenAPI parsing, and tests."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, make_url


_SECRET_KEYS = {"api_key", "bearer_token", "password"}


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


def _fernet() -> Fernet:
    secret = (os.environ.get("NEXUSAI_SECRET_KEY") or "dev-secret-change-in-production").encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def _encrypt(raw: str) -> str:
    if not raw:
        return ""
    return "enc:" + _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")


def _decrypt(raw: str) -> str:
    if not raw:
        return ""
    if not raw.startswith("enc:"):
        return raw
    token = raw[4:]
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return ""


def normalize_auth_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge and encrypt secret fields in auth payload."""
    base = dict(existing or {})
    incoming = dict(payload or {})
    for key, value in incoming.items():
        if key in _SECRET_KEYS:
            if str(value or "").strip() == "":
                continue
            base[key] = _encrypt(str(value))
        else:
            base[key] = value
    return base


def mask_auth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return auth payload with secret values redacted."""
    out = dict(payload or {})
    for key in _SECRET_KEYS:
        if key in out and str(out.get(key) or "").strip():
            out[key] = "[REDACTED]"
    return out


def resolve_auth_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return auth payload with encrypted values decrypted."""
    out = dict(payload or {})
    for key in _SECRET_KEYS:
        if key in out:
            out[key] = _decrypt(str(out.get(key) or ""))
    return out


def parse_openapi_actions(schema_text: str) -> list[dict[str, str]]:
    """Extract callable operations from an OpenAPI schema string."""
    raw = (schema_text or "").strip()
    if not raw:
        return []
    try:
        doc = json.loads(raw)
    except Exception:
        try:
            doc = yaml.safe_load(raw)
        except Exception:
            return []
    if not isinstance(doc, dict):
        return []
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return []
    actions: list[dict[str, str]] = []
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            m = str(method or "").strip().lower()
            if m not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            op_obj = op if isinstance(op, dict) else {}
            op_id = str(op_obj.get("operationId") or f"{m}_{path}").strip()
            actions.append(
                {
                    "operation_id": op_id,
                    "method": m.upper(),
                    "path": str(path),
                }
            )
    return actions


def _find_action(schema_text: str, operation_id: str | None, method: str | None, path: str | None) -> dict[str, str] | None:
    actions = parse_openapi_actions(schema_text)
    if operation_id:
        for a in actions:
            if a["operation_id"] == operation_id:
                return a
    if method and path:
        m = method.upper()
        for a in actions:
            if a["method"] == m and a["path"] == path:
                return a
    return None


def _build_url(base_url: str, path: str, path_params: dict[str, Any] | None) -> str:
    resolved = path
    for key, value in (path_params or {}).items():
        resolved = resolved.replace("{" + str(key) + "}", urllib.parse.quote(str(value), safe=""))
    if base_url:
        return urllib.parse.urljoin(base_url.rstrip("/") + "/", resolved.lstrip("/"))
    return resolved


def test_http_connection(
    *,
    config: dict[str, Any],
    auth: dict[str, Any],
    schema_text: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute a single HTTP action call and return response metadata."""
    base_url = str(config.get("base_url") or "").strip()
    timeout_seconds = int(config.get("timeout_seconds") or 15)
    op = _find_action(
        schema_text,
        operation_id=str(payload.get("operation_id") or "").strip() or None,
        method=str(payload.get("method") or "").strip() or None,
        path=str(payload.get("path") or "").strip() or None,
    )
    method = str((op or {}).get("method") or payload.get("method") or "GET").upper()
    path = str((op or {}).get("path") or payload.get("path") or "/")
    url = _build_url(base_url, path, payload.get("path_params") if isinstance(payload.get("path_params"), dict) else {})

    headers = {}
    cfg_headers = config.get("headers")
    if isinstance(cfg_headers, dict):
        headers.update({str(k): str(v) for k, v in cfg_headers.items()})

    auth_type = str(auth.get("type") or "none").strip().lower()
    if auth_type == "api_key":
        key_name = str(auth.get("name") or "X-API-Key")
        key_value = str(auth.get("api_key") or "")
        where = str(auth.get("in") or "header").strip().lower()
        if where == "query":
            parsed = urllib.parse.urlparse(url)
            q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            q.append((key_name, key_value))
            url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q)))
        else:
            headers[key_name] = key_value
    elif auth_type == "bearer":
        token = str(auth.get("bearer_token") or "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif auth_type == "basic":
        username = str(auth.get("username") or "")
        password = str(auth.get("password") or "")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("utf-8")
        headers["Authorization"] = f"Basic {token}"

    query_params = payload.get("query_params")
    if isinstance(query_params, dict):
        parsed = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        for k, v in query_params.items():
            q.append((str(k), str(v)))
        url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(q)))

    body_json = payload.get("body_json")
    body_bytes = None
    if body_json is not None:
        body_bytes = json.dumps(body_json).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, method=method, headers=headers, data=body_bytes)
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read(8000).decode("utf-8", errors="replace")
            return {
                "ok": 200 <= int(resp.status) < 300,
                "status": int(resp.status),
                "url": url,
                "method": method,
                "body_preview": raw,
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read(8000).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": int(exc.code),
            "url": url,
            "method": method,
            "body_preview": raw,
        }


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
