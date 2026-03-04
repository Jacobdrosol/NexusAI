"""Workers blueprint — page + JSON API."""
from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Worker

bp = Blueprint("workers", __name__)


def _worker_to_dict(w: Worker) -> dict[str, Any]:
    """Serialise a Worker ORM row to a plain dict."""
    return {
        "id": w.id,
        "name": w.name,
        "host": w.host,
        "port": w.port,
        "status": w.status,
        "enabled": w.enabled,
        "capabilities": w.capabilities_as_dict(),
        "metrics": w.metrics_as_dict(),
    }


@bp.get("/workers")
@login_required
def workers_page() -> str:
    """Render the workers table page."""
    db = get_db()
    try:
        workers = db.query(Worker).all()
        return render_template(
            "workers.html",
            workers=[_worker_to_dict(w) for w in workers],
            error=None,
        )
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/workers")
@login_required
def api_list_workers():
    """List all workers as JSON."""
    db = get_db()
    try:
        workers = db.query(Worker).all()
        return jsonify([_worker_to_dict(w) for w in workers])
    finally:
        db.close()


@bp.post("/api/workers")
@login_required
def api_create_worker():
    """Create a new worker."""
    data: dict[str, Any] = request.get_json(force=True) or {}
    if not data.get("name") or not data.get("host"):
        return jsonify({"error": "name and host are required"}), 400
    db = get_db()
    try:
        worker = Worker(
            name=data["name"],
            host=data["host"],
            port=int(data.get("port", 8001)),
            status=data.get("status", "offline"),
            capabilities=json.dumps(data.get("capabilities", [])),
            metrics=json.dumps(data.get("metrics", {})),
            enabled=bool(data.get("enabled", True)),
        )
        db.add(worker)
        db.commit()
        db.refresh(worker)
        return jsonify(_worker_to_dict(worker)), 201
    finally:
        db.close()


@bp.get("/api/workers/<int:worker_id>")
@login_required
def api_get_worker(worker_id: int):
    """Get a single worker by ID."""
    db = get_db()
    try:
        worker = db.get(Worker, worker_id)
        if not worker:
            return jsonify({"error": "not found"}), 404
        return jsonify(_worker_to_dict(worker))
    finally:
        db.close()


@bp.put("/api/workers/<int:worker_id>")
@login_required
def api_update_worker(worker_id: int):
    """Update an existing worker."""
    db = get_db()
    try:
        worker = db.get(Worker, worker_id)
        if not worker:
            return jsonify({"error": "not found"}), 404
        data: dict[str, Any] = request.get_json(force=True) or {}
        for field in ("name", "host", "status"):
            if field in data:
                setattr(worker, field, data[field])
        if "port" in data:
            worker.port = int(data["port"])
        if "enabled" in data:
            worker.enabled = bool(data["enabled"])
        if "capabilities" in data:
            worker.capabilities = json.dumps(data["capabilities"])
        if "metrics" in data:
            worker.metrics = json.dumps(data["metrics"])
        db.commit()
        db.refresh(worker)
        return jsonify(_worker_to_dict(worker))
    finally:
        db.close()


@bp.delete("/api/workers/<int:worker_id>")
@login_required
def api_delete_worker(worker_id: int):
    """Delete a worker."""
    db = get_db()
    try:
        worker = db.get(Worker, worker_id)
        if not worker:
            return jsonify({"error": "not found"}), 404
        db.delete(worker)
        db.commit()
        return "", 204
    finally:
        db.close()
