"""Dashboard schedule management for bounded autonomous bot dispatch."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client


bp = Blueprint("schedules", __name__)


def _cp_error_response(cp, fallback: str):
    error = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = str((error or {}).get("detail") or fallback)
    status = int((error or {}).get("status_code") or 502)
    return jsonify({"error": detail}), status if 400 <= status <= 599 else 502


def _as_rows(response: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    value = response.get(key)
    return value if isinstance(value, list) else []


def _safe_limit(value: Any, default: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 500))


@bp.get("/schedules")
@login_required
def schedules_page() -> str:
    cp = get_cp_client()
    schedule_response = cp.list_schedules(limit=200)
    return render_template(
        "schedules.html",
        schedules=_as_rows(schedule_response, "schedules"),
        bots=cp.list_bots() or [],
        projects=cp.list_projects() or [],
        error=None if schedule_response is not None else "Control plane unavailable",
        active_page="schedules",
    )


@bp.get("/api/schedules")
@login_required
def api_list_schedules():
    cp = get_cp_client()
    data = cp.list_schedules(
        limit=_safe_limit(request.args.get("limit")),
        status=str(request.args.get("status") or "").strip() or None,
        target_bot_id=str(request.args.get("target_bot_id") or "").strip() or None,
    )
    if data is None:
        return _cp_error_response(cp, "failed to list schedules")
    return jsonify(data)


@bp.get("/api/schedules/bots/<bot_id>/readiness")
@login_required
def api_get_schedule_bot_readiness(bot_id: str):
    """Expose the control plane's non-secret dispatch readiness for schedule setup."""
    cp = get_cp_client()
    data = cp.get_bot_readiness(bot_id)
    if data is None:
        return _cp_error_response(cp, "failed to load bot readiness")
    return jsonify(data)


@bp.post("/api/schedules")
@login_required
def api_create_schedule():
    cp = get_cp_client()
    data = cp.create_schedule(request.get_json(silent=True) or {})
    if data is None:
        return _cp_error_response(cp, "failed to create schedule")
    return jsonify(data), 201


@bp.patch("/api/schedules/<schedule_id>")
@login_required
def api_update_schedule(schedule_id: str):
    cp = get_cp_client()
    data = cp.update_schedule(schedule_id, request.get_json(silent=True) or {})
    if data is None:
        return _cp_error_response(cp, "failed to update schedule")
    return jsonify(data)


@bp.post("/api/schedules/<schedule_id>/trigger")
@login_required
def api_trigger_schedule(schedule_id: str):
    cp = get_cp_client()
    data = cp.trigger_schedule(schedule_id)
    if data is None:
        return _cp_error_response(cp, "failed to trigger schedule")
    return jsonify(data)


@bp.post("/api/schedules/<schedule_id>/preview")
@login_required
def api_preview_schedule(schedule_id: str):
    cp = get_cp_client()
    data = cp.preview_schedule(schedule_id)
    if data is None:
        return _cp_error_response(cp, "failed to preview schedule payload")
    return jsonify(data)


@bp.get("/api/schedules/<schedule_id>/runs")
@login_required
def api_schedule_runs(schedule_id: str):
    cp = get_cp_client()
    data = cp.list_schedule_runs(schedule_id, limit=_safe_limit(request.args.get("limit"), default=50))
    if data is None:
        return _cp_error_response(cp, "failed to load schedule runs")
    return jsonify(data)
