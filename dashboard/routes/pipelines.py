from __future__ import annotations

from collections import Counter
from typing import Any

from flask import Blueprint, jsonify, render_template
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("pipelines", __name__)


def _cp_list_tasks_safe(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


def _task_sort_key(task: dict[str, Any]) -> tuple[str, str]:
    return (str(task.get("created_at") or ""), str(task.get("updated_at") or ""))


def _status_summary(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(task.get("status") or "unknown") for task in tasks)
    return {
        "queued": counts.get("queued", 0),
        "blocked": counts.get("blocked", 0),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "retried": counts.get("retried", 0),
        "cancelled": counts.get("cancelled", 0),
    }


def _pipeline_status(tasks: list[dict[str, Any]]) -> str:
    summary = _status_summary(tasks)
    if summary["running"] or summary["queued"] or summary["blocked"]:
        return "running"
    if summary["failed"]:
        return "failed"
    if summary["cancelled"] and not summary["completed"] and not summary["retried"]:
        return "cancelled"
    if summary["completed"]:
        return "completed"
    if summary["retried"]:
        return "retried"
    return "unknown"


def _usage_totals(tasks: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for task in tasks:
        usage = task.get("usage")
        if not isinstance(usage, dict):
            usage = ((task.get("result") or {}).get("usage") if isinstance(task.get("result"), dict) else None) or {}
        for key in totals:
            try:
                totals[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return totals


def _root_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in sorted(tasks, key=_task_sort_key):
        meta = task.get("metadata") or {}
        if str(meta.get("workflow_root_task_id") or "") == str(task.get("id") or ""):
            return task
    return sorted(tasks, key=_task_sort_key)[0] if tasks else None


def _pipeline_groups(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        meta = task.get("metadata") or {}
        orchestration_id = str(meta.get("orchestration_id") or "").strip()
        if not orchestration_id:
            continue
        groups.setdefault(orchestration_id, []).append(task)

    rows: list[dict[str, Any]] = []
    for orchestration_id, items in groups.items():
        root = _root_task(items)
        if not root:
            continue
        root_meta = root.get("metadata") or {}
        if str(root_meta.get("source") or "") != "saved_launch_pipeline" and not str(root_meta.get("pipeline_name") or "").strip():
            continue
        items_sorted = sorted(items, key=_task_sort_key)
        rows.append(
            {
                "id": orchestration_id,
                "name": str(root_meta.get("pipeline_name") or root.get("bot_id") or orchestration_id),
                "entry_bot_id": str(root_meta.get("pipeline_entry_bot_id") or root.get("bot_id") or ""),
                "root_task_id": str(root.get("id") or ""),
                "created_at": str(items_sorted[0].get("created_at") or ""),
                "updated_at": str(items_sorted[-1].get("updated_at") or items_sorted[-1].get("created_at") or ""),
                "task_count": len(items_sorted),
                "bot_count": len({str(task.get("bot_id") or "") for task in items_sorted}),
                "status": _pipeline_status(items_sorted),
                "status_summary": _status_summary(items_sorted),
                "usage": _usage_totals(items_sorted),
            }
        )
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("created_at") or "")), reverse=True)
    return rows


def _pipeline_detail(cp, orchestration_id: str) -> dict[str, Any] | None:
    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, limit=1000, include_content=False) or []
    if not tasks:
        return None
    tasks = sorted(tasks, key=_task_sort_key)
    root = _root_task(tasks)
    root_meta = (root or {}).get("metadata") or {}

    task_ids = {str(task.get("id") or "") for task in tasks}
    artifacts: list[dict[str, Any]] = []
    for bot_id in sorted({str(task.get("bot_id") or "") for task in tasks if str(task.get("bot_id") or "").strip()}):
        rows = cp.list_bot_artifacts(bot_id, limit=1000, include_content=False) or []
        artifacts.extend(row for row in rows if str(row.get("task_id") or "") in task_ids)
    artifacts.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("task_id") or "")), reverse=True)

    return {
        "id": orchestration_id,
        "name": str(root_meta.get("pipeline_name") or (root or {}).get("bot_id") or orchestration_id),
        "entry_bot_id": str(root_meta.get("pipeline_entry_bot_id") or (root or {}).get("bot_id") or ""),
        "root_task_id": str((root or {}).get("id") or ""),
        "status": _pipeline_status(tasks),
        "status_summary": _status_summary(tasks),
        "usage": _usage_totals(tasks),
        "tasks": tasks,
        "artifacts": artifacts,
    }


@bp.get("/pipelines")
@login_required
def pipelines_page() -> str:
    cp = get_cp_client()
    cp_tasks = _cp_list_tasks_safe(cp, limit=1000, include_content=False)
    if cp_tasks is None:
        return render_template("pipelines.html", pipelines=[], error="Control plane unavailable", active_page="pipelines")
    return render_template("pipelines.html", pipelines=_pipeline_groups(cp_tasks), error=None, active_page="pipelines")


@bp.get("/pipelines/<orchestration_id>")
@login_required
def pipeline_detail_page(orchestration_id: str) -> str:
    cp = get_cp_client()
    detail = _pipeline_detail(cp, orchestration_id)
    if detail is None:
        return render_template("pipeline_detail.html", pipeline=None, error="Pipeline not found", active_page="pipelines")
    return render_template("pipeline_detail.html", pipeline=detail, error=None, active_page="pipelines")


@bp.get("/api/pipelines")
@login_required
def api_list_pipelines():
    cp = get_cp_client()
    cp_tasks = _cp_list_tasks_safe(cp, limit=1000, include_content=False)
    if cp_tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(_pipeline_groups(cp_tasks))


@bp.get("/api/pipelines/<orchestration_id>")
@login_required
def api_get_pipeline(orchestration_id: str):
    cp = get_cp_client()
    detail = _pipeline_detail(cp, orchestration_id)
    if detail is None:
        return jsonify({"error": "pipeline not found"}), 404
    return jsonify(detail)
