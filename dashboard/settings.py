"""Settings Blueprint (FastAPI APIRouter) for the NexusAI Dashboard.

Provides:
  - GET  /dashboard/settings          – settings UI page (admin only)
  - GET  /api/settings                – list all settings (secrets masked)
  - GET  /api/settings/export/yaml    – download settings as YAML
  - GET  /api/settings/export/json    – download settings as JSON
  - POST /api/settings/import         – import from uploaded YAML/JSON file
  - GET  /api/settings/{key}          – get single setting
  - POST /api/settings                – bulk update (admin only)
  - PUT  /api/settings/{key}          – update single setting (admin only)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict

import yaml
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from shared.settings_manager import SettingsManager

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

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


# ---------------------------------------------------------------------------
# UI route
# ---------------------------------------------------------------------------

@router.get("/dashboard/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    """Render the settings management page."""
    mgr = _get_mgr()
    all_settings = await asyncio.to_thread(mgr.get_all, mask_secrets=False)
    audit_log = await asyncio.to_thread(mgr.get_audit_log, 50)
    groups = _group_by_category(all_settings)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "groups": groups,
            "audit_log": audit_log,
            "active_page": "settings",
        },
    )


# ---------------------------------------------------------------------------
# API routes — ordering matters: fixed paths before parameterised ones
# ---------------------------------------------------------------------------

@router.get("/api/settings/export/yaml")
async def export_yaml() -> Response:
    """Download all settings as a YAML file (secrets masked)."""
    mgr = _get_mgr()
    content = await asyncio.to_thread(mgr.export_yaml)
    return Response(
        content=content,
        media_type="application/x-yaml",
        headers={"Content-Disposition": "attachment; filename=nexusai_settings.yaml"},
    )


@router.get("/api/settings/export/json")
async def export_json_endpoint() -> Response:
    """Download all settings as a JSON file (secrets masked)."""
    mgr = _get_mgr()
    content = await asyncio.to_thread(mgr.export_json)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=nexusai_settings.json"},
    )


@router.post("/api/settings/import")
async def import_settings(file: UploadFile, request: Request) -> JSONResponse:
    """Import settings from an uploaded YAML or JSON file."""
    raw = await file.read()
    filename = file.filename or ""
    try:
        if filename.endswith((".yaml", ".yml")):
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Imported file must be a JSON/YAML object.")
    mgr = _get_mgr()
    changed_by = request.headers.get("X-User", "import")
    await asyncio.to_thread(mgr.import_from_dict, data, changed_by)
    return JSONResponse({"status": "ok", "imported": len(data)})


@router.get("/api/settings")
async def list_settings() -> JSONResponse:
    """Return all settings with secrets masked."""
    mgr = _get_mgr()
    all_settings = await asyncio.to_thread(mgr.get_all, mask_secrets=True)
    return JSONResponse(all_settings)


@router.get("/api/settings/{key}")
async def get_setting(key: str) -> JSONResponse:
    """Return a single setting (secret values are masked)."""
    mgr = _get_mgr()
    all_settings = await asyncio.to_thread(mgr.get_all, mask_secrets=True)
    if key not in all_settings:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found.")
    return JSONResponse(all_settings[key])


@router.post("/api/settings")
async def bulk_update_settings(request: Request) -> JSONResponse:
    """Bulk-update settings from a JSON body ``{key: value, ...}``."""
    try:
        body: Dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    mgr = _get_mgr()
    changed_by = request.headers.get("X-User", "api")
    await asyncio.to_thread(mgr.import_from_dict, body, changed_by)
    return JSONResponse({"status": "ok", "updated": len(body)})


@router.put("/api/settings/{key}")
async def update_setting(key: str, request: Request) -> JSONResponse:
    """Update a single setting value."""
    try:
        body: Dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body.") from exc
    if "value" not in body:
        raise HTTPException(status_code=400, detail="Body must contain a 'value' field.")
    mgr = _get_mgr()
    changed_by = request.headers.get("X-User", "api")
    await asyncio.to_thread(mgr.set, key, body["value"], changed_by)
    return JSONResponse({"status": "ok", "key": key})
