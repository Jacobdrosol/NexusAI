"""Read-only readiness checks for bot dispatch and scheduling."""
from __future__ import annotations

from typing import Any

from shared.exceptions import BotNotFoundError, WorkerNotFoundError
from shared.models import BackendConfig, Bot, Worker


def _check(
    component: str,
    status: str,
    message: str,
    *,
    backend_index: int | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "component": component,
        "status": status,
        "message": message,
    }
    if backend_index is not None:
        item["backend_index"] = backend_index
    return item


def _worker_supports_backend(worker: Worker, backend: BackendConfig) -> bool:
    expected_provider = str(backend.provider or "").strip().lower()
    expected_model = str(backend.model or "").strip()
    for capability in worker.capabilities or []:
        if str(capability.type or "").strip().lower() != "llm":
            continue
        if str(capability.provider or "").strip().lower() != expected_provider:
            continue
        if expected_model in {str(model or "").strip() for model in capability.models or []}:
            return True
    return False


async def assess_bot_readiness(
    bot_id: str,
    *,
    bot_registry: Any,
    worker_registry: Any,
    connection_resolver: Any,
) -> dict[str, Any]:
    """Return non-secret operational checks for the bot's declared backend chain."""
    bot: Bot = await bot_registry.get(str(bot_id or "").strip())
    checks: list[dict[str, Any]] = []

    if bot.enabled:
        checks.append(_check("bot", "ready", "Bot is enabled."))
    else:
        checks.append(_check("bot", "failed", "Bot is disabled."))

    if not bot.backends:
        checks.append(_check("backends", "failed", "Bot has no configured backends."))

    for index, backend in enumerate(bot.backends):
        backend_type = str(backend.type or "").strip().lower()
        provider = str(backend.provider or "").strip().lower()
        label = f"backend[{index}]"

        if backend_type in {"local_llm", "remote_llm", "cli"}:
            worker_id = str(backend.worker_id or "").strip()
            if not worker_id:
                checks.append(_check(label, "failed", "Worker-backed backend is missing worker_id.", backend_index=index))
                continue
            try:
                worker: Worker = await worker_registry.get(worker_id)
            except WorkerNotFoundError:
                checks.append(_check(label, "failed", f"Worker '{worker_id}' is not registered.", backend_index=index))
                continue

            if not worker.enabled:
                checks.append(_check(label, "failed", f"Worker '{worker_id}' is disabled.", backend_index=index))
                continue
            if worker.status != "online":
                checks.append(_check(label, "failed", f"Worker '{worker_id}' is {worker.status}.", backend_index=index))
                continue
            if backend_type != "cli" and not _worker_supports_backend(worker, backend):
                checks.append(
                    _check(
                        label,
                        "failed",
                        f"Worker '{worker_id}' does not advertise {backend.provider}/{backend.model}.",
                        backend_index=index,
                    )
                )
                continue
            checks.append(_check(label, "ready", f"Worker '{worker_id}' is online and supports this backend.", backend_index=index))
            continue

        if backend_type == "custom" and provider == "http_connection":
            connections = connection_resolver.list_bot_connections(bot.id)
            http_connections = [
                connection
                for connection in connections
                if str(connection.get("kind") or "").strip().lower() == "http"
                and bool(connection.get("enabled", True))
            ]
            if not http_connections:
                checks.append(_check(label, "failed", "No enabled HTTP connection is attached to this bot.", backend_index=index))
            elif len(http_connections) == 1:
                checks.append(_check(label, "ready", "One enabled HTTP connection is attached.", backend_index=index))
            else:
                checks.append(
                    _check(
                        label,
                        "warning",
                        "Multiple HTTP connections are attached; tasks must select a connection explicitly.",
                        backend_index=index,
                    )
                )
            continue

        if backend_type == "cloud_api":
            if backend.api_key_ref:
                checks.append(_check(label, "ready", "Cloud API backend has a key reference.", backend_index=index))
            else:
                checks.append(_check(label, "warning", "Cloud API backend has no key reference.", backend_index=index))
            continue

        checks.append(_check(label, "warning", f"No readiness probe is available for {backend_type}/{provider}.", backend_index=index))

    failed = [item for item in checks if item["status"] == "failed"]
    warnings = [item for item in checks if item["status"] == "warning"]
    return {
        "bot_id": bot.id,
        "ready": not failed,
        "summary": {
            "checks": len(checks),
            "failed": len(failed),
            "warnings": len(warnings),
        },
        "checks": checks,
    }
