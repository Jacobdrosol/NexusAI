"""Read-only live diagnostics for registered worker nodes."""
from __future__ import annotations

import ipaddress
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from shared.models import Worker

_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9_](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?$")
_MAX_CAPABILITIES = 64
_MAX_LIST_ITEMS = 64
_MAX_VALUE_LENGTH = 160


class WorkerProbeError(ValueError):
    """Raised when a registered worker address cannot be safely probed."""


def _probe_timeout_seconds() -> float:
    try:
        configured = float(os.environ.get("NEXUSAI_WORKER_PROBE_TIMEOUT_SECONDS", "5"))
    except (TypeError, ValueError):
        configured = 5.0
    return min(max(configured, 0.5), 20.0)


def worker_base_url(worker: Worker) -> str:
    """Return the worker's HTTP base URL after rejecting URL-shaped host values."""
    host = str(worker.host or "").strip()
    port = int(worker.port or 0)
    if not 1 <= port <= 65535:
        raise WorkerProbeError("registered worker port is invalid")
    if not host or any(character in host for character in "/?#@\\\\"):
        raise WorkerProbeError("registered worker host is invalid")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _HOST_RE.fullmatch(host):
            raise WorkerProbeError("registered worker host is invalid") from None
        return f"http://{host}:{port}"

    if address.version == 6:
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def _safe_text(value: Any) -> str:
    return str(value or "").strip()[:_MAX_VALUE_LENGTH]


def _safe_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value[:_MAX_LIST_ITEMS]:
        normalized = _safe_text(item)
        if normalized:
            values.append(normalized)
    return values


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _normalize_capabilities(value: Any) -> tuple[list[dict[str, Any]], int]:
    """Return a bounded, nonsecret capability view and count malformed entries."""
    if not isinstance(value, list):
        return [], 1

    capabilities: list[dict[str, Any]] = []
    malformed = 0
    for item in value[:_MAX_CAPABILITIES]:
        if not isinstance(item, dict):
            malformed += 1
            continue
        capability_type = _safe_text(item.get("type")).lower()
        provider = _safe_text(item.get("provider")).lower()
        if not capability_type or not provider:
            malformed += 1
            continue
        capabilities.append(
            {
                "type": capability_type,
                "provider": provider,
                "models": _safe_string_list(item.get("models")),
            }
        )
    return capabilities, malformed


def _capability_contract_gaps(
    registered: list[dict[str, Any]], reported: list[dict[str, Any]]
) -> list[str]:
    reported_models: dict[tuple[str, str], set[str]] = {}
    for capability in reported:
        key = (capability["type"], capability["provider"])
        reported_models.setdefault(key, set()).update(
            model.lower() for model in capability["models"]
        )

    gaps: list[str] = []
    for capability in registered:
        key = (capability["type"], capability["provider"])
        actual_models = reported_models.get(key)
        if actual_models is None:
            gaps.append(f"{key[0]}/{key[1]}")
            continue
        missing_models = [
            model for model in capability["models"] if model.lower() not in actual_models
        ]
        if missing_models:
            gaps.append(f"{key[0]}/{key[1]} ({', '.join(missing_models)})")
    return gaps


def _safe_attestation(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    browser = value.get("browser") if isinstance(value.get("browser"), dict) else {}
    return {
        "configured_cli_tools": _safe_string_list(value.get("configured_cli_tools")),
        "installed_cli_tools": _safe_string_list(value.get("installed_cli_tools")),
        "enabled_cli_tools": _safe_string_list(value.get("enabled_cli_tools")),
        "unavailable_cli_tools": _safe_string_list(value.get("unavailable_cli_tools")),
        "auth_required_cli_tools": _safe_string_list(value.get("auth_required_cli_tools")),
        "unauthenticated_cli_tools": _safe_string_list(value.get("unauthenticated_cli_tools")),
        "discarded_declared_tool_capabilities": _safe_nonnegative_int(
            value.get("discarded_declared_tool_capabilities")
        ),
        "browser": {
            "configured": bool(browser.get("configured")),
            "ready": bool(browser.get("ready")),
            "reason": _safe_text(browser.get("reason")),
            "browser": _safe_text(browser.get("browser")),
        },
    }


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


async def _get_json(client: httpx.AsyncClient, url: str, label: str) -> dict[str, Any]:
    response = await client.get(url, headers={"Accept": "application/json"})
    if not response.is_success:
        raise WorkerProbeError(f"{label} endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise WorkerProbeError(f"{label} endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WorkerProbeError(f"{label} endpoint returned an invalid response")
    return payload


async def probe_worker(
    worker: Worker,
    *,
    client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
) -> dict[str, Any]:
    """Probe a worker's runtime without dispatching work or changing registry state."""
    checked_at = datetime.now(timezone.utc).isoformat()
    checks: list[dict[str, str]] = []
    result: dict[str, Any] = {
        "worker_id": worker.id,
        "worker_status": worker.status,
        "worker_enabled": worker.enabled,
        "dispatch_eligible": worker.enabled and worker.status == "online",
        "checked_at": checked_at,
        "probe_status": "unreachable",
        "health": {},
        "reported_capabilities": [],
        "capability_attestation": {},
        "checks": checks,
    }

    try:
        base_url = worker_base_url(worker)
    except WorkerProbeError as exc:
        checks.append(_check("registered_address", "fail", str(exc)))
        return result

    capabilities_error = ""
    try:
        async with client_factory(
            timeout=httpx.Timeout(_probe_timeout_seconds()), follow_redirects=False
        ) as client:
            health_payload = await _get_json(client, f"{base_url}/health", "health")
            try:
                capabilities_payload = await _get_json(
                    client, f"{base_url}/capabilities", "capabilities"
                )
            except WorkerProbeError as exc:
                capabilities_payload = None
                capabilities_error = str(exc)
            except httpx.HTTPError:
                capabilities_payload = None
                capabilities_error = "capabilities endpoint did not respond"
    except WorkerProbeError as exc:
        checks.append(_check("runtime_reachability", "fail", str(exc)))
        return result
    except httpx.HTTPError:
        checks.append(_check("runtime_reachability", "fail", "worker did not respond to probe"))
        return result

    health_status = _safe_text(health_payload.get("status")).lower()
    health_worker_id = _safe_text(health_payload.get("worker_id"))
    result["health"] = {
        "status": health_status,
        "worker_id": health_worker_id,
        "enabled_cli_tools": _safe_string_list(health_payload.get("enabled_cli_tools")),
        "browser_ready": bool(health_payload.get("browser_ready")),
    }
    if health_status == "ok":
        checks.append(_check("health", "pass", "health endpoint returned ok"))
    else:
        checks.append(_check("health", "fail", "health endpoint did not report ok"))
    if health_worker_id == worker.id:
        checks.append(_check("worker_identity", "pass", "runtime identity matches registration"))
    else:
        checks.append(
            _check("worker_identity", "fail", "runtime identity differs from registration")
        )

    if capabilities_payload is None:
        checks.append(_check("capability_report", "fail", capabilities_error))
        result["probe_status"] = "degraded"
        return result

    reported, malformed = _normalize_capabilities(
        capabilities_payload.get("configured_capabilities")
    )
    registered, _ = _normalize_capabilities(
        [capability.model_dump(mode="json") for capability in worker.capabilities]
    )
    result["reported_capabilities"] = reported
    result["capability_attestation"] = _safe_attestation(
        capabilities_payload.get("capability_attestation")
    )

    capability_worker_id = _safe_text(capabilities_payload.get("worker_id"))
    if capability_worker_id == worker.id:
        checks.append(
            _check("capability_identity", "pass", "capability report belongs to this worker")
        )
    else:
        checks.append(
            _check("capability_identity", "fail", "capability report belongs to another worker")
        )

    if malformed:
        checks.append(
            _check("capability_report", "fail", "capability report contains invalid entries")
        )
    else:
        gaps = _capability_contract_gaps(registered, reported)
        if gaps:
            checks.append(
                _check(
                    "capability_contract",
                    "fail",
                    "registered capabilities missing from runtime: " + "; ".join(gaps),
                )
            )
        else:
            checks.append(
                _check(
                    "capability_contract",
                    "pass",
                    "runtime satisfies registered capability contract",
                )
            )

    if worker.enabled and worker.status == "online":
        checks.append(
            _check("dispatch_state", "pass", "worker is enabled and registered online")
        )
    else:
        checks.append(
            _check("dispatch_state", "warn", "worker is not currently eligible for dispatch")
        )

    result["probe_status"] = (
        "ready" if all(check["status"] != "fail" for check in checks) else "degraded"
    )
    return result
