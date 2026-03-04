"""Tasks blueprint — page + JSON API."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Task

logger = logging.getLogger(__name__)

bp = Blueprint("tasks", __name__)


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
    from dashboard.cp_client import get_cp_client

    cp_data = get_cp_client().list_tasks()
    if cp_data is not None:
        return render_template("tasks.html", tasks=cp_data, error=None)

    flash("Control plane unavailable — showing local data.", "warning")
    db = get_db()
    try:
        tasks = db.query(Task).order_by(Task.created_at.desc()).limit(100).all()
        return render_template(
            "tasks.html",
            tasks=[_task_to_dict(t) for t in tasks],
            error=None,
        )
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/tasks")
@login_required
def api_list_tasks():
    """List tasks with optional filters."""
    db = get_db()
    try:
        query = db.query(Task)
        status: Optional[str] = request.args.get("status")
        bot_id: Optional[str] = request.args.get("bot_id")
        limit_str: Optional[str] = request.args.get("limit", "100")
        if status:
            query = query.filter(Task.status == status)
        if bot_id:
            query = query.filter(Task.bot_id == int(bot_id))
        try:
            limit = min(int(limit_str), 500)
        except (ValueError, TypeError):
            limit = 100
        tasks = query.order_by(Task.created_at.desc()).limit(limit).all()
        return jsonify([_task_to_dict(t) for t in tasks])
    finally:
        db.close()


@bp.get("/api/tasks/<int:task_id>")
@login_required
def api_get_task(task_id: int):
    """Get a single task by ID."""
    db = get_db()
    try:
        task = db.get(Task, task_id)
        if not task:
            return jsonify({"error": "not found"}), 404
        return jsonify(_task_to_dict(task))
    finally:
        db.close()
