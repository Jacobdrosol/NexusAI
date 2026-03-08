"""Tasks blueprint — page + JSON API."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client
from dashboard.db import get_db
from dashboard.models import Task

logger = logging.getLogger(__name__)

bp = Blueprint("tasks", __name__)


def _parse_iso(raw: Any) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _task_sort_key(task: dict[str, Any]) -> tuple[str, str]:
    return (str(task.get("updated_at") or ""), str(task.get("created_at") or ""))


def _safe_cp_list_tasks(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


def _task_to_dict(t: Task) -> dict[str, Any]:
    """Serialise a Task ORM row to a plain dict."""
    return {
        "id": t.id,
        "bot_id": t.bot_id,
        "status": t.status,
        "payload": t.payload_as_dict(),
        "result": json.loads(t.result) if t.result else None,
        "error": json.loads(t.error) if t.error else None,
        "created_at": t.created_at.isoformat() if t.created_at else "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else "",
    }


@bp.get("/tasks")
@login_required
def tasks_page() -> str:
    """Render the tasks table page."""
    from dashboard.bot_launch import launchable_bots

    cp = get_cp_client()
    cp_data = _safe_cp_list_tasks(cp, limit=400)
    if cp_data is not None:
        now = datetime.now(timezone.utc)
        recent_cutoff = now - timedelta(hours=24)
        sorted_tasks = sorted(cp_data, key=_task_sort_key, reverse=True)
        running_tasks = [task for task in sorted_tasks if task.get("status") == "running"]
        queued_tasks = [task for task in sorted_tasks if task.get("status") in {"queued", "blocked"}]
        recent_completed = [
            task
            for task in sorted_tasks
            if task.get("status") == "completed" and (_parse_iso(task.get("updated_at")) or now) >= recent_cutoff
        ]
        recent_failed = [
            task
            for task in sorted_tasks
            if task.get("status") == "failed" and (_parse_iso(task.get("updated_at")) or now) >= recent_cutoff
        ]
        return render_template(
            "tasks.html",
            tasks=sorted_tasks,
            running_tasks=running_tasks,
            queued_tasks=queued_tasks,
            recent_completed_tasks=recent_completed,
            recent_failed_tasks=recent_failed,
            launchable_bots=launchable_bots(cp.list_bots() or [], surface="tasks"),
            error=None,
        )

    flash("Control plane unavailable — showing local data.", "warning")
    db = get_db()
    try:
        tasks = db.query(Task).order_by(Task.created_at.desc()).limit(100).all()
        task_rows = [_task_to_dict(t) for t in tasks]
        return render_template(
            "tasks.html",
            tasks=task_rows,
            running_tasks=[task for task in task_rows if task.get("status") == "running"],
            queued_tasks=[task for task in task_rows if task.get("status") in {"queued", "blocked"}],
            recent_completed_tasks=[task for task in task_rows if task.get("status") == "completed"],
            recent_failed_tasks=[task for task in task_rows if task.get("status") == "failed"],
            launchable_bots=[],
            error=None,
        )
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/tasks")
@login_required
def api_list_tasks():
    """List tasks with optional filters."""
    cp = get_cp_client()
    status = request.args.get("status")
    bot_id = request.args.get("bot_id")
    orchestration_id = request.args.get("orchestration_id")
    limit_str: Optional[str] = request.args.get("limit", "100")
    try:
        limit = min(int(limit_str), 500)
    except (ValueError, TypeError):
        limit = 100
    statuses = [part.strip() for part in str(status or "").split(",") if part.strip()]
    cp_tasks = _safe_cp_list_tasks(
        cp,
        orchestration_id=orchestration_id,
        statuses=statuses or None,
        bot_id=bot_id,
        limit=limit,
    )
    if cp_tasks is not None:
        return jsonify(cp_tasks)

    db = get_db()
    try:
        query = db.query(Task)
        if status:
            query = query.filter(Task.status == status)
        if bot_id:
            query = query.filter(Task.bot_id == int(bot_id))
        tasks = query.order_by(Task.created_at.desc()).limit(limit).all()
        return jsonify([_task_to_dict(t) for t in tasks])
    finally:
        db.close()


@bp.get("/api/tasks/<task_id>")
@login_required
def api_get_task(task_id: str):
    """Get a single task by ID."""
    cp = get_cp_client()
    cp_task = cp.get_task(task_id)
    if cp_task is not None:
        return jsonify(cp_task)

    db = get_db()
    try:
        task = db.get(Task, task_id)
        if not task:
            return jsonify({"error": "not found"}), 404
        return jsonify(_task_to_dict(task))
    finally:
        db.close()
