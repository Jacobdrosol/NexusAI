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

from shared.bot_policy import supervision_manager_config


FLEET_HEALTH_SUMMARY_SOURCE = "control_plane_fleet_summary_v1"
OPERATIONAL_QUALITY_SNAPSHOT_SOURCE = "control_plane_operational_quality_v1"
CSV_WORK_ITEMS_SOURCE = "csv_work_items_v1"
SUPERVISION_PORTFOLIO_SOURCE = "control_plane_supervision_portfolio_v1"
_SYSTEM_PAYLOAD_SOURCE_KEY = "system_payload_source"
_SYSTEM_PAYLOAD_SOURCES_KEY = "system_payload_sources"
_MAX_SYSTEM_PAYLOAD_SOURCES = 4
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_FLEET_HEALTH_RECENT_WINDOW_HOURS = 24
_CSV_SOURCE_MAX_COLUMNS = 16
_CSV_SOURCE_MAX_ROWS = 20
_CSV_SOURCE_MAX_FILE_BYTES = 1_000_000
_CSV_SOURCE_MAX_VALUE_CHARS = 2_000
_CSV_SOURCE_MAX_AGE_HOURS = 168
_CSV_SOURCE_MAX_PAYLOAD_FIELD_MAPPINGS = 12
_CSV_SOURCE_MAX_CATALOG_FILES = 100
_CSV_SOURCE_MAX_CATALOG_HEADERS = 64
_CSV_RESERVED_PAYLOAD_FIELDS = {
    "bot_id",
    "error",
    "id",
    "metadata",
    "node_overrides",
    "project_id",
    "result",
    "schedule_id",
    "source",
    "task_id",
}
_SUPERVISION_RESULT_STATUSES = {
    "approved",
    "attention",
    "blocked",
    "completed",
    "failed",
    "healthy",
    "passed",
    "rejected",
}
_SUPERVISION_ARTIFACT_TYPE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_SUPERVISION_SCOPE_INTEGER_FIELDS = ("course_id", "lesson_id", "unit_number", "lesson_number")


def _failure_category(task: Any) -> str:
    """Classify a failed task without exposing its error text to a cloud worker."""
    error = getattr(task, "error", None)
    code = str(getattr(error, "code", "") or "").strip().lower()
    message = str(getattr(error, "message", "") or "").strip().lower()
    haystack = f"{code} {message}"

    if any(token in haystack for token in ("policy", "scope", "approval", "forbidden")):
        return "policy"
    if (
        code == "output_contract_invalid"
        or "output contract" in haystack
        or "no valid json object or array found" in haystack
        or "requires structured json output" in haystack
    ):
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


def _task_result_status(task: Any) -> str | None:
    """Return only an allowlisted terminal outcome from a task result.

    Manager schedules need enough evidence to distinguish a successful QC from a
    merely completed transport call.  Do not forward generated prose, findings,
    or arbitrary result fields into a manager payload.
    """

    result = getattr(task, "result", None)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "").strip().lower()
    return status if status in _SUPERVISION_RESULT_STATUSES else None


def _task_workflow_scope(task: Any) -> dict[str, Any]:
    """Expose only bounded, non-content workflow identifiers to managers."""

    payload = getattr(task, "payload", None)
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        return {}
    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        return {}

    scope: dict[str, Any] = {}
    for field in _SUPERVISION_SCOPE_INTEGER_FIELDS:
        value = artifact.get(field)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 < value <= 2_147_483_647:
            scope[field] = value
    artifact_type = str(artifact.get("artifact_type") or "").strip()
    if _SUPERVISION_ARTIFACT_TYPE.fullmatch(artifact_type):
        scope["artifact_type"] = artifact_type
    return scope


class SystemPayloadSourceError(ValueError):
    """A schedule requested an unsupported or unsafe internal data source."""


class ScheduleWorkQueueEmpty(SystemPayloadSourceError):
    """A mapped CSV queue had no eligible work item for this run."""

    skip_schedule_run = True


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


def _csv_payload_field_map(raw: Any, *, columns: list[str], require_non_empty_fields: list[str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not raw:
        raise SystemPayloadSourceError("csv_work_items_v1 payload_field_map must be a non-empty object")
    if len(raw) > _CSV_SOURCE_MAX_PAYLOAD_FIELD_MAPPINGS:
        raise SystemPayloadSourceError(
            "csv_work_items_v1 payload_field_map exceeds the maximum of "
            f"{_CSV_SOURCE_MAX_PAYLOAD_FIELD_MAPPINGS} fields"
        )

    mapping: dict[str, str] = {}
    for raw_target, raw_source in raw.items():
        target = str(raw_target or "").strip()
        source = str(raw_source or "").strip()
        if not _SAFE_FIELD_NAME.fullmatch(target):
            raise SystemPayloadSourceError("csv_work_items_v1 payload_field_map contains an invalid payload field name")
        if target in _CSV_RESERVED_PAYLOAD_FIELDS:
            raise SystemPayloadSourceError(
                f"csv_work_items_v1 payload_field_map cannot override reserved field '{target}'"
            )
        if source not in columns:
            raise SystemPayloadSourceError(
                "csv_work_items_v1 payload_field_map values must reference selected columns"
            )
        if source not in require_non_empty_fields:
            raise SystemPayloadSourceError(
                "csv_work_items_v1 payload_field_map source columns must be required non-empty fields"
            )
        mapping[target] = source
    return mapping


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
    columns = _safe_csv_field_list(raw.get("columns"), field_name="csv_work_items_v1 columns")
    required_raw = raw.get("require_non_empty_fields")
    require_non_empty_fields = (
        _safe_csv_field_list(
            required_raw,
            field_name="csv_work_items_v1 require_non_empty_fields",
        )
        if required_raw is not None
        else []
    )
    if any(field not in columns for field in require_non_empty_fields):
        raise SystemPayloadSourceError(
            "csv_work_items_v1 require_non_empty_fields must reference selected columns"
        )
    payload_field_map = _csv_payload_field_map(
        raw.get("payload_field_map"),
        columns=columns,
        require_non_empty_fields=require_non_empty_fields,
    )
    if payload_field_map and max_rows != 1:
        raise SystemPayloadSourceError(
            "csv_work_items_v1 payload_field_map requires max_rows=1 to avoid ambiguous task inputs"
        )
    return {
        "type": CSV_WORK_ITEMS_SOURCE,
        "target_field": target_field,
        "relative_path": relative_path,
        "columns": columns,
        "include_equals": _csv_filter_map(raw.get("include_equals"), field_name="csv_work_items_v1 include_equals"),
        "exclude_equals": _csv_filter_map(raw.get("exclude_equals"), field_name="csv_work_items_v1 exclude_equals"),
        "require_non_empty_fields": require_non_empty_fields,
        "payload_field_map": payload_field_map,
        "max_rows": max_rows,
        "max_age_hours": max_age_hours,
    }


def _system_payload_source_config(raw: Any, *, field_name: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise SystemPayloadSourceError(f"{field_name} must be an object")
    source_type = str(raw.get("type") or "").strip()
    target_field = str(raw.get("target_field") or "monitoring_events").strip()
    if source_type not in {
        FLEET_HEALTH_SUMMARY_SOURCE,
        OPERATIONAL_QUALITY_SNAPSHOT_SOURCE,
        CSV_WORK_ITEMS_SOURCE,
        SUPERVISION_PORTFOLIO_SOURCE,
    }:
        raise SystemPayloadSourceError(f"unsupported {field_name} type: {source_type or 'unset'}")
    if not _SAFE_FIELD_NAME.fullmatch(target_field):
        raise SystemPayloadSourceError(f"{field_name} target_field must be a simple payload field name")
    if source_type == CSV_WORK_ITEMS_SOURCE:
        return _csv_source_config(raw, target_field=target_field)
    return {"type": source_type, "target_field": target_field}


def system_payload_source_configs(schedule: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return every bounded internal source configured for a read-only schedule.

    ``system_payload_source`` remains supported for existing schedules. New schedules
    can use ``system_payload_sources`` to compose a small number of independently
    bounded snapshots into distinct task-payload fields.
    """
    metadata = schedule.get("metadata") if isinstance(schedule.get("metadata"), dict) else {}
    legacy = metadata.get(_SYSTEM_PAYLOAD_SOURCE_KEY)
    raw_sources = metadata.get(_SYSTEM_PAYLOAD_SOURCES_KEY)
    if legacy is not None and raw_sources is not None:
        raise SystemPayloadSourceError(
            "system_payload_source and system_payload_sources cannot be used together"
        )
    if raw_sources is None:
        return [] if legacy is None else [_system_payload_source_config(legacy, field_name=_SYSTEM_PAYLOAD_SOURCE_KEY)]
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SystemPayloadSourceError("system_payload_sources must be a non-empty list")
    if len(raw_sources) > _MAX_SYSTEM_PAYLOAD_SOURCES:
        raise SystemPayloadSourceError(
            f"system_payload_sources exceeds the maximum of {_MAX_SYSTEM_PAYLOAD_SOURCES} sources"
        )
    configs = [
        _system_payload_source_config(raw, field_name=f"system_payload_sources[{index}]")
        for index, raw in enumerate(raw_sources)
    ]
    target_fields = [str(config["target_field"]) for config in configs]
    if len(target_fields) != len(set(target_fields)):
        raise SystemPayloadSourceError("system_payload_sources must use distinct target_field values")
    return configs


def system_payload_source_config(schedule: Dict[str, Any]) -> Dict[str, Any] | None:
    """Return the legacy single source configuration, if present.

    Callers that can compose more than one payload field must use
    :func:`system_payload_source_configs` instead.
    """
    configs = system_payload_source_configs(schedule)
    if len(configs) > 1:
        raise SystemPayloadSourceError("multiple system payload sources require plural-aware handling")
    return configs[0] if configs else None


def validate_system_payload_source(schedule: Dict[str, Any], bot: Any) -> None:
    """Allow system sources only for bots with explicitly non-mutating scopes."""
    source_configs = system_payload_source_configs(schedule)
    if not source_configs:
        return
    routing_rules = getattr(bot, "routing_rules", None)
    profile = routing_rules.get("worker_profile") if isinstance(routing_rules, dict) else None
    profile = profile if isinstance(profile, dict) else {}
    task_scope = str(profile.get("task_scope") or "").strip().lower()
    if bool(profile.get("can_edit")):
        raise SystemPayloadSourceError("system payload sources require a non-editing worker_profile")
    for source_config in source_configs:
        if source_config["type"] == FLEET_HEALTH_SUMMARY_SOURCE and not task_scope.startswith("read-only-monitoring"):
            raise SystemPayloadSourceError(
                "system payload sources require a worker_profile with a read-only monitoring task scope"
            )
        if source_config["type"] == OPERATIONAL_QUALITY_SNAPSHOT_SOURCE and not task_scope.startswith(
            ("read-only-quality-review", "read-only-manager-review")
        ):
            raise SystemPayloadSourceError(
                "operational quality snapshots require a worker_profile with a read-only quality-review or manager-review task scope"
            )
        if source_config["type"] == SUPERVISION_PORTFOLIO_SOURCE:
            if not task_scope.startswith("read-only-manager-review"):
                raise SystemPayloadSourceError(
                    "supervision portfolio snapshots require a worker_profile with a read-only manager-review task scope"
                )
            if supervision_manager_config(bot) is None:
                raise SystemPayloadSourceError(
                    "supervision portfolio snapshots require an enabled bounded supervision_manager configuration"
                )
        if source_config["type"] == CSV_WORK_ITEMS_SOURCE and not task_scope.startswith(("read-only", "draft-only")):
            raise SystemPayloadSourceError(
                "csv_work_items_v1 requires a worker_profile with a read-only or draft-only task scope"
            )


def _csv_source_root() -> Path:
    root_raw = str(os.environ.get("NEXUSAI_READONLY_CSV_ROOT") or "").strip()
    if not root_raw:
        raise SystemPayloadSourceError("NEXUSAI_READONLY_CSV_ROOT is not configured")
    root = Path(root_raw).resolve()
    if not root.is_dir():
        raise SystemPayloadSourceError("NEXUSAI_READONLY_CSV_ROOT is not an accessible directory")
    return root


def _csv_source_path(relative_path: str) -> Path:
    root = _csv_source_root()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise SystemPayloadSourceError("csv_work_items_v1 path escapes NEXUSAI_READONLY_CSV_ROOT")
    return candidate


def list_csv_work_items_sources() -> list[Dict[str, Any]]:
    """List bounded, non-content metadata for operator-selectable CSV work queues."""
    root = _csv_source_root()
    sources: list[Dict[str, Any]] = []
    for candidate in sorted(root.rglob("*.csv"), key=lambda path: path.as_posix().casefold()):
        if len(sources) >= _CSV_SOURCE_MAX_CATALOG_FILES:
            break
        try:
            source_path = candidate.resolve()
            if not source_path.is_relative_to(root) or not source_path.is_file():
                continue
            source_stat = source_path.stat()
        except OSError:
            continue

        modified_at = datetime.fromtimestamp(source_stat.st_mtime, tz=timezone.utc)
        source_age_seconds = max(0, int((datetime.now(timezone.utc) - modified_at).total_seconds()))
        headers: list[str] = []
        row_count = 0
        issue = ""
        if source_stat.st_size > _CSV_SOURCE_MAX_FILE_BYTES:
            issue = f"source exceeds the {_CSV_SOURCE_MAX_FILE_BYTES}-byte limit"
        else:
            try:
                with source_path.open("r", encoding="utf-8-sig", newline="") as source_file:
                    reader = csv.reader(source_file)
                    raw_headers = next(reader, [])
                    headers = [str(field or "").strip() for field in raw_headers]
                    row_count = sum(1 for _ in reader)
            except (OSError, UnicodeDecodeError, csv.Error):
                issue = "source cannot be read as UTF-8 CSV"

        if not issue:
            if not headers:
                issue = "source has no header row"
            elif len(headers) > _CSV_SOURCE_MAX_CATALOG_HEADERS:
                issue = f"source has more than {_CSV_SOURCE_MAX_CATALOG_HEADERS} header fields"
            elif any(not _SAFE_FIELD_NAME.fullmatch(field) for field in headers):
                issue = "source has unsupported header field names"
            elif len(set(headers)) != len(headers):
                issue = "source has duplicate header field names"

        sources.append(
            {
                "relative_path": source_path.relative_to(root).as_posix(),
                "headers": headers if not issue else [],
                "row_count": row_count,
                "size_bytes": int(source_stat.st_size),
                "modified_at": modified_at.isoformat(),
                "source_age_seconds": source_age_seconds,
                "max_supported_age_hours": _CSV_SOURCE_MAX_AGE_HOURS,
                "available": not issue,
                "issue": issue or None,
            }
        )
    return sources


def _truncate_csv_value(value: Any) -> str:
    text = str(value or "").strip()
    return text[:_CSV_SOURCE_MAX_VALUE_CHARS]


def _matches_csv_filters(
    row: Dict[str, Any],
    *,
    include_equals: Dict[str, set[str]],
    exclude_equals: Dict[str, set[str]],
    require_non_empty_fields: list[str],
) -> bool:
    for field, allowed in include_equals.items():
        if _truncate_csv_value(row.get(field)).casefold() not in allowed:
            return False
    for field, blocked in exclude_equals.items():
        if _truncate_csv_value(row.get(field)).casefold() in blocked:
            return False
    if any(not _truncate_csv_value(row.get(field)) for field in require_non_empty_fields):
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
            required_fields = (
                set(config["columns"])
                | set(config["include_equals"])
                | set(config["exclude_equals"])
                | set(config["require_non_empty_fields"])
            )
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
                    require_non_empty_fields=config["require_non_empty_fields"],
                ):
                    continue
                selected_rows.append({field: _truncate_csv_value(row.get(field)) for field in config["columns"]})
                if len(selected_rows) >= int(config["max_rows"]):
                    break
    except UnicodeDecodeError as exc:
        raise SystemPayloadSourceError("csv_work_items_v1 source must be UTF-8 text") from exc

    payload_field_map = config.get("payload_field_map") or {}
    if payload_field_map and not selected_rows:
        raise ScheduleWorkQueueEmpty("csv_work_items_v1 has no eligible work item for this run")
    mapped_task_payload = {
        target: selected_rows[0][source]
        for target, source in payload_field_map.items()
    }

    payload = {
        "source": CSV_WORK_ITEMS_SOURCE,
        "source_name": source_path.name,
        "source_modified_at": modified_at.isoformat(),
        "source_age_seconds": source_age_seconds,
        "selected_count": len(selected_rows),
        "max_rows": int(config["max_rows"]),
        "selected_rows": selected_rows,
    }
    if mapped_task_payload:
        payload["mapped_task_payload"] = mapped_task_payload
    return payload


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
    failed_active_schedule_count = sum(
        1
        for schedule in (schedules or [])
        if isinstance(schedule, dict)
        and str(schedule.get("status") or "").lower() == "active"
        and str(schedule.get("last_run_status") or "").lower() == "failed"
    )
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
            "failed_active_last_run_count": failed_active_schedule_count,
        },
    }


async def operational_quality_snapshot(
    *,
    worker_registry: Any,
    worker_probe_store: Any,
    bot_registry: Any,
    task_manager: Any,
    schedule_engine: Any,
) -> Dict[str, Any]:
    """Return aggregate operational evidence for a non-mutating quality review.

    This deliberately excludes task, worker, schedule, backend, and provider identifiers.
    The target quality worker receives enough evidence to assess the control plane's
    operational posture without receiving task content, error text, or a fleet map.
    """
    summary = await fleet_health_summary(
        worker_registry=worker_registry,
        worker_probe_store=worker_probe_store,
        bot_registry=bot_registry,
        task_manager=task_manager,
        schedule_engine=schedule_engine,
    )
    workers = summary.get("workers") if isinstance(summary.get("workers"), dict) else {}
    bots = summary.get("bots") if isinstance(summary.get("bots"), dict) else {}
    tasks = summary.get("tasks") if isinstance(summary.get("tasks"), dict) else {}
    schedules = summary.get("schedules") if isinstance(summary.get("schedules"), dict) else {}
    runtime_attention = workers.get("runtime_attention")

    return {
        "source": OPERATIONAL_QUALITY_SNAPSHOT_SOURCE,
        "generated_at": summary.get("generated_at"),
        "scope": "aggregate control-plane operational metadata only",
        "quality_dimensions": {
            "worker_readiness": {
                "enabled": int(workers.get("enabled") or 0),
                "online": int(workers.get("online") or 0),
                "offline": int(workers.get("offline") or 0),
                "runtime_attention_count": len(runtime_attention) if isinstance(runtime_attention, list) else 0,
            },
            "bot_readiness": {
                "enabled": int(bots.get("enabled") or 0),
                "enabled_with_runtime_attention": int(bots.get("enabled_with_runtime_attention") or 0),
            },
            "task_reliability": {
                "recent_window_hours": int(tasks.get("recent_window_hours") or 0),
                "current_by_status": dict(tasks.get("by_status") or {}),
                "recent_by_status": dict(tasks.get("recent_by_status") or {}),
                "recent_failed_by_category": dict(tasks.get("recent_failed_by_category") or {}),
                "recent_unrecovered_failed_by_category": dict(
                    tasks.get("recent_unrecovered_failed_by_category") or {}
                ),
                "recent_recovered_failed_by_category": dict(
                    tasks.get("recent_recovered_failed_by_category") or {}
                ),
            },
            "schedule_reliability": {
                "active": int(schedules.get("active") or 0),
                "failed_active_last_run_count": int(schedules.get("failed_active_last_run_count") or 0),
            },
        },
    }


async def supervision_portfolio_snapshot(
    *,
    manager_bot: Any,
    bot_registry: Any,
    task_manager: Any,
    schedule_engine: Any,
    supervision_store: Any,
) -> Dict[str, Any]:
    """Return bounded, non-content evidence for one declared manager portfolio."""
    config = supervision_manager_config(manager_bot)
    if config is None:
        raise SystemPayloadSourceError("manager bot has no enabled supervision_manager configuration")
    bot_ids = list(config.get("bot_ids") or [])[:100]
    schedule_ids = list(config.get("schedule_ids") or [])[:100]
    tasks = await _await_if_needed(task_manager.list_tasks(limit=200))
    latest_task_by_bot: Dict[str, Any] = {}
    for task in tasks or []:
        bot_id = str(getattr(task, "bot_id", "") or "").strip()
        if bot_id not in bot_ids:
            continue
        previous = latest_task_by_bot.get(bot_id)
        current_updated = _task_updated_at(task)
        previous_updated = _task_updated_at(previous) if previous is not None else None
        if previous is None or (current_updated is not None and (previous_updated is None or current_updated > previous_updated)):
            latest_task_by_bot[bot_id] = task
    known_bots = {
        str(getattr(bot, "id", "") or "").strip(): bot
        for bot in (await _await_if_needed(bot_registry.list()) or [])
    }
    schedules = await _await_if_needed(schedule_engine.list_schedules(limit=500))
    schedule_by_id = {
        str(schedule.get("id") or "").strip(): schedule
        for schedule in schedules or []
        if isinstance(schedule, dict)
    }
    holds_by_bot = {
        str(hold.get("bot_id") or "").strip(): hold
        for hold in (await _await_if_needed(supervision_store.list_holds(limit=200)) or [])
        if isinstance(hold, dict)
    }
    portfolio_bots: list[Dict[str, Any]] = []
    for bot_id in bot_ids:
        bot = known_bots.get(bot_id)
        task = latest_task_by_bot.get(bot_id)
        hold = holds_by_bot.get(bot_id)
        portfolio_bots.append(
            {
                "bot_id": bot_id,
                "name": str(getattr(bot, "name", "") or "")[:160] or None,
                "registered": bot is not None,
                "enabled": bool(getattr(bot, "enabled", False)) if bot is not None else False,
                "supervision_hold": {
                    "active": hold is not None,
                    "reason": str(hold.get("reason") or "")[:2_000] if hold else None,
                },
                "latest_task": (
                    {
                        "status": str(getattr(task, "status", "unknown") or "unknown").lower(),
                        "updated_at": getattr(task, "updated_at", None),
                        **(
                            {"result_status": result_status}
                            if (result_status := _task_result_status(task)) is not None
                            else {}
                        ),
                        **(
                            {"workflow_scope": workflow_scope}
                            if (workflow_scope := _task_workflow_scope(task))
                            else {}
                        ),
                    }
                    if task is not None
                    else None
                ),
            }
        )
    portfolio_schedules: list[Dict[str, Any]] = []
    for schedule_id in schedule_ids:
        schedule = schedule_by_id.get(schedule_id)
        portfolio_schedules.append(
            {
                "schedule_id": schedule_id,
                "name": str(schedule.get("name") or "")[:160] if schedule else None,
                "registered": schedule is not None,
                "status": str(schedule.get("status") or "unknown").lower() if schedule else "missing",
                "last_run_status": str(schedule.get("last_run_status") or "").lower() if schedule else None,
                "last_run_at": schedule.get("last_run_at") if schedule else None,
            }
        )
    return {
        "source": SUPERVISION_PORTFOLIO_SOURCE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "declared manager portfolio metadata only; no task content, prompts, or raw errors",
        "project_id": config.get("project_id"),
        "bots": portfolio_bots,
        "schedules": portfolio_schedules,
    }


async def materialize_system_schedule_payload(
    schedule: Dict[str, Any],
    *,
    worker_registry: Any,
    worker_probe_store: Any,
    bot_registry: Any,
    task_manager: Any,
    schedule_engine: Any,
    supervision_store: Any = None,
) -> Dict[str, Any]:
    """Return non-secret payload additions for an approved internal source."""
    configs = system_payload_source_configs(schedule)
    if not configs:
        return {}
    materialized: Dict[str, Any] = {}
    for config in configs:
        additions = await _materialize_system_payload_source(
            schedule,
            config=config,
            worker_registry=worker_registry,
            worker_probe_store=worker_probe_store,
            bot_registry=bot_registry,
            task_manager=task_manager,
            schedule_engine=schedule_engine,
            supervision_store=supervision_store,
        )
        duplicate_fields = set(materialized).intersection(additions)
        if duplicate_fields:
            raise SystemPayloadSourceError(
                "system payload sources produced duplicate fields: " + ", ".join(sorted(duplicate_fields))
            )
        materialized.update(additions)
    return materialized


async def _materialize_system_payload_source(
    schedule: Dict[str, Any],
    *,
    config: Dict[str, Any],
    worker_registry: Any,
    worker_probe_store: Any,
    bot_registry: Any,
    task_manager: Any,
    schedule_engine: Any,
    supervision_store: Any = None,
) -> Dict[str, Any]:
    if config["type"] == FLEET_HEALTH_SUMMARY_SOURCE:
        summary = await fleet_health_summary(
            worker_registry=worker_registry,
            worker_probe_store=worker_probe_store,
            bot_registry=bot_registry,
            task_manager=task_manager,
            schedule_engine=schedule_engine,
        )
        return {config["target_field"]: json.dumps(summary, sort_keys=True, separators=(",", ":"))}
    if config["type"] == OPERATIONAL_QUALITY_SNAPSHOT_SOURCE:
        snapshot = await operational_quality_snapshot(
            worker_registry=worker_registry,
            worker_probe_store=worker_probe_store,
            bot_registry=bot_registry,
            task_manager=task_manager,
            schedule_engine=schedule_engine,
        )
        return {config["target_field"]: json.dumps(snapshot, sort_keys=True, separators=(",", ":"))}
    if config["type"] == SUPERVISION_PORTFOLIO_SOURCE:
        if supervision_store is None:
            raise SystemPayloadSourceError("supervision store is unavailable")
        target_bot_id = str(schedule.get("target_bot_id") or "").strip()
        if not target_bot_id:
            raise SystemPayloadSourceError("supervision portfolio snapshots require a target_bot_id")
        try:
            manager_bot = await _await_if_needed(bot_registry.get(target_bot_id))
        except Exception as exc:
            raise SystemPayloadSourceError("supervision manager bot is unavailable") from exc
        snapshot = await supervision_portfolio_snapshot(
            manager_bot=manager_bot,
            bot_registry=bot_registry,
            task_manager=task_manager,
            schedule_engine=schedule_engine,
            supervision_store=supervision_store,
        )
        return {config["target_field"]: json.dumps(snapshot, sort_keys=True, separators=(",", ":"))}
    if config["type"] == CSV_WORK_ITEMS_SOURCE:
        payload = csv_work_items_payload(config)
        mapped_task_payload = payload.pop("mapped_task_payload", {})
        if not isinstance(mapped_task_payload, dict):
            mapped_task_payload = {}
        return {
            config["target_field"]: json.dumps(payload, sort_keys=True, separators=(",", ":")),
            **mapped_task_payload,
        }
    raise SystemPayloadSourceError(f"unsupported system_payload_source type: {config['type']}")
