"""Helpers for bot connections: secret handling, OpenAPI parsing, and tests."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text


_SECRET_KEYS = {"api_key", "bearer_token", "password"}


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
    dsn = str(config.get("dsn") or "").strip()
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

