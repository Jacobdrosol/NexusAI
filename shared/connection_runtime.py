"""Non-UI runtime helpers for executing declared HTTP connections."""

from __future__ import annotations

import base64
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import yaml


def parse_openapi_actions(schema_text: str) -> list[dict[str, str]]:
    """Extract the HTTP operations explicitly declared by an OpenAPI document."""
    raw = (schema_text or "").strip()
    if not raw:
        return []
    try:
        document = json.loads(raw)
    except Exception:
        try:
            document = yaml.safe_load(raw)
        except Exception:
            return []
    if not isinstance(document, dict):
        return []
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return []

    actions: list[dict[str, str]] = []
    for path, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, operation in methods.items():
            normalized_method = str(method or "").strip().lower()
            if normalized_method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            operation_data = operation if isinstance(operation, dict) else {}
            actions.append(
                {
                    "operation_id": str(operation_data.get("operationId") or f"{normalized_method}_{path}").strip(),
                    "method": normalized_method.upper(),
                    "path": str(path),
                }
            )
    return actions


def _find_action(
    schema_text: str,
    operation_id: str | None,
    method: str | None,
    path: str | None,
) -> dict[str, str] | None:
    for action in parse_openapi_actions(schema_text):
        if operation_id and action["operation_id"] == operation_id:
            return action
        if method and path and action["method"] == method.upper() and action["path"] == path:
            return action
    return None


def _build_url(base_url: str, path: str, path_params: dict[str, Any] | None) -> str:
    resolved_path = path
    for key, value in (path_params or {}).items():
        resolved_path = resolved_path.replace("{" + str(key) + "}", urllib.parse.quote(str(value), safe=""))
    if base_url:
        return urllib.parse.urljoin(base_url.rstrip("/") + "/", resolved_path.lstrip("/"))
    return resolved_path


def _safe_result_url(url: str, auth: dict[str, Any]) -> str:
    """Avoid returning query-string credentials in task or connection-test output."""
    parsed = urllib.parse.urlparse(url)
    auth_type = str(auth.get("type") or "none").strip().lower()
    secret_query_key = ""
    if auth_type == "api_key" and str(auth.get("in") or "header").strip().lower() == "query":
        secret_query_key = str(auth.get("name") or "X-API-Key").strip()
    query = [
        (
            key,
            "[REDACTED]"
            if secret_query_key and key.casefold() == secret_query_key.casefold()
            else value,
        )
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    ]
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    if parsed.username or parsed.password:
        hostname = ("[REDACTED]@" + hostname) if hostname else "[REDACTED]"
    return urllib.parse.urlunparse(parsed._replace(netloc=hostname, query=urllib.parse.urlencode(query)))


def test_http_connection(
    *,
    config: dict[str, Any],
    auth: dict[str, Any],
    schema_text: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute one declared HTTP action and return non-secret response metadata."""
    base_url = str(config.get("base_url") or "").strip()
    timeout_seconds = int(config.get("timeout_seconds") or 15)
    verify_ssl = bool(config.get("verify_ssl", True))
    declared_actions = parse_openapi_actions(schema_text)
    action = _find_action(
        schema_text,
        operation_id=str(payload.get("operation_id") or "").strip() or None,
        method=str(payload.get("method") or "").strip() or None,
        path=str(payload.get("path") or "").strip() or None,
    )
    if declared_actions and action is None:
        return {"ok": False, "error": "Requested HTTP action is not declared in the connection schema."}

    method = str((action or {}).get("method") or payload.get("method") or "GET").upper()
    path = str((action or {}).get("path") or payload.get("path") or "/")
    url = _build_url(
        base_url,
        path,
        payload.get("path_params") if isinstance(payload.get("path_params"), dict) else {},
    )
    headers: dict[str, str] = {}
    configured_headers = config.get("headers")
    if isinstance(configured_headers, dict):
        headers.update({str(key): str(value) for key, value in configured_headers.items()})

    auth_type = str(auth.get("type") or "none").strip().lower()
    if auth_type == "api_key":
        key_name = str(auth.get("name") or "X-API-Key")
        key_value = str(auth.get("api_key") or "")
        if str(auth.get("in") or "header").strip().lower() == "query":
            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            query.append((key_name, key_value))
            url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
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

    action_headers = payload.get("headers")
    if isinstance(action_headers, dict):
        headers.update({str(key): str(value) for key, value in action_headers.items() if value is not None})
    query_params = payload.get("query_params")
    if isinstance(query_params, dict):
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.extend((str(key), str(value)) for key, value in query_params.items())
        url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))

    body_json = payload.get("body_json")
    body_bytes = None
    if body_json is not None:
        body_bytes = json.dumps(body_json).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url=url, method=method, headers=headers, data=body_bytes)
    ssl_context = None if verify_ssl else ssl._create_unverified_context()
    result_url = _safe_result_url(url, auth)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds, context=ssl_context) as response:
            body_preview = response.read(8000).decode("utf-8", errors="replace")
            return {
                "ok": 200 <= int(response.status) < 300,
                "status": int(response.status),
                "url": result_url,
                "method": method,
                "verify_ssl": verify_ssl,
                "body_preview": body_preview,
            }
    except urllib.error.HTTPError as exc:
        body_preview = exc.read(8000).decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": int(exc.code),
            "url": result_url,
            "method": method,
            "verify_ssl": verify_ssl,
            "body_preview": body_preview,
        }
