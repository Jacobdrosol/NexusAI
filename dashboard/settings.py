"""Settings Blueprint for the NexusAI Dashboard (Flask).

Provides:
  - GET  /settings                – settings UI page (admin only)
  - GET  /api/settings            – list all settings (secrets masked)
  - GET  /api/settings/export/yaml – download settings as YAML
  - GET  /api/settings/export/json – download settings as JSON
  - POST /api/settings/import     – import from uploaded YAML/JSON file
  - GET  /api/settings/<key>      – get single setting
  - POST /api/settings            – bulk update (admin only)
  - PUT  /api/settings/<key>      – update single setting (admin only)
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict

import yaml
from flask import (
    Blueprint,
    abort,
    jsonify,
    make_response,
    render_template,
    request,
)
from flask_login import current_user, login_required

from dashboard.deploy_manager import DeployManager
from shared.settings_manager import SettingsManager

logger = logging.getLogger(__name__)

bp = Blueprint("settings", __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CATEGORY_ORDER = ["general", "auth", "llm", "logging", "advanced"]
_CATEGORY_LABELS: Dict[str, str] = {
    "general": "General",
    "auth": "Auth",
    "llm": "LLM / Workers",
    "logging": "Logging",
    "advanced": "Advanced",
}


def _get_mgr() -> SettingsManager:
    """Return the shared SettingsManager singleton."""
    return SettingsManager.instance()


def _group_by_category(
    all_settings: Dict[str, Dict[str, Any]],
) -> list[Dict[str, Any]]:
    """Group settings rows by category, preserving the canonical order."""
    groups: Dict[str, list] = {cat: [] for cat in _CATEGORY_ORDER}
    for key, meta in all_settings.items():
        cat = meta.get("category", "general")
        if cat not in groups:
            groups[cat] = []
        groups[cat].append({"key": key, **meta})
    return [
        {
            "id": cat,
            "label": _CATEGORY_LABELS.get(cat, cat.title()),
            "settings": groups[cat],
        }
        for cat in _CATEGORY_ORDER
        if groups.get(cat)
    ]


def _require_admin() -> None:
    """Abort with 403 if the current user is not an admin."""
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


_TOOL_CHECK_TIMEOUT_SECONDS = 20
_TOOL_INSTALL_TIMEOUT_SECONDS = 1800
_WINGET_FLAGS = [
    "--accept-package-agreements",
    "--accept-source-agreements",
    "--silent",
]


def _load_enabled_tool_ids() -> list[str]:
    from shared.tool_catalog import default_enabled_tools

    mgr = _get_mgr()
    raw = mgr.get("enabled_tools")
    try:
        return json.loads(raw) if isinstance(raw, str) and raw else default_enabled_tools()
    except Exception:
        return default_enabled_tools()


def _save_enabled_tool_ids(enabled_ids: list[str]) -> None:
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "api")
    mgr.set("enabled_tools", json.dumps(enabled_ids), changed_by)


def _tool_install_plan(tool_id: str) -> dict[str, Any] | None:
    is_windows = platform.system().lower().startswith("win")
    if is_windows:
        winget = {
            "code_exec_python": {
                "label": "Install Python 3.12",
                "notes": "Uses winget to install Python 3.12 on this machine.",
                "commands": [["winget", "install", "--id", "Python.Python.3.12", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_dotnet": {
                "label": "Install .NET SDK 8",
                "notes": "Uses winget to install the .NET SDK required for C# build and test tasks.",
                "commands": [["winget", "install", "--id", "Microsoft.DotNet.SDK.8", "-e", *_WINGET_FLAGS]],
            },
            "test_runner_dotnet_test": {
                "label": "Install .NET SDK 8",
                "notes": "dotnet test is included with the .NET SDK.",
                "commands": [["winget", "install", "--id", "Microsoft.DotNet.SDK.8", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_node": {
                "label": "Install Node.js LTS",
                "notes": "Uses winget to install the Node.js LTS runtime.",
                "commands": [["winget", "install", "--id", "OpenJS.NodeJS.LTS", "-e", *_WINGET_FLAGS]],
            },
            "devops_git": {
                "label": "Install Git",
                "notes": "Uses winget to install Git for Windows.",
                "commands": [["winget", "install", "--id", "Git.Git", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_go": {
                "label": "Install Go",
                "notes": "Uses winget to install the Go toolchain.",
                "commands": [["winget", "install", "--id", "GoLang.Go", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_rust": {
                "label": "Install Rust",
                "notes": "Uses winget to install rustup and the Rust toolchain.",
                "commands": [["winget", "install", "--id", "Rustlang.Rustup", "-e", *_WINGET_FLAGS]],
            },
            "test_runner_cargo_test": {
                "label": "Install Rust",
                "notes": "cargo test is included with the Rust toolchain.",
                "commands": [["winget", "install", "--id", "Rustlang.Rustup", "-e", *_WINGET_FLAGS]],
            },
            "code_exec_java": {
                "label": "Install Temurin JDK 17",
                "notes": "Uses winget to install the JDK required for JVM tasks.",
                "commands": [["winget", "install", "--id", "EclipseAdoptium.Temurin.17.JDK", "-e", *_WINGET_FLAGS]],
            },
            "test_runner_junit": {
                "label": "Install Temurin JDK 17",
                "notes": "JUnit tasks require a JDK. Build tooling remains project-specific.",
                "commands": [["winget", "install", "--id", "EclipseAdoptium.Temurin.17.JDK", "-e", *_WINGET_FLAGS]],
            },
        }
        if tool_id in winget:
            return winget[tool_id]

    pip_tools = {
        "test_runner_pytest": {
            "label": "Install pytest",
            "notes": "Installs pytest into the Python environment used by the dashboard host.",
            "commands": [[sys.executable, "-m", "pip", "install", "pytest"]],
        }
    }
    return pip_tools.get(tool_id)


def _classify_check_failure(output: str) -> str:
    lowered = output.lower()
    missing_markers = [
        "not recognized as an internal or external command",
        "command not found",
        "not found",
        "no such file",
        "is not recognized",
    ]
    if any(marker in lowered for marker in missing_markers):
        return "missing"
    return "error"


def _check_tool_availability(check_command: str | None) -> dict[str, Any]:
    if not check_command:
        return {
            "status": "unverified",
            "ok": None,
            "summary": "No automatic check is defined for this tool.",
            "output": "",
        }
    try:
        completed = subprocess.run(
            check_command,
            capture_output=True,
            text=True,
            shell=True,
            timeout=_TOOL_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "ok": False,
            "summary": f"Check timed out after {_TOOL_CHECK_TIMEOUT_SECONDS} seconds.",
            "output": "",
        }
    output = (completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or "")
    output = output.strip()
    if completed.returncode == 0:
        summary = output.splitlines()[0] if output else "Command succeeded."
        return {"status": "installed", "ok": True, "summary": summary, "output": output}
    status = _classify_check_failure(output)
    summary = output.splitlines()[0] if output else "Command exited with a non-zero status."
    return {"status": status, "ok": False, "summary": summary, "output": output}


def _tool_runtime_status(tool: Any) -> dict[str, Any]:
    plan = _tool_install_plan(tool.id)
    check = _check_tool_availability(tool.check_command)
    return {
        **check,
        "install_supported": plan is not None,
        "install_label": plan["label"] if plan else None,
        "install_notes": plan["notes"] if plan else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# UI route
# ---------------------------------------------------------------------------

@bp.get("/settings")
@login_required
def settings_page() -> str:
    """Render the settings management page (admin only)."""
    _require_admin()
    mgr = _get_mgr()
    all_settings = mgr.get_all(mask_secrets=False)
    audit_log = mgr.get_audit_log(50)
    groups = _group_by_category(all_settings)
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    if cp.health():
        api_keys = cp.list_keys() or []
        model_catalog = cp.list_models() or []
        projects = cp.list_projects() or []
    else:
        api_keys = []
        model_catalog = []
        projects = []
    deploy_status = DeployManager.instance().status(refresh_remote=False)
    return render_template(
        "settings.html",
        groups=groups,
        audit_log=audit_log,
        api_keys=api_keys,
        model_catalog=model_catalog,
        projects=projects,
        deploy_status=deploy_status,
        active_page="settings",
    )


# ---------------------------------------------------------------------------
# API routes — fixed paths before parameterised ones
# ---------------------------------------------------------------------------

@bp.get("/api/settings/export/yaml")
@login_required
def export_yaml():
    """Download all settings as a YAML file (secrets masked)."""
    _require_admin()
    mgr = _get_mgr()
    content = mgr.export_yaml()
    resp = make_response(content)
    resp.headers["Content-Type"] = "application/x-yaml"
    resp.headers["Content-Disposition"] = "attachment; filename=nexusai_settings.yaml"
    return resp


@bp.get("/api/settings/export/json")
@login_required
def export_json_endpoint():
    """Download all settings as a JSON file (secrets masked)."""
    _require_admin()
    mgr = _get_mgr()
    content = mgr.export_json()
    resp = make_response(content)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = "attachment; filename=nexusai_settings.json"
    return resp


@bp.post("/api/settings/import")
@login_required
def import_settings():
    """Import settings from an uploaded YAML or JSON file."""
    _require_admin()
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400
    file = request.files["file"]
    filename = file.filename or ""
    raw = file.read()
    try:
        if filename.endswith((".yaml", ".yml")):
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except Exception as exc:
        return jsonify({"error": f"Failed to parse file: {exc}"}), 400
    if not isinstance(data, dict):
        return jsonify({"error": "Imported file must be a JSON/YAML object."}), 400
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "import")
    mgr.import_from_dict(data, changed_by)
    return jsonify({"status": "ok", "imported": len(data)})


@bp.get("/api/settings")
@login_required
def list_settings():
    """Return all settings with secrets masked."""
    _require_admin()
    mgr = _get_mgr()
    return jsonify(mgr.get_all(mask_secrets=True))


@bp.get("/api/settings/keys")
@login_required
def list_api_keys():
    _require_admin()
    from dashboard.cp_client import get_cp_client

    keys = get_cp_client().list_keys()
    if keys is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(keys)


@bp.post("/api/settings/keys")
@login_required
def create_or_update_api_key():
    _require_admin()
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    provider = (body.get("provider") or "").strip()
    value = body.get("value") or ""
    if not name or not provider or not value:
        return jsonify({"error": "name, provider, and value are required"}), 400
    from dashboard.cp_client import get_cp_client

    result = get_cp_client().upsert_key(name=name, provider=provider, value=value)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result), 201


@bp.delete("/api/settings/keys/<name>")
@login_required
def delete_api_key(name: str):
    _require_admin()
    from dashboard.cp_client import get_cp_client

    ok = get_cp_client().delete_key(name)
    if not ok:
        return jsonify({"error": "delete failed"}), 502
    return "", 204


@bp.get("/api/settings/models")
@login_required
def list_model_catalog():
    _require_admin()
    from dashboard.cp_client import get_cp_client

    models = get_cp_client().list_models()
    if models is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(models)


@bp.post("/api/settings/models")
@login_required
def create_catalog_model():
    _require_admin()
    body = request.get_json(silent=True) or {}
    model_id = (body.get("id") or "").strip()
    name = (body.get("name") or "").strip()
    provider = (body.get("provider") or "").strip()
    if not model_id or not name or not provider:
        return jsonify({"error": "id, name, and provider are required"}), 400
    payload = {
        "id": model_id,
        "name": name,
        "provider": provider,
        "context_window": body.get("context_window"),
        "capabilities": body.get("capabilities", []),
        "input_cost_per_1k": body.get("input_cost_per_1k"),
        "output_cost_per_1k": body.get("output_cost_per_1k"),
        "notes": body.get("notes"),
        "enabled": bool(body.get("enabled", True)),
    }
    from dashboard.cp_client import get_cp_client

    created = get_cp_client().create_model(payload)
    if created is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(created), 201


@bp.delete("/api/settings/models/<model_id>")
@login_required
def delete_catalog_model(model_id: str):
    _require_admin()
    from dashboard.cp_client import get_cp_client

    ok = get_cp_client().delete_model(model_id)
    if not ok:
        return jsonify({"error": "delete failed"}), 502
    return "", 204


@bp.get("/api/settings/projects")
@login_required
def list_projects():
    _require_admin()
    from dashboard.cp_client import get_cp_client

    projects = get_cp_client().list_projects()
    if projects is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(projects)


@bp.get("/api/settings/deploy/status")
@login_required
def deploy_status():
    _require_admin()
    refresh_remote = request.args.get("fetch", "0") in {"1", "true", "yes"}
    return jsonify(DeployManager.instance().status(refresh_remote=refresh_remote))


@bp.post("/api/settings/deploy/check")
@login_required
def deploy_check():
    _require_admin()
    return jsonify(DeployManager.instance().status(refresh_remote=True))


@bp.post("/api/settings/deploy/run")
@login_required
def deploy_run():
    _require_admin()
    who = getattr(current_user, "email", "admin")
    ok, message = DeployManager.instance().start(requested_by=who)
    if not ok:
        return jsonify({"status": "blocked", "error": message}), 409
    return jsonify({"status": "started", "message": message}), 202


@bp.post("/api/settings/deploy/log/clear")
@login_required
def deploy_log_clear():
    _require_admin()
    DeployManager.instance().clear_log()
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Tool Catalog endpoints
# ---------------------------------------------------------------------------

@bp.get("/api/settings/tools")
@login_required
def list_tools():
    """Return the full tool catalog with per-tool enabled status."""
    _require_admin()
    from shared.tool_catalog import (
        CATEGORY_LABELS,
        TOOL_CATALOG,
        TOOL_CATEGORIES,
        TOOL_PRESETS,
    )

    enabled_ids = set(_load_enabled_tool_ids())

    tools_out = [
        {
            "id": t.id,
            "name": t.name,
            "category": t.category,
            "category_label": CATEGORY_LABELS.get(t.category, t.category.title()),
            "description": t.description,
            "check_command": t.check_command,
            "install_hint": t.install_hint,
            "default_enabled": t.default_enabled,
            "enabled": t.id in enabled_ids,
            "install_supported": _tool_install_plan(t.id) is not None,
            "presets": t.presets,
        }
        for t in TOOL_CATALOG
    ]
    return jsonify(
        {
            "tools": tools_out,
            "categories": [
                {"id": c, "label": CATEGORY_LABELS.get(c, c.title())}
                for c in TOOL_CATEGORIES
            ],
            "presets": [
                {"id": k, "label": v["label"], "description": v["description"]}
                for k, v in TOOL_PRESETS.items()
            ],
            "enabled_count": sum(1 for t in tools_out if t["enabled"]),
            "total_count": len(tools_out),
        }
    )


@bp.put("/api/settings/tools")
@login_required
def update_tools_bulk():
    """Bulk-update enabled tools: body ``{\"enabled_tools\": [\"id1\", \"id2\", ...]}``."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG_BY_ID

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "enabled_tools" not in body:
        return jsonify({"error": "Body must contain an 'enabled_tools' list."}), 400
    raw_ids = body["enabled_tools"]
    if not isinstance(raw_ids, list):
        return jsonify({"error": "'enabled_tools' must be a list of tool ID strings."}), 400
    valid_ids = [i for i in raw_ids if isinstance(i, str) and i in TOOL_CATALOG_BY_ID]
    _save_enabled_tool_ids(valid_ids)
    return jsonify({"status": "ok", "enabled_tools": valid_ids})


@bp.put("/api/settings/tools/<tool_id>")
@login_required
def update_tool(tool_id: str):
    """Toggle a single tool on or off: body ``{\"enabled\": true|false}``."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG_BY_ID

    if tool_id not in TOOL_CATALOG_BY_ID:
        return jsonify({"error": f"Unknown tool ID '{tool_id}'."}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "enabled" not in body:
        return jsonify({"error": "Body must contain an 'enabled' boolean."}), 400
    enabled_ids = _load_enabled_tool_ids()
    if body["enabled"]:
        if tool_id not in enabled_ids:
            enabled_ids.append(tool_id)
    else:
        enabled_ids = [i for i in enabled_ids if i != tool_id]
    _save_enabled_tool_ids(enabled_ids)
    return jsonify({"status": "ok", "tool_id": tool_id, "enabled": bool(body["enabled"])})


@bp.post("/api/settings/tools/preset/<preset_id>")
@login_required
def apply_tool_preset(preset_id: str):
    """Apply a tool preset: replaces enabled_tools with the preset's tool list."""
    _require_admin()
    from shared.tool_catalog import TOOL_PRESETS, tools_for_preset

    if preset_id not in TOOL_PRESETS:
        return jsonify({"error": f"Unknown preset '{preset_id}'."}), 404
    tool_ids = tools_for_preset(preset_id)
    _save_enabled_tool_ids(tool_ids)
    return jsonify({"status": "ok", "preset": preset_id, "enabled_tools": tool_ids})


@bp.post("/api/settings/tools/test")
@login_required
def test_tools():
    """Run availability checks for enabled tools, or all tools when requested."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG, TOOL_CATALOG_BY_ID

    body = request.get_json(silent=True) or {}
    scope = str(body.get("scope") or "enabled").strip().lower()
    tool_id = str(body.get("tool_id") or "").strip()
    enabled_ids = set(_load_enabled_tool_ids())
    if tool_id:
        tool = TOOL_CATALOG_BY_ID.get(tool_id)
        if tool is None:
            return jsonify({"error": f"Unknown tool ID '{tool_id}'."}), 404
        selected_tools = [tool]
        scope = "single"
    elif scope == "all":
        selected_tools = TOOL_CATALOG
    else:
        selected_tools = [tool for tool in TOOL_CATALOG if tool.id in enabled_ids]
    statuses = []
    for tool in selected_tools:
        statuses.append(
            {
                "id": tool.id,
                "enabled": tool.id in enabled_ids,
                "check_command": tool.check_command,
                **_tool_runtime_status(tool),
            }
        )
    if scope == "enabled" and not statuses:
        for tool in TOOL_CATALOG_BY_ID.values():
            statuses.append(
                {
                    "id": tool.id,
                    "enabled": False,
                    "check_command": tool.check_command,
                    **_tool_runtime_status(tool),
                }
            )
        scope = "all"
    return jsonify(
        {
            "scope": scope,
            "statuses": statuses,
            "checked_count": len(statuses),
        }
    )


@bp.post("/api/settings/tools/install/<tool_id>")
@login_required
def install_tool(tool_id: str):
    """Install a supported tool runtime on the dashboard host and enable it."""
    _require_admin()
    from shared.tool_catalog import TOOL_CATALOG_BY_ID

    tool = TOOL_CATALOG_BY_ID.get(tool_id)
    if tool is None:
        return jsonify({"error": f"Unknown tool ID '{tool_id}'."}), 404
    plan = _tool_install_plan(tool_id)
    if plan is None:
        return (
            jsonify(
                {
                    "error": f"No curated installer is available for '{tool_id}'.",
                    "install_hint": tool.install_hint,
                }
            ),
            400,
        )
    command_log: list[dict[str, Any]] = []
    for command in plan["commands"]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_TOOL_INSTALL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                jsonify(
                    {
                        "error": f"Installer timed out for '{tool_id}'.",
                        "tool_id": tool_id,
                        "command_log": command_log,
                    }
                ),
                504,
            )
        output = (completed.stdout or "") + ("\n" if completed.stdout and completed.stderr else "") + (completed.stderr or "")
        command_log.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "output": output.strip(),
            }
        )
        if completed.returncode != 0:
            return (
                jsonify(
                    {
                        "error": f"Installer command failed for '{tool_id}'.",
                        "tool_id": tool_id,
                        "command_log": command_log,
                    }
                ),
                502,
            )
    status = _tool_runtime_status(tool)
    if status["status"] != "installed":
        return (
            jsonify(
                {
                    "error": f"Installation finished but '{tool_id}' still does not appear installed.",
                    "tool_id": tool_id,
                    "command_log": command_log,
                    "status": status,
                }
            ),
            502,
        )
    enabled_ids = _load_enabled_tool_ids()
    if tool_id not in enabled_ids:
        enabled_ids.append(tool_id)
        _save_enabled_tool_ids(enabled_ids)
    return jsonify(
        {
            "status": "ok",
            "tool_id": tool_id,
            "enabled": True,
            "command_log": command_log,
            "tool_status": status,
        }
    )


@bp.get("/api/settings/<key>")
@login_required
def get_setting(key: str):
    """Return a single setting (secret values are masked)."""
    _require_admin()
    mgr = _get_mgr()
    all_settings = mgr.get_all(mask_secrets=True)
    if key not in all_settings:
        return jsonify({"error": f"Setting '{key}' not found."}), 404
    return jsonify(all_settings[key])


@bp.post("/api/settings")
@login_required
def bulk_update_settings():
    """Bulk-update settings from a JSON body ``{key: value, ...}``."""
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "api")
    mgr.import_from_dict(body, changed_by)
    return jsonify({"status": "ok", "updated": len(body)})


@bp.put("/api/settings/<key>")
@login_required
def update_setting(key: str):
    """Update a single setting value."""
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "value" not in body:
        return jsonify({"error": "Body must contain a 'value' field."}), 400
    mgr = _get_mgr()
    changed_by = getattr(current_user, "email", "api")
    mgr.set(key, body["value"], changed_by)
    return jsonify({"status": "ok", "key": key})

