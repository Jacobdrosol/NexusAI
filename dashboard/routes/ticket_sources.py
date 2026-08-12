"""Ticket sources dashboard page and proxy API."""
from __future__ import annotations

import json
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("ticket_sources", __name__)


def _cp_error_response(cp, fallback="control plane unavailable"):
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = str(err.get("detail") or fallback)
    status_code = err.get("status_code")
    return jsonify({"error": detail or fallback}), (status_code or 502)


@bp.get("/projects/<project_id>/ticket-sources")
@login_required
def ticket_sources_page(project_id: str):
    cp = get_cp_client()
    result = cp.list_ticket_sources(project_id)
    sources = (result or {}).get("sources", []) if result else []
    # Fetch projects list for the sidebar
    projects = cp.list_projects() or []
    # PM bots available for manager assignment (role pm / workflow entry bots)
    bots = cp.list_bots() or []
    manager_bots = [
        b for b in bots
        if isinstance(b, dict) and (str(b.get("role") or "").strip().lower() in {"pm", "manager"}
                                    or str(b.get("id") or "").strip().startswith("pm-"))
    ]
    return render_template(
        "ticket_sources.html",
        project_id=project_id,
        projects=projects,
        sources=sources,
        manager_bots=manager_bots,
    )


# ---------------------------------------------------------------------------
#  API proxies
# ---------------------------------------------------------------------------

@bp.get("/api/projects/<project_id>/ticket-sources")
@login_required
def api_list_ticket_sources(project_id: str):
    cp = get_cp_client()
    result = cp.list_ticket_sources(project_id)
    if result is None:
        return _cp_error_response(cp, "Failed to list ticket sources")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/ticket-sources")
@login_required
def api_create_ticket_source(project_id: str):
    data = request.get_json(force=True) or {}
    cp = get_cp_client()
    result = cp.create_ticket_source(
        project_id,
        name=data.get("name", ""),
        source_type=data.get("source_type", ""),
        config=data.get("config"),
        credential_value=data.get("credential_value"),
        credential_key_ref=data.get("credential_key_ref"),
        enabled=data.get("enabled", True),
    )
    if result is None:
        return _cp_error_response(cp, "Failed to create ticket source")
    return jsonify(result), 201


@bp.get("/api/projects/<project_id>/ticket-sources/<source_id>")
@login_required
def api_get_ticket_source(project_id: str, source_id: str):
    cp = get_cp_client()
    result = cp.get_ticket_source(project_id, source_id)
    if result is None:
        return _cp_error_response(cp, "Failed to get ticket source")
    return jsonify(result)


@bp.patch("/api/projects/<project_id>/ticket-sources/<source_id>")
@login_required
def api_update_ticket_source(project_id: str, source_id: str):
    data = request.get_json(silent=True) or {}
    cp = get_cp_client()
    kwargs: dict[str, Any] = {}
    for key in ("name", "config", "credential_value", "credential_key_ref", "enabled"):
        if key in data:
            kwargs[key] = data[key]
    result = cp.update_ticket_source(project_id, source_id, **kwargs)
    if result is None:
        return _cp_error_response(cp, "Failed to update ticket source")
    return jsonify(result)


@bp.delete("/api/projects/<project_id>/ticket-sources/<source_id>")
@login_required
def api_delete_ticket_source(project_id: str, source_id: str):
    cp = get_cp_client()
    result = cp.delete_ticket_source(project_id, source_id)
    if result is None:
        return _cp_error_response(cp, "Failed to delete ticket source")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/ticket-sources/<source_id>/poll")
@login_required
def api_poll_ticket_source(project_id: str, source_id: str):
    data = request.get_json(silent=True) or {}
    cp = get_cp_client()
    result = cp.poll_ticket_source(project_id, source_id, max_items=data.get("max_items"))
    if result is None:
        return _cp_error_response(cp, "Failed to poll ticket source")
    return jsonify(result)


@bp.get("/api/projects/<project_id>/ticket-sources/<source_id>/items")
@login_required
def api_list_ticket_source_items(project_id: str, source_id: str):
    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    unlinked_only = request.args.get("unlinked_only", "false").lower() == "true"
    status = request.args.get("status") or None
    manager_bot_id = request.args.get("manager_bot_id") or None
    cp = get_cp_client()
    result = cp.list_ticket_source_items(
        project_id, source_id,
        limit=limit, offset=offset, unlinked_only=unlinked_only,
        status=status, manager_bot_id=manager_bot_id,
    )
    if result is None:
        return _cp_error_response(cp, "Failed to list ticket source items")
    return jsonify(result)


@bp.patch("/api/projects/<project_id>/ticket-sources/<source_id>/items/<external_id>")
@login_required
def api_update_ticket_source_item(project_id: str, source_id: str, external_id: str):
    data = request.get_json(silent=True) or {}
    cp = get_cp_client()
    result = cp.update_ticket_source_item(
        project_id, source_id, external_id,
        status=data.get("status"),
        manager_bot_id=data.get("manager_bot_id"),
        clear_manager=bool(data.get("clear_manager")),
        clear_task=bool(data.get("clear_task")),
    )
    if result is None:
        return _cp_error_response(cp, "Failed to update ticket source item")
    return jsonify(result)


@bp.post("/api/projects/<project_id>/ticket-sources/<source_id>/items/<external_id>/dispatch")
@login_required
def api_dispatch_ticket_source_item(project_id: str, source_id: str, external_id: str):
    data = request.get_json(silent=True) or {}
    cp = get_cp_client()
    result = cp.dispatch_ticket_source_item(
        project_id, source_id, external_id,
        manager_bot_id=data.get("manager_bot_id"),
        instruction=data.get("instruction"),
        plan_approval_required=data.get("plan_approval_required"),
    )
    if result is None:
        return _cp_error_response(cp, "Failed to dispatch ticket source item")
    return jsonify(result)