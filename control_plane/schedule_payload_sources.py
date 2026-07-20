"""Bounded internal data sources for read-only recurring schedules."""
from __future__ import annotations

import inspect
import json
import csv
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict


FLEET_HEALTH_SUMMARY_SOURCE = "control_plane_fleet_summary_v1"
CSV_WORK_ITEMS_SOURCE = "csv_work_items_v1"
_SYSTEM_PAYLOAD_SOURCE_KEY = "system_payload_source"
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_FLEET_HEALTH_RECENT_WINDOW_HOURS = 24
_CSV_SOURCE_MAX_COLUMNS = 16
_CSV_SOURCE_MAX_ROWS = 20
_CSV_SOURCE_MAX_FILE_BYTES = 1_000_000
_CSV_SOURCE_MAX_VALUE_CHARS = 2_000
_CSV_SOURCE_MAX_AGE_HOURS = 168


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


def _task_execution_provenance(task: Any) -> dict[str, Any]:
    """Return the bounded route evidence persisted for one task, if present."""
    metadata = getattr(task, "metadata", None)
    if hasattr(metadata, "model_dump"):
        metadata = metadata.model_dump()
    if not isinstance(metadata, dict):
        return {}
    provenance = metadata.get("execution_provenance")
    return provenance if isinstance(provenance, dict) else {}


class SystemPayloadSourceError(ValueError):
    """A schedule requested an unsupported or unsafe internal data source."""


def _safe_csv_field_list(raw: Any, *, field_name: str) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise SystemPayloadSourceError(f"{field_name} must be a non-empty list")
    if len(raw) > _CSV_SOURCE_MAX_COLUMNS:
        raise SystemPayloadSourceError(f"{field_name} exceeds the maximum of {_CSV_SOURCE_MAX_COLUMNS} fields")
    fields = [str(item or "").strip() for item in raw]
    if any(not _SAFE_FIELD_NAME.fullmatch(item) for item in fields):
        raise SystemPayloadSourceError(f"{field_name} contains an invalid CSV field name")
    if len(set(fields)) != len(fields):
        raise SystemPayloadSourceError(f"{field_name} must not contain duplicates")
    return fields


def _csv_filter_map(raw: Any, *, field_name: str) -> dict[str, set[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SystemPayloadSourceError(f"{field_name} must be an object")
    if len(raw) > _CSV_SOURCE_MAX_COLUMNS:
        raise SystemPayloadSourceError(f"{field_name} exceeds the maximum of {_CSV_SOURCE_MAX_COLUMNS} fields")
    filters: dict[str, set[str]] = {}
    for key, values in raw.items():
        field = str(key or "").strip()
        if not _SAFE_FIELD_NAME.fullmatch(field):
            raise SystemPayloadSourceError(f"{field_name} contains an invalid CSV field name")
        if not isinstance(values, list) or not values or len(values) > 32:
            raise SystemPayloadSourceError(f"{field_name}.{field} must be a non-empty list of at most 32 values")
        normalized = {str(value or "").strip().casefold() for value in values}
        normalized.discard("")
        if not normalized:
            raise SystemPayloadSourceError(f"{field_name}.{field} must include at least one non-empty value")
        filters[field] = normalized
    return filters


def _csv_source_config(raw: Dict[str, Any], *, target_field: str) -> Dict[str, Any]:
    relative_path = str(raw.get("relative_path") or "").strip()
    candidate = Path(relative_path)
    if not relative_path or candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise SystemPayloadSourceError("csv_work_items_v1 relative_path must be a non-empty relative path")
    if candidate.suffix.casefold() != ".csv":
        raise SystemPayloadSourceError("csv_work_items_v1 relative_path must reference a .csv file")
    max_rows = raw.get("max_rows", 1)
    max_age_hours = raw.get("max_age_hours", 48)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= _CSV_SOURCE_MAX_ROWS:
        raise SystemPayloadSourceError(f"csv_work_items_v1 max_rows must be between 1 and {_CSV_SOURCE_MAX_ROWS}")
    if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, int) or not 1 <= max_age_hours <= _CSV_SOURCE_MAX_AGE_HOURS:
        raise SystemPayloadSourceError(
            f"csv_work_items_v1 max_age_hours must be between 1 and {_CSV_SOURCE_MAX_AGE_HOURS}"
        )
    return {
        "type": CSV_WORK_ITEMS_SOURCE,
        "target_field": target_field,
        "relative_path": relative_path,
        "columns": _safe_csv_field_list(raw.get("columns"), field_name="csv_work_items_v1 columns"),
        "include_equals": _csv_filter_map(raw.get("include_equals"), field_name="csv_work_items_v1 include_equals"),
        "exclude_equals": _csv_filter_map(raw.get("exclude_equals"), field_name="csv_work_items_v1 exclude_equals"),
        "max_rows": max_rows,
        "max_age_hours": max_age_hours,
    }


def system_payload_source_config(schedule: Dict[str, Any]) -> Dict[str, Any] | None:
    metadata = schedule.get("metadata") if isinstance(schedule.get("metadata"), dict) else {}
    raw = metadata.get(_SYSTEM_PAYLOAD_SOURCE_KEY)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SystemPayloadSourceError("system_payload_source must be an object")
    source_type = str(raw.get("type") or "").strip()
    target_field = str(raw.get("target_field") or "monitoring_events").strip()
    if source_type not in {FLEET_HEALTH_SUMMARY_SOURCE, CSV_WORK_ITEMS_SOURCE}:
        raise SystemPayloadSourceError(f"unsupported system_payload_source type: {source_type or 'unset'}")
    if not _SAFE_FIELD_NAME.fullmatch(target_field):
        raise SystemPayloadSourceError("system_payload_source target_field must be a simple payload field name")
    if source_type == CSV_WORK_ITEMS_SOURCE:
        return _csv_source_config(raw, target_field=target_field)
    return {"type": source_type, "target_field": target_field}


def validate_system_payload_source(schedule: Dict[str, Any], bot: Any) -> None:
    """Allow system sources only for bots with explicitly non-mutating scopes."""
    source_config = system_payload_source_config(schedule)
    if source_config is None:
        return
    routing_rules = getattr(bot, "routing_rules", None)
    profile = routing_rules.get("worker_profile") if isinstance(routing_rules, dict) else None
    profile = profile if isinstance(profile, dict) else {}
    task_scope = str(profile.get("task_scope") or "").strip().lower()
    if bool(profile.get("can_edit")):
        raise SystemPayloadSourceError("system payload sources require a non-editing worker_profile")
    if source_config["type"] == FLEET_HEALTH_SUMMARY_SOURCE and not task_scope.startswith("read-only-monitoring"):
        raise SystemPayloadSourceError(
            "system payload sources require a worker_profile with a read-only monitoring task scope"
        )
    if source_config["type"] == CSV_WORK_ITEMS_SOURCE and not task_scope.startswith(("read-only", "draft-only")):
        raise SystemPayloadSourceError(
            "csv_work_items_v1 requires a worker_profile with a read-only or draft-only task scope"
        )


def _csv_source_path(relative_path: str) -> Path:
    root_raw = str(os.environ.get("NEXUSAI_READONLY_CSV_ROOT") or "").strip()
    if not root_raw:
        raise SystemPayloadSourceError("NEXUSAI_READONLY_CSV_ROOT is not configured")
    root = Path(root_raw).resolve()
    if not root.is_dir():
        raise SystemPayloadSourceError("NEXUSAI_READONLY_CSV_ROOT is not an accessible directory")
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise SystemPayloadSourceError("csv_work_items_v1 path escapes NEXUSAI_READONLY_CSV_ROOT")
    return candidate


def _truncate_csv_value(value: Any) -> str:
    text = str(value or "").strip()
    return text[:_CSV_SOURCE_MAX_VALUE_CHARS]


def _matches_csv_filters(
    row: Dict[str, Any],
    *,
    include_equals: Dict[str, set[str]],
    exclude_equals: Dict[str, set[str]],
) -> bool:
    for field, allowed in include_equals.items():
        if _truncate_csv_value(row.get(field)).casefold() not in allowed:
            return False
    for field, blocked in exclude_equals.items():
        if _truncate_csv_value(row.get(field)).casefold() in blocked:
            return False
    return True


def csv_work_items_payload(config: Dict[str, Any]) -> Dict[str, Any]:
    """Read a bounded, explicitly selected CSV snapshot from the configured read-only root."""
    source_path = _csv_source_path(str(config["relative_path"]))
    try:
        source_stat = source_path.stat()
    except FileNotFoundError as exc:
        raise SystemPayloadSourceError("csv_work_items_v1 source file does not exist") from exc
    if not source_path.is_file():
        raise SystemPayloadSourceError("csv_work_items_v1 source path is not a file")
    if source_stat.st_size > _CSV_SOURCE_MAX_FILE_BYTES:
        raise SystemPayloadSourceError(
            f"csv_work_items_v1 source exceeds the {_CSV_SOURCE_MAX_FILE_BYTES}-byte size limit"
        )
    now = datetime.now(timezone.utc)
    modified_at = datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc)
    source_age_seconds = max(0, int((now - modified_at).total_seconds()))
    if source_age_seconds > int(config["max_age_hours"]) * 3600:
        raise SystemPayloadSourceError("csv_work_items_v1 source is stale and cannot be dispatched")

    try:
        with source_path.open("r", encoding="utf-8-sig", newline="") as source_file:
            reader = csv.DictReader(source_file)
            headers = {str(header or "").strip() for header in (reader.fieldnames or [])}
            required_fields = set(config["columns"]) | set(config["include_equals"]) | set(config["exclude_equals"])
            missing_fields = sorted(field for field in required_fields if field not in headers)
            if missing_fields:
                raise SystemPayloadSourceError(
                    "csv_work_items_v1 source is missing required columns: " + ", ".join(missing_fields)
                )
            selected_rows = []
            for raw_row in reader:
                row = raw_row if isinstance(raw_row, dict) else {}
                if not _matches_csv_filters(
                    row,
                    include_equals=config["include_equals"],
                    exclude_equals=config["exclude_equals"],
                ):
                    continue
                selected_rows.append({field: _truncate_csv_value(row.get(field)) for field in config["columns"]})
                if len(selected_rows) >= int(config["max_rows"]):
                    break
    except UnicodeDecodeError as exc:
        raise SystemPayloadSourceError("csv_work_items_v1 source must be UTF-8 text") from exc

    return {
        "source": CSV_WORK_ITEMS_SOURCE,
        "source_name": source_path.name,
        "source_modified_at": modified_at.isoformat(),
        "source_age_seconds": source_age_seconds,
        "selected_count": len(selected_rows),
        "max_rows": int(config["max_rows"]),
        "selected_rows": selected_rows,
    }


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
        if not bool(getattr(worker, "enabled", True)):
            continue
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
    latest_worker_activity: dict[str, dict[str, str]] = {}
    for task in recent_tasks:
        provenance = _task_execution_provenance(task)
        worker_id = str(provenance.get("worker_id") or "").strip()
        updated_at = _task_updated_at(task)
        if not worker_id or updated_at is None:
            continue
        previous = latest_worker_activity.get(worker_id)
        if previous and previous["updated_at"] >= updated_at.isoformat():
            continue
        latest_worker_activity[worker_id] = {
            "worker_id": worker_id,
            "bot_id": str(getattr(task, "bot_id", "") or "").strip(),
            "task_id": str(getattr(task, "id", "") or "").strip(),
            "status": str(getattr(task, "status", "unknown") or "unknown").strip().lower(),
            "updated_at": updated_at.isoformat(),
            "backend_type": str(provenance.get("backend_type") or "").strip(),
            "provider": str(provenance.get("provider") or "").strip(),
            "model": str(provenance.get("model") or "").strip(),
        }
    recent_failure_categories = Counter(
        _failure_category(task)
        for task in recent_tasks
        if str(getattr(task, "status", "") or "").strip().lower() == "failed"
    )
    latest_completion_by_bot: dict[str, datetime] = {}
    for task in recent_tasks:
        if str(getattr(task, "status", "") or "").strip().lower() != "completed":
            continue
        bot_id = str(getattr(task, "bot_id", "") or "").strip()
        completed_at = _task_updated_at(task)
        if not bot_id or completed_at is None:
            continue
        previous = latest_completion_by_bot.get(bot_id)
        if previous is None or completed_at > previous:
            latest_completion_by_bot[bot_id] = completed_at

    unrecovered_failure_categories = Counter()
    recovered_failure_categories = Counter()
    for task in recent_tasks:
        if str(getattr(task, "status", "") or "").strip().lower() != "failed":
            continue
        category = _failure_category(task)
        bot_id = str(getattr(task, "bot_id", "") or "").strip()
        failed_at = _task_updated_at(task)
        completed_at = latest_completion_by_bot.get(bot_id)
        if bot_id and failed_at is not None and completed_at is not None and completed_at > failed_at:
            recovered_failure_categories[category] += 1
        else:
            unrecovered_failure_categories[category] += 1
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
    enabled_workers = [worker for worker in workers if bool(getattr(worker, "enabled", True))]

    return {
        "source": FLEET_HEALTH_SUMMARY_SOURCE,
        "generated_at": generated_at.isoformat(),
        "workers": {
            "registered": len(workers),
            "enabled": len(enabled_workers),
            "disabled": len(workers) - len(enabled_workers),
            "online": sum(
                1
                for worker in enabled_workers
                if str(getattr(worker, "status", "")).lower() == "online"
            ),
            "offline": sum(
                1
                for worker in enabled_workers
                if str(getattr(worker, "status", "")).lower() == "offline"
            ),
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
            "recent_worker_activity": sorted(
                latest_worker_activity.values(),
                key=lambda item: (item["updated_at"], item["worker_id"]),
                reverse=True,
            )[:50],
            "recent_failed_by_category": dict(sorted(recent_failure_categories.items())),
            "recent_unrecovered_failed_by_category": dict(sorted(unrecovered_failure_categories.items())),
            "recent_recovered_failed_by_category": dict(sorted(recovered_failure_categories.items())),
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
    if config["type"] == CSV_WORK_ITEMS_SOURCE:
        payload = csv_work_items_payload(config)
        return {config["target_field"]: json.dumps(payload, sort_keys=True, separators=(",", ":"))}
    raise SystemPayloadSourceError(f"unsupported system_payload_source type: {config['type']}")
