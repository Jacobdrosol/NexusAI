"""Build operator-facing work overview summaries from control-plane snapshots."""
from __future__ import annotations

from collections import Counter
from typing import Any

ACTIVE_STATUSES = {"running"}
WAITING_STATUSES = {"queued", "blocked"}
PROBLEM_STATUSES = {"failed", "retried"}
QC_MARKERS = ("qc", "quality", "review", "tester", "validator", "auditor")


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
) -> dict[str, Any]:
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

    for task in task_rows:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        project_id = _project_id_for_task(task)
        manager_id = _manager_id_for_task(task)
        bot_id = str(task.get("bot_id") or "").strip()
        is_qc = _is_qc_task(task, bot_lookup)
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

        project_bucket = projects_by_id.setdefault(
            project_id,
            {
                "project_id": project_id,
                "project_name": project_names.get(project_id, project_id if project_id != "unassigned" else "Unassigned"),
                "totals": Counter(),
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

        manager_key = (project_id, manager_id)
        manager_bucket = manager_buckets.setdefault(
            manager_key,
            {
                "project_id": project_id,
                "manager_id": manager_id,
                "manager_name": str((bot_lookup.get(manager_id) or {}).get("name") or manager_id),
                "totals": Counter(),
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
        if bot_id:
            manager_bucket["bots"][bot_id] += 1
        if status in ACTIVE_STATUSES | WAITING_STATUSES | PROBLEM_STATUSES:
            compact_task = {
                "id": str(task.get("id") or ""),
                "bot_id": bot_id,
                "status": status,
                "updated_at": str(task.get("updated_at") or ""),
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
    return {
        "totals": dict(totals),
        "projects": project_summaries,
        "workers": worker_summary,
        "recent_problem_tasks": sorted(
            recent_problem_tasks,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )[:12],
    }
