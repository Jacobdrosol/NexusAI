from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client


bp = Blueprint("platform_ai", __name__)


def _cp_error_response(cp, fallback: str = "control plane unavailable"):
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = ""
    status_code = None
    if isinstance(err, dict):
        detail = str(err.get("detail") or "").strip()
        raw_code = err.get("status_code")
        if isinstance(raw_code, int) and 400 <= raw_code <= 599:
            status_code = raw_code
    return jsonify({"error": detail or fallback}), (status_code or 502)


@bp.get("/platform-ai")
@login_required
def platform_ai_page() -> str:
    cp = get_cp_client()
    sessions_resp = cp.list_platform_ai_sessions(limit=300) or {}
    suites_resp = cp.list_platform_ai_quality_suites_global(limit=300) or {}
    sessions = sessions_resp.get("sessions") if isinstance(sessions_resp.get("sessions"), list) else []
    suites = suites_resp.get("suites") if isinstance(suites_resp.get("suites"), list) else []
    error = None
    if sessions_resp is None and suites_resp is None:
        error = "Control plane unavailable"
    return render_template(
        "platform_ai.html",
        sessions=sessions,
        suites=suites,
        error=error,
        active_page="platform_ai",
    )


@bp.get("/api/platform-ai/sessions")
@login_required
def api_list_platform_ai_sessions():
    cp = get_cp_client()
    assignment_id = str(request.args.get("assignment_id") or "").strip() or None
    orchestration_id = str(request.args.get("orchestration_id") or "").strip() or None
    mode = str(request.args.get("mode") or "").strip() or None
    limit = int(request.args.get("limit") or 100)
    data = cp.list_platform_ai_sessions(
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        mode=mode,
        limit=limit,
    )
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai sessions")
    return jsonify(data)


@bp.get("/api/platform-ai/test-suites")
@login_required
def api_list_platform_ai_test_suites():
    cp = get_cp_client()
    session_id = str(request.args.get("session_id") or "").strip() or None
    assignment_id = str(request.args.get("assignment_id") or "").strip() or None
    orchestration_id = str(request.args.get("orchestration_id") or "").strip() or None
    limit = int(request.args.get("limit") or 200)
    data = cp.list_platform_ai_quality_suites_global(
        session_id=session_id,
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        limit=limit,
    )
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai test suites")
    return jsonify(data)
