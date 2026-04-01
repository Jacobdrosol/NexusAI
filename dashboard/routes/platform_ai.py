from __future__ import annotations

from typing import Any, Dict, List, Optional

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


def _safe_int(value: Any, default: int, min_value: int = 1, max_value: int = 2000) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(min_value, min(max_value, parsed))


def _as_list(value: Any) -> List[Dict[str, Any]]:
    return value if isinstance(value, list) else []


def _session_pipeline_bot_id(session: Dict[str, Any]) -> Optional[str]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    pipeline_bot_id = str(metadata.get("pipeline_bot_id") or "").strip()
    return pipeline_bot_id or None


@bp.get("/platform-ai")
@login_required
def platform_ai_page() -> str:
    cp = get_cp_client()
    sessions_resp = cp.list_platform_ai_sessions(limit=300) or {}
    pipelines_resp = cp.list_platform_ai_pipelines() or {}
    sessions = _as_list(sessions_resp.get("sessions"))
    pipelines = _as_list(pipelines_resp.get("pipelines"))
    error = None
    if sessions_resp is None and pipelines_resp is None:
        error = "Control plane unavailable"
    return render_template(
        "platform_ai.html",
        sessions=sessions,
        pipelines=pipelines,
        error=error,
        active_page="platform_ai",
    )


@bp.get("/platform-ai/sessions/<session_id>")
@login_required
def platform_ai_session_page(session_id: str) -> str:
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return render_template(
            "platform_ai_session.html",
            session=None,
            messages=[],
            events=[],
            pipeline=None,
            suites=[],
            suite_runs=[],
            error="Platform AI session not found or control plane unavailable.",
            active_page="platform_ai",
        )

    messages_resp = cp.list_platform_ai_messages(session_id, limit=400) or {}
    events_resp = cp.list_platform_ai_events(session_id, limit=600) or {}
    messages = _as_list(messages_resp.get("messages"))
    events = _as_list(events_resp.get("events"))

    pipeline: Optional[Dict[str, Any]] = None
    suites: List[Dict[str, Any]] = []
    suite_runs: List[Dict[str, Any]] = []
    pipeline_bot_id = _session_pipeline_bot_id(session)
    if pipeline_bot_id:
        suites_resp = cp.list_platform_ai_pipeline_test_suites(pipeline_bot_id, limit=200) or {}
        pipeline = suites_resp.get("pipeline") if isinstance(suites_resp.get("pipeline"), dict) else None
        suites = _as_list(suites_resp.get("suites"))
        if suites:
            runs_resp = cp.list_platform_ai_quality_suite_runs(str(suites[0].get("id") or ""), limit=40) or {}
            suite_runs = _as_list(runs_resp.get("runs"))
    return render_template(
        "platform_ai_session.html",
        session=session,
        messages=messages,
        events=events,
        pipeline=pipeline,
        suites=suites,
        suite_runs=suite_runs,
        error=None,
        active_page="platform_ai",
    )


@bp.get("/api/platform-ai/sessions")
@login_required
def api_list_platform_ai_sessions():
    cp = get_cp_client()
    assignment_id = str(request.args.get("assignment_id") or "").strip() or None
    orchestration_id = str(request.args.get("orchestration_id") or "").strip() or None
    mode = str(request.args.get("mode") or "").strip() or None
    limit = _safe_int(request.args.get("limit"), 100, min_value=1, max_value=2000)
    data = cp.list_platform_ai_sessions(
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        mode=mode,
        limit=limit,
    )
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai sessions")
    return jsonify(data)


@bp.post("/api/platform-ai/sessions")
@login_required
def api_create_platform_ai_session():
    cp = get_cp_client()
    body = request.get_json(silent=True) or {}
    data = cp.create_platform_ai_session(body)
    if data is None:
        return _cp_error_response(cp, "failed to create platform ai session")
    return jsonify(data), 201


@bp.get("/api/platform-ai/sessions/<session_id>")
@login_required
def api_get_platform_ai_session(session_id: str):
    cp = get_cp_client()
    data = cp.get_platform_ai_session(session_id)
    if data is None:
        return _cp_error_response(cp, "failed to load platform ai session")
    return jsonify(data)


@bp.patch("/api/platform-ai/sessions/<session_id>")
@login_required
def api_patch_platform_ai_session(session_id: str):
    cp = get_cp_client()
    body = request.get_json(silent=True) or {}
    data = cp.patch_platform_ai_session(session_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to update platform ai session")
    return jsonify(data)


@bp.post("/api/platform-ai/sessions/<session_id>/control")
@login_required
def api_control_platform_ai_session(session_id: str):
    cp = get_cp_client()
    body = request.get_json(silent=True) or {}
    data = cp.control_platform_ai_session(session_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to control platform ai session")
    return jsonify(data)


@bp.get("/api/platform-ai/sessions/<session_id>/events")
@login_required
def api_list_platform_ai_session_events(session_id: str):
    cp = get_cp_client()
    limit = _safe_int(request.args.get("limit"), 200, min_value=1, max_value=2000)
    data = cp.list_platform_ai_events(session_id, limit=limit)
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai events")
    return jsonify(data)


@bp.get("/api/platform-ai/sessions/<session_id>/messages")
@login_required
def api_list_platform_ai_session_messages(session_id: str):
    cp = get_cp_client()
    limit = _safe_int(request.args.get("limit"), 200, min_value=1, max_value=2000)
    data = cp.list_platform_ai_messages(session_id, limit=limit)
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai messages")
    return jsonify(data)


@bp.post("/api/platform-ai/sessions/<session_id>/messages")
@login_required
def api_post_platform_ai_session_message(session_id: str):
    cp = get_cp_client()
    body = request.get_json(silent=True) or {}
    data = cp.post_platform_ai_message(session_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to post platform ai message")
    return jsonify(data), 201


@bp.get("/api/platform-ai/pipelines")
@login_required
def api_list_platform_ai_pipelines():
    cp = get_cp_client()
    data = cp.list_platform_ai_pipelines()
    if data is None:
        return _cp_error_response(cp, "failed to list pipelines")
    return jsonify(data)


@bp.get("/api/platform-ai/pipelines/<pipeline_bot_id>/test-suites")
@login_required
def api_list_platform_ai_pipeline_suites(pipeline_bot_id: str):
    cp = get_cp_client()
    limit = _safe_int(request.args.get("limit"), 200, min_value=1, max_value=2000)
    data = cp.list_platform_ai_pipeline_test_suites(pipeline_bot_id, limit=limit)
    if data is None:
        return _cp_error_response(cp, "failed to list pipeline test suites")
    return jsonify(data)


@bp.post("/api/platform-ai/pipelines/<pipeline_bot_id>/test-suites/design")
@login_required
def api_design_platform_ai_pipeline_suite(pipeline_bot_id: str):
    cp = get_cp_client()
    body = request.get_json(silent=True) or {}
    data = cp.design_platform_ai_pipeline_test_suite(pipeline_bot_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to design pipeline test suite")
    return jsonify(data)


@bp.post("/api/platform-ai/pipelines/<pipeline_bot_id>/test-suites/run")
@login_required
def api_run_platform_ai_pipeline_suite(pipeline_bot_id: str):
    cp = get_cp_client()
    body = request.get_json(silent=True) or {}
    data = cp.run_platform_ai_pipeline_test_suite(pipeline_bot_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to run pipeline test suite")
    return jsonify(data)


@bp.get("/api/platform-ai/test-suites")
@login_required
def api_list_platform_ai_test_suites():
    cp = get_cp_client()
    session_id = str(request.args.get("session_id") or "").strip() or None
    pipeline_bot_id = str(request.args.get("pipeline_bot_id") or "").strip() or None
    assignment_id = str(request.args.get("assignment_id") or "").strip() or None
    orchestration_id = str(request.args.get("orchestration_id") or "").strip() or None
    limit = _safe_int(request.args.get("limit"), 200, min_value=1, max_value=2000)
    data = cp.list_platform_ai_quality_suites_global(
        session_id=session_id,
        pipeline_bot_id=pipeline_bot_id,
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        limit=limit,
    )
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai test suites")
    return jsonify(data)


@bp.get("/api/platform-ai/test-suites/<suite_id>/runs")
@login_required
def api_list_platform_ai_test_suite_runs(suite_id: str):
    cp = get_cp_client()
    limit = _safe_int(request.args.get("limit"), 100, min_value=1, max_value=2000)
    data = cp.list_platform_ai_quality_suite_runs(suite_id, limit=limit)
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai test suite runs")
    return jsonify(data)


@bp.get("/api/platform-ai/test-runs/<run_id>")
@login_required
def api_get_platform_ai_test_run(run_id: str):
    cp = get_cp_client()
    data = cp.get_platform_ai_quality_run(run_id)
    if data is None:
        return _cp_error_response(cp, "failed to load platform ai test run")
    return jsonify(data)
