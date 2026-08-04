"""Build operator-facing work overview summaries from control-plane snapshots."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

ACTIVE_STATUSES = {"running"}
WAITING_STATUSES = {"queued", "blocked"}
PROBLEM_STATUSES = {"failed", "retried"}
QC_MARKERS = ("qc", "quality", "review", "tester", "validator", "auditor")
STALE_ACTIVE_SECONDS = 60 * 60
STALE_WAITING_SECONDS = 30 * 60


def _safe_metadata(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _execution_provenance(task: dict[str, Any]) -> dict[str, Any]:
    provenance = _safe_metadata(task).get("execution_provenance")
    return provenance if isinstance(provenance, dict) else {}


def _project_id_for_task(task: dict[str, Any]) -> str:
    project_id, _source = _project_scope_for_task(task)
    return project_id


def _project_scope_for_task(task: dict[str, Any]) -> tuple[str, str]:
    metadata = _safe_metadata(task)
    metadata_project_id = str(metadata.get("project_id") or "").strip()
    if metadata_project_id:
        return metadata_project_id, "metadata.project_id"
    task_project_id = str(task.get("project_id") or "").strip()
    if task_project_id:
        return task_project_id, "task.project_id"
    return "unassigned", "missing"


def _manager_id_for_task(task: dict[str, Any]) -> str:
    manager_id, _source = _manager_scope_for_task(task)
    return manager_id


def _manager_scope_for_task(task: dict[str, Any]) -> tuple[str, str]:
    metadata = _safe_metadata(task)
    for key in ("root_pm_bot_id", "pipeline_entry_bot_id", "manager_bot_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value, f"metadata.{key}"
    parent_task_id = str(metadata.get("parent_task_id") or "").strip()
    if parent_task_id:
        return f"parent:{parent_task_id[:12]}", "metadata.parent_task_id"
    bot_id = str(task.get("bot_id") or "").strip()
    if bot_id:
        return bot_id, "task.bot_id"
    return "unassigned-manager", "missing"


def project_id_for_task(task: dict[str, Any]) -> str:
    return _project_id_for_task(task)


def manager_id_for_task(task: dict[str, Any]) -> str:
    return _manager_id_for_task(task)


def _parse_datetime(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_seconds(since: datetime | None, now: datetime) -> int | None:
    if since is None:
        return None
    return max(0, int((now - since).total_seconds()))


def _age_label(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remainder = minutes % 60
    if hours < 24:
        return f"{hours}h {remainder}m" if remainder else f"{hours}h"
    days = hours // 24
    day_hours = hours % 24
    return f"{days}d {day_hours}h" if day_hours else f"{days}d"


def _freshness_summary() -> dict[str, Any]:
    return {
        "stale_active": 0,
        "stale_waiting": 0,
        "oldest_active_age_seconds": None,
        "oldest_active_label": "none",
        "oldest_waiting_age_seconds": None,
        "oldest_waiting_label": "none",
    }


def _metadata_health_summary() -> dict[str, Any]:
    return {
        "task_count": 0,
        "missing_project_count": 0,
        "inferred_manager_count": 0,
        "missing_manager_count": 0,
        "sample_tasks": [],
    }


def _route_evidence_summary() -> dict[str, Any]:
    return {
        "task_count": 0,
        "attributed_task_count": 0,
        "missing_worker_count": 0,
        "missing_active_problem_count": 0,
        "missing_waiting_count": 0,
        "by_worker": Counter(),
        "sample_tasks": [],
    }


def _record_metadata_gap(
    metadata_health: dict[str, Any],
    *,
    task: dict[str, Any],
    issue: str,
    project_id: str,
    manager_id: str,
    manager_source: str,
) -> None:
    samples = metadata_health["sample_tasks"]
    if len(samples) >= 12:
        return
    samples.append(
        {
            "id": str(task.get("id") or ""),
            "bot_id": str(task.get("bot_id") or ""),
            "status": str(task.get("status") or "unknown").strip().lower() or "unknown",
            "issue": issue,
            "project_id": project_id,
            "manager_id": manager_id,
            "manager_source": manager_source,
        }
    )


def _problem_label(task: dict[str, Any]) -> str:
    error_summary = task.get("error_summary") if isinstance(task.get("error_summary"), dict) else {}
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    code = str(error_summary.get("code") or error.get("code") or error.get("error_code") or "").strip()
    if code:
        return code
    error_type = str(error_summary.get("type") or task.get("error_type") or "").strip()
    if error_type:
        return error_type
    if bool(task.get("has_error")):
        return "error_without_summary"
    return str(task.get("status") or "problem").strip().lower() or "problem"


def _counter_rows(counter: Counter, key_name: str, limit: int = 8) -> list[dict[str, Any]]:
    return [{key_name: str(key), "count": int(count)} for key, count in counter.most_common(limit)]


def _status_group(status: str) -> str:
    if status in ACTIVE_STATUSES:
        return "active"
    if status in WAITING_STATUSES:
        return "waiting"
    if status in PROBLEM_STATUSES:
        return "problem"
    if status == "completed":
        return "completed"
    if status == "cancelled":
        return "cancelled"
    return "other"


def _record_age(
    freshness: dict[str, Any],
    *,
    status: str,
    active_age_seconds: int | None,
    waiting_age_seconds: int | None,
) -> None:
    if status in ACTIVE_STATUSES:
        if active_age_seconds is not None:
            current = freshness.get("oldest_active_age_seconds")
            if current is None or active_age_seconds > int(current):
                freshness["oldest_active_age_seconds"] = active_age_seconds
                freshness["oldest_active_label"] = _age_label(active_age_seconds)
        if active_age_seconds is None or active_age_seconds >= STALE_ACTIVE_SECONDS:
            freshness["stale_active"] += 1
    if status in WAITING_STATUSES:
        if waiting_age_seconds is not None:
            current = freshness.get("oldest_waiting_age_seconds")
            if current is None or waiting_age_seconds > int(current):
                freshness["oldest_waiting_age_seconds"] = waiting_age_seconds
                freshness["oldest_waiting_label"] = _age_label(waiting_age_seconds)
        if waiting_age_seconds is None or waiting_age_seconds >= STALE_WAITING_SECONDS:
            freshness["stale_waiting"] += 1


def _is_qc_task(task: dict[str, Any], bot_lookup: dict[str, dict[str, Any]]) -> bool:
    bot_id = str(task.get("bot_id") or "").strip()
    bot = bot_lookup.get(bot_id) or {}
    haystack = " ".join(
        str(value or "").lower()
        for value in (
            bot_id,
            task.get("status"),
            bot.get("name"),
            bot.get("role"),
            _safe_metadata(task).get("step_id"),
            _safe_metadata(task).get("source"),
        )
    )
    return any(marker in haystack for marker in QC_MARKERS)


def _project_name_map(projects: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("id") or "").strip()
        if project_id:
            names[project_id] = str(project.get("name") or project_id).strip() or project_id
    return names


def _bot_lookup(bots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for bot in bots:
        if not isinstance(bot, dict):
            continue
        bot_id = str(bot.get("id") or "").strip()
        if bot_id:
            lookup[bot_id] = bot
    return lookup


def _worker_row(worker: dict[str, Any]) -> dict[str, Any]:
    metrics = worker.get("metrics") if isinstance(worker.get("metrics"), dict) else {}
    try:
        queue_depth = int(metrics.get("queue_depth") or 0)
    except (TypeError, ValueError):
        queue_depth = 0
    load = metrics.get("load")
    try:
        load_value = float(load) if load is not None else None
    except (TypeError, ValueError):
        load_value = None
    return {
        "id": str(worker.get("id") or "").strip(),
        "name": str(worker.get("name") or worker.get("id") or "").strip(),
        "status": str(worker.get("status") or "unknown").strip().lower() or "unknown",
        "enabled": bool(worker.get("enabled", True)),
        "queue_depth": queue_depth,
        "load": load_value,
        "overloaded": load_value is not None and load_value >= 0.85,
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_holds(holds: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hold in holds or []:
        if not isinstance(hold, dict):
            continue
        project_id = str(hold.get("project_id") or "").strip()
        if not project_id:
            continue
        manager_id = str(hold.get("manager_id") or "").strip()
        hold_id = str(hold.get("id") or f"{project_id}::{manager_id or '*'}").strip()
        if hold_id in seen:
            continue
        seen.add(hold_id)
        rows.append(
            {
                "id": hold_id,
                "project_id": project_id,
                "manager_id": manager_id,
                "reason": str(hold.get("reason") or "").strip(),
                "created_at": str(hold.get("created_at") or "").strip(),
                "created_by": str(hold.get("created_by") or "").strip(),
                "queued_task_count": _safe_int(hold.get("queued_task_count")),
                "bot_count": _safe_int(hold.get("bot_count")),
            }
        )
    return rows


def _hold_for_scope(
    holds: list[dict[str, Any]],
    *,
    project_id: str,
    manager_id: str = "",
) -> dict[str, Any] | None:
    for hold in holds:
        if hold["project_id"] != project_id:
            continue
        hold_manager = str(hold.get("manager_id") or "").strip()
        if manager_id:
            if hold_manager == manager_id:
                return hold
            continue
        if not hold_manager:
            return hold
    return None


def _record_route_evidence(
    route_evidence: dict[str, Any],
    *,
    task: dict[str, Any],
    status: str,
    bot_id: str,
    worker_id: str,
    provenance: dict[str, Any],
) -> None:
    route_evidence["task_count"] += 1
    if worker_id:
        route_evidence["attributed_task_count"] += 1
        route_evidence["by_worker"][worker_id] += 1
    else:
        route_evidence["missing_worker_count"] += 1
        if status in ACTIVE_STATUSES | PROBLEM_STATUSES:
            route_evidence["missing_active_problem_count"] += 1
        elif status in WAITING_STATUSES:
            route_evidence["missing_waiting_count"] += 1
    if len(route_evidence["sample_tasks"]) < 6:
        route_evidence["sample_tasks"].append(
            {
                "id": str(task.get("id") or ""),
                "bot_id": bot_id,
                "status": status,
                "worker_id": worker_id,
                "backend_type": str(provenance.get("backend_type") or ""),
                "provider": str(provenance.get("provider") or ""),
                "model": str(provenance.get("model") or ""),
            }
        )


def _attention_lanes(project_summaries: list[dict[str, Any]], limit: int = 12) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for project in project_summaries:
        project_id = str(project.get("project_id") or "")
        project_name = str(project.get("project_name") or project_id)
        for manager in project.get("managers") or []:
            if not isinstance(manager, dict):
                continue
            totals = manager.get("totals") if isinstance(manager.get("totals"), dict) else {}
            freshness = manager.get("freshness") if isinstance(manager.get("freshness"), dict) else {}
            route = manager.get("route_evidence") if isinstance(manager.get("route_evidence"), dict) else {}
            problem = _safe_int(totals.get("problem"))
            stale = _safe_int(freshness.get("stale_active")) + _safe_int(freshness.get("stale_waiting"))
            route_gaps = _safe_int(route.get("missing_active_problem_count"))
            held = bool(manager.get("held"))
            active = _safe_int(totals.get("active"))
            waiting = _safe_int(totals.get("waiting"))
            reasons: list[str] = []
            if problem:
                reasons.append(f"{problem} problem")
            if stale:
                reasons.append(f"{stale} stale")
            if route_gaps:
                reasons.append(f"{route_gaps} route gap")
            if held:
                reasons.append("held")
            if not reasons:
                continue
            score = (problem * 100) + (stale * 25) + (route_gaps * 15) + (10 if held else 0) + active + waiting
            rows.append(
                {
                    "project_id": project_id,
                    "project_name": project_name,
                    "manager_id": str(manager.get("manager_id") or ""),
                    "manager_name": str(manager.get("manager_name") or manager.get("manager_id") or ""),
                    "active": active,
                    "waiting": waiting,
                    "problem": problem,
                    "stale": stale,
                    "route_gaps": route_gaps,
                    "held": held,
                    "oldest_active_label": str(freshness.get("oldest_active_label") or "none"),
                    "oldest_waiting_label": str(freshness.get("oldest_waiting_label") or "none"),
                    "reasons": reasons,
                    "score": score,
                }
            )
    rows.sort(
        key=lambda row: (
            int(row.get("score", 0)),
            int(row.get("problem", 0)),
            int(row.get("stale", 0)),
            int(row.get("route_gaps", 0)),
            int(row.get("active", 0)) + int(row.get("waiting", 0)),
        ),
        reverse=True,
    )
    return rows[:limit]


def build_work_overview(
    *,
    tasks: list[dict[str, Any]] | None,
    projects: list[dict[str, Any]] | None,
    bots: list[dict[str, Any]] | None,
    workers: list[dict[str, Any]] | None,
    holds: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    task_rows = [task for task in (tasks or []) if isinstance(task, dict)]
    project_rows = [project for project in (projects or []) if isinstance(project, dict)]
    bot_rows = [bot for bot in (bots or []) if isinstance(bot, dict)]
    worker_rows = [_worker_row(worker) for worker in (workers or []) if isinstance(worker, dict)]
    hold_rows = _normalize_holds(holds)
    project_names = _project_name_map(project_rows)
    bot_lookup = _bot_lookup(bot_rows)

    totals = Counter()
    projects_by_id: dict[str, dict[str, Any]] = {}
    manager_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    recent_problem_tasks: list[dict[str, Any]] = []
    problem_codes = Counter()
    problem_sources = Counter()
    problem_bots = Counter()
    orchestrations: dict[str, dict[str, Any]] = {}
    freshness = _freshness_summary()
    metadata_health = _metadata_health_summary()
    route_evidence = _route_evidence_summary()

    for task in task_rows:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        project_id, project_source = _project_scope_for_task(task)
        manager_id, manager_source = _manager_scope_for_task(task)
        bot_id = str(task.get("bot_id") or "").strip()
        orchestration_id = str(_safe_metadata(task).get("orchestration_id") or "").strip()
        is_qc = _is_qc_task(task, bot_lookup)
        created_at = _parse_datetime(task.get("created_at"))
        updated_at = _parse_datetime(task.get("updated_at")) or created_at
        active_age_seconds = _age_seconds(updated_at, now_utc)
        waiting_age_seconds = _age_seconds(created_at or updated_at, now_utc)
        metadata_health["task_count"] += 1
        if project_source == "missing":
            metadata_health["missing_project_count"] += 1
            _record_metadata_gap(
                metadata_health,
                task=task,
                issue="missing_project",
                project_id=project_id,
                manager_id=manager_id,
                manager_source=manager_source,
            )
        if manager_source in {"metadata.parent_task_id", "task.bot_id", "missing"}:
            metadata_health["inferred_manager_count"] += 1
            if manager_source == "missing":
                metadata_health["missing_manager_count"] += 1
            _record_metadata_gap(
                metadata_health,
                task=task,
                issue="inferred_manager",
                project_id=project_id,
                manager_id=manager_id,
                manager_source=manager_source,
            )
        totals["total"] += 1
        totals[status] += 1
        if status in ACTIVE_STATUSES:
            totals["active"] += 1
        if status in WAITING_STATUSES:
            totals["waiting"] += 1
        if status in PROBLEM_STATUSES:
            totals["problem"] += 1
            problem_codes[_problem_label(task)] += 1
            problem_sources[str(_safe_metadata(task).get("source") or "unknown").strip() or "unknown"] += 1
            problem_bots[bot_id or "unknown"] += 1
        if is_qc:
            totals["qc"] += 1
        _record_age(
            freshness,
            status=status,
            active_age_seconds=active_age_seconds,
            waiting_age_seconds=waiting_age_seconds,
        )
        if orchestration_id:
            orchestration = orchestrations.setdefault(
                orchestration_id,
                {
                    "orchestration_id": orchestration_id,
                    "project_id": project_id,
                    "manager_id": manager_id,
                    "task_count": 0,
                    "status_counts": Counter(),
                    "stale_active": 0,
                    "stale_waiting": 0,
                    "problem_count": 0,
                    "latest_updated_at": "",
                    "latest_task_id": "",
                    "latest_status": "",
                },
            )
            orchestration["task_count"] += 1
            orchestration["status_counts"][status] += 1
            if status in PROBLEM_STATUSES:
                orchestration["problem_count"] += 1
            if status in ACTIVE_STATUSES and (active_age_seconds is None or active_age_seconds >= STALE_ACTIVE_SECONDS):
                orchestration["stale_active"] += 1
            if status in WAITING_STATUSES and (waiting_age_seconds is None or waiting_age_seconds >= STALE_WAITING_SECONDS):
                orchestration["stale_waiting"] += 1
            latest_updated_at = str(task.get("updated_at") or "")
            if latest_updated_at >= str(orchestration.get("latest_updated_at") or ""):
                orchestration["latest_updated_at"] = latest_updated_at
                orchestration["latest_task_id"] = str(task.get("id") or "")
                orchestration["latest_status"] = status

        project_bucket = projects_by_id.setdefault(
            project_id,
            {
                "project_id": project_id,
                "project_name": project_names.get(project_id, project_id if project_id != "unassigned" else "Unassigned"),
                "totals": Counter(),
                "freshness": _freshness_summary(),
                "hold": None,
                "held": False,
                "managers": [],
            },
        )
        project_bucket["totals"]["total"] += 1
        project_bucket["totals"][status] += 1
        if status in ACTIVE_STATUSES:
            project_bucket["totals"]["active"] += 1
        if status in WAITING_STATUSES:
            project_bucket["totals"]["waiting"] += 1
        if status in PROBLEM_STATUSES:
            project_bucket["totals"]["problem"] += 1
        if is_qc:
            project_bucket["totals"]["qc"] += 1
        _record_age(
            project_bucket["freshness"],
            status=status,
            active_age_seconds=active_age_seconds,
            waiting_age_seconds=waiting_age_seconds,
        )

        manager_key = (project_id, manager_id)
        manager_bucket = manager_buckets.setdefault(
            manager_key,
            {
                "project_id": project_id,
                "manager_id": manager_id,
                "manager_name": str((bot_lookup.get(manager_id) or {}).get("name") or manager_id),
                "totals": Counter(),
                "freshness": _freshness_summary(),
                "hold": None,
                "held": False,
                "bots": Counter(),
                "route_evidence": _route_evidence_summary(),
                "latest_tasks": [],
            },
        )
        manager_bucket["totals"]["total"] += 1
        manager_bucket["totals"][status] += 1
        if status in ACTIVE_STATUSES:
            manager_bucket["totals"]["active"] += 1
        if status in WAITING_STATUSES:
            manager_bucket["totals"]["waiting"] += 1
        if status in PROBLEM_STATUSES:
            manager_bucket["totals"]["problem"] += 1
        if is_qc:
            manager_bucket["totals"]["qc"] += 1
        _record_age(
            manager_bucket["freshness"],
            status=status,
            active_age_seconds=active_age_seconds,
            waiting_age_seconds=waiting_age_seconds,
        )
        if bot_id:
            manager_bucket["bots"][bot_id] += 1
        if status in ACTIVE_STATUSES | WAITING_STATUSES | PROBLEM_STATUSES:
            metadata = _safe_metadata(task)
            provenance = _execution_provenance(task)
            worker_id = str(provenance.get("worker_id") or "").strip()
            _record_route_evidence(
                route_evidence,
                task=task,
                status=status,
                bot_id=bot_id,
                worker_id=worker_id,
                provenance=provenance,
            )
            _record_route_evidence(
                manager_bucket["route_evidence"],
                task=task,
                status=status,
                bot_id=bot_id,
                worker_id=worker_id,
                provenance=provenance,
            )
            compact_task = {
                "id": str(task.get("id") or ""),
                "bot_id": bot_id,
                "status": status,
                "updated_at": str(task.get("updated_at") or ""),
                "age_label": _age_label(waiting_age_seconds if status in WAITING_STATUSES else active_age_seconds),
                "orchestration_id": str(metadata.get("orchestration_id") or ""),
                "step_id": str(metadata.get("step_id") or ""),
                "source": str(metadata.get("source") or ""),
                "worker_id": worker_id,
            }
            manager_bucket["latest_tasks"].append(compact_task)
            if status in PROBLEM_STATUSES:
                recent_problem_tasks.append(
                    compact_task
                    | {
                        "project_id": project_id,
                        "manager_id": manager_id,
                        "problem_label": _problem_label(task),
                    }
                )

    for hold in hold_rows:
        hold_project_id = hold["project_id"]
        project_bucket = projects_by_id.setdefault(
            hold_project_id,
            {
                "project_id": hold_project_id,
                "project_name": project_names.get(hold_project_id, hold_project_id),
                "totals": Counter(),
                "freshness": _freshness_summary(),
                "hold": None,
                "held": False,
                "managers": [],
            },
        )
        hold_manager_id = str(hold.get("manager_id") or "").strip()
        if hold_manager_id:
            manager_buckets.setdefault(
                (hold_project_id, hold_manager_id),
                {
                    "project_id": hold_project_id,
                    "manager_id": hold_manager_id,
                    "manager_name": str((bot_lookup.get(hold_manager_id) or {}).get("name") or hold_manager_id),
                    "totals": Counter(),
                    "freshness": _freshness_summary(),
                    "hold": None,
                    "held": False,
                    "bots": Counter(),
                    "route_evidence": _route_evidence_summary(),
                    "latest_tasks": [],
                },
            )

    for bucket in manager_buckets.values():
        project_hold = _hold_for_scope(hold_rows, project_id=bucket["project_id"])
        manager_hold = _hold_for_scope(
            hold_rows,
            project_id=bucket["project_id"],
            manager_id=bucket["manager_id"],
        )
        bucket["hold"] = manager_hold or project_hold
        bucket["held"] = bucket["hold"] is not None
        bucket["totals"] = dict(bucket["totals"])
        bucket["bots"] = [
            {"bot_id": bot_id, "task_count": count}
            for bot_id, count in bucket["bots"].most_common(8)
        ]
        manager_route_evidence = bucket["route_evidence"]
        manager_route_evidence["by_worker"] = [
            {"worker_id": worker_id, "task_count": count}
            for worker_id, count in manager_route_evidence["by_worker"].most_common(6)
        ]
        bucket["latest_tasks"] = sorted(
            bucket["latest_tasks"],
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:8]
        projects_by_id[bucket["project_id"]]["managers"].append(bucket)

    project_summaries = []
    for bucket in projects_by_id.values():
        bucket["hold"] = _hold_for_scope(hold_rows, project_id=bucket["project_id"])
        bucket["held"] = bucket["hold"] is not None
        bucket["totals"] = dict(bucket["totals"])
        bucket["managers"] = sorted(
            bucket["managers"],
            key=lambda item: (
                int(item["totals"].get("active", 0)) + int(item["totals"].get("waiting", 0)),
                int(item["totals"].get("problem", 0)),
                int(item["totals"].get("total", 0)),
            ),
            reverse=True,
        )
        project_summaries.append(bucket)

    project_summaries.sort(
        key=lambda item: (
            int(item["totals"].get("active", 0)) + int(item["totals"].get("waiting", 0)),
            int(item["totals"].get("problem", 0)),
            int(item["totals"].get("total", 0)),
            str(item["project_name"]).lower(),
        ),
        reverse=True,
    )
    worker_summary = {
        "total": len(worker_rows),
        "online": sum(1 for worker in worker_rows if worker["status"] == "online" and worker["enabled"]),
        "disabled": sum(1 for worker in worker_rows if not worker["enabled"]),
        "offline_enabled": sum(1 for worker in worker_rows if worker["enabled"] and worker["status"] != "online"),
        "overloaded": sum(1 for worker in worker_rows if worker["enabled"] and worker["overloaded"]),
        "queued_workers": sum(1 for worker in worker_rows if worker["queue_depth"] > 0),
        "queue_depth": sum(worker["queue_depth"] for worker in worker_rows),
        "workers": sorted(worker_rows, key=lambda worker: (worker["queue_depth"], worker["id"]), reverse=True),
    }
    worker_summary["issue_count"] = (
        worker_summary["disabled"] + worker_summary["offline_enabled"] + worker_summary["overloaded"]
    )
    route_evidence["by_worker"] = [
        {"worker_id": worker_id, "task_count": count}
        for worker_id, count in route_evidence["by_worker"].most_common(8)
    ]
    totals_out = dict(totals)
    totals_out["stale_active"] = freshness["stale_active"]
    totals_out["stale_waiting"] = freshness["stale_waiting"]
    orchestration_rows = []
    for row in orchestrations.values():
        status_counts = dict(row["status_counts"])
        row_out = {
            **row,
            "status_counts": status_counts,
            "active": sum(int(status_counts.get(status, 0)) for status in ACTIVE_STATUSES),
            "waiting": sum(int(status_counts.get(status, 0)) for status in WAITING_STATUSES),
            "completed": int(status_counts.get("completed", 0)),
            "cancelled": int(status_counts.get("cancelled", 0)),
        }
        row_out["state"] = _status_group(str(row.get("latest_status") or ""))
        orchestration_rows.append(row_out)
    orchestration_rows.sort(
        key=lambda row: (
            int(row.get("active", 0)) + int(row.get("waiting", 0)),
            int(row.get("problem_count", 0)),
            str(row.get("latest_updated_at") or ""),
        ),
        reverse=True,
    )
    return {
        "totals": totals_out,
        "freshness": freshness,
        "projects": project_summaries,
        "workers": worker_summary,
        "holds": hold_rows,
        "metadata_health": metadata_health,
        "route_evidence": route_evidence,
        "attention_lanes": _attention_lanes(project_summaries),
        "problem_summary": {
            "total": int(sum(problem_codes.values())),
            "by_code": _counter_rows(problem_codes, "code"),
            "by_source": _counter_rows(problem_sources, "source"),
            "by_bot": _counter_rows(problem_bots, "bot_id"),
        },
        "orchestrations": orchestration_rows[:20],
        "recent_problem_tasks": sorted(
            recent_problem_tasks,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:12],
    }
