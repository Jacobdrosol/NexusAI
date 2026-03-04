"""Projects dashboard page and lightweight proxy API."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("projects", __name__)


@bp.get("/projects")
@login_required
def projects_page() -> str:
    cp = get_cp_client()
    projects = cp.list_projects()
    error = None
    if projects is None:
        projects = []
        error = "Control plane unavailable — projects could not be loaded."
    return render_template("projects.html", projects=projects, error=error)


@bp.get("/projects/<project_id>")
@login_required
def project_detail_page(project_id: str):
    cp = get_cp_client()
    project = cp.get_project(project_id)
    if project is None:
        return render_template(
            "project_detail.html",
            project=None,
            bots=[],
            tasks=[],
            vault_items=[],
            all_projects=[],
            error="Control plane unavailable or project not found.",
        ), 502

    all_projects = cp.list_projects() or []
    bots = cp.list_bots() or []
    tasks = cp.list_tasks() or []
    vault_items = cp.list_vault_items(project_id=project_id, limit=100) or []

    project_bot_ids = set(project.get("bot_ids") or [])
    project_bots = [b for b in bots if str(b.get("id")) in project_bot_ids] if project_bot_ids else []
    project_tasks = []
    for t in tasks:
        md = t.get("metadata") or {}
        if isinstance(md, dict) and str(md.get("project_id", "")) == str(project_id):
            project_tasks.append(t)
    return render_template(
        "project_detail.html",
        project=project,
        bots=project_bots,
        tasks=project_tasks,
        vault_items=vault_items,
        all_projects=all_projects,
        error=None,
    )


@bp.post("/api/projects")
@login_required
def api_create_project():
    data: dict[str, Any] = request.get_json(force=True) or {}
    if not data.get("id") or not data.get("name"):
        return jsonify({"error": "id and name are required"}), 400
    cp = get_cp_client()
    created = cp.create_project(
        {
            "id": data["id"],
            "name": data["name"],
            "description": data.get("description"),
            "mode": data.get("mode", "isolated"),
            "bridge_project_ids": data.get("bridge_project_ids", []),
            "bot_ids": data.get("bot_ids", []),
            "settings_overrides": data.get("settings_overrides"),
            "enabled": bool(data.get("enabled", True)),
        }
    )
    if created is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(created), 201


@bp.post("/api/projects/<project_id>/bridges")
@login_required
def api_add_project_bridge(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    target_project_id = (data.get("target_project_id") or "").strip()
    if not target_project_id:
        return jsonify({"error": "target_project_id is required"}), 400
    cp = get_cp_client()
    result = cp.add_project_bridge(project_id, target_project_id)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/bridges/<target_project_id>")
@login_required
def api_remove_project_bridge(project_id: str, target_project_id: str):
    cp = get_cp_client()
    ok = cp.remove_project_bridge(project_id, target_project_id)
    if not ok:
        return jsonify({"error": "control plane unavailable"}), 502
    return "", 204
