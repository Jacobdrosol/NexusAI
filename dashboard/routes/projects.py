"""Projects dashboard page and lightweight proxy API."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.connections_service import (
    inspect_database_schema,
    normalize_connection_config,
    normalize_database_dsn,
    render_database_schema_document,
    resolve_connection_config,
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
_REPO_ROOT = Path(__file__).resolve().parents[2]


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
    config = resolve_connection_config(_parse_json(row.config_json or "{}", {}))
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


def _build_project_ai_readiness(
    *,
    project: dict[str, Any],
    project_bots: list[dict[str, Any]],
    vault_items: list[dict[str, Any]],
    project_connections: list[dict[str, Any]],
    github_status: dict[str, Any],
    chat_tool_access: dict[str, Any],
    repo_workspace: dict[str, Any],
) -> dict[str, Any]:
    enabled_bots = [bot for bot in project_bots if bool(bot.get("enabled", True))]
    vault_count = len(vault_items)
    connection_count = len(project_connections)
    checks: list[dict[str, str]] = []

    def add_check(label: str, state: str, detail: str) -> None:
        checks.append({"label": label, "state": state, "detail": detail})

    project_enabled = bool(project.get("enabled", True))
    add_check(
        "Project",
        "ready" if project_enabled else "disabled",
        "Project is enabled." if project_enabled else "Project is disabled.",
    )
    add_check(
        "Assigned Bots",
        "ready" if enabled_bots else "attention",
        f"{len(enabled_bots)} enabled bot(s), {len(project_bots)} assigned bot(s).",
    )
    blocked_bots = [
        bot
        for bot in enabled_bots
        if str((bot.get("readiness") or {}).get("state") or "").strip().lower() in {"blocked", "disabled"}
    ]
    unknown_bots = [
        bot
        for bot in enabled_bots
        if str((bot.get("readiness") or {}).get("state") or "").strip().lower() == "unknown"
    ]
    if blocked_bots:
        add_check("Bot Readiness", "attention", f"{len(blocked_bots)} enabled assigned bot(s) are blocked.")
    elif unknown_bots:
        add_check("Bot Readiness", "attention", f"{len(unknown_bots)} enabled assigned bot(s) have unknown readiness.")
    elif enabled_bots:
        add_check("Bot Readiness", "ready", "All enabled assigned bots report ready.")

    memory_enabled = bool(project.get("memory_profiles_enabled", False))
    add_check(
        "Personal Memory",
        "ready" if memory_enabled else "disabled",
        "Project memory gate is enabled." if memory_enabled else "Project memory gate is off.",
    )

    chat_tools_enabled = bool(chat_tool_access.get("enabled", False))
    chat_tool_modes = [
        label
        for key, label in (("filesystem", "filesystem"), ("repo_search", "repo search"))
        if bool(chat_tool_access.get(key, False))
    ]
    if not chat_tools_enabled:
        add_check("Chat Tools", "disabled", "Project chat workspace tools are off.")
    elif chat_tool_modes:
        add_check("Chat Tools", "ready", "Enabled for " + ", ".join(chat_tool_modes) + ".")
    else:
        add_check("Chat Tools", "attention", "Enabled with no filesystem or repo-search capability.")

    repo_enabled = bool(repo_workspace.get("enabled", False))
    if repo_enabled:
        repo_detail_parts = ["managed workspace enabled"]
        if repo_workspace.get("default_branch"):
            repo_detail_parts.append(f"default branch {repo_workspace.get('default_branch')}")
        if bool(repo_workspace.get("allow_command_execution", False)):
            repo_detail_parts.append("command runner allowed")
        if bool(repo_workspace.get("allow_push", False)):
            repo_detail_parts.append("push allowed")
        add_check("Repo Workspace", "ready", "; ".join(repo_detail_parts) + ".")
    else:
        add_check(
            "Repo Workspace",
            "attention" if bool(chat_tool_access.get("filesystem", False)) else "disabled",
            "Repository workspace is off.",
        )

    context_sources = []
    if vault_count:
        context_sources.append(f"{vault_count} vault item(s)")
    if connection_count:
        context_sources.append(f"{connection_count} database connection(s)")
    if bool(github_status.get("connected", False)):
        context_sources.append("GitHub connected")
    github_sync = github_status.get("context_sync") if isinstance(github_status.get("context_sync"), dict) else {}
    if github_sync.get("namespace"):
        context_sources.append(f"context namespace {github_sync.get('namespace')}")
    add_check(
        "Context Sources",
        "ready" if context_sources else "attention",
        ", ".join(context_sources) + "." if context_sources else "No vault, database, or GitHub context is configured.",
    )

    attention_count = sum(1 for check in checks if check["state"] == "attention")
    disabled_count = sum(1 for check in checks if check["state"] == "disabled")
    if attention_count:
        overall_state = "attention"
        overall_label = f"{attention_count} setup item(s) need attention"
    elif disabled_count:
        overall_state = "disabled"
        overall_label = f"{disabled_count} optional gate(s) off"
    else:
        overall_state = "ready"
        overall_label = "Ready for AI work"

    return {
        "overall_state": overall_state,
        "overall_label": overall_label,
        "checks": checks,
        "enabled_bot_count": len(enabled_bots),
        "assigned_bot_count": len(project_bots),
        "vault_item_count": vault_count,
        "connection_count": connection_count,
    }


def _project_bot_scope_view(bot: dict[str, Any]) -> dict[str, Any]:
    policy = bot.get("execution_policy") if isinstance(bot.get("execution_policy"), dict) else {}

    def policy_list(key: str) -> list[str]:
        values = policy.get(key)
        if not isinstance(values, list):
            return []
        result = []
        for value in values:
            label = str(value or "").strip()
            if label and label not in result:
                result.append(label)
        return result

    return {
        "required_tools": policy_list("required_worker_tools"),
        "connection_actions": policy_list("connection_action_allowlist"),
        "connection_owner_approvals": policy_list("connection_action_owner_approval_required"),
        "browser_actions": policy_list("browser_action_allowlist"),
        "browser_owner_approvals": policy_list("browser_action_owner_approval_required"),
        "repo_output_mode": str(policy.get("repo_output_mode") or "deny").strip().lower() or "deny",
        "can_apply_db_actions": bool(policy.get("can_apply_db_actions", False)),
    }


def _with_project_bot_scope_views(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for bot in bots:
        row = dict(bot)
        row["scope_view"] = _project_bot_scope_view(row)
        enriched.append(row)
    return enriched


def _bot_readiness_view(readiness: Any) -> dict[str, Any]:
    if not isinstance(readiness, dict):
        return {"state": "unknown", "detail": "readiness unavailable"}
    state = str(readiness.get("state") or "").strip().lower()
    if not state:
        state = "ready" if bool(readiness.get("ready", False)) else "blocked"
    failed_messages = [
        str(check.get("message") or check.get("component") or "").strip()
        for check in readiness.get("checks") or []
        if isinstance(check, dict) and str(check.get("status") or "").strip().lower() in {"failed", "blocking"}
    ]
    detail = "; ".join(message for message in failed_messages if message)
    if not detail:
        detail = "ready" if state == "ready" else "no readiness detail"
    return {"state": state, "detail": detail}


def _with_project_bot_readiness_views(bots: list[dict[str, Any]], readiness_payload: Any) -> list[dict[str, Any]]:
    rows = readiness_payload.get("readiness") if isinstance(readiness_payload, dict) else []
    by_id = {
        str(row.get("bot_id") or "").strip(): row
        for row in rows or []
        if isinstance(row, dict) and str(row.get("bot_id") or "").strip()
    }
    enriched = []
    for bot in bots:
        row = dict(bot)
        row["readiness"] = _bot_readiness_view(by_id.get(str(bot.get("id") or "").strip()))
        enriched.append(row)
    return enriched


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


def _run_git(args: list[str]) -> tuple[str | None, str | None]:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return (cp.stdout or "").rstrip(), None
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or exc.stdout or str(exc)).strip()
        return None, err or "git command failed"
    except Exception as exc:
        return None, str(exc)


def _git_working_tree_status() -> dict[str, Any]:
    branch, branch_error = _run_git(["rev-parse", "--abbrev-ref", "HEAD"])
    raw_status, status_error = _run_git(["status", "--short", "--untracked-files=all"])
    entries: list[dict[str, str]] = []
    if raw_status:
        for line in raw_status.splitlines():
            if not line.strip():
                continue
            code = line[:2]
            path = line[3:].strip() if len(line) > 3 else ""
            entries.append({"code": code, "path": path})
    error = status_error or branch_error
    return {
        "branch": branch,
        "repo_root": str(_REPO_ROOT),
        "has_changes": bool(entries),
        "count": len(entries),
        "entries": entries,
        "summary": f"{len(entries)} uncommitted file(s)." if entries else "Working tree clean.",
        "error": error,
    }


def _attach_project_autonomy_coverage(
    projects: list[dict[str, Any]],
    bots: Any,
    schedules_response: Any,
    readiness_response: Any = None,
) -> list[dict[str, Any]]:
    """Add read-only bot and schedule coverage to project rows for the overview."""
    bot_rows = [row for row in bots if isinstance(row, dict)] if isinstance(bots, list) else None
    schedule_rows = (
        [row for row in schedules_response.get("schedules", []) if isinstance(row, dict)]
        if isinstance(schedules_response, dict) and isinstance(schedules_response.get("schedules"), list)
        else None
    )
    coverage_available = bot_rows is not None and schedule_rows is not None
    readiness_rows = (
        [row for row in readiness_response.get("readiness", []) if isinstance(row, dict)]
        if isinstance(readiness_response, dict) and isinstance(readiness_response.get("readiness"), list)
        else None
    )
    readiness_by_bot_id = {
        str(row.get("bot_id") or "").strip(): row
        for row in readiness_rows or []
        if str(row.get("bot_id") or "").strip()
    }
    enriched: list[dict[str, Any]] = []

    for project in projects:
        row = dict(project) if isinstance(project, dict) else {}
        project_id = str(row.get("id") or "").strip()
        registered_bot_ids = {
            str(bot_id).strip()
            for bot_id in (row.get("bot_ids") or [])
            if str(bot_id).strip()
        }
        configured_bots = (
            [bot for bot in bot_rows or [] if str(bot.get("project_id") or "").strip() == project_id]
            if coverage_available
            else []
        )
        enabled_configured_bots = [bot for bot in configured_bots if bool(bot.get("enabled", True))]
        active_schedules = (
            [
                schedule
                for schedule in schedule_rows or []
                if str(schedule.get("project_id") or "").strip() == project_id
                and str(schedule.get("status") or "").strip().lower() == "active"
            ]
            if coverage_available
            else []
        )
        completed_schedule_count = sum(
            1
            for schedule in active_schedules
            if str(schedule.get("last_run_status") or "").strip().lower() == "completed"
        )
        attention_schedule_count = sum(
            1
            for schedule in active_schedules
            if (last_run_status := str(schedule.get("last_run_status") or "").strip().lower())
            and last_run_status != "completed"
        )
        awaiting_first_run_count = sum(
            1
            for schedule in active_schedules
            if not str(schedule.get("last_run_status") or "").strip()
        )
        scheduled_bot_ids = {
            str(schedule.get("target_bot_id") or "").strip()
            for schedule in active_schedules
            if str(schedule.get("target_bot_id") or "").strip()
        }
        ready_enabled_bot_ids = {
            str(bot.get("id") or "").strip()
            for bot in enabled_configured_bots
            if bool((readiness_by_bot_id.get(str(bot.get("id") or "").strip()) or {}).get("ready"))
        }
        blocked_enabled_bot_count = sum(
            1
            for bot in enabled_configured_bots
            if str(bot.get("id") or "").strip() in readiness_by_bot_id
            and not bool((readiness_by_bot_id.get(str(bot.get("id") or "").strip()) or {}).get("ready"))
        )

        def _policy(bot: dict[str, Any]) -> dict[str, Any]:
            return bot.get("execution_policy") if isinstance(bot.get("execution_policy"), dict) else {}

        def _has_policy_list(bot: dict[str, Any], key: str) -> bool:
            values = _policy(bot).get(key)
            return isinstance(values, list) and any(str(value or "").strip() for value in values)

        tooling_bot_count = sum(1 for bot in enabled_configured_bots if _has_policy_list(bot, "required_worker_tools"))
        connection_action_bot_count = sum(
            1 for bot in enabled_configured_bots if _has_policy_list(bot, "connection_action_allowlist")
        )
        browser_action_bot_count = sum(
            1 for bot in enabled_configured_bots if _has_policy_list(bot, "browser_action_allowlist")
        )
        repo_edit_bot_count = sum(
            1
            for bot in enabled_configured_bots
            if str(_policy(bot).get("repo_output_mode") or "").strip().lower() == "allow"
        )
        row["autonomy_coverage"] = {
            "available": coverage_available,
            "readiness_available": readiness_rows is not None,
            "registered_bot_count": len(registered_bot_ids),
            "configured_bot_count": len(configured_bots),
            "enabled_configured_bot_count": len(enabled_configured_bots),
            "tooling_bot_count": tooling_bot_count,
            "connection_action_bot_count": connection_action_bot_count,
            "browser_action_bot_count": browser_action_bot_count,
            "repo_edit_bot_count": repo_edit_bot_count,
            "active_schedule_count": len(active_schedules),
            "scheduled_bot_count": len(scheduled_bot_ids),
            "completed_schedule_count": completed_schedule_count,
            "attention_schedule_count": attention_schedule_count,
            "awaiting_first_run_count": awaiting_first_run_count,
            "ready_enabled_bot_count": len(ready_enabled_bot_ids),
            "ready_unscheduled_bot_count": len(ready_enabled_bot_ids - scheduled_bot_ids),
            "blocked_enabled_bot_count": blocked_enabled_bot_count,
        }
        enriched.append(row)
    return enriched


@bp.get("/projects")
@login_required
def projects_page() -> str:
    cp = get_cp_client()
    projects = cp.list_projects()
    error = None
    if projects is None:
        projects = []
        error = cp.unavailable_reason()
    else:
        projects = _attach_project_autonomy_coverage(
            [project for project in projects if isinstance(project, dict)],
            cp.list_bots() if hasattr(cp, "list_bots") else None,
            cp.list_schedules() if hasattr(cp, "list_schedules") else None,
            cp.list_bot_readiness() if hasattr(cp, "list_bot_readiness") else None,
        )
    return render_template("projects.html", projects=projects, error=error)


@bp.get("/api/projects")
@login_required
def api_list_projects():
    cp = get_cp_client()
    projects = cp.list_projects()
    if projects is None:
        return _cp_error_response(cp)
    return jsonify(projects)


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
    project_bots = [
        bot
        for bot in bots
        if str(bot.get("id") or "") in project_bot_ids
        or str(bot.get("project_id") or "") == str(project_id)
    ]
    project_bots = _with_project_bot_scope_views(project_bots)
    list_readiness = getattr(cp, "list_bot_readiness", None)
    readiness_payload = list_readiness() if callable(list_readiness) else None
    project_bots = _with_project_bot_readiness_views(project_bots, readiness_payload)
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
    github_status = _normalize_github_status(cp.get_project_github_status(project_id))
    project_connections = _project_connections(project_id)
    project_ai_readiness = _build_project_ai_readiness(
        project=project,
        project_bots=project_bots,
        vault_items=vault_items,
        project_connections=project_connections,
        github_status=github_status,
        chat_tool_access=chat_tool_access,
        repo_workspace=repo_workspace,
    )
    orchestration_workspaces = (
        cp.list_project_orchestration_workspaces(project_id)
        if hasattr(cp, "list_project_orchestration_workspaces")
        else {"workspaces": []}
    ) or {"workspaces": []}
    return render_template(
        "project_detail.html",
        project=project,
        bots=project_bots,
        all_bots=bots,
        tasks=project_tasks,
        vault_items=vault_items,
        all_projects=all_projects,
        github_status=github_status,
        webhook_events=_normalize_webhook_events(
            cp.list_project_github_webhook_events(project_id, limit=30)
        ),
        chat_tool_access=chat_tool_access,
        repo_workspace=repo_workspace,
        orchestration_workspaces=orchestration_workspaces.get("workspaces") or [],
        project_data_root=str(project_data_root),
        project_data_tree=build_project_data_tree(project_id),
        project_connections=project_connections,
        project_ai_readiness=project_ai_readiness,
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
            "memory_profiles_enabled": bool(data.get("memory_profiles_enabled", False)),
        }
    )
    if created is None:
        return _cp_error_response(cp)
    return jsonify(created), 201


@bp.put("/api/projects/<project_id>/memory-profile")
@login_required
def api_update_project_memory_profile(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    project = cp.get_project(project_id)
    if project is None:
        return _cp_error_response(cp, "project not found")
    merged = dict(project)
    merged["memory_profiles_enabled"] = bool(data.get("enabled", False))
    updated = cp.update_project(project_id, merged)
    if updated is None:
        return _cp_error_response(cp, "failed to update project memory profile")
    return jsonify(updated)


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


@bp.get("/api/projects/<project_id>/git/status")
@login_required
def api_project_git_status(project_id: str):
    cp = get_cp_client()
    if cp.get_project(project_id) is None:
        return _cp_error_response(cp, "project not found")
    return jsonify(_git_working_tree_status())


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


@bp.post("/api/projects/<project_id>/repo/workspace/discard-untracked")
@login_required
def api_discard_project_repo_workspace_untracked(project_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    raw_paths = data.get("paths")
    if raw_paths is None:
        paths: list[str] = []
    elif isinstance(raw_paths, list):
        paths = [str(path).strip() for path in raw_paths if str(path).strip()]
    else:
        return jsonify({"error": "paths must be an array of strings"}), 400
    cp = get_cp_client()
    result = cp.discard_project_repo_workspace_untracked(project_id=project_id, paths=paths)
    if result is None:
        return _cp_error_response(cp, "failed to discard untracked repo workspace files")
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
            config_json=json.dumps(
                normalize_connection_config({"dsn": normalized_dsn, "readonly": bool(body.get("readonly", True))})
            ),
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
        config = resolve_connection_config(_parse_json(row.config_json or "{}", {}))
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
        config = resolve_connection_config(_parse_json(row.config_json or "{}", {}))
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
