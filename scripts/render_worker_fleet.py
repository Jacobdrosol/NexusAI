#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import yaml


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "deploy" / "worker-fleet" / "workers.example.yaml"


def _default_output_dir() -> Path:
    configured_root = str(os.environ.get("NEXUSAI_PRIVATE_CONFIG_DIR") or "").strip()
    private_root = Path(configured_root).expanduser() if configured_root else Path.home() / ".nexusai"
    return private_root / "worker-fleet"


DEFAULT_OUTPUT_DIR = _default_output_dir()
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_RESERVED_WORKER_ENV = {
    "NEXUS_WORKER_CONFIG_PATH",
    "CONTROL_PLANE_URL",
    "CONTROL_PLANE_API_TOKEN",
    "NEXUS_WORKER_AUTO_REGISTER",
    "HEARTBEAT_INTERVAL",
    "NEXUS_WORKER_CLOUD_CONTEXT_POLICY",
    "OLLAMA_CLOUD_BASE_URL",
    "OLLAMA_API_KEY",
}
_DEFAULT_RESOURCE_LIMITS = {
    "cpus": "1.0",
    "memory": "1g",
    "pids_limit": 256,
}
_MEMORY_LIMIT = re.compile(r"^([1-9][0-9]*)(b|k|kb|kib|m|mb|mib|g|gb|gib)?$", re.IGNORECASE)
_COMPOSE_PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _merged_env(paths: list[Path]) -> dict[str, str]:
    merged = dict(os.environ)
    for path in paths:
        for key, value in _read_env_file(path).items():
            if not merged.get(key):
                merged[key] = value
    return merged


def _load_profile(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Worker fleet profile must be a YAML object.")
    if not isinstance(data.get("workers"), list):
        raise ValueError("Worker fleet profile must contain a workers list.")
    fleet = data.get("fleet") if isinstance(data.get("fleet"), dict) else {}
    data["workers"] = _expand_worker_replicas(data["workers"], fleet)
    return data


def _render_replica_text(value: Any, *, index: int, count: int, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    if "{index" in text:
        try:
            return text.format(index=index, count=count)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid replica template for {field}: {text!r}") from exc
    if count == 1:
        return text
    if field == "name":
        return f"{text} {index:02d}"
    return f"{text}-{index:02d}"


def _expand_worker_replicas(raw_workers: list[Any], fleet: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        max_workers = int(fleet.get("max_workers") or 64)
    except (TypeError, ValueError) as exc:
        raise ValueError("fleet.max_workers must be an integer between 1 and 256.") from exc
    if not 1 <= max_workers <= 256:
        raise ValueError("fleet.max_workers must be between 1 and 256.")

    expanded: list[dict[str, Any]] = []
    for raw_worker in raw_workers:
        if not isinstance(raw_worker, dict):
            raise ValueError("Worker fleet entries must be mappings.")
        try:
            replicas = int(raw_worker.get("replicas") or 1)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Worker {raw_worker.get('id') or raw_worker.get('name')} replicas must be an integer."
            ) from exc
        if not 1 <= replicas <= max_workers:
            raise ValueError(
                f"Worker {raw_worker.get('id') or raw_worker.get('name')} replicas must be between 1 and {max_workers}."
            )

        for index in range(1, replicas + 1):
            worker = copy.deepcopy(raw_worker)
            worker.pop("replicas", None)
            for field in ("id", "name", "service"):
                worker[field] = _render_replica_text(
                    worker.get(field),
                    index=index,
                    count=replicas,
                    field=field,
                )
            bot = worker.get("bot")
            if isinstance(bot, dict) and bot.get("id"):
                bot["id"] = _render_replica_text(
                    bot.get("id"),
                    index=index,
                    count=replicas,
                    field="bot.id",
                )
            expanded.append(worker)

    if len(expanded) > max_workers:
        raise ValueError(
            f"Worker fleet expands to {len(expanded)} workers, exceeding fleet.max_workers={max_workers}."
        )

    worker_ids = [str(worker.get("id") or "").strip() for worker in expanded]
    if not all(worker_ids) or len(worker_ids) != len(set(worker_ids)):
        raise ValueError("Expanded worker IDs must be present and unique.")
    return expanded


def _compose_project_name(fleet: dict[str, Any]) -> str:
    value = str(fleet.get("compose_project_name") or "").strip()
    if not value:
        return ""
    if not _COMPOSE_PROJECT_NAME.fullmatch(value):
        raise ValueError(
            "fleet.compose_project_name must contain only lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _slug(value: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return out or "worker"


def _resolve_path(raw: str, *, base: Path) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _mapping(value: Any, *, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping.")
    result: dict[str, str] = {}
    for key, source in value.items():
        target_name = str(key or "").strip()
        source_name = str(source or "").strip()
        if not _ENV_NAME.fullmatch(target_name):
            raise ValueError(f"{label} contains an invalid environment variable name: {target_name!r}")
        if not _ENV_NAME.fullmatch(source_name):
            raise ValueError(f"{label} source for {target_name!r} must be an environment variable name")
        if target_name in _RESERVED_WORKER_ENV:
            raise ValueError(f"{label} cannot override the reserved worker setting {target_name}")
        result[target_name] = source_name
    return result


def _worker_runtime(worker: dict[str, Any]) -> dict[str, Any]:
    value = worker.get("runtime")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} runtime must be a mapping.")
    return value


def _resource_limit_values(value: Any, *, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping.")
    allowed = {"cpus", "memory", "memory_reservation", "pids_limit"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} has unsupported fields: {', '.join(unknown)}")
    return dict(value)


def _memory_limit_bytes(value: Any, *, label: str) -> int:
    memory = str(value or "").strip()
    match = _MEMORY_LIMIT.fullmatch(memory)
    if not match:
        raise ValueError(f"{label} must be a positive Docker memory value")

    units = {
        "": 1,
        "b": 1,
        "k": 1_000,
        "kb": 1_000,
        "kib": 1_024,
        "m": 1_000_000,
        "mb": 1_000_000,
        "mib": 1_048_576,
        "g": 1_000_000_000,
        "gb": 1_000_000_000,
        "gib": 1_073_741_824,
    }
    return int(match.group(1)) * units[match.group(2).lower() if match.group(2) else ""]


def _validated_resource_budget(fleet: dict[str, Any]) -> dict[str, Any] | None:
    raw_budget = fleet.get("resource_budget")
    if raw_budget is None:
        return None
    if not isinstance(raw_budget, dict):
        raise ValueError("fleet.resource_budget must be a mapping.")

    allowed = {"cpus", "memory"}
    unknown = sorted(set(raw_budget) - allowed)
    if unknown:
        raise ValueError(f"fleet.resource_budget has unsupported fields: {', '.join(unknown)}")
    if "cpus" not in raw_budget or "memory" not in raw_budget:
        raise ValueError("fleet.resource_budget requires both cpus and memory.")

    try:
        cpus = float(str(raw_budget["cpus"]).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("fleet.resource_budget.cpus must be a positive number") from exc
    if not 0 < cpus <= 128:
        raise ValueError("fleet.resource_budget.cpus must be greater than zero and at most 128")

    memory = str(raw_budget["memory"]).strip()
    return {
        "cpus": cpus,
        "memory": memory,
        "memory_bytes": _memory_limit_bytes(memory, label="fleet.resource_budget.memory"),
    }


def _validate_enabled_worker_resource_budget(workers: list[dict[str, Any]], fleet: dict[str, Any]) -> None:
    budget = _validated_resource_budget(fleet)
    if budget is None:
        return

    enabled_workers = [worker for worker in workers if bool(worker.get("enabled", True))]
    total_cpus = sum(float(_validated_resource_limits(worker, fleet)["cpus"]) for worker in enabled_workers)
    total_memory_bytes = sum(
        _memory_limit_bytes(_validated_resource_limits(worker, fleet)["mem_limit"], label="worker resource limit memory")
        for worker in enabled_workers
    )

    if total_cpus > budget["cpus"]:
        raise ValueError(
            "Enabled workers exceed fleet.resource_budget.cpus "
            f"({total_cpus:g} > {budget['cpus']:g})."
        )
    if total_memory_bytes > budget["memory_bytes"]:
        raise ValueError(
            "Enabled workers exceed fleet.resource_budget.memory "
            f"({total_memory_bytes} bytes > {budget['memory']})."
        )


def _disabled_worker_compose_profile(fleet: dict[str, Any]) -> str:
    value = str(fleet.get("disabled_worker_compose_profile", "staged") or "").strip()
    if not value:
        return ""
    if not _COMPOSE_PROJECT_NAME.fullmatch(value):
        raise ValueError(
            "fleet.disabled_worker_compose_profile must contain only lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _validated_resource_limits(worker: dict[str, Any], fleet: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(worker.get("id") or worker.get("name") or "worker").strip()
    limits = dict(_DEFAULT_RESOURCE_LIMITS)
    limits.update(_resource_limit_values(fleet.get("resource_limits"), label="fleet.resource_limits"))
    limits.update(
        _resource_limit_values(
            _worker_runtime(worker).get("resource_limits"),
            label=f"worker {worker_id} runtime.resource_limits",
        )
    )

    cpus = str(limits.get("cpus") or "").strip()
    try:
        parsed_cpus = float(cpus)
    except ValueError as exc:
        raise ValueError(f"Worker {worker_id} resource limit cpus must be a positive number") from exc
    if not 0 < parsed_cpus <= 128:
        raise ValueError(f"Worker {worker_id} resource limit cpus must be greater than zero and at most 128")

    memory = str(limits.get("memory") or "").strip()
    _memory_limit_bytes(memory, label=f"Worker {worker_id} resource limit memory")

    compose_limits: dict[str, Any] = {
        "cpus": cpus,
        "mem_limit": memory,
    }
    memory_reservation = limits.get("memory_reservation")
    if memory_reservation is not None:
        normalized_reservation = str(memory_reservation).strip()
        _memory_limit_bytes(
            normalized_reservation,
            label=f"Worker {worker_id} resource limit memory_reservation",
        )
        compose_limits["mem_reservation"] = normalized_reservation

    pids_limit = limits.get("pids_limit")
    if isinstance(pids_limit, bool):
        raise ValueError(f"Worker {worker_id} resource limit pids_limit must be a positive integer")
    try:
        normalized_pids = int(pids_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Worker {worker_id} resource limit pids_limit must be a positive integer") from exc
    if normalized_pids < 32:
        raise ValueError(f"Worker {worker_id} resource limit pids_limit must be at least 32")
    compose_limits["pids_limit"] = normalized_pids
    return compose_limits


def _worker_runtime_limits(worker: dict[str, Any], fleet: dict[str, Any]) -> dict[str, Any]:
    limits = _validated_resource_limits(worker, fleet)
    result: dict[str, Any] = {
        "cpus": float(limits["cpus"]),
        "memory_limit": limits["mem_limit"],
        "pids_limit": limits["pids_limit"],
    }
    if "mem_reservation" in limits:
        result["memory_reservation"] = limits["mem_reservation"]
    return result


def _worker_tooling(worker: dict[str, Any]) -> dict[str, Any]:
    tooling = worker.get("tooling")
    if tooling is None:
        return {}
    if not isinstance(tooling, dict):
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} tooling must be a mapping.")
    return tooling


def _worker_cli_tools(worker: dict[str, Any]) -> list[str]:
    return _as_list(_worker_tooling(worker).get("cli_tools"))


def _worker_browser_tooling(worker: dict[str, Any]) -> dict[str, Any] | None:
    """Return a validated private browser runtime declaration when configured."""

    raw_browser = _worker_tooling(worker).get("browser")
    if raw_browser is None:
        return None
    if not isinstance(raw_browser, dict):
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} tooling.browser must be a mapping.")

    allowed_fields = {
        "enabled",
        "base_url",
        "allowed_paths",
        "user_data_dir",
        "request_token_env",
        "headless",
        "timeout_seconds",
    }
    unknown_fields = sorted(set(raw_browser) - allowed_fields)
    if unknown_fields:
        raise ValueError(
            f"Worker {worker.get('id') or worker.get('name')} tooling.browser has unsupported fields: "
            + ", ".join(unknown_fields)
        )

    browser = dict(raw_browser)
    browser["enabled"] = bool(browser.get("enabled", True))
    if not browser["enabled"]:
        return browser

    base_url = str(browser.get("base_url") or "").strip()
    parsed_base_url = urlsplit(base_url)
    if (
        parsed_base_url.scheme not in {"http", "https"}
        or not parsed_base_url.netloc
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.query
        or parsed_base_url.fragment
    ):
        raise ValueError(
            f"Worker {worker.get('id') or worker.get('name')} tooling.browser.base_url must be an origin or base path without credentials, query, or fragment."
        )
    browser["base_url"] = base_url.rstrip("/")

    allowed_paths = _as_list(browser.get("allowed_paths"))
    if not allowed_paths:
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} tooling.browser.allowed_paths is required.")
    for path in allowed_paths:
        parsed_path = urlsplit(path[:-1] if path.endswith("/*") else path)
        if (
            parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or not parsed_path.path.startswith("/")
            or parsed_path.path.startswith("//")
            or "\\" in parsed_path.path
        ):
            raise ValueError(
                f"Worker {worker.get('id') or worker.get('name')} tooling.browser.allowed_paths must contain relative absolute paths only."
            )
    browser["allowed_paths"] = allowed_paths

    user_data_dir = str(browser.get("user_data_dir") or "").strip()
    if not user_data_dir:
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} tooling.browser.user_data_dir is required.")
    browser["user_data_dir"] = user_data_dir

    token_env = str(browser.get("request_token_env") or "").strip()
    if not _ENV_NAME.fullmatch(token_env):
        raise ValueError(
            f"Worker {worker.get('id') or worker.get('name')} tooling.browser.request_token_env must be an environment variable name."
        )
    browser["request_token_env"] = token_env

    timeout_seconds = int(browser.get("timeout_seconds") or 30)
    if not 1 <= timeout_seconds <= 120:
        raise ValueError(
            f"Worker {worker.get('id') or worker.get('name')} tooling.browser.timeout_seconds must be between 1 and 120."
        )
    browser["timeout_seconds"] = timeout_seconds
    browser["headless"] = bool(browser.get("headless", True))
    return browser


def _first_model(worker: dict[str, Any], fleet: dict[str, Any]) -> str:
    models = _as_list(worker.get("models"))
    if models:
        return models[0]
    model = str(worker.get("model") or fleet.get("default_model") or "").strip()
    if not model:
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} has no model.")
    return model


def _worker_models(worker: dict[str, Any], fleet: dict[str, Any]) -> list[str]:
    models = _as_list(worker.get("models"))
    if models:
        return models
    return [_first_model(worker, fleet)]


def _worker_provider(worker: dict[str, Any], fleet: dict[str, Any]) -> str:
    provider = str(worker.get("provider") or fleet.get("provider") or "ollama_cloud").strip()
    if not provider:
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} has no provider.")
    return provider


def _worker_service(worker: dict[str, Any]) -> str:
    return _slug(str(worker.get("service") or worker.get("id") or worker.get("name")))


def _worker_request_token_env(worker: dict[str, Any], fleet: dict[str, Any]) -> str:
    value = str(worker.get("request_token_env") or fleet.get("worker_request_token_env") or "").strip()
    if not value:
        return ""
    if not _ENV_NAME.fullmatch(value):
        raise ValueError(
            f"Worker {worker.get('id') or worker.get('name')} request_token_env must be an environment variable name."
        )
    return value


def _worker_config(worker: dict[str, Any], fleet: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(worker.get("id") or "").strip()
    if not worker_id:
        raise ValueError("Every worker entry needs an id.")
    service = _worker_service(worker)
    port = int(worker.get("port") or fleet.get("internal_worker_port") or 8010)
    capabilities: list[dict[str, Any]] = [
        {
            "type": "llm",
            "provider": _worker_provider(worker, fleet),
            "models": _worker_models(worker, fleet),
        }
    ]
    cli_tools = _worker_cli_tools(worker)
    if cli_tools:
        capabilities.append({"type": "tool", "provider": "cli", "models": cli_tools})
    config = {
        "id": worker_id,
        "name": str(worker.get("name") or worker_id).strip(),
        "host": service,
        "listen_host": "0.0.0.0",
        "port": port,
        "status": "offline",
        "enabled": bool(worker.get("enabled", True)),
        "capabilities": capabilities,
        "metrics": {},
        "runtime_limits": _worker_runtime_limits(worker, fleet),
    }
    tooling: dict[str, Any] = {}
    if cli_tools:
        tooling["cli_tools"] = cli_tools
    browser = _worker_browser_tooling(worker)
    if browser is not None:
        tooling["browser"] = browser
    if tooling:
        config["tooling"] = tooling
    request_token_env = _worker_request_token_env(worker, fleet)
    if request_token_env:
        config["request_token_env"] = request_token_env
    return config


def _system_prompt(worker: dict[str, Any]) -> str:
    bot = worker.get("bot") if isinstance(worker.get("bot"), dict) else {}
    custom = str(bot.get("system_prompt") or "").strip()
    if custom:
        return custom
    name = str(worker.get("name") or worker.get("id") or "Worker").strip()
    role = str(worker.get("role") or "worker").strip()
    return (
        f"You are {name}, a NexusAI worker bot for the {role} role. "
        "Follow the worker_profile in routing_rules exactly. Stay within assigned scope, "
        "ask for owner direction when blocked, and do not perform mutations that the profile "
        "does not explicitly allow."
    )


def _bot_backends(worker: dict[str, Any], fleet: dict[str, Any]) -> list[dict[str, Any]]:
    bot = worker.get("bot") if isinstance(worker.get("bot"), dict) else {}
    configured = bot.get("backends")
    if configured is None:
        return [
            {
                "type": "remote_llm",
                "worker_id": str(worker["id"]).strip(),
                "provider": _worker_provider(worker, fleet),
                "model": _first_model(worker, fleet),
                "params": {
                    **dict(fleet.get("backend_params") or {}),
                    **dict(bot.get("backend_params") or {}),
                },
            }
        ]
    if not isinstance(configured, list) or not configured:
        raise ValueError(f"Worker {worker.get('id')} bot.backends must be a non-empty list.")

    allowed_fields = {
        "type",
        "worker_id",
        "provider",
        "model",
        "params",
        "api_key_ref",
        "gpu_id",
        "command",
    }
    allowed_types = {"local_llm", "remote_llm", "cloud_api", "cli", "browser", "custom"}
    worker_id = str(worker["id"]).strip()
    cli_tools = set(_worker_cli_tools(worker))
    browser = _worker_browser_tooling(worker)
    resolved: list[dict[str, Any]] = []
    for index, raw_backend in enumerate(configured):
        if not isinstance(raw_backend, dict):
            raise ValueError(f"Worker {worker_id} bot.backends[{index}] must be a mapping.")
        unknown_fields = set(raw_backend) - allowed_fields
        if unknown_fields:
            fields = ", ".join(sorted(unknown_fields))
            raise ValueError(f"Worker {worker_id} bot.backends[{index}] has unsupported fields: {fields}")
        backend_type = str(raw_backend.get("type") or "").strip()
        provider = str(raw_backend.get("provider") or "").strip()
        model = str(raw_backend.get("model") or "").strip()
        if backend_type not in allowed_types:
            raise ValueError(f"Worker {worker_id} bot.backends[{index}] has unsupported type {backend_type!r}")
        if not provider or not model:
            raise ValueError(f"Worker {worker_id} bot.backends[{index}] needs provider and model values.")

        backend: dict[str, Any] = {
            "type": backend_type,
            "provider": provider,
            "model": model,
        }
        if backend_type in {"local_llm", "remote_llm", "cli", "browser"}:
            backend["worker_id"] = str(raw_backend.get("worker_id") or worker_id).strip()
        elif raw_backend.get("worker_id"):
            backend["worker_id"] = str(raw_backend["worker_id"]).strip()

        for field in ("api_key_ref", "gpu_id"):
            value = str(raw_backend.get(field) or "").strip()
            if value:
                backend[field] = value
        if raw_backend.get("params") is not None:
            if not isinstance(raw_backend["params"], dict):
                raise ValueError(f"Worker {worker_id} bot.backends[{index}].params must be a mapping.")
            backend["params"] = raw_backend["params"]

        command = str(raw_backend.get("command") or "").strip()
        if backend_type == "cli":
            if not command:
                raise ValueError(f"Worker {worker_id} bot.backends[{index}] needs a fixed CLI command.")
            try:
                executable = shlex.split(command, posix=True)[0].replace("\\", "/").rsplit("/", 1)[-1]
            except (IndexError, ValueError) as exc:
                raise ValueError(f"Worker {worker_id} bot.backends[{index}] has an invalid CLI command.") from exc
            if executable not in cli_tools:
                raise ValueError(
                    f"Worker {worker_id} bot.backends[{index}] command executable {executable!r} "
                    "is not declared in tooling.cli_tools."
                )
            backend["command"] = command
        elif backend_type == "browser":
            if not isinstance(browser, dict) or not bool(browser.get("enabled")):
                raise ValueError(
                    f"Worker {worker_id} bot.backends[{index}] uses a browser backend without enabled tooling.browser."
                )
            if provider.casefold() != "browser" or model.casefold() != "browser-ui":
                raise ValueError(
                    f"Worker {worker_id} bot.backends[{index}] must use provider=browser and model=browser-ui."
                )
            if not str(backend.get("api_key_ref") or "").strip():
                raise ValueError(
                    f"Worker {worker_id} bot.backends[{index}] requires api_key_ref for the browser request token."
                )
            if command:
                raise ValueError(f"Worker {worker_id} bot.backends[{index}] command is only valid for cli backends.")
        elif command:
            raise ValueError(f"Worker {worker_id} bot.backends[{index}] command is only valid for cli backends.")
        resolved.append(backend)
    return resolved


def _bot_routing_rules(worker: dict[str, Any]) -> dict[str, Any]:
    bot = worker.get("bot") if isinstance(worker.get("bot"), dict) else {}
    raw_rules = bot.get("routing_rules")
    if raw_rules is None:
        return {}
    if not isinstance(raw_rules, dict):
        raise ValueError(f"Worker {worker.get('id')} bot.routing_rules must be a mapping.")
    reserved = {"worker_profile", "launch_profile"}
    conflicts = sorted(reserved & set(raw_rules))
    if conflicts:
        raise ValueError(
            f"Worker {worker.get('id')} bot.routing_rules cannot override renderer-managed fields: "
            + ", ".join(conflicts)
        )
    return dict(raw_rules)


def _bot_payload(worker: dict[str, Any], fleet: dict[str, Any]) -> dict[str, Any]:
    bot = worker.get("bot") if isinstance(worker.get("bot"), dict) else {}
    worker_id = str(worker["id"]).strip()
    bot_id = str(bot.get("id") or f"{worker_id}-bot").strip()
    backends = _bot_backends(worker, fleet)
    primary_backend = backends[0]
    configured_policy = bot.get("execution_policy") if isinstance(bot.get("execution_policy"), dict) else {}
    required_worker_tools = _as_list(configured_policy.get("required_worker_tools"))
    if any(str(backend.get("type") or "").strip().lower() == "browser" for backend in backends):
        required_worker_tools = list(dict.fromkeys([*required_worker_tools, "browser-ui"]))
    routing_rules = _bot_routing_rules(worker)
    routing_rules["worker_profile"] = {
        "worker_id": worker_id,
        "service": _worker_service(worker),
        "role": str(worker.get("role") or "").strip(),
        "can_edit": bool(worker.get("can_edit", False)),
        "task_scope": str(worker.get("task_scope") or "").strip(),
        "allowed_pages": _as_list(worker.get("allowed_pages")),
        "course_scope": _as_list(worker.get("course_scope")),
        "lesson_scope": _as_list(worker.get("lesson_scope")),
        "cli_tools": _worker_cli_tools(worker),
    }
    routing_rules["launch_profile"] = {
        "worker_node_service": _worker_service(worker),
        "backend_type": primary_backend["type"],
        "provider": primary_backend["provider"],
        "model": primary_backend["model"],
    }
    execution_policy = {
        "repo_output_mode": str(configured_policy.get("repo_output_mode") or "deny"),
        "workspace_context_injection": bool(configured_policy.get("workspace_context_injection", False)),
        "inline_coding_default": bool(configured_policy.get("inline_coding_default", False)),
        "required_worker_tools": required_worker_tools,
        "can_apply_db_actions": bool(configured_policy.get("can_apply_db_actions", False)),
        "allow_run_result_ingest": bool(configured_policy.get("allow_run_result_ingest", True)),
    }
    for field in (
        "browser_action_allowlist",
        "browser_action_owner_approval_required",
        "documentation_action_allowlist",
        "connection_action_allowlist",
        "connection_action_owner_approval_required",
    ):
        values = _as_list(configured_policy.get(field))
        if values:
            execution_policy[field] = values

    return {
        "id": bot_id,
        "name": str(bot.get("name") or worker.get("name") or bot_id).strip(),
        "role": str(worker.get("role") or bot.get("role") or "worker").strip(),
        "system_prompt": _system_prompt(worker),
        "priority": int(bot.get("priority") or fleet.get("bot_priority") or 0),
        "enabled": bool(bot.get("enabled", True)),
        "backends": backends,
        "context_access": {
            "receives": ["instruction", "job", "worker_profile"],
            "can_self_serve": [],
        },
        "execution_policy": execution_policy,
        "routing_rules": routing_rules,
    }


def _catalog_model_id(provider: str, model: str) -> str:
    return f"fleet-{_slug(provider)}-{_slug(model)}"


def _catalog_models_for_bots(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the non-secret catalog entries required by generated bot backends."""

    models_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    keys_by_id: dict[str, tuple[str, str]] = {}
    for bot in bots:
        for backend in bot.get("backends") or []:
            if not isinstance(backend, dict):
                continue
            if str(backend.get("type") or "").strip().lower() == "browser":
                continue
            provider = str(backend.get("provider") or "").strip()
            model = str(backend.get("model") or "").strip()
            if not provider or not model:
                continue
            key = (provider.casefold(), model.casefold())
            model_id = _catalog_model_id(provider, model)
            existing_key = keys_by_id.get(model_id)
            if existing_key is not None and existing_key != key:
                raise ValueError(
                    "Generated catalog model id collision between "
                    f"{existing_key[0]}/{existing_key[1]} and {provider}/{model}."
                )
            keys_by_id[model_id] = key
            models_by_key.setdefault(
                key,
                {
                    "id": model_id,
                    "name": model,
                    "provider": provider,
                    "capabilities": [],
                    "enabled": True,
                },
            )
    return sorted(
        models_by_key.values(),
        key=lambda item: (item["provider"].casefold(), item["name"].casefold()),
    )


def _write_env_file(
    path: Path,
    *,
    control_plane_url: str,
    control_plane_token: str,
    ollama_api_key: str,
    ollama_cloud_base_url: str,
    cloud_context_policy: str,
    heartbeat_interval_seconds: int,
    extra_env: dict[str, str],
) -> None:
    lines = [
        "NEXUS_WORKER_CONFIG_PATH=/app/worker.yaml",
        f"CONTROL_PLANE_URL={control_plane_url}",
        f"CONTROL_PLANE_API_TOKEN={control_plane_token}",
        "NEXUS_WORKER_AUTO_REGISTER=1",
        f"HEARTBEAT_INTERVAL={heartbeat_interval_seconds}",
        f"NEXUS_WORKER_CLOUD_CONTEXT_POLICY={cloud_context_policy}",
        f"OLLAMA_CLOUD_BASE_URL={ollama_cloud_base_url}",
        f"OLLAMA_API_KEY={ollama_api_key}",
    ]
    for key, value in sorted(extra_env.items()):
        if "\n" in value or "\r" in value:
            raise ValueError(f"Worker environment value for {key} cannot contain a newline")
        lines.append(f"{key}={value}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _worker_node_env(
    worker: dict[str, Any],
    fleet: dict[str, Any],
    env: dict[str, str],
    warnings: list[str],
) -> dict[str, str]:
    runtime = _worker_runtime(worker)
    sources = _mapping(fleet.get("node_env_from"), label="fleet.node_env_from")
    sources.update(
        _mapping(
            runtime.get("env_from"),
            label=f"worker {worker.get('id') or worker.get('name')} runtime.env_from",
        )
    )
    values: dict[str, str] = {}
    for target_name, source_name in sorted(sources.items()):
        value = str(env.get(source_name, "")).strip()
        if not value:
            warnings.append(
                f"Missing node runtime env value: {source_name} for worker {worker.get('id') or worker.get('name')}"
            )
            continue
        values[target_name] = value
    request_token_env = _worker_request_token_env(worker, fleet)
    if request_token_env:
        token = str(env.get(request_token_env, "")).strip()
        if not token:
            warnings.append(
                f"Missing worker request token: {request_token_env} for worker {worker.get('id') or worker.get('name')}"
            )
        else:
            values[request_token_env] = token
    return values


def _service_image_and_build(
    worker: dict[str, Any],
    fleet: dict[str, Any],
    *,
    profile_base: Path,
    worker_node_source: Path,
) -> tuple[str, dict[str, str] | None]:
    runtime = _worker_runtime(worker)
    image = str(runtime.get("image") or fleet.get("image") or "nexus-worker-node:latest").strip()
    if not image:
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} has an empty runtime image")

    if "build" not in runtime:
        if "image" in runtime:
            return image, None
        return image, {"context": worker_node_source.as_posix(), "dockerfile": "Dockerfile"}

    raw_build = runtime.get("build")
    if raw_build in (None, False):
        return image, None
    if isinstance(raw_build, str):
        return image, {"context": _resolve_path(raw_build, base=profile_base).as_posix(), "dockerfile": "Dockerfile"}
    if not isinstance(raw_build, dict):
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} runtime.build must be false, a path, or a mapping.")
    raw_context = str(raw_build.get("context") or "").strip()
    build_context = _resolve_path(raw_context, base=profile_base) if raw_context else worker_node_source
    dockerfile = str(raw_build.get("dockerfile") or "Dockerfile").strip()
    if not dockerfile:
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} runtime.build.dockerfile is empty")
    return image, {"context": build_context.as_posix(), "dockerfile": dockerfile}


def _build_identity(image: str, build: dict[str, str]) -> str:
    """Return a stable key for a reusable compose image build definition."""
    return json.dumps({"image": image, "build": build}, sort_keys=True)


def render(profile_path: Path, output_dir: Path, env: dict[str, str], *, allow_missing_ollama_key: bool = False) -> dict[str, Any]:
    profile = _load_profile(profile_path)
    fleet = profile.get("fleet") if isinstance(profile.get("fleet"), dict) else {}
    workers = [item for item in profile["workers"] if isinstance(item, dict)]
    if not workers:
        raise ValueError("Worker fleet profile has no worker entries.")
    _validate_enabled_worker_resource_budget(workers, fleet)
    disabled_worker_profile = _disabled_worker_compose_profile(fleet)

    token_env = str(fleet.get("control_plane_token_env") or "CONTROL_PLANE_API_TOKEN")
    control_plane_token = str(env.get(token_env, "")).strip()
    if not control_plane_token:
        raise ValueError(f"Missing control-plane token env value: {token_env}")

    ollama_key_env = str(fleet.get("ollama_api_key_env") or "OLLAMA_API_KEY")
    ollama_api_key = str(env.get(ollama_key_env, "")).strip()
    uses_ollama_cloud = any(_worker_provider(worker, fleet) == "ollama_cloud" for worker in workers)
    warnings: list[str] = []
    if uses_ollama_cloud and not ollama_api_key:
        message = f"Missing Ollama Cloud API key env value: {ollama_key_env}"
        if not allow_missing_ollama_key:
            raise ValueError(message)
        warnings.append(message)

    worker_node_source = _resolve_path(
        str(fleet.get("worker_node_source") or "../../../nexus-worker-node"),
        base=profile_path.parent,
    )
    control_plane_url = str(fleet.get("control_plane_url") or "http://control_plane:8000").strip()
    network_name = str(fleet.get("docker_network") or "nexusai_nexus-net").strip()
    compose_project_name = _compose_project_name(fleet)
    image = str(fleet.get("image") or "nexus-worker-node:latest").strip()
    cloud_context_policy = str(fleet.get("cloud_context_policy") or "redact").strip().lower()
    if cloud_context_policy not in {"allow", "redact", "block"}:
        cloud_context_policy = "redact"
    heartbeat = int(fleet.get("heartbeat_interval_seconds") or 15)
    ollama_base_url = str(fleet.get("ollama_cloud_base_url") or "https://ollama.com/api").strip()

    configs_dir = output_dir / "workers"
    env_dir = output_dir / "env"
    bots_dir = output_dir / "bots"
    models_dir = output_dir / "models"
    output_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)
    env_dir.mkdir(parents=True, exist_ok=True)
    bots_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    compose: dict[str, Any] = {
        "services": {},
        "networks": {
            "nexus-net": {
                "external": True,
                "name": network_name,
            }
        },
    }
    if compose_project_name:
        compose["name"] = compose_project_name
    rendered_workers: list[dict[str, str]] = []
    rendered_bots: list[str] = []
    rendered_bot_payloads: list[dict[str, Any]] = []
    emitted_builds: set[str] = set()

    for worker in workers:
        worker_id = str(worker["id"]).strip()
        service = _worker_service(worker)
        worker_config = _worker_config(worker, fleet)
        config_path = configs_dir / f"{worker_id}.yaml"
        env_path = env_dir / f"{worker_id}.env"
        bot_path = bots_dir / f"{worker_id}.bot.json"
        node_env = _worker_node_env(worker, fleet, env, warnings)
        with config_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(worker_config, fh, sort_keys=False)
        _write_env_file(
            env_path,
            control_plane_url=control_plane_url,
            control_plane_token=control_plane_token,
            ollama_api_key=ollama_api_key,
            ollama_cloud_base_url=ollama_base_url,
            cloud_context_policy=cloud_context_policy,
            heartbeat_interval_seconds=heartbeat,
            extra_env=node_env,
        )
        bot_payload = _bot_payload(worker, fleet)
        bot_path.write_text(json.dumps(bot_payload, indent=2) + "\n", encoding="utf-8")
        rendered_bots.append(bot_payload["id"])
        rendered_bot_payloads.append(bot_payload)

        image, build = _service_image_and_build(
            worker,
            fleet,
            profile_base=profile_path.parent,
            worker_node_source=worker_node_source,
        )
        service_config: dict[str, Any] = {
            "image": image,
            "restart": "unless-stopped",
            **_validated_resource_limits(worker, fleet),
            "env_file": [env_path.resolve().as_posix()],
            "volumes": [f"{config_path.resolve().as_posix()}:/app/worker.yaml:ro"],
            "networks": ["nexus-net"],
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://localhost:8010/health')",
                ],
                "interval": "30s",
                "timeout": "10s",
                "retries": 3,
            },
        }
        runtime = _worker_runtime(worker)
        volumes = runtime.get("volumes")
        if volumes is not None:
            if not isinstance(volumes, list) or not all(isinstance(value, str) and value.strip() for value in volumes):
                raise ValueError(f"Worker {worker_id} runtime.volumes must be a list of non-empty compose volume strings.")
            service_config["volumes"].extend(volumes)
        shm_size = str(runtime.get("shm_size") or "").strip()
        if shm_size:
            service_config["shm_size"] = shm_size
        if build is not None:
            build_key = _build_identity(image, build)
            if build_key not in emitted_builds:
                service_config["build"] = build
                emitted_builds.add(build_key)
        if not bool(worker.get("enabled", True)) and disabled_worker_profile:
            service_config["profiles"] = [disabled_worker_profile]
        compose["services"][service] = service_config
        rendered_workers.append(
            {
                "id": worker_id,
                "name": worker_config["name"],
                "service": service,
                "bot_id": bot_payload["id"],
            }
        )

    catalog_models = _catalog_models_for_bots(rendered_bot_payloads)
    catalog_models_path = models_dir / "catalog-models.json"
    catalog_models_path.write_text(json.dumps(catalog_models, indent=2) + "\n", encoding="utf-8")

    compose_path = output_dir / "docker-compose.worker-node.generated.yml"
    with compose_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(compose, fh, sort_keys=False)

    summary = {
        "profile": str(profile_path),
        "output_dir": str(output_dir),
        "compose_path": str(compose_path),
        "worker_node_source": str(worker_node_source),
        "compose_project_name": compose_project_name,
        "docker_network": network_name,
        "workers": rendered_workers,
        "bots": rendered_bots,
        "models": catalog_models,
        "warnings": warnings,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Nexus-API-Key"] = token
    return headers


def apply_models(output_dir: Path, *, api_url: str, api_token: str) -> list[dict[str, Any]]:
    """Register only catalog entries missing from the control plane.

    A fleet profile declares what its generated bots need, but should never overwrite a
    catalog record that an operator has already curated with cost or capability metadata.
    """

    models_path = output_dir / "models" / "catalog-models.json"
    if not models_path.exists():
        raise ValueError(f"Rendered catalog model file not found: {models_path}")
    desired = json.loads(models_path.read_text(encoding="utf-8"))
    if not isinstance(desired, list):
        raise ValueError("Rendered catalog model file must contain a list.")

    headers = _headers(api_token)
    base = api_url.rstrip("/")
    existing_response = requests.get(f"{base}/v1/models", headers=headers, timeout=30)
    existing_response.raise_for_status()
    existing = existing_response.json()
    if not isinstance(existing, list):
        raise ValueError("Control-plane model catalog response must be a list.")

    existing_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    existing_by_id: dict[str, dict[str, Any]] = {}
    for item in existing:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        provider = str(item.get("provider") or "").strip()
        name = str(item.get("name") or "").strip()
        if model_id:
            existing_by_id[model_id] = item
        if provider and name:
            existing_by_key[(provider.casefold(), name.casefold())] = item

    results: list[dict[str, Any]] = []
    for model in desired:
        if not isinstance(model, dict):
            results.append({"ok": False, "action": "validate", "detail": "invalid rendered model"})
            continue
        model_id = str(model.get("id") or "").strip()
        provider = str(model.get("provider") or "").strip()
        name = str(model.get("name") or "").strip()
        if not model_id or not provider or not name:
            results.append(
                {
                    "ok": False,
                    "action": "validate",
                    "detail": "rendered model is missing id, provider, or name",
                }
            )
            continue
        key = (provider.casefold(), name.casefold())
        current = existing_by_key.get(key)
        if current is not None:
            results.append(
                {
                    "model_id": model_id,
                    "provider": provider,
                    "name": name,
                    "ok": True,
                    "action": "existing",
                    "catalog_model_id": str(current.get("id") or ""),
                }
            )
            continue
        collision = existing_by_id.get(model_id)
        if collision is not None:
            results.append(
                {
                    "model_id": model_id,
                    "provider": provider,
                    "name": name,
                    "ok": False,
                    "action": "collision",
                    "detail": "catalog model id is already assigned to a different provider/model",
                }
            )
            continue
        response = requests.post(f"{base}/v1/models", headers=headers, data=json.dumps(model), timeout=30)
        ok = 200 <= response.status_code < 300
        results.append(
            {
                "model_id": model_id,
                "provider": provider,
                "name": name,
                "ok": ok,
                "action": "created",
                "status_code": response.status_code,
                "detail": "" if ok else response.text[:500],
            }
        )
        if ok:
            existing_by_key[key] = model
            existing_by_id[model_id] = model

    (output_dir / "apply-models-summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def apply_bots(output_dir: Path, *, api_url: str, api_token: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    headers = _headers(api_token)
    base = api_url.rstrip("/")
    for path in sorted((output_dir / "bots").glob("*.bot.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        bot_id = str(payload.get("id") or "").strip()
        if not bot_id:
            results.append({"path": str(path), "ok": False, "detail": "missing bot id"})
            continue
        get_resp = requests.get(f"{base}/v1/bots/{bot_id}", headers=headers, timeout=30)
        if get_resp.status_code == 404:
            resp = requests.post(f"{base}/v1/bots", headers=headers, data=json.dumps(payload), timeout=30)
            action = "created"
        elif 200 <= get_resp.status_code < 300:
            resp = requests.put(f"{base}/v1/bots/{bot_id}", headers=headers, data=json.dumps(payload), timeout=30)
            action = "updated"
        else:
            results.append(
                {
                    "bot_id": bot_id,
                    "ok": False,
                    "status_code": get_resp.status_code,
                    "action": "lookup",
                    "detail": get_resp.text[:500],
                }
            )
            continue
        results.append(
            {
                "bot_id": bot_id,
                "ok": 200 <= resp.status_code < 300,
                "status_code": resp.status_code,
                "action": action,
                "detail": resp.text[:500] if not (200 <= resp.status_code < 300) else "",
            }
        )
    (output_dir / "apply-bots-summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def verify_bots_ready(output_dir: Path, *, api_url: str, api_token: str) -> list[dict[str, Any]]:
    """Verify rendered bots are eligible for dispatch without starting any work."""

    bot_paths = sorted((output_dir / "bots").glob("*.bot.json"))
    if not bot_paths:
        raise ValueError(f"No rendered bot files found in: {output_dir / 'bots'}")

    headers = _headers(api_token)
    base = api_url.rstrip("/")
    results: list[dict[str, Any]] = []
    for path in bot_paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        bot_id = str(payload.get("id") or "").strip()
        if not bot_id:
            results.append({"path": str(path), "ok": False, "action": "validate", "detail": "missing bot id"})
            continue

        response = requests.get(f"{base}/v1/bots/{bot_id}/readiness", headers=headers, timeout=30)
        if not (200 <= response.status_code < 300):
            results.append(
                {
                    "bot_id": bot_id,
                    "ok": False,
                    "action": "lookup",
                    "status_code": response.status_code,
                    "detail": response.text[:500],
                }
            )
            continue

        readiness = response.json()
        if not isinstance(readiness, dict):
            results.append(
                {
                    "bot_id": bot_id,
                    "ok": False,
                    "action": "validate",
                    "detail": "control-plane readiness response must be an object",
                }
            )
            continue

        failed_checks = [
            str(check.get("message") or "")[:500]
            for check in readiness.get("checks") or []
            if isinstance(check, dict) and str(check.get("status") or "").lower() == "failed"
        ]
        summary = readiness.get("summary")
        results.append(
            {
                "bot_id": bot_id,
                "ok": bool(readiness.get("ready")) and str(readiness.get("state") or "") == "ready",
                "action": "verified",
                "state": str(readiness.get("state") or "unknown"),
                "summary": summary if isinstance(summary, dict) else {},
                "blockers": failed_checks[:8],
            }
        )

    (output_dir / "verify-readiness-summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def wait_workers(worker_ids: list[str], *, api_url: str, api_token: str, timeout_seconds: int) -> dict[str, Any]:
    headers = _headers(api_token)
    base = api_url.rstrip("/")
    deadline = time.time() + timeout_seconds
    last_seen: dict[str, str] = {}
    while time.time() < deadline:
        resp = requests.get(f"{base}/v1/workers", headers=headers, timeout=30)
        resp.raise_for_status()
        workers = resp.json()
        last_seen = {
            str(item.get("id")): str(item.get("status") or "")
            for item in workers
            if isinstance(item, dict)
        }
        if all(last_seen.get(worker_id) == "online" for worker_id in worker_ids):
            return {"ok": True, "statuses": {worker_id: last_seen.get(worker_id) for worker_id in worker_ids}}
        time.sleep(3)
    return {"ok": False, "statuses": {worker_id: last_seen.get(worker_id) for worker_id in worker_ids}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render and optionally apply a NexusAI worker-node fleet.")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE), help="Worker fleet YAML profile.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Private generated runtime output directory.")
    parser.add_argument("--env-file", action="append", default=[str(ROOT / ".env")], help="Env file to read. May be repeated.")
    parser.add_argument("--allow-missing-ollama-key", action="store_true", help="Render ollama_cloud workers even when OLLAMA_API_KEY is not set.")
    parser.add_argument(
        "--apply-models",
        action="store_true",
        help="Register missing rendered catalog models without changing existing catalog entries.",
    )
    parser.add_argument("--apply-bots", action="store_true", help="Create or update rendered bot records in the control plane.")
    parser.add_argument(
        "--verify-readiness",
        action="store_true",
        help="Verify rendered bots are dispatch-ready without starting tasks.",
    )
    parser.add_argument("--api-url", default="", help="Host-reachable control-plane API URL for apply, verify, or wait.")
    parser.add_argument("--wait-workers", action="store_true", help="Wait until rendered workers self-register as online.")
    parser.add_argument("--wait-timeout-seconds", type=int, default=180)
    args = parser.parse_args(argv)

    profile_path = Path(args.profile).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    env = _merged_env([Path(item).expanduser().resolve() for item in args.env_file or []])
    try:
        summary = render(
            profile_path,
            output_dir,
            env,
            allow_missing_ollama_key=bool(args.allow_missing_ollama_key),
        )
        print(json.dumps(summary, indent=2))

        api_url = str(args.api_url or env.get("CONTROL_PLANE_URL") or "http://127.0.0.1:8000").strip()
        api_token = str(env.get("CONTROL_PLANE_API_TOKEN") or "").strip()
        if args.apply_models:
            results = apply_models(output_dir, api_url=api_url, api_token=api_token)
            print(json.dumps({"apply_models": results}, indent=2))
            if any(not item.get("ok") for item in results):
                return 1
        if args.apply_bots:
            results = apply_bots(output_dir, api_url=api_url, api_token=api_token)
            print(json.dumps({"apply_bots": results}, indent=2))
            if any(not item.get("ok") for item in results):
                return 1
        if args.wait_workers:
            worker_ids = [item["id"] for item in summary["workers"]]
            result = wait_workers(
                worker_ids,
                api_url=api_url,
                api_token=api_token,
                timeout_seconds=int(args.wait_timeout_seconds),
            )
            print(json.dumps({"wait_workers": result}, indent=2))
            if not result.get("ok"):
                return 1
        if args.verify_readiness:
            results = verify_bots_ready(output_dir, api_url=api_url, api_token=api_token)
            print(json.dumps({"verify_readiness": results}, indent=2))
            if any(not item.get("ok") for item in results):
                return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
