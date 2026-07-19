"""Bounded internal data sources for read-only recurring schedules."""
from __future__ import annotations

import inspect
import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict


FLEET_HEALTH_SUMMARY_SOURCE = "control_plane_fleet_summary_v1"
_SYSTEM_PAYLOAD_SOURCE_KEY = "system_payload_source"
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_FLEET_HEALTH_RECENT_WINDOW_HOURS = 24


def _failure_category(task: Any) -> str:
    """Classify a failed task without exposing its error text to a cloud worker."""
    error = getattr(task, "error", None)
    code = str(getattr(error, "code", "") or "").strip().lower()
    message = str(getattr(error, "message", "") or "").strip().lower()
    haystack = f"{code} {message}"

    if any(token in haystack for token in ("policy", "scope", "approval", "forbidden")):
        return "policy"
    if "output contract" in haystack or "schema" in haystack:
        return "output_contract"
    if any(token in haystack for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in haystack for token in ("auth", "credential", "api key", "unauthorized", "forbidden")):
        return "authentication"
    if any(token in haystack for token in ("connection", "transport", "unavailable", "refused")):
        return "transport"
    if any(token in haystack for token in ("model", "inference", "provider")):
        return "model"
    return "other"


def _task_updated_at(task: Any) -> datetime | None:
    raw = str(getattr(task, "updated_at", "") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SystemPayloadSourceError(ValueError):
    """A schedule requested an unsupported or unsafe internal data source."""


def system_payload_source_config(schedule: Dict[str, Any]) -> Dict[str, str] | None:
    metadata = schedule.get("metadata") if isinstance(schedule.get("metadata"), dict) else {}
    raw = metadata.get(_SYSTEM_PAYLOAD_SOURCE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SystemPayloadSourceError("system_payload_source must be an object")
    source_type = str(raw.get("type") or "").strip()
    target_field = str(raw.get("target_field") or "monitoring_events").strip()
    if source_type != FLEET_HEALTH_SUMMARY_SOURCE:
        raise SystemPayloadSourceError(f"unsupported system_payload_source type: {source_type or 'unset'}")
    if not _SAFE_FIELD_NAME.fullmatch(target_field):
        raise SystemPayloadSourceError("system_payload_source target_field must be a simple payload field name")
    return {"type": source_type, "target_field": target_field}


def validate_system_payload_source(schedule: Dict[str, Any], bot: Any) -> None:
    """Allow system snapshots only for explicitly read-only monitoring workers."""
    if system_payload_source_config(schedule) is None:
        return
    routing_rules = getattr(bot, "routing_rules", None)
    profile = routing_rules.get("worker_profile") if isinstance(routing_rules, dict) else None
    profile = profile if isinstance(profile, dict) else {}
    task_scope = str(profile.get("task_scope") or "").strip().lower()
    if bool(profile.get("can_edit")) or not task_scope.startswith("read-only-monitoring"):
        raise SystemPayloadSourceError(
            "system payload sources require a worker_profile with a read-only monitoring task scope"
        )


def _probe_attention_reason_codes(probe: Dict[str, Any]) -> list[str]:
    """Return stable, non-secret reason codes for an attention-worthy worker probe."""
    reasons: list[str] = []
    if str(probe.get("probe_status") or "unknown").strip().lower() != "ready":
        reasons.append("runtime_probe_not_ready")
    attestation = probe.get("capability_attestation")
    attestation = attestation if isinstance(attestation, dict) else {}
    browser = attestation.get("browser")
    if isinstance(browser, dict) and bool(browser.get("configured")) and not bool(browser.get("ready")):
        reasons.append("browser_session_unavailable")
    if attestation.get("unauthenticated_cli_tools"):
        reasons.append("cli_authentication_required")
    return reasons


def _probe_requires_attention(probe: Dict[str, Any]) -> bool:
    return bool(_probe_attention_reason_codes(probe))


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def fleet_health_summary(
    *,
    worker_registry: Any,
    worker_probe_store: Any,
    bot_registry: Any,
    task_manager: Any,
    schedule_engine: Any,
) -> Dict[str, Any]:
    generated_at = datetime.now(timezone.utc)
    recent_cutoff = generated_at - timedelta(hours=_FLEET_HEALTH_RECENT_WINDOW_HOURS)
    workers = list(await _await_if_needed(worker_registry.list()) or [])
    worker_ids = [str(getattr(worker, "id", "") or "").strip() for worker in workers]
    stored_probes = await _await_if_needed(worker_probe_store.list_for_workers(worker_ids))
    stored_probes = stored_probes if isinstance(stored_probes, dict) else {}

    runtime_attention = []
    for worker in workers:
        worker_id = str(getattr(worker, "id", "") or "").strip()
        probe = stored_probes.get(worker_id)
        if not isinstance(probe, dict):
            continue
        reason_codes = _probe_attention_reason_codes(probe)
        if not reason_codes:
            continue
        runtime_attention.append(
            {
                "worker_id": worker_id,
                "probe_status": str(probe.get("probe_status") or "unknown").strip().lower(),
                "reason_codes": reason_codes,
            }
        )

    bots = await _await_if_needed(bot_registry.list())
    bots = list(bots or [])
    tasks = await _await_if_needed(task_manager.list_tasks(limit=200))
    schedules = await _await_if_needed(schedule_engine.list_schedules(limit=100))
    task_statuses = Counter(str(getattr(task, "status", "unknown") or "unknown") for task in (tasks or []))
    recent_tasks = []
    for task in tasks or []:
        updated_at = _task_updated_at(task)
        if updated_at is not None and updated_at >= recent_cutoff:
            recent_tasks.append(task)
    recent_task_statuses = Counter(
        str(getattr(task, "status", "unknown") or "unknown") for task in recent_tasks
    )
    recent_failure_categories = Counter(
        _failure_category(task)
        for task in recent_tasks
        if str(getattr(task, "status", "") or "").strip().lower() == "failed"
    )
    runtime_attention_worker_ids = {
        str(item.get("worker_id") or "").strip()
        for item in runtime_attention
        if str(item.get("worker_id") or "").strip()
    }
    enabled_bots_with_runtime_attention = sum(
        1
        for bot in bots
        if bool(getattr(bot, "enabled", False))
        and any(
            str(getattr(backend, "worker_id", "") or "").strip() in runtime_attention_worker_ids
            for backend in (getattr(bot, "backends", None) or [])
        )
    )
    failed_schedules = [
        str(schedule.get("id") or "")
        for schedule in (schedules or [])
        if isinstance(schedule, dict) and str(schedule.get("last_run_status") or "").lower() == "failed"
    ]

    return {
        "source": FLEET_HEALTH_SUMMARY_SOURCE,
        "generated_at": generated_at.isoformat(),
        "workers": {
            "registered": len(workers),
            "online": sum(1 for worker in workers if str(getattr(worker, "status", "")).lower() == "online"),
            "offline": sum(1 for worker in workers if str(getattr(worker, "status", "")).lower() == "offline"),
            "runtime_attention": runtime_attention[:50],
        },
        "bots": {
            "registered": len(bots),
            "enabled": sum(1 for bot in bots if bool(getattr(bot, "enabled", False))),
            "enabled_with_runtime_attention": enabled_bots_with_runtime_attention,
        },
        "tasks": {
            "sample_limit": 200,
            "by_status": dict(sorted(task_statuses.items())),
            "recent_window_hours": _FLEET_HEALTH_RECENT_WINDOW_HOURS,
            "recent_by_status": dict(sorted(recent_task_statuses.items())),
            "recent_failed_by_category": dict(sorted(recent_failure_categories.items())),
        },
        "schedules": {
            "registered": len(schedules or []),
            "active": sum(
                1
                for schedule in (schedules or [])
                if isinstance(schedule, dict) and str(schedule.get("status") or "").lower() == "active"
            ),
            "failed_recent_schedule_ids": failed_schedules[:50],
        },
    }


async def materialize_system_schedule_payload(
    schedule: Dict[str, Any],
    *,
    worker_registry: Any,
    worker_probe_store: Any,
    bot_registry: Any,
    task_manager: Any,
    schedule_engine: Any,
) -> Dict[str, Any]:
    """Return non-secret payload additions for an approved internal source."""
    config = system_payload_source_config(schedule)
    if config is None:
        return {}
    if config["type"] == FLEET_HEALTH_SUMMARY_SOURCE:
        summary = await fleet_health_summary(
            worker_registry=worker_registry,
            worker_probe_store=worker_probe_store,
            bot_registry=bot_registry,
            task_manager=task_manager,
            schedule_engine=schedule_engine,
        )
        return {config["target_field"]: json.dumps(summary, sort_keys=True, separators=(",", ":"))}
    raise SystemPayloadSourceError(f"unsupported system_payload_source type: {config['type']}")
