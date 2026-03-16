"""Projects dashboard page and lightweight proxy API."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.connections_service import (
    inspect_database_schema,
    normalize_database_dsn,
    render_database_schema_document,
    test_database_connection,
    _mask_dsn_password,
)
from dashboard.cp_client import get_cp_client
from dashboard.db import get_db
from dashboard.models import Connection, ProjectConnection
from dashboard.project_data import (
    build_project_data_tree,
    create_project_data_folder,
    delete_project_data_path,
    delete_project_data_paths,
    ensure_project_data_layout,
    list_project_data_files,
    save_project_data_upload,
)
from dashboard.project_data_ingest import latest_job_for_project, start_project_data_ingest

bp = Blueprint("projects", __name__)


def _cp_list_tasks_safe(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


def _parse_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


def _project_connection_to_dict(row: Connection) -> dict[str, Any]:
    config = _parse_json(row.config_json or "{}", {})
    schema_text = row.schema_text or ""
    schema_snapshot = _parse_json(schema_text, {}) if schema_text else {}
    schema_totals = schema_snapshot.get("totals") if isinstance(schema_snapshot, dict) else {}
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "description": row.description or "",
        "config": {
            "readonly": bool(config.get("readonly", True)),
            "dsn_preview": _mask_dsn_password(str(config.get("dsn") or "")),
        },
        "schema_text": schema_text,
        "schema_totals": schema_totals if isinstance(schema_totals, dict) else {},
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _project_connections(project_id: str) -> list[dict[str, Any]]:
    db = get_db()
    try:
        links = db.query(ProjectConnection).filter(ProjectConnection.project_ref == str(project_id)).all()
        ids = [link.connection_id for link in links]
        if not ids:
            return []
        rows = db.query(Connection).filter(Connection.id.in_(ids)).order_by(Connection.name.asc()).all()
        return [_project_connection_to_dict(row) for row in rows]
    finally:
        db.close()


def _report_artifact_sort_key(artifact: dict[str, Any]) -> tuple[str, str]:
    created_at = str(artifact.get("created_at") or "")
    task_id = str(artifact.get("task_id") or "")
    return (created_at, task_id)


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
        "context_sync": raw.get("context_sync") if isinstance(raw.get("context_sync"), dict) else {},
    }


def _normalize_webhook_events(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    events = raw.get("events")
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict)]


def _normalize_project_chat_tool_access(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    workspace_root = str(raw.get("workspace_root") or "").strip() or None
    return {
        "enabled": bool(raw.get("enabled", False)),
        "filesystem": bool(raw.get("filesystem", False)),
        "repo_search": bool(raw.get("repo_search", False)),
        "workspace_root": workspace_root,
    }


def _normalize_project_repo_workspace(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    clone_url = str(raw.get("clone_url") or "").strip() or None
    default_branch = str(raw.get("default_branch") or "").strip() or None
    return {
        "enabled": bool(raw.get("enabled", False)),
        "managed_path_mode": bool(raw.get("managed_path_mode", True)),
        "workspace_binding": str(raw.get("workspace_binding") or "managed"),
        "root_path": None,
        "clone_url": clone_url,
        "default_branch": default_branch,
        "allow_push": bool(raw.get("allow_push", False)),
        "allow_command_execution": bool(raw.get("allow_command_execution", False)),
    }


def _cp_error_response(cp, fallback: str = "control plane unavailable") -> tuple[Any, int]:
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = ""
    status_code = None
    if isinstance(err, dict):
        detail = str(err.get("detail") or "").strip()
        raw_code = err.get("status_code")
        if isinstance(raw_code, int) and 400 <= raw_code <= 599:
            status_code = raw_code
    return jsonify({"error": detail or fallback}), (status_code or 502)


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
            chat_tool_access=_normalize_project_chat_tool_access(None),
            repo_workspace=_normalize_project_repo_workspace(None),
            project_data_root=None,
            project_data_tree=None,
            project_connections=[],
            error="Control plane unavailable or project not found.",
        )

    all_projects = cp.list_projects() or []
    bots = cp.list_bots() or []
    tasks = _cp_list_tasks_safe(cp, limit=400, include_content=False) or []
    vault_items = cp.list_vault_items(project_id=project_id, limit=100, include_content=False) or []

    project_bot_ids = set(project.get("bot_ids") or [])
    project_bots = [b for b in bots if str(b.get("id")) in project_bot_ids] if project_bot_ids else []
    project_reports: list[dict[str, Any]] = []
    for bot in project_bots:
        bot_id = str(bot.get("id") or "")
        if not bot_id:
            continue
        artifacts = cp.list_bot_artifacts(bot_id, limit=20) or []
        for artifact in artifacts:
            if str(artifact.get("label") or "") != "Run Report":
                continue
            project_reports.append(
                {
                    "bot_id": bot_id,
                    "bot_name": bot.get("name") or bot_id,
                    **artifact,
                }
            )
    project_reports = sorted(project_reports, key=_report_artifact_sort_key, reverse=True)[:20]
    project_tasks = []
    for t in tasks:
        md = t.get("metadata") or {}
        if isinstance(md, dict) and str(md.get("project_id", "")) == str(project_id):
            project_tasks.append(t)
    project_data_root = ensure_project_data_layout(project_id)
    chat_tool_access = _normalize_project_chat_tool_access(
        cp.get_project_chat_tool_access(project_id)
        if hasattr(cp, "get_project_chat_tool_access")
        else None
    )
    repo_workspace = _normalize_project_repo_workspace(
        cp.get_project_repo_workspace(project_id)
        if hasattr(cp, "get_project_repo_workspace")
        else None
    )
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
        chat_tool_access=chat_tool_access,
        repo_workspace=repo_workspace,
        project_data_root=str(project_data_root),
        project_data_tree=build_project_data_tree(project_id),
        project_connections=_project_connections(project_id),
        project_reports=project_reports,
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
        return _cp_error_response(cp)
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
        return _cp_error_response(cp)
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/bridges/<target_project_id>")
@login_required
def api_remove_project_bridge(project_id: str, target_project_id: str):
    cp = get_cp_client()
    ok = cp.remove_project_bridge(project_id, target_project_id)
    if not ok:
        return _cp_error_response(cp, "remove bridge failed")
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
        return _cp_error_response(cp, "GitHub PAT connect failed")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/github/status")
@login_required
def api_project_github_status(project_id: str):
    validate_arg = (request.args.get("validate") or "false").strip().lower()
    validate = validate_arg in {"1", "true", "yes", "on"}
    cp = get_cp_client()
    result = cp.get_project_github_status(project_id=project_id, validate=validate)
    if result is None:
        return _cp_error_response(cp)
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/github/pat")
@login_required
def api_disconnect_project_github_pat(project_id: str):
    cp = get_cp_client()
    ok = cp.disconnect_project_github_pat(project_id)
    if not ok:
        return _cp_error_response(cp, "disconnect failed")
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
        return _cp_error_response(cp, "failed to save webhook secret")
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/github/webhook/secret")
@login_required
def api_delete_project_github_webhook_secret(project_id: str):
    cp = get_cp_client()
    ok = cp.delete_project_github_webhook_secret(project_id)
    if not ok:
        return _cp_error_response(cp, "failed to remove webhook secret")
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
        return _cp_error_response(cp)
    return jsonify(result)


@bp.post("/api/projects/<project_id>/github/context/sync")
@login_required
def api_sync_project_github_context(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    sync_mode = (data.get("sync_mode") or "full").strip().lower()
    if sync_mode not in {"full", "update"}:
        return jsonify({"error": "sync_mode must be full or update"}), 400
    cp = get_cp_client()
    result = cp.sync_project_github_context(
        project_id=project_id,
        sync_mode=sync_mode,
        branch=(data.get("branch") or "").strip() or None,
        namespace=(data.get("namespace") or "").strip() or None,
    )
    if result is None:
        return _cp_error_response(cp, "Repository context sync failed")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/github/context/sync")
@login_required
def api_get_project_github_context_sync_status(project_id: str):
    cp = get_cp_client()
    result = cp.get_project_github_context_sync_status(project_id)
    if result is None:
        return _cp_error_response(cp, "Repository context sync status failed")
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
        return _cp_error_response(cp, "failed to save PR review config")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/cloud-context-policy")
@login_required
def api_get_project_cloud_context_policy(project_id: str):
    cp = get_cp_client()
    result = cp.get_project_cloud_context_policy(project_id)
    if result is None:
        return _cp_error_response(cp)
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
        return _cp_error_response(cp, "failed to update cloud context policy")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/chat-tool-access")
@login_required
def api_get_project_chat_tool_access(project_id: str):
    cp = get_cp_client()
    result = cp.get_project_chat_tool_access(project_id)
    if result is None:
        return _cp_error_response(cp)
    return jsonify(result)


@bp.put("/api/projects/<project_id>/chat-tool-access")
@login_required
def api_update_project_chat_tool_access(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    result = cp.update_project_chat_tool_access(
        project_id=project_id,
        enabled=bool(data.get("enabled", False)),
        filesystem=bool(data.get("filesystem", False)),
        repo_search=bool(data.get("repo_search", False)),
        workspace_root=(str(data.get("workspace_root") or "").strip() or None),
    )
    if result is None:
        return _cp_error_response(cp, "failed to update chat tool access")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/repo/workspace")
@login_required
def api_get_project_repo_workspace(project_id: str):
    cp = get_cp_client()
    result = cp.get_project_repo_workspace(project_id) if hasattr(cp, "get_project_repo_workspace") else None
    if result is None:
        return _cp_error_response(cp)
    return jsonify(result)


@bp.put("/api/projects/<project_id>/repo/workspace")
@login_required
def api_update_project_repo_workspace(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    include_clone_url = "clone_url" in data
    include_default_branch = "default_branch" in data
    result = cp.update_project_repo_workspace(
        project_id=project_id,
        enabled=bool(data.get("enabled", False)),
        managed_path_mode=bool(data.get("managed_path_mode", True)),
        root_path=(str(data.get("root_path") or "").strip() or None),
        clone_url=(str(data.get("clone_url") or "").strip() or None),
        default_branch=(str(data.get("default_branch") or "").strip() or None),
        allow_push=bool(data.get("allow_push", False)),
        allow_command_execution=bool(data.get("allow_command_execution", False)),
        include_clone_url=include_clone_url,
        include_default_branch=include_default_branch,
    )
    if result is None:
        return _cp_error_response(cp, "failed to update repo workspace")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/repo/workspace/status")
@login_required
def api_get_project_repo_workspace_status(project_id: str):
    cp = get_cp_client()
    result = cp.get_project_repo_workspace_status(project_id)
    if result is None:
        return _cp_error_response(cp, "failed to load repo workspace status")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/repo/workspace/clone")
@login_required
def api_clone_project_repo_workspace(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    depth_raw = data.get("depth")
    depth: int | None = None
    if depth_raw not in (None, ""):
        try:
            depth = int(depth_raw)
        except Exception:
            return jsonify({"error": "depth must be an integer"}), 400
    result = cp.clone_project_repo_workspace(
        project_id=project_id,
        clone_url=(str(data.get("clone_url") or "").strip() or None),
        branch=(str(data.get("branch") or "").strip() or None),
        depth=depth,
    )
    if result is None:
        return _cp_error_response(cp, "repo clone failed")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/repo/workspace/pull")
@login_required
def api_pull_project_repo_workspace(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    result = cp.pull_project_repo_workspace(
        project_id=project_id,
        remote=(str(data.get("remote") or "").strip() or "origin"),
        branch=(str(data.get("branch") or "").strip() or None),
        rebase=bool(data.get("rebase", False)),
    )
    if result is None:
        return _cp_error_response(cp, "repo pull failed")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/repo/workspace/commit")
@login_required
def api_commit_project_repo_workspace(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    message = (str(data.get("message") or "").strip())
    if not message:
        return jsonify({"error": "message is required"}), 400
    cp = get_cp_client()
    result = cp.commit_project_repo_workspace(
        project_id=project_id,
        message=message,
        add_all=bool(data.get("add_all", True)),
    )
    if result is None:
        return _cp_error_response(cp, "repo commit failed")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/repo/workspace/push")
@login_required
def api_push_project_repo_workspace(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    result = cp.push_project_repo_workspace(
        project_id=project_id,
        remote=(str(data.get("remote") or "").strip() or "origin"),
        branch=(str(data.get("branch") or "").strip() or None),
    )
    if result is None:
        return _cp_error_response(cp, "repo push failed")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/repo/workspace/run")
@login_required
def api_run_project_repo_workspace_command(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    command = data.get("command")
    if not isinstance(command, list) or not command:
        return jsonify({"error": "command must be a non-empty array"}), 400
    timeout_raw = data.get("timeout_seconds")
    timeout_seconds: int | None = None
    if timeout_raw not in (None, ""):
        try:
            timeout_seconds = int(timeout_raw)
        except Exception:
            return jsonify({"error": "timeout_seconds must be an integer"}), 400
    cp = get_cp_client()
    bootstrap_languages = data.get("bootstrap_languages")
    if bootstrap_languages is None:
        bootstrap_languages_list: list[str] = []
    elif isinstance(bootstrap_languages, list):
        bootstrap_languages_list = [str(x).strip() for x in bootstrap_languages if str(x).strip()]
    else:
        return jsonify({"error": "bootstrap_languages must be an array of strings"}), 400
    result = cp.run_project_repo_workspace_command(
        project_id=project_id,
        command=[str(part) for part in command],
        timeout_seconds=timeout_seconds,
        use_temp_workspace=bool(data.get("use_temp_workspace", False)),
        temp_ref=(str(data.get("temp_ref") or "").strip() or None),
        bootstrap=bool(data.get("bootstrap", False)),
        bootstrap_languages=bootstrap_languages_list,
        keep_temp_workspace=bool(data.get("keep_temp_workspace", False)),
    )
    if result is None:
        return _cp_error_response(cp, "repo command failed")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/repo/workspace/runs")
@login_required
def api_list_project_repo_workspace_runs(project_id: str):
    limit_raw = (request.args.get("limit") or "100").strip()
    try:
        limit = max(1, min(int(limit_raw), 1000))
    except Exception:
        limit = 100
    cp = get_cp_client()
    result = cp.list_project_repo_workspace_runs(project_id=project_id, limit=limit)
    if result is None:
        return _cp_error_response(cp, "failed to list repo workspace runs")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/repo/workspace/runs/summary")
@login_required
def api_summarize_project_repo_workspace_runs(project_id: str):
    since_raw = (request.args.get("since_hours") or "").strip()
    since_hours: int | None = None
    if since_raw:
        try:
            since_hours = max(1, min(int(since_raw), 24 * 365))
        except Exception:
            return jsonify({"error": "since_hours must be an integer"}), 400
    cp = get_cp_client()
    result = cp.summarize_project_repo_workspace_runs(project_id=project_id, since_hours=since_hours)
    if result is None:
        return _cp_error_response(cp, "failed to summarize repo workspace runs")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/data/files")
@login_required
def api_list_project_data_files(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    return jsonify(
        {
            "project_id": project_id,
            "root": str(ensure_project_data_layout(project_id)),
            "tree": build_project_data_tree(project_id),
            "entries": list_project_data_files(project_id),
        }
    )


@bp.post("/api/projects/<project_id>/data/folders")
@login_required
def api_create_project_data_folder(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    data: dict[str, Any] = request.get_json(force=True) or {}
    try:
        folder = create_project_data_folder(
            project_id=project_id,
            parent_path=(data.get("parent_path") or "").strip(),
            folder_name=(data.get("folder_name") or "").strip(),
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(
        {
            "project_id": project_id,
            "created": folder.name,
            "path": folder.relative_to(ensure_project_data_layout(project_id)).as_posix(),
        }
    ), 201


@bp.post("/api/projects/<project_id>/data/upload")
@login_required
def api_upload_project_data_file(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    target_path = (request.form.get("target_path") or "").strip()
    files = request.files.getlist("files")
    if not files:
        single = request.files.get("file")
        if single is not None:
            files = [single]
    if not files:
        return jsonify({"error": "at least one file is required"}), 400

    relative_paths = request.form.getlist("relative_paths")
    uploaded: list[dict[str, str]] = []
    for idx, storage in enumerate(files):
        relative_path = relative_paths[idx] if idx < len(relative_paths) else ""
        try:
            saved = save_project_data_upload(
                project_id,
                target_path,
                storage,
                relative_path=relative_path,
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        uploaded.append(
            {
                "name": saved.name,
                "path": saved.relative_to(ensure_project_data_layout(project_id)).as_posix(),
            }
        )
    return jsonify({"project_id": project_id, "uploaded": uploaded}), 201


@bp.delete("/api/projects/<project_id>/data/path")
@login_required
def api_delete_project_data_path(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    raw_path = (request.args.get("path") or "").strip()
    if not raw_path:
        return jsonify({"error": "path is required"}), 400
    try:
        deleted = delete_project_data_path(project_id, raw_path)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"project_id": project_id, "deleted": deleted})


@bp.post("/api/projects/<project_id>/data/delete")
@login_required
def api_delete_project_data_paths(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    body: dict[str, Any] = request.get_json(force=True) or {}
    paths = body.get("paths") if isinstance(body.get("paths"), list) else []
    try:
        deleted = delete_project_data_paths(project_id, [str(path or "") for path in paths])
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"project_id": project_id, "deleted": deleted})


@bp.post("/api/projects/<project_id>/data/ingest")
@login_required
def api_start_project_data_ingest(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    data: dict[str, Any] = request.get_json(force=True) or {}
    namespace = (data.get("namespace") or "").strip() or None
    job = start_project_data_ingest(project_id=project_id, namespace=namespace, max_bytes=None)
    return jsonify(job)


@bp.get("/api/projects/<project_id>/data/ingest")
@login_required
def api_get_project_data_ingest_status(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    job = latest_job_for_project(project_id) or {
        "job_id": None,
        "project_id": project_id,
        "namespace": f"project:{project_id}:data",
        "status": "idle",
        "counts": {"discovered": 0, "ingested": 0, "skipped": 0, "failed": 0},
        "current_path": None,
        "errors": [],
    }
    return jsonify(job)


@bp.get("/api/projects/<project_id>/connections")
@login_required
def api_list_project_connections(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    return jsonify(_project_connections(project_id))


@bp.post("/api/projects/<project_id>/connections")
@login_required
def api_create_project_connection(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    body: dict[str, Any] = request.get_json(force=True) or {}
    name = str(body.get("name") or "").strip()
    dsn = str(body.get("dsn") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not dsn:
        return jsonify({"error": "dsn is required"}), 400
    try:
        normalized_dsn = normalize_database_dsn(dsn)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    db = get_db()
    try:
        row = Connection(
            name=name,
            kind="database",
            description=str(body.get("description") or ""),
            config_json=json.dumps({"dsn": normalized_dsn, "readonly": bool(body.get("readonly", True))}),
            auth_json="{}",
            schema_text="",
            enabled=bool(body.get("enabled", True)),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        db.add(
            ProjectConnection(
                project_ref=str(project_id),
                connection_id=row.id,
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        return jsonify(_project_connection_to_dict(row)), 201
    finally:
        db.close()


@bp.delete("/api/projects/<project_id>/connections/<int:connection_id>")
@login_required
def api_delete_project_connection(project_id: str, connection_id: int):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    db = get_db()
    try:
        link = (
            db.query(ProjectConnection)
            .filter(
                ProjectConnection.project_ref == str(project_id),
                ProjectConnection.connection_id == connection_id,
            )
            .first()
        )
        if not link:
            return jsonify({"error": "not found"}), 404
        row = db.get(Connection, connection_id)
        db.delete(link)
        if row is not None:
            db.delete(row)
        db.commit()
        return "", 204
    finally:
        db.close()


@bp.post("/api/projects/<project_id>/connections/<int:connection_id>/test")
@login_required
def api_test_project_connection(project_id: str, connection_id: int):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    body: dict[str, Any] = request.get_json(force=True) or {}
    db = get_db()
    try:
        link = (
            db.query(ProjectConnection)
            .filter(
                ProjectConnection.project_ref == str(project_id),
                ProjectConnection.connection_id == connection_id,
            )
            .first()
        )
        if not link:
            return jsonify({"error": "not found"}), 404
        row = db.get(Connection, connection_id)
        if row is None or row.kind != "database":
            return jsonify({"error": "not found"}), 404
        config = _parse_json(row.config_json or "{}", {})
        try:
            result = test_database_connection(config=config if isinstance(config, dict) else {}, payload=body)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    finally:
        db.close()


@bp.post("/api/projects/<project_id>/connections/<int:connection_id>/schema-ingest")
@login_required
def api_ingest_project_connection_schema(project_id: str, connection_id: int):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    body: dict[str, Any] = request.get_json(force=True) or {}
    namespace = str(body.get("namespace") or "").strip() or f"project:{project_id}:data"
    db = get_db()
    try:
        link = (
            db.query(ProjectConnection)
            .filter(
                ProjectConnection.project_ref == str(project_id),
                ProjectConnection.connection_id == connection_id,
            )
            .first()
        )
        if not link:
            return jsonify({"error": "not found"}), 404
        row = db.get(Connection, connection_id)
        if row is None or row.kind != "database":
            return jsonify({"error": "not found"}), 404
        config = _parse_json(row.config_json or "{}", {})
        try:
            snapshot = inspect_database_schema(config=config if isinstance(config, dict) else {})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500
        if not snapshot.get("ok"):
            return jsonify(snapshot), 400
        schema_text = json.dumps(snapshot, indent=2)
        row.schema_text = schema_text
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        content = render_database_schema_document(connection_name=row.name, snapshot=snapshot)
        item = cp.upsert_vault_item(
            {
                "source_type": "custom",
                "source_ref": f"project-db://{project_id}/{connection_id}/schema",
                "title": f"{project_id} database schema: {row.name}",
                "content": content,
                "namespace": namespace,
                "project_id": project_id,
                "metadata": {
                    "kind": "project_database_schema",
                    "connection_id": connection_id,
                    "connection_name": row.name,
                },
            }
        )
        if item is None:
            return _cp_error_response(cp, "database schema ingest failed")
        return jsonify(
            {
                "ok": True,
                "connection": _project_connection_to_dict(row),
                "vault_item": item,
                "namespace": namespace,
                "snapshot": snapshot,
            }
        )
    finally:
        db.close()
