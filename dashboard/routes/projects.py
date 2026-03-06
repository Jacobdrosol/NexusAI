"""Projects dashboard page and lightweight proxy API."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("projects", __name__)


def _normalize_github_status(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    return {
        "connected": bool(raw.get("connected", False)),
        "has_webhook_secret": bool(raw.get("has_webhook_secret", False)),
        "repo_full_name": raw.get("repo_full_name"),
        "validated": raw.get("validated"),
        "user_login": raw.get("user_login"),
        "user_id": raw.get("user_id"),
        "repo": raw.get("repo") if isinstance(raw.get("repo"), dict) else {},
        "pr_review": raw.get("pr_review") if isinstance(raw.get("pr_review"), dict) else {},
    }


def _normalize_webhook_events(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    events = raw.get("events")
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict)]


@bp.get("/projects")
@login_required
def projects_page() -> str:
    cp = get_cp_client()
    projects = cp.list_projects()
    error = None
    if projects is None:
        projects = []
        error = cp.unavailable_reason()
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
            github_status=_normalize_github_status(None),
            webhook_events=[],
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
        all_bots=bots,
        tasks=project_tasks,
        vault_items=vault_items,
        all_projects=all_projects,
        github_status=_normalize_github_status(cp.get_project_github_status(project_id)),
        webhook_events=_normalize_webhook_events(
            cp.list_project_github_webhook_events(project_id, limit=30)
        ),
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


@bp.post("/api/projects/<project_id>/github/pat")
@login_required
def api_connect_project_github_pat(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    token = (data.get("token") or "").strip()
    repo_full_name = (data.get("repo_full_name") or "").strip() or None
    validate = bool(data.get("validate", True))
    if not token:
        return jsonify({"error": "token is required"}), 400
    cp = get_cp_client()
    result = cp.connect_project_github_pat(
        project_id=project_id,
        token=token,
        repo_full_name=repo_full_name,
        validate=validate,
    )
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.get("/api/projects/<project_id>/github/status")
@login_required
def api_project_github_status(project_id: str):
    validate_arg = (request.args.get("validate") or "false").strip().lower()
    validate = validate_arg in {"1", "true", "yes", "on"}
    cp = get_cp_client()
    result = cp.get_project_github_status(project_id=project_id, validate=validate)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/github/pat")
@login_required
def api_disconnect_project_github_pat(project_id: str):
    cp = get_cp_client()
    ok = cp.disconnect_project_github_pat(project_id)
    if not ok:
        return jsonify({"error": "control plane unavailable"}), 502
    return "", 204


@bp.post("/api/projects/<project_id>/github/webhook/secret")
@login_required
def api_set_project_github_webhook_secret(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    secret = (data.get("secret") or "").strip()
    if not secret:
        return jsonify({"error": "secret is required"}), 400
    cp = get_cp_client()
    result = cp.set_project_github_webhook_secret(project_id, secret)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/github/webhook/secret")
@login_required
def api_delete_project_github_webhook_secret(project_id: str):
    cp = get_cp_client()
    ok = cp.delete_project_github_webhook_secret(project_id)
    if not ok:
        return jsonify({"error": "control plane unavailable"}), 502
    return "", 204


@bp.get("/api/projects/<project_id>/github/webhook/events")
@login_required
def api_list_project_github_webhook_events(project_id: str):
    limit_raw = (request.args.get("limit") or "30").strip()
    try:
        limit = max(1, min(int(limit_raw), 200))
    except Exception:
        limit = 30
    cp = get_cp_client()
    result = cp.list_project_github_webhook_events(project_id, limit=limit)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.post("/api/projects/<project_id>/github/context/sync")
@login_required
def api_sync_project_github_context(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    max_files_raw = data.get("max_files", 25)
    try:
        max_files = max(1, min(int(max_files_raw), 200))
    except Exception:
        max_files = 25
    cp = get_cp_client()
    result = cp.sync_project_github_context(
        project_id=project_id,
        branch=(data.get("branch") or "").strip() or None,
        max_files=max_files,
        namespace=(data.get("namespace") or "").strip() or None,
    )
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.post("/api/projects/<project_id>/github/pr-review/config")
@login_required
def api_configure_project_github_pr_review(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    enabled = bool(data.get("enabled", True))
    bot_id = (data.get("bot_id") or "").strip() or None
    cp = get_cp_client()
    result = cp.configure_project_github_pr_review(
        project_id=project_id,
        enabled=enabled,
        bot_id=bot_id,
    )
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.get("/api/projects/<project_id>/cloud-context-policy")
@login_required
def api_get_project_cloud_context_policy(project_id: str):
    cp = get_cp_client()
    result = cp.get_project_cloud_context_policy(project_id)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.put("/api/projects/<project_id>/cloud-context-policy")
@login_required
def api_update_project_cloud_context_policy(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    provider_policies = data.get("provider_policies") if isinstance(data.get("provider_policies"), dict) else {}
    bot_overrides = data.get("bot_overrides") if isinstance(data.get("bot_overrides"), dict) else {}
    cp = get_cp_client()
    result = cp.update_project_cloud_context_policy(
        project_id=project_id,
        provider_policies=provider_policies,
        bot_overrides=bot_overrides,
    )
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)
