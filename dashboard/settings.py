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
    return render_template(
        "settings.html",
        groups=groups,
        audit_log=audit_log,
        api_keys=api_keys,
        model_catalog=model_catalog,
        projects=projects,
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

