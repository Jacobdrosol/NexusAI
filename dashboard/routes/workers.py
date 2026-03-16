"""Workers blueprint — page + JSON API."""
from __future__ import annotations

import json
import logging
from typing import Any

import requests
from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Task, Worker

logger = logging.getLogger(__name__)

bp = Blueprint("workers", __name__)


def _cp_list_tasks_safe(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


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


def _worker_base_url(worker: dict[str, Any]) -> str:
    host = str(worker.get("host") or "").strip()
    port = int(worker.get("port") or 0)
    if not host or not port:
        raise ValueError("worker host/port unavailable")
    return f"http://{host}:{port}"


@bp.get("/workers")
@login_required
def workers_page() -> str:
    """Render the workers table page."""
    from dashboard.cp_client import get_cp_client

    cp_data = get_cp_client().list_workers()
    if cp_data is not None:
        return render_template("workers.html", workers=cp_data, error=None)

    flash(get_cp_client().unavailable_reason(), "warning")
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


@bp.get("/workers/<worker_id>")
@login_required
def worker_detail_page(worker_id: str):
    """Render worker detail with capabilities, metrics, and basic actions."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    worker = cp.get_worker(worker_id)
    running_tasks = _cp_list_tasks_safe(cp, statuses=["running"], limit=200, include_content=False) or []
    running_tasks = [t for t in running_tasks if t.get("status") == "running"]
    if worker is not None:
        return render_template("worker_detail.html", worker=worker, running_tasks=running_tasks, error=None)

    flash(get_cp_client().unavailable_reason(), "warning")
    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return render_template("worker_detail.html", worker=None, running_tasks=[], error="Worker not found")
        local = db.get(Worker, int(worker_id))
        if not local:
            return render_template("worker_detail.html", worker=None, running_tasks=[], error="Worker not found")
        return render_template(
            "worker_detail.html",
            worker=_worker_to_dict(local),
            running_tasks=[],
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


@bp.get("/api/workers/<worker_id>")
@login_required
def api_get_worker(worker_id: str):
    """Get a single worker by ID."""
    from dashboard.cp_client import get_cp_client
    cp_worker = get_cp_client().get_worker(worker_id)
    if cp_worker is not None:
        return jsonify(cp_worker)
    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        worker = db.get(Worker, worker_id)
        if not worker:
            return jsonify({"error": "not found"}), 404
        return jsonify(_worker_to_dict(worker))
    finally:
        db.close()


@bp.put("/api/workers/<worker_id>")
@login_required
def api_update_worker(worker_id: str):
    """Update an existing worker."""
    from dashboard.cp_client import get_cp_client
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    cp_worker = cp.get_worker(worker_id)
    if cp_worker is not None:
        merged = dict(cp_worker)
        merged.update(data)
        updated = cp.update_worker(worker_id, merged)
        if updated is None:
            return jsonify({"error": "control plane unavailable"}), 502
        return jsonify(updated)

    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        worker_id_int = int(worker_id)
        worker = db.get(Worker, worker_id_int)
        if not worker:
            return jsonify({"error": "not found"}), 404
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


@bp.delete("/api/workers/<worker_id>")
@login_required
def api_delete_worker(worker_id: str):
    """Delete a worker."""
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    cp_worker = cp.get_worker(worker_id)
    if cp_worker is not None:
        ok = cp.delete_worker(worker_id)
        if not ok:
            return jsonify({"error": "delete failed"}), 502
        return "", 204

    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        worker = db.get(Worker, int(worker_id))
        if not worker:
            return jsonify({"error": "not found"}), 404
        db.delete(worker)
        db.commit()
        return "", 204
    finally:
        db.close()


@bp.post("/api/workers/<worker_id>/ping")
@login_required
def api_ping_worker(worker_id: str):
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    resp = cp.heartbeat_worker(worker_id)
    if resp is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(resp)


@bp.get("/api/workers/<worker_id>/live")
@login_required
def api_worker_live(worker_id: str):
    """Return worker details and a running-task snapshot for live UI polling."""
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    cp_worker = cp.get_worker(worker_id)
    if cp_worker is not None:
        running_tasks = _cp_list_tasks_safe(cp, statuses=["running"], limit=200, include_content=False) or []
        running_tasks = [t for t in running_tasks if t.get("status") == "running"]
        return jsonify({"worker": cp_worker, "running_tasks": running_tasks})

    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        local = db.get(Worker, int(worker_id))
        if not local:
            return jsonify({"error": "not found"}), 404
        running = db.query(Task).filter(Task.status == "running").order_by(Task.updated_at.desc()).limit(20).all()
        running_tasks = []
        for t in running:
            running_tasks.append(
                {
                    "id": t.id,
                    "bot_id": t.bot_id,
                    "status": t.status,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else "",
                }
            )
        return jsonify({"worker": _worker_to_dict(local), "running_tasks": running_tasks})
    finally:
        db.close()


@bp.post("/api/workers/<worker_id>/models/pull")
@login_required
def api_worker_pull_model(worker_id: str):
    from dashboard.cp_client import get_cp_client

    data: dict[str, Any] = request.get_json(force=True) or {}
    model = str(data.get("model") or "").strip()
    provider = str(data.get("provider") or "ollama").strip().lower() or "ollama"
    if not model:
        return jsonify({"error": "model is required"}), 400

    cp = get_cp_client()
    worker = cp.get_worker(worker_id)
    if worker is None:
        return jsonify({"error": "worker lookup failed"}), 502

    try:
        base_url = _worker_base_url(worker)
        resp = requests.post(
            f"{base_url}/models/local/pull",
            json={"model": model, "provider": provider},
            timeout=600,
        )
        if resp.text:
            payload = resp.json()
        else:
            payload = {}
        if resp.status_code >= 400:
            return jsonify({"error": payload.get("detail") or payload.get("error") or "model pull failed"}), resp.status_code
        return jsonify(payload)
    except requests.RequestException as exc:
        logger.warning("Worker model pull failed for %s: %s", worker_id, exc)
        return jsonify({"error": str(exc)}), 502
