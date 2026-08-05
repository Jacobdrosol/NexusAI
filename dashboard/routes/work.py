"""Work overview blueprint for project and manager operational visibility."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from dashboard.cp_client import get_cp_client
from dashboard.work_overview import (
    ACTIVE_STATUSES,
    STALE_ACTIVE_SECONDS,
    STALE_WAITING_SECONDS,
    WAITING_STATUSES,
    _age_label,
    _age_seconds,
    _parse_datetime,
    build_work_overview,
    manager_id_for_task,
    project_id_for_task,
)
from shared.settings_manager import SettingsManager

bp = Blueprint("work", __name__)

STOPPABLE_WORK_STATUSES = {"queued", "blocked", "running"}
LANE_DETAIL_STATUSES = {"queued", "blocked", "running", "failed", "retried"}
OVERVIEW_ACTIVE_STATUSES = sorted(LANE_DETAIL_STATUSES)
OVERVIEW_RECENT_LIMIT = 250
OVERVIEW_ACTIVE_LIMIT = 1000


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "totals": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tasks_with_usage": 0,
            "tasks_without_usage": 0,
        },
        "by_project": [],
        "by_manager": [],
        "by_project_manager_bot": [],
        "by_bot": [],
        "by_provider_model": [],
    }


def _empty_chat_usage_summary() -> dict[str, Any]:
    return {
        "totals": {
            "messages": 0,
            "messages_with_usage": 0,
            "messages_without_usage": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "by_conversation": [],
        "by_bot": [],
        "by_provider_model": [],
        "chat_token_governor": {"enabled": False, "limits": {}},
    }


def _normalize_usage_summary(summary: Any) -> dict[str, Any]:
    normalized = _empty_usage_summary()
    if not isinstance(summary, dict):
        return normalized
    for key, value in summary.items():
        if key not in normalized:
            normalized[key] = value
    totals = summary.get("totals")
    if isinstance(totals, dict):
        normalized["totals"] = {**normalized["totals"], **totals}
    for key in ("by_project", "by_manager", "by_project_manager_bot", "by_bot", "by_provider_model"):
        value = summary.get(key)
        normalized[key] = value if isinstance(value, list) else []
    return normalized


def _normalize_chat_usage_summary(summary: Any) -> dict[str, Any]:
    normalized = _empty_chat_usage_summary()
    if not isinstance(summary, dict):
        return normalized
    for key, value in summary.items():
        if key not in normalized:
            normalized[key] = value
    totals = summary.get("totals")
    if isinstance(totals, dict):
        normalized["totals"] = {**normalized["totals"], **totals}
    for key in ("by_conversation", "by_bot", "by_provider_model"):
        value = summary.get(key)
        normalized[key] = value if isinstance(value, list) else []
    governor = summary.get("chat_token_governor")
    normalized["chat_token_governor"] = governor if isinstance(governor, dict) else {"enabled": False, "limits": {}}
    return normalized


def _usage_health(usage: dict[str, Any]) -> dict[str, Any]:
    totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}
    measured_tasks = _safe_count(totals, "tasks_with_usage")
    missing_tasks = _safe_count(totals, "tasks_without_usage")
    total_tasks = measured_tasks + missing_tasks
    total_tokens = _safe_count(totals, "total_tokens")
    missing_ratio = round(missing_tasks / total_tasks, 2) if total_tasks else 0.0

    if missing_tasks and missing_ratio >= 0.5:
        level = "critical"
        reason = "token usage telemetry is incomplete for most measured tasks"
    elif missing_tasks:
        level = "warning"
        reason = "some tasks are missing token usage telemetry"
    elif total_tokens:
        level = "ready"
        reason = "token usage telemetry is complete for measured tasks"
    else:
        level = "idle"
        reason = "no token usage recorded in this window"

    return {
        "level": level,
        "reason": reason,
        "measured_tasks": measured_tasks,
        "missing_tasks": missing_tasks,
        "total_tasks": total_tasks,
        "missing_ratio": missing_ratio,
        "total_tokens": total_tokens,
    }


def _chat_usage_health(chat_usage: dict[str, Any]) -> dict[str, Any]:
    totals = chat_usage.get("totals") if isinstance(chat_usage.get("totals"), dict) else {}
    measured_messages = _safe_count(totals, "messages_with_usage")
    missing_messages = _safe_count(totals, "messages_without_usage")
    total_messages = measured_messages + missing_messages
    total_tokens = _safe_count(totals, "total_tokens")
    missing_ratio = round(missing_messages / total_messages, 2) if total_messages else 0.0

    if missing_messages and missing_ratio >= 0.5:
        level = "critical"
        reason = "chat token usage telemetry is incomplete for most assistant messages"
    elif missing_messages:
        level = "warning"
        reason = "some assistant chat messages are missing token usage telemetry"
    elif total_tokens:
        level = "ready"
        reason = "chat token usage telemetry is complete for measured messages"
    else:
        level = "idle"
        reason = "no chat token usage recorded in this window"

    return {
        "level": level,
        "reason": reason,
        "measured_messages": measured_messages,
        "missing_messages": missing_messages,
        "total_messages": total_messages,
        "missing_ratio": missing_ratio,
        "total_tokens": total_tokens,
    }


def _usage_pressure_lanes(usage: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    governor = usage.get("token_governor") if isinstance(usage.get("token_governor"), dict) else {}
    limits = governor.get("limits") if isinstance(governor.get("limits"), dict) else {}
    default_bot_limit = _safe_count(limits, "bot_hourly_tokens")
    bot_overrides = limits.get("bot_hourly_token_overrides")
    bot_overrides = bot_overrides if isinstance(bot_overrides, dict) else {}
    rows: list[dict[str, Any]] = []
    for item in usage.get("by_bot") or []:
        if not isinstance(item, dict):
            continue
        bot_id = str(item.get("bot_id") or "").strip()
        if not bot_id:
            continue
        try:
            effective_limit = int(bot_overrides.get(bot_id, default_bot_limit) or 0)
        except (TypeError, ValueError):
            effective_limit = default_bot_limit
        if effective_limit <= 0:
            continue
        total_tokens = _safe_count(item, "total_tokens")
        ratio = round(total_tokens / effective_limit, 2) if effective_limit else 0.0
        if ratio >= 1:
            level = "critical"
        elif ratio >= 0.8:
            level = "warning"
        else:
            level = "ready"
        rows.append(
            {
                "bot_id": bot_id,
                "total_tokens": total_tokens,
                "hourly_limit": effective_limit,
                "remaining_tokens": max(0, effective_limit - total_tokens),
                "usage_ratio": ratio,
                "level": level,
                "cap_source": "override" if bot_id in bot_overrides else "default",
                "tasks_with_usage": _safe_count(item, "tasks_with_usage"),
                "tasks_without_usage": _safe_count(item, "tasks_without_usage"),
            }
        )
    rows.sort(key=lambda row: (row["level"] != "critical", row["level"] != "warning", -row["usage_ratio"], -row["total_tokens"]))
    return rows[: max(1, int(limit or 10))]


def _chat_usage_pressure_lanes(chat_usage: dict[str, Any], *, limit: int = 10) -> list[dict[str, Any]]:
    governor = chat_usage.get("chat_token_governor") if isinstance(chat_usage.get("chat_token_governor"), dict) else {}
    if not governor.get("enabled"):
        return []
    limits = governor.get("limits") if isinstance(governor.get("limits"), dict) else {}
    default_bot_limit = _safe_count(limits, "bot_hourly_tokens")
    bot_overrides = limits.get("bot_hourly_token_overrides")
    bot_overrides = bot_overrides if isinstance(bot_overrides, dict) else {}
    rows: list[dict[str, Any]] = []
    for item in chat_usage.get("by_bot") or []:
        if not isinstance(item, dict):
            continue
        bot_id = str(item.get("bot_id") or "").strip()
        if not bot_id:
            continue
        try:
            effective_limit = int(bot_overrides.get(bot_id, default_bot_limit) or 0)
        except (TypeError, ValueError):
            effective_limit = default_bot_limit
        if effective_limit <= 0:
            continue
        total_tokens = _safe_count(item, "total_tokens")
        ratio = round(total_tokens / effective_limit, 2) if effective_limit else 0.0
        if ratio >= 1:
            level = "critical"
        elif ratio >= 0.8:
            level = "warning"
        else:
            level = "ready"
        rows.append(
            {
                "bot_id": bot_id,
                "total_tokens": total_tokens,
                "hourly_limit": effective_limit,
                "remaining_tokens": max(0, effective_limit - total_tokens),
                "usage_ratio": ratio,
                "level": level,
                "cap_source": "override" if bot_id in bot_overrides else "default",
                "messages_with_usage": _safe_count(item, "messages_with_usage"),
                "messages_without_usage": _safe_count(item, "messages_without_usage"),
                "last_message_at": item.get("last_message_at") or "",
            }
        )
    rows.sort(key=lambda row: (row["level"] != "critical", row["level"] != "warning", -row["usage_ratio"], -row["total_tokens"]))
    return rows[: max(1, int(limit or 10))]


def _usage_brief(usage: dict[str, Any], *, limit: int = 5) -> dict[str, Any]:
    totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}

    def _top_rows(key: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in usage.get(key) or [] if isinstance(row, dict)]
        rows.sort(key=lambda row: _safe_count(row, "total_tokens"), reverse=True)
        return rows[: max(1, int(limit or 5))]

    return {
        "totals": {
            "prompt_tokens": _safe_count(totals, "prompt_tokens"),
            "completion_tokens": _safe_count(totals, "completion_tokens"),
            "total_tokens": _safe_count(totals, "total_tokens"),
            "tasks_with_usage": _safe_count(totals, "tasks_with_usage"),
            "tasks_without_usage": _safe_count(totals, "tasks_without_usage"),
        },
        "top_bots": _top_rows("by_bot"),
        "top_provider_models": _top_rows("by_provider_model"),
        "top_projects": _top_rows("by_project"),
        "top_managers": _top_rows("by_manager"),
        "top_project_manager_bots": _top_rows("by_project_manager_bot"),
    }


def _chat_usage_brief(chat_usage: dict[str, Any], *, limit: int = 5) -> dict[str, Any]:
    totals = chat_usage.get("totals") if isinstance(chat_usage.get("totals"), dict) else {}

    def _top_rows(key: str) -> list[dict[str, Any]]:
        rows = [dict(row) for row in chat_usage.get(key) or [] if isinstance(row, dict)]
        rows.sort(key=lambda row: _safe_count(row, "total_tokens"), reverse=True)
        return rows[: max(1, int(limit or 5))]

    return {
        "totals": {
            "messages": _safe_count(totals, "messages"),
            "messages_with_usage": _safe_count(totals, "messages_with_usage"),
            "messages_without_usage": _safe_count(totals, "messages_without_usage"),
            "prompt_tokens": _safe_count(totals, "prompt_tokens"),
            "completion_tokens": _safe_count(totals, "completion_tokens"),
            "total_tokens": _safe_count(totals, "total_tokens"),
        },
        "top_conversations": _top_rows("by_conversation"),
        "top_bots": _top_rows("by_bot"),
        "top_provider_models": _top_rows("by_provider_model"),
    }


def _parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _bot_cap_audit_rows(*, limit: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        audit_log = SettingsManager.instance().get_audit_log(50)
    except Exception:
        return rows

    for item in audit_log:
        if not isinstance(item, dict) or item.get("key") != "token_governor_bot_hourly_limits":
            continue
        old_limits = _parse_json_object(item.get("old_value"))
        new_limits = _parse_json_object(item.get("new_value"))
        changed_bots = sorted(
            str(bot_id)
            for bot_id in set(old_limits) | set(new_limits)
            if old_limits.get(bot_id) != new_limits.get(bot_id)
        )
        rows.append(
            {
                "changed_at": item.get("changed_at") or "",
                "changed_by": item.get("changed_by") or "unknown",
                "override_count": len(new_limits),
                "changed_bots": changed_bots[:8],
                "changed_bot_count": len(changed_bots),
            }
        )
        if len(rows) >= max(1, int(limit or 8)):
            break
    return rows


def _token_governor_queue_pressure(
    tasks: list[dict[str, Any]],
    usage: dict[str, Any],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    governor = usage.get("token_governor") if isinstance(usage.get("token_governor"), dict) else {}
    limits = governor.get("limits") if isinstance(governor.get("limits"), dict) else {}
    cap_by_scope = {
        "bot": _safe_count(limits, "max_queued_llm_tasks_per_bot"),
        "project": _safe_count(limits, "max_queued_llm_tasks_per_project"),
        "manager": _safe_count(limits, "max_queued_llm_tasks_per_manager"),
    }
    if not any(cap_by_scope.values()):
        return []

    queued_by_scope: dict[tuple[str, str], int] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("status") or "").strip().lower() != "queued":
            continue
        values = {
            "bot": str(task.get("bot_id") or "").strip(),
            "project": project_id_for_task(task),
            "manager": f"{project_id_for_task(task)}::{manager_id_for_task(task)}",
        }
        for scope, value in values.items():
            if value and cap_by_scope.get(scope, 0) > 0:
                key = (scope, value)
                queued_by_scope[key] = queued_by_scope.get(key, 0) + 1

    rows: list[dict[str, Any]] = []
    for (scope, value), queued_count in queued_by_scope.items():
        cap = cap_by_scope.get(scope, 0)
        if cap <= 0:
            continue
        ratio = round(queued_count / cap, 2)
        if ratio >= 1:
            level = "critical"
        elif ratio >= 0.8:
            level = "warning"
        else:
            level = "ready"
        rows.append(
            {
                "scope": scope,
                "value": value,
                "queued_count": queued_count,
                "limit": cap,
                "remaining": max(0, cap - queued_count),
                "usage_ratio": ratio,
                "level": level,
            }
        )
    rows.sort(key=lambda row: (row["level"] != "critical", row["level"] != "warning", -row["usage_ratio"], -row["queued_count"]))
    return rows[: max(1, int(limit or 12))]


def _quality_gate_summary(cp: Any, suites_payload: Any, *, limit: int = 5) -> dict[str, Any]:
    suites = suites_payload.get("suites") if isinstance(suites_payload, dict) else []
    suites = [suite for suite in (suites or []) if isinstance(suite, dict)]
    rows: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    list_runs = getattr(cp, "list_platform_ai_quality_suite_runs", None)

    for suite in suites[: max(1, int(limit or 5))]:
        suite_id = str(suite.get("id") or "").strip()
        if not suite_id:
            continue
        runs_payload = _safe_call(list_runs, suite_id, limit=1, timeout=1.0) if callable(list_runs) else None
        runs = runs_payload.get("runs") if isinstance(runs_payload, dict) else []
        runs = [run for run in (runs or []) if isinstance(run, dict)]
        latest = runs[0] if runs else {}
        status = str(latest.get("status") or "not_run").strip().lower() or "not_run"
        status_counts[status] = status_counts.get(status, 0) + 1
        suite_def = suite.get("suite") if isinstance(suite.get("suite"), dict) else {}
        tests = suite_def.get("tests") if isinstance(suite_def.get("tests"), list) else []
        target = (
            str(suite.get("pipeline_bot_id") or "").strip()
            or str(suite.get("assignment_id") or "").strip()
            or str(suite.get("session_id") or "").strip()
            or "global"
        )
        rows.append(
            {
                "suite_id": suite_id,
                "name": str(suite.get("name") or suite_id).strip(),
                "target": target,
                "test_count": len(tests),
                "latest_status": status,
                "latest_score": latest.get("score"),
                "latest_at": str(latest.get("completed_at") or latest.get("created_at") or "").strip(),
            }
        )

    return {
        "available": isinstance(suites_payload, dict),
        "suite_count": len(suites),
        "shown_count": len(rows),
        "status_counts": status_counts,
        "rows": rows,
    }


def _safe_count(container: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(container.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _attach_attention_summary(overview: dict[str, Any]) -> None:
    totals = overview.get("totals") if isinstance(overview.get("totals"), dict) else {}
    workers = overview.get("workers") if isinstance(overview.get("workers"), dict) else {}
    metadata_health = overview.get("metadata_health") if isinstance(overview.get("metadata_health"), dict) else {}
    route_evidence = overview.get("route_evidence") if isinstance(overview.get("route_evidence"), dict) else {}
    usage = overview.get("usage") if isinstance(overview.get("usage"), dict) else {}
    usage_totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}

    problem_tasks = _safe_count(totals, "problem")
    stale_work = _safe_count(totals, "stale_active") + _safe_count(totals, "stale_waiting")
    metadata_gaps = _safe_count(metadata_health, "missing_project_count") + _safe_count(metadata_health, "inferred_manager_count")
    route_gaps = _safe_count(route_evidence, "missing_active_problem_count")
    worker_issues = _safe_count(workers, "issue_count")
    usage_gaps = _safe_count(usage_totals, "tasks_without_usage")
    total = problem_tasks + stale_work + metadata_gaps + route_gaps + worker_issues + usage_gaps
    overview["attention"] = {
        "total": total,
        "problem_tasks": problem_tasks,
        "stale_work": stale_work,
        "metadata_gaps": metadata_gaps,
        "route_gaps": route_gaps,
        "worker_issues": worker_issues,
        "usage_gaps": usage_gaps,
        "level": "critical" if problem_tasks or stale_work or worker_issues else ("warning" if total else "ready"),
    }


def _snapshot_health(task_snapshot: dict[str, Any]) -> dict[str, Any]:
    unavailable = []
    capped = []
    if bool(task_snapshot.get("active_unavailable")):
        unavailable.append("active/problem")
    if bool(task_snapshot.get("recent_unavailable")):
        unavailable.append("recent")
    if bool(task_snapshot.get("active_window_at_limit")):
        capped.append("active/problem")
    if bool(task_snapshot.get("recent_window_at_limit")):
        capped.append("recent")

    if unavailable:
        level = "critical"
        reason = "task snapshot windows unavailable: " + ", ".join(unavailable)
    elif capped:
        level = "warning"
        reason = "task snapshot windows at limit: " + ", ".join(capped)
    else:
        level = "ready"
        reason = "task snapshot loaded within configured windows"

    return {
        "level": level,
        "reason": reason,
        "unavailable_windows": unavailable,
        "capped_windows": capped,
        "active_rows": _safe_count(task_snapshot, "active_rows"),
        "recent_rows": _safe_count(task_snapshot, "recent_rows"),
        "merged_rows": _safe_count(task_snapshot, "merged_rows"),
        "active_limit": _safe_count(task_snapshot, "active_limit"),
        "recent_limit": _safe_count(task_snapshot, "recent_limit"),
    }


def _merge_task_rows(*groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    anonymous_index = 0
    for group in groups:
        for task in group or []:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                task_id = f"anonymous:{anonymous_index}"
                anonymous_index += 1
            merged[task_id] = task
    return list(merged.values())


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        fallback_kwargs = {key: value for key, value in kwargs.items() if key != "timeout"}
        try:
            return fn(*args, **fallback_kwargs)
        except TypeError:
            try:
                return fn(*args)
            except Exception:
                return None
        except Exception:
            return None
    except Exception:
        return None


def _control_plane_warning(cp: Any, source: str) -> dict[str, Any]:
    reason = "Control plane request failed."
    detail = ""
    status_code = None

    unavailable_reason = getattr(cp, "unavailable_reason", None)
    if callable(unavailable_reason):
        try:
            reason = str(unavailable_reason() or reason)
        except Exception:
            reason = "Control plane request failed."

    last_error = getattr(cp, "last_error", None)
    if callable(last_error):
        try:
            error = last_error()
        except Exception:
            error = None
        if isinstance(error, dict):
            detail = str(error.get("detail") or error.get("error") or "")
            status_code = error.get("status_code")

    warning: dict[str, Any] = {"source": source, "reason": reason}
    if detail:
        warning["detail"] = detail
    if status_code is not None:
        warning["status_code"] = status_code
    return warning


def _safe_cp_call(cp: Any, source: str, fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, dict[str, Any] | None]:
    result = _safe_call(fn, *args, **kwargs)
    if result is None:
        return None, _control_plane_warning(cp, source)
    return result, None


def _require_admin() -> None:
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


def _load_work_overview() -> dict[str, Any]:
    cp = get_cp_client()
    warnings: list[dict[str, Any]] = []
    active_tasks_result, warning = _safe_cp_call(
        cp,
        "active/problem task summaries",
        cp.list_tasks,
        limit=OVERVIEW_ACTIVE_LIMIT,
        statuses=OVERVIEW_ACTIVE_STATUSES,
        include_content=False,
        timeout=1.5,
    )
    if warning:
        warnings.append(warning)
    active_tasks = active_tasks_result if isinstance(active_tasks_result, list) else []

    recent_tasks_result, warning = _safe_cp_call(
        cp,
        "recent task summaries",
        cp.list_tasks,
        limit=OVERVIEW_RECENT_LIMIT,
        include_content=False,
        timeout=1.5,
    )
    if warning:
        warnings.append(warning)
    recent_tasks = recent_tasks_result if isinstance(recent_tasks_result, list) else []

    tasks = _merge_task_rows(active_tasks, recent_tasks)
    projects_result, warning = _safe_cp_call(cp, "projects", cp.list_projects, timeout=1.0)
    if warning:
        warnings.append(warning)
    projects = projects_result if isinstance(projects_result, list) else []

    bots_result, warning = _safe_cp_call(cp, "bots", cp.list_bots, timeout=1.0)
    if warning:
        warnings.append(warning)
    bots = bots_result if isinstance(bots_result, list) else []

    workers_result, warning = _safe_cp_call(cp, "workers", cp.list_workers, timeout=1.0)
    if warning:
        warnings.append(warning)
    workers = workers_result if isinstance(workers_result, list) else []

    holds_payload, warning = _safe_cp_call(cp, "dispatch holds", cp.list_work_dispatch_holds, timeout=1.0)
    if warning:
        warnings.append(warning)
    holds_payload = holds_payload if isinstance(holds_payload, dict) else {}
    holds = holds_payload.get("holds") if isinstance(holds_payload, dict) else []
    overview = build_work_overview(tasks=tasks, projects=projects, bots=bots, workers=workers, holds=holds)
    overview["data_degraded"] = bool(warnings)
    overview["data_warnings"] = warnings
    overview["task_snapshot"] = {
        "active_limit": OVERVIEW_ACTIVE_LIMIT,
        "recent_limit": OVERVIEW_RECENT_LIMIT,
        "active_rows": len(active_tasks) if isinstance(active_tasks, list) else 0,
        "recent_rows": len(recent_tasks) if isinstance(recent_tasks, list) else 0,
        "merged_rows": len(tasks),
        "active_statuses": OVERVIEW_ACTIVE_STATUSES,
        "active_unavailable": active_tasks_result is None,
        "recent_unavailable": recent_tasks_result is None,
        "active_window_at_limit": isinstance(active_tasks, list) and len(active_tasks) >= OVERVIEW_ACTIVE_LIMIT,
        "recent_window_at_limit": isinstance(recent_tasks, list) and len(recent_tasks) >= OVERVIEW_RECENT_LIMIT,
    }
    overview["snapshot_health"] = _snapshot_health(overview["task_snapshot"])
    task_usage = getattr(cp, "task_usage", None)
    if callable(task_usage):
        usage, warning = _safe_cp_call(cp, "token usage", task_usage, hours=24, limit_bots=25, timeout=1.5)
        overview["usage"] = _normalize_usage_summary(usage)
        if warning:
            warnings.append(warning)
            overview["data_degraded"] = True
            overview["data_warnings"] = warnings
    else:
        overview["usage"] = _empty_usage_summary()
    overview["usage_health"] = _usage_health(overview["usage"])
    overview["usage_brief"] = _usage_brief(overview["usage"])
    overview["usage_pressure_lanes"] = _usage_pressure_lanes(overview["usage"])
    overview["token_governor_queue_pressure"] = _token_governor_queue_pressure(tasks, overview["usage"])
    chat_usage = getattr(cp, "chat_usage", None)
    if callable(chat_usage):
        chat_usage_result, warning = _safe_cp_call(cp, "chat token usage", chat_usage, hours=24, limit_conversations=25, timeout=1.5)
        overview["chat_usage"] = _normalize_chat_usage_summary(chat_usage_result)
        if warning:
            warnings.append(warning)
            overview["data_degraded"] = True
            overview["data_warnings"] = warnings
    else:
        overview["chat_usage"] = _empty_chat_usage_summary()
    overview["chat_usage_health"] = _chat_usage_health(overview["chat_usage"])
    overview["chat_usage_brief"] = _chat_usage_brief(overview["chat_usage"])
    overview["chat_usage_pressure_lanes"] = _chat_usage_pressure_lanes(overview["chat_usage"])
    list_quality_suites = getattr(cp, "list_platform_ai_quality_suites_global", None)
    if callable(list_quality_suites):
        quality_suites, warning = _safe_cp_call(cp, "quality gate suites", list_quality_suites, limit=8, timeout=1.0)
        overview["quality_gates"] = _quality_gate_summary(cp, quality_suites)
        if warning:
            warnings.append(warning)
            overview["data_degraded"] = True
            overview["data_warnings"] = warnings
    else:
        overview["quality_gates"] = _quality_gate_summary(cp, None)
    overview["bot_cap_audit"] = _bot_cap_audit_rows()
    _attach_attention_summary(overview)
    return overview


@bp.get("/work")
@login_required
def work_page() -> str:
    _require_admin()
    return render_template("work.html", overview=_load_work_overview(), error=None)


@bp.get("/api/work/overview")
@login_required
def api_work_overview():
    _require_admin()
    return jsonify(_load_work_overview())


@bp.get("/api/work/brief")
@login_required
def api_work_brief():
    _require_admin()
    overview = _load_work_overview()
    return jsonify(
        {
            "operations_brief": overview.get("operations_brief") or {},
            "attention": overview.get("attention") or {},
            "snapshot_health": overview.get("snapshot_health") or {},
            "usage_health": overview.get("usage_health") or {},
            "usage_brief": overview.get("usage_brief") or {},
            "chat_usage_health": overview.get("chat_usage_health") or {},
            "chat_usage_brief": overview.get("chat_usage_brief") or {},
            "chat_token_governor": (overview.get("chat_usage") or {}).get("chat_token_governor") or {},
            "usage_pressure_lanes": overview.get("usage_pressure_lanes") or [],
            "chat_usage_pressure_lanes": overview.get("chat_usage_pressure_lanes") or [],
            "token_governor_queue_pressure": overview.get("token_governor_queue_pressure") or [],
            "capacity": overview.get("capacity") or {},
            "workers": overview.get("workers") or {},
            "data_degraded": bool(overview.get("data_degraded", False)),
            "data_warnings": overview.get("data_warnings") or [],
        }
    )


def _stoppable_work_matches(
    task: dict[str, Any],
    *,
    project_id: str,
    manager_id: str,
) -> bool:
    status = str(task.get("status") or "").strip().lower()
    if status not in STOPPABLE_WORK_STATUSES:
        return False
    if project_id_for_task(task) != project_id:
        return False
    return not manager_id or manager_id_for_task(task) == manager_id


def _lane_work_matches(
    task: dict[str, Any],
    *,
    project_id: str,
    manager_id: str,
) -> bool:
    status = str(task.get("status") or "").strip().lower()
    if status not in LANE_DETAIL_STATUSES:
        return False
    if project_id_for_task(task) != project_id:
        return False
    return not manager_id or manager_id_for_task(task) == manager_id


def _compact_lane_task(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    provenance = metadata.get("execution_provenance") if isinstance(metadata.get("execution_provenance"), dict) else {}
    error_summary = task.get("error_summary") if isinstance(task.get("error_summary"), dict) else {}
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    status = str(task.get("status") or "")
    status_key = status.strip().lower()
    created_at = _parse_datetime(task.get("created_at"))
    updated_at = _parse_datetime(task.get("updated_at")) or created_at
    now = datetime.now(timezone.utc)
    active_age_seconds = _age_seconds(updated_at, now)
    waiting_age_seconds = _age_seconds(created_at or updated_at, now)
    if status_key in WAITING_STATUSES:
        age_seconds = waiting_age_seconds
        age_basis = "created_at"
        stale = waiting_age_seconds is None or waiting_age_seconds >= STALE_WAITING_SECONDS
    else:
        age_seconds = active_age_seconds
        age_basis = "updated_at"
        stale = status_key in ACTIVE_STATUSES and (active_age_seconds is None or active_age_seconds >= STALE_ACTIVE_SECONDS)
    return {
        "id": str(task.get("id") or ""),
        "bot_id": str(task.get("bot_id") or ""),
        "status": status,
        "project_id": project_id_for_task(task),
        "manager_id": manager_id_for_task(task),
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or ""),
        "age_seconds": age_seconds,
        "age_label": _age_label(age_seconds),
        "age_basis": age_basis,
        "stale": stale,
        "orchestration_id": str(metadata.get("orchestration_id") or ""),
        "step_id": str(metadata.get("step_id") or ""),
        "source": str(metadata.get("source") or ""),
        "worker_id": str(provenance.get("worker_id") or ""),
        "backend_type": str(provenance.get("backend_type") or ""),
        "provider": str(provenance.get("provider") or ""),
        "model": str(provenance.get("model") or ""),
        "has_error": bool(task.get("has_error")),
        "error_type": str(error_summary.get("type") or task.get("error_type") or error.get("type") or ""),
        "error_code": str(error_summary.get("code") or error.get("code") or error.get("error_code") or ""),
        "error_message": str(error_summary.get("message") or error.get("message") or error.get("detail") or ""),
    }


def _orchestration_task_matches(task: dict[str, Any], orchestration_id: str) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return str(metadata.get("orchestration_id") or "").strip() == orchestration_id


@bp.post("/api/work/stop")
@login_required
def api_stop_work():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    project_id = str(body.get("project_id") or "").strip()
    manager_id = str(body.get("manager_id") or "").strip()
    reason = str(body.get("reason") or "operator_stopped_from_work_overview").strip()
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    if not reason:
        return jsonify({"error": "reason is required."}), 400

    try:
        max_tasks = min(max(int(body.get("max_tasks", 50)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "max_tasks must be an integer between 1 and 200."}), 400
    dry_run = bool(body.get("dry_run", False))

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        limit=1000,
        statuses=sorted(STOPPABLE_WORK_STATUSES),
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503

    matches = [
        task
        for task in tasks
        if isinstance(task, dict)
        and _stoppable_work_matches(task, project_id=project_id, manager_id=manager_id)
    ]
    selected = matches[:max_tasks]
    selected_rows = [
        {
            "id": str(task.get("id") or ""),
            "bot_id": str(task.get("bot_id") or ""),
            "status": str(task.get("status") or ""),
            "project_id": project_id_for_task(task),
            "manager_id": manager_id_for_task(task),
        }
        for task in selected
    ]

    if dry_run:
        return jsonify(
            {
                "status": "dry_run",
                "project_id": project_id,
                "manager_id": manager_id,
                "matched_task_count": len(matches),
                "selected_task_count": len(selected_rows),
                "truncated": len(matches) > len(selected_rows),
                "tasks": selected_rows,
            }
        )

    cancelled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for task in selected:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        result = _safe_call(cp.cancel_task, task_id, reason=reason)
        if isinstance(result, dict):
            cancelled.append({"id": task_id, "status": str(result.get("status") or "cancelled")})
        else:
            failed.append({"id": task_id, "error": "cancel failed"})

    return jsonify(
        {
            "status": "ok" if not failed else "partial",
            "project_id": project_id,
            "manager_id": manager_id,
            "reason": reason,
            "matched_task_count": len(matches),
            "selected_task_count": len(selected_rows),
            "cancelled_task_count": len(cancelled),
            "failed_task_count": len(failed),
            "truncated": len(matches) > len(selected_rows),
            "cancelled": cancelled,
            "failed": failed,
        }
    ), (200 if not failed else 207)


@bp.get("/api/work/orchestration")
@login_required
def api_work_orchestration():
    _require_admin()
    orchestration_id = str(request.args.get("orchestration_id") or "").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required."}), 400
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer between 1 and 200."}), 400

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        orchestration_id=orchestration_id,
        limit=1000,
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503

    matching_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and _orchestration_task_matches(task, orchestration_id)
    ]
    matching_tasks.sort(key=lambda task: str(task.get("updated_at") or task.get("created_at") or ""), reverse=True)
    counts: dict[str, int] = {}
    for task in matching_tasks:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1

    return jsonify(
        {
            "orchestration_id": orchestration_id,
            "count": len(matching_tasks),
            "counts": counts,
            "stoppable_count": sum(
                1
                for task in matching_tasks
                if str(task.get("status") or "").strip().lower() in STOPPABLE_WORK_STATUSES
            ),
            "tasks": [_compact_lane_task(task) for task in matching_tasks[:limit]],
            "truncated": len(matching_tasks) > limit,
        }
    )


@bp.post("/api/work/orchestration/stop")
@login_required
def api_stop_orchestration_work():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    orchestration_id = str(body.get("orchestration_id") or "").strip()
    reason = str(body.get("reason") or "operator_stopped_orchestration_from_work_overview").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required."}), 400
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    dry_run = bool(body.get("dry_run", False))

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        orchestration_id=orchestration_id,
        limit=1000,
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503
    matching_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and _orchestration_task_matches(task, orchestration_id)
    ]
    status_counts: dict[str, int] = {}
    cancellable_tasks: list[dict[str, Any]] = []
    for task in matching_tasks:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in STOPPABLE_WORK_STATUSES:
            cancellable_tasks.append(task)

    preview = {
        "orchestration_id": orchestration_id,
        "task_count": len(matching_tasks),
        "cancellable_task_count": len(cancellable_tasks),
        "status_counts": status_counts,
        "tasks": [
            {
                "id": str(task.get("id") or ""),
                "bot_id": str(task.get("bot_id") or ""),
                "status": str(task.get("status") or ""),
                "project_id": project_id_for_task(task),
                "manager_id": manager_id_for_task(task),
            }
            for task in cancellable_tasks[:50]
        ],
        "truncated": len(cancellable_tasks) > 50,
    }
    if dry_run:
        return jsonify({"status": "dry_run", **preview})

    result = _safe_call(cp.cancel_orchestration, orchestration_id, reason=reason)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 503
    return jsonify({"status": "ok", "preview": preview, "result": result})


@bp.get("/api/work/lane")
@login_required
def api_work_lane():
    _require_admin()
    project_id = str(request.args.get("project_id") or "").strip()
    manager_id = str(request.args.get("manager_id") or "").strip()
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer between 1 and 200."}), 400

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        limit=1000,
        statuses=sorted(LANE_DETAIL_STATUSES),
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503
    holds_payload = _safe_call(cp.list_work_dispatch_holds, timeout=1.0) or {}
    holds = holds_payload.get("holds") if isinstance(holds_payload, dict) else []
    hold = None
    if isinstance(holds, list):
        for row in holds:
            if not isinstance(row, dict):
                continue
            if str(row.get("project_id") or "").strip() != project_id:
                continue
            hold_manager = str(row.get("manager_id") or "").strip()
            if (manager_id and hold_manager == manager_id) or (not manager_id and not hold_manager):
                hold = row
                break

    matches = [
        task
        for task in tasks
        if isinstance(task, dict)
        and _lane_work_matches(task, project_id=project_id, manager_id=manager_id)
    ]
    matches.sort(key=lambda task: str(task.get("updated_at") or task.get("created_at") or ""), reverse=True)
    counts: dict[str, int] = {}
    for task in matches:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1

    return jsonify(
        {
            "project_id": project_id,
            "manager_id": manager_id,
            "count": len(matches),
            "counts": counts,
            "stoppable_count": sum(
                1
                for task in matches
                if str(task.get("status") or "").strip().lower() in STOPPABLE_WORK_STATUSES
            ),
            "hold": hold,
            "tasks": [_compact_lane_task(task) for task in matches[:limit]],
            "truncated": len(matches) > limit,
        }
    )


@bp.post("/api/work/hold")
@login_required
def api_hold_work():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    action = str(body.get("action") or "hold").strip().lower()
    project_id = str(body.get("project_id") or "").strip()
    manager_id = str(body.get("manager_id") or "").strip()
    reason = str(body.get("reason") or "operator_hold_from_work_overview").strip()
    if action not in {"hold", "release"}:
        return jsonify({"error": "action must be hold or release."}), 400
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    if action == "hold" and not reason:
        return jsonify({"error": "reason is required."}), 400

    cp = get_cp_client()
    operator_id = str(getattr(current_user, "email", "") or "operator").strip() or "operator"
    if action == "release":
        result = _safe_call(
            cp.release_work_dispatch_hold,
            project_id=project_id,
            manager_id=manager_id,
            operator_id=operator_id,
        )
    else:
        result = _safe_call(
            cp.set_work_dispatch_hold,
            project_id=project_id,
            manager_id=manager_id,
            reason=reason,
            operator_id=operator_id,
        )
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 503
    return jsonify(result)


@bp.post("/api/work/bot-cap")
@login_required
def api_work_bot_cap():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    action = str(body.get("action") or "set").strip().lower()
    bot_id = str(body.get("bot_id") or "").strip()
    if action not in {"set", "clear"}:
        return jsonify({"error": "action must be set or clear."}), 400
    if not bot_id:
        return jsonify({"error": "bot_id is required."}), 400

    mgr = SettingsManager.instance()
    raw_limits = mgr.get("token_governor_bot_hourly_limits", {})
    limits = raw_limits if isinstance(raw_limits, dict) else {}
    updated_limits: dict[str, int] = {}
    for key, value in limits.items():
        normalized_key = str(key or "").strip()
        if not normalized_key or normalized_key == bot_id:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            updated_limits[normalized_key] = parsed

    if action == "set":
        try:
            hourly_limit = int(body.get("hourly_limit"))
        except (TypeError, ValueError):
            return jsonify({"error": "hourly_limit must be a positive integer."}), 400
        if hourly_limit <= 0:
            return jsonify({"error": "hourly_limit must be a positive integer."}), 400
        updated_limits[bot_id] = hourly_limit

    changed_by = getattr(current_user, "email", "api")
    mgr.set("token_governor_bot_hourly_limits", json.dumps(updated_limits, sort_keys=True), changed_by)
    return jsonify(
        {
            "status": "ok",
            "action": action,
            "bot_id": bot_id,
            "hourly_limit": updated_limits.get(bot_id),
            "token_governor_bot_hourly_limits": updated_limits,
        }
    )
