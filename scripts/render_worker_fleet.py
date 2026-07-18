#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

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
    return data


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


def _worker_cli_tools(worker: dict[str, Any]) -> list[str]:
    tooling = worker.get("tooling")
    if tooling is None:
        return []
    if not isinstance(tooling, dict):
        raise ValueError(f"Worker {worker.get('id') or worker.get('name')} tooling must be a mapping.")
    return _as_list(tooling.get("cli_tools"))


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
    }
    if cli_tools:
        config["tooling"] = {"cli_tools": cli_tools}
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
    allowed_types = {"local_llm", "remote_llm", "cloud_api", "cli", "custom"}
    worker_id = str(worker["id"]).strip()
    cli_tools = set(_worker_cli_tools(worker))
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
        if backend_type in {"local_llm", "remote_llm", "cli"}:
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
        elif command:
            raise ValueError(f"Worker {worker_id} bot.backends[{index}] command is only valid for cli backends.")
        resolved.append(backend)
    return resolved


def _bot_payload(worker: dict[str, Any], fleet: dict[str, Any]) -> dict[str, Any]:
    bot = worker.get("bot") if isinstance(worker.get("bot"), dict) else {}
    worker_id = str(worker["id"]).strip()
    bot_id = str(bot.get("id") or f"{worker_id}-bot").strip()
    backends = _bot_backends(worker, fleet)
    primary_backend = backends[0]
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
        "execution_policy": {
            "repo_output_mode": "deny",
            "workspace_context_injection": False,
            "inline_coding_default": False,
            "can_apply_db_actions": False,
            "allow_run_result_ingest": True,
        },
        "routing_rules": {
            "worker_profile": {
                "worker_id": worker_id,
                "service": _worker_service(worker),
                "role": str(worker.get("role") or "").strip(),
                "can_edit": bool(worker.get("can_edit", False)),
                "task_scope": str(worker.get("task_scope") or "").strip(),
                "allowed_pages": _as_list(worker.get("allowed_pages")),
                "course_scope": _as_list(worker.get("course_scope")),
                "lesson_scope": _as_list(worker.get("lesson_scope")),
                "cli_tools": _worker_cli_tools(worker),
            },
            "launch_profile": {
                "worker_node_service": _worker_service(worker),
                "backend_type": primary_backend["type"],
                "provider": primary_backend["provider"],
                "model": primary_backend["model"],
            },
        },
    }


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


def render(profile_path: Path, output_dir: Path, env: dict[str, str], *, allow_missing_ollama_key: bool = False) -> dict[str, Any]:
    profile = _load_profile(profile_path)
    fleet = profile.get("fleet") if isinstance(profile.get("fleet"), dict) else {}
    workers = [item for item in profile["workers"] if isinstance(item, dict)]
    if not workers:
        raise ValueError("Worker fleet profile has no worker entries.")

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
    image = str(fleet.get("image") or "nexus-worker-node:latest").strip()
    cloud_context_policy = str(fleet.get("cloud_context_policy") or "redact").strip().lower()
    if cloud_context_policy not in {"allow", "redact", "block"}:
        cloud_context_policy = "redact"
    heartbeat = int(fleet.get("heartbeat_interval_seconds") or 15)
    ollama_base_url = str(fleet.get("ollama_cloud_base_url") or "https://ollama.com/api").strip()

    configs_dir = output_dir / "workers"
    env_dir = output_dir / "env"
    bots_dir = output_dir / "bots"
    output_dir.mkdir(parents=True, exist_ok=True)
    configs_dir.mkdir(parents=True, exist_ok=True)
    env_dir.mkdir(parents=True, exist_ok=True)
    bots_dir.mkdir(parents=True, exist_ok=True)

    compose: dict[str, Any] = {
        "services": {},
        "networks": {
            "nexus-net": {
                "external": True,
                "name": network_name,
            }
        },
    }
    rendered_workers: list[dict[str, str]] = []
    rendered_bots: list[str] = []

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

        image, build = _service_image_and_build(
            worker,
            fleet,
            profile_base=profile_path.parent,
            worker_node_source=worker_node_source,
        )
        service_config: dict[str, Any] = {
            "image": image,
            "restart": "unless-stopped",
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
        if build is not None:
            service_config["build"] = build
        compose["services"][service] = service_config
        rendered_workers.append(
            {
                "id": worker_id,
                "name": worker_config["name"],
                "service": service,
                "bot_id": bot_payload["id"],
            }
        )

    compose_path = output_dir / "docker-compose.worker-node.generated.yml"
    with compose_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(compose, fh, sort_keys=False)

    summary = {
        "profile": str(profile_path),
        "output_dir": str(output_dir),
        "compose_path": str(compose_path),
        "worker_node_source": str(worker_node_source),
        "docker_network": network_name,
        "workers": rendered_workers,
        "bots": rendered_bots,
        "warnings": warnings,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Nexus-API-Key"] = token
    return headers


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
    parser.add_argument("--apply-bots", action="store_true", help="Create or update rendered bot records in the control plane.")
    parser.add_argument("--api-url", default="", help="Host-reachable control-plane API URL for apply/wait.")
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
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
