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


def _project_id_for_task(task: dict[str, Any]) -> str:
    metadata = _safe_metadata(task)
    project_id = str(metadata.get("project_id") or task.get("project_id") or "").strip()
    return project_id or "unassigned"


def _manager_id_for_task(task: dict[str, Any]) -> str:
    metadata = _safe_metadata(task)
    for key in ("root_pm_bot_id", "pipeline_entry_bot_id", "manager_bot_id"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    parent_task_id = str(metadata.get("parent_task_id") or "").strip()
    if parent_task_id:
        return f"parent:{parent_task_id[:12]}"
    bot_id = str(task.get("bot_id") or "").strip()
    return bot_id or "unassigned-manager"


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
    }


def build_work_overview(
    *,
    tasks: list[dict[str, Any]] | None,
    projects: list[dict[str, Any]] | None,
    bots: list[dict[str, Any]] | None,
    workers: list[dict[str, Any]] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    task_rows = [task for task in (tasks or []) if isinstance(task, dict)]
    project_rows = [project for project in (projects or []) if isinstance(project, dict)]
    bot_rows = [bot for bot in (bots or []) if isinstance(bot, dict)]
    worker_rows = [_worker_row(worker) for worker in (workers or []) if isinstance(worker, dict)]
    project_names = _project_name_map(project_rows)
    bot_lookup = _bot_lookup(bot_rows)

    totals = Counter()
    projects_by_id: dict[str, dict[str, Any]] = {}
    manager_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    recent_problem_tasks: list[dict[str, Any]] = []
    freshness = _freshness_summary()

    for task in task_rows:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        project_id = _project_id_for_task(task)
        manager_id = _manager_id_for_task(task)
        bot_id = str(task.get("bot_id") or "").strip()
        is_qc = _is_qc_task(task, bot_lookup)
        created_at = _parse_datetime(task.get("created_at"))
        updated_at = _parse_datetime(task.get("updated_at")) or created_at
        active_age_seconds = _age_seconds(updated_at, now_utc)
        waiting_age_seconds = _age_seconds(created_at or updated_at, now_utc)
        totals["total"] += 1
        totals[status] += 1
        if status in ACTIVE_STATUSES:
            totals["active"] += 1
        if status in WAITING_STATUSES:
            totals["waiting"] += 1
        if status in PROBLEM_STATUSES:
            totals["problem"] += 1
        if is_qc:
            totals["qc"] += 1
        _record_age(
            freshness,
            status=status,
            active_age_seconds=active_age_seconds,
            waiting_age_seconds=waiting_age_seconds,
        )

        project_bucket = projects_by_id.setdefault(
            project_id,
            {
                "project_id": project_id,
                "project_name": project_names.get(project_id, project_id if project_id != "unassigned" else "Unassigned"),
                "totals": Counter(),
                "freshness": _freshness_summary(),
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
                "bots": Counter(),
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
            compact_task = {
                "id": str(task.get("id") or ""),
                "bot_id": bot_id,
                "status": status,
                "updated_at": str(task.get("updated_at") or ""),
                "age_label": _age_label(waiting_age_seconds if status in WAITING_STATUSES else active_age_seconds),
                "orchestration_id": str(_safe_metadata(task).get("orchestration_id") or ""),
            }
            manager_bucket["latest_tasks"].append(compact_task)
            if status in PROBLEM_STATUSES:
                recent_problem_tasks.append(compact_task | {"project_id": project_id, "manager_id": manager_id})

    for bucket in manager_buckets.values():
        bucket["totals"] = dict(bucket["totals"])
        bucket["bots"] = [
            {"bot_id": bot_id, "task_count": count}
            for bot_id, count in bucket["bots"].most_common(8)
        ]
        bucket["latest_tasks"] = sorted(
            bucket["latest_tasks"],
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:8]
        projects_by_id[bucket["project_id"]]["managers"].append(bucket)

    project_summaries = []
    for bucket in projects_by_id.values():
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
        "queue_depth": sum(worker["queue_depth"] for worker in worker_rows),
        "workers": sorted(worker_rows, key=lambda worker: (worker["queue_depth"], worker["id"]), reverse=True),
    }
    totals_out = dict(totals)
    totals_out["stale_active"] = freshness["stale_active"]
    totals_out["stale_waiting"] = freshness["stale_waiting"]
    return {
        "totals": totals_out,
        "freshness": freshness,
        "projects": project_summaries,
        "workers": worker_summary,
        "recent_problem_tasks": sorted(
            recent_problem_tasks,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:12],
    }
