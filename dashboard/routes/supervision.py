"""Dashboard operations surface for approval-gated manager supervision."""
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("supervision", __name__)


def _cp_error(cp, fallback: str):
    error = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = str((error or {}).get("detail") or fallback)
    status = int((error or {}).get("status_code") or 502)
    return jsonify({"error": detail}), status if 400 <= status <= 599 else 502


@bp.get("/supervision")
@login_required
def supervision_page():
    cp = get_cp_client()
    overview = cp.get_supervision_overview()
    payload = overview if isinstance(overview, dict) else {}
    return render_template(
        "supervision.html",
        fleet=payload.get("fleet") if isinstance(payload.get("fleet"), dict) else {},
        reports=payload.get("latest_reports") if isinstance(payload.get("latest_reports"), list) else [],
        actions=payload.get("pending_actions") if isinstance(payload.get("pending_actions"), list) else [],
        holds=payload.get("active_holds") if isinstance(payload.get("active_holds"), list) else [],
        error=None if overview is not None else "Control plane unavailable",
        active_page="supervision",
    )


@bp.get("/api/supervision/overview")
@login_required
def api_supervision_overview():
    cp = get_cp_client()
    payload = cp.get_supervision_overview()
    if payload is None:
        return _cp_error(cp, "failed to load supervision overview")
    return jsonify(payload)


@bp.post("/api/supervision/actions/<action_id>/approve")
@login_required
def api_approve_supervision_action(action_id: str):
    cp = get_cp_client()
    payload = cp.approve_supervision_action(action_id, request.get_json(silent=True) or {})
    if payload is None:
        return _cp_error(cp, "failed to approve supervision action")
    return jsonify(payload)


@bp.post("/api/supervision/actions/<action_id>/reject")
@login_required
def api_reject_supervision_action(action_id: str):
    cp = get_cp_client()
    payload = cp.reject_supervision_action(action_id, request.get_json(silent=True) or {})
    if payload is None:
        return _cp_error(cp, "failed to reject supervision action")
    return jsonify(payload)


@bp.post("/api/supervision/holds/<bot_id>/release")
@login_required
def api_release_supervision_hold(bot_id: str):
    cp = get_cp_client()
    payload = cp.release_supervision_hold(bot_id, request.get_json(silent=True) or {})
    if payload is None:
        return _cp_error(cp, "failed to release supervision hold")
    return jsonify(payload)
