"""Read-only readiness checks for bot dispatch and scheduling."""
from __future__ import annotations

from typing import Any

from shared.exceptions import BotNotFoundError, WorkerNotFoundError
from shared.models import BackendConfig, Bot, Worker
from shared.worker_capabilities import required_worker_tools, worker_missing_tools


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
    expected_capability_type = (
        "tool"
        if str(backend.type or "").strip().lower() in {"browser", "cli"}
        else "llm"
    )
    for capability in worker.capabilities or []:
        if str(capability.type or "").strip().lower() != expected_capability_type:
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
    worker_probe_store: Any = None,
) -> dict[str, Any]:
    """Return non-secret operational checks for the bot's declared backend chain."""
    bot: Bot = await bot_registry.get(str(bot_id or "").strip())
    return await assess_bot_instance_readiness(
        bot,
        worker_registry=worker_registry,
        connection_resolver=connection_resolver,
        worker_probe_store=worker_probe_store,
    )


async def assess_bot_instance_readiness(
    bot: Bot,
    *,
    worker_registry: Any,
    connection_resolver: Any,
    worker_probe_store: Any = None,
) -> dict[str, Any]:
    """Assess a persisted or staged bot without exposing connection secrets."""
    checks: list[dict[str, Any]] = []
    required_tools = required_worker_tools(bot)
    viable_backends = 0
    ready_worker_backends = 0

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

        if backend_type in {"local_llm", "remote_llm", "cli", "browser"}:
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
            attestation_blocker = await _worker_attestation_blocker(
                worker_id=worker_id,
                backend=backend,
                worker_probe_store=worker_probe_store,
            )
            if attestation_blocker:
                checks.append(_check(label, "failed", attestation_blocker, backend_index=index))
                continue
            if not _worker_supports_backend(worker, backend):
                checks.append(
                    _check(
                        label,
                        "failed",
                        f"Worker '{worker_id}' does not advertise {backend.provider}/{backend.model}.",
                        backend_index=index,
                    )
                )
                continue
            missing_tools = worker_missing_tools(worker, required_tools)
            if missing_tools:
                checks.append(
                    _check(
                        label,
                        "failed",
                        f"Worker '{worker_id}' is missing required tool capabilities: {', '.join(missing_tools)}.",
                        backend_index=index,
                    )
                )
                continue
            viable_backends += 1
            ready_worker_backends += 1
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
                viable_backends += 1
                checks.append(_check(label, "ready", "One enabled HTTP connection is attached.", backend_index=index))
            else:
                viable_backends += 1
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
            viable_backends += 1
            if backend.api_key_ref:
                checks.append(_check(label, "ready", "Cloud API backend has a key reference.", backend_index=index))
            else:
                checks.append(_check(label, "warning", "Cloud API backend has no key reference.", backend_index=index))
            continue

        viable_backends += 1
        checks.append(_check(label, "warning", f"No readiness probe is available for {backend_type}/{provider}.", backend_index=index))

    if required_tools and ready_worker_backends == 0:
        checks.append(
            _check(
                "worker-tools",
                "failed",
                f"Required worker tools need a ready worker-backed backend: {', '.join(required_tools)}.",
            )
        )

    failed = [item for item in checks if item["status"] == "failed"]
    warnings = [item for item in checks if item["status"] == "warning"]
    blocking = [
        item
        for item in failed
        if item["component"] in {"bot", "backends", "worker-tools"}
    ]
    if viable_backends == 0:
        blocking.extend(
            item for item in failed if str(item["component"]).startswith("backend[")
        )
    return {
        "bot_id": bot.id,
        "ready": not blocking and viable_backends > 0,
        "summary": {
            "checks": len(checks),
            "failed": len(failed),
            "blocking": len(blocking),
            "warnings": len(warnings),
            "viable_backends": viable_backends,
        },
        "checks": checks,
    }


async def _worker_attestation_blocker(
    *,
    worker_id: str,
    backend: BackendConfig,
    worker_probe_store: Any,
) -> str:
    """Return a concrete nonsecret blocker from the latest worker evidence, if any."""
    if worker_probe_store is None:
        return ""
    try:
        probe = await worker_probe_store.get(worker_id)
    except Exception:
        return ""
    if not isinstance(probe, dict):
        return ""

    status = str(probe.get("probe_status") or "").strip().lower()
    if status and status != "ready":
        return f"Worker '{worker_id}' latest runtime probe is {status}."

    attestation = probe.get("capability_attestation")
    if not isinstance(attestation, dict):
        return ""
    backend_type = str(backend.type or "").strip().lower()
    if backend_type == "cli":
        unauthenticated = {
            str(tool or "").strip().lower()
            for tool in attestation.get("unauthenticated_cli_tools") or []
            if str(tool or "").strip()
        }
        tool_names = {str(backend.model or "").strip().lower()}
        command = str(backend.command or "").strip()
        if command:
            tool_names.add(command.split(maxsplit=1)[0].rsplit("/", 1)[-1].lower())
        tool_names.discard("")
        blocked_tools = sorted(tool_names & unauthenticated)
        if blocked_tools:
            return (
                f"Worker '{worker_id}' requires CLI authentication for "
                f"{', '.join(blocked_tools)}."
            )
    elif backend_type == "browser":
        browser = attestation.get("browser")
        if isinstance(browser, dict) and not bool(browser.get("ready")):
            reason = str(browser.get("reason") or "").strip()
            suffix = f": {reason}" if reason else "."
            return f"Worker '{worker_id}' browser runtime is not ready{suffix}"
    return ""
