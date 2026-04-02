from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, abort, jsonify, render_template, request, send_file
from flask_login import login_required
from werkzeug.utils import secure_filename

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upload_root() -> Path:
    configured = str(os.environ.get("NEXUSAI_PLATFORM_AI_UPLOAD_ROOT", "") or "").strip()
    if configured:
        return Path(configured).resolve()
    return (Path(__file__).resolve().parents[2] / "data" / "platform_ai" / "session_uploads").resolve()


def _session_context_files(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    rows = metadata.get("context_files")
    if not isinstance(rows, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        normalized.append(
            {
                "id": str(row.get("id") or "").strip() or None,
                "name": str(row.get("name") or Path(path).name).strip(),
                "path": path,
                "size_bytes": int(row.get("size_bytes") or 0),
                "content_type": str(row.get("content_type") or "").strip() or None,
                "uploaded_at": str(row.get("uploaded_at") or "").strip() or None,
            }
        )
    return normalized


def _session_message_files(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    rows = metadata.get("message_files")
    if not isinstance(rows, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").strip()
        file_id = str(row.get("id") or "").strip()
        if not path or not file_id:
            continue
        normalized.append(
            {
                "id": file_id,
                "name": str(row.get("name") or Path(path).name).strip(),
                "path": path,
                "size_bytes": int(row.get("size_bytes") or 0),
                "content_type": str(row.get("content_type") or "").strip() or None,
                "uploaded_at": str(row.get("uploaded_at") or "").strip() or None,
                "url": f"/api/platform-ai/sessions/{secure_filename(str(session.get('id') or ''))}/files/{file_id}",
            }
        )
    return normalized


def _safe_session_file_path(session_id: str, path: str) -> Optional[Path]:
    root = _upload_root().resolve()
    sid = secure_filename(str(session_id or "").strip())
    if not sid:
        return None
    base = (root / sid).resolve()
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(base)
    except Exception:
        return None
    return candidate if candidate.exists() and candidate.is_file() else None


@bp.get("/platform-ai")
@login_required
def platform_ai_page() -> str:
    cp = get_cp_client()
    sessions_active_resp = cp.list_platform_ai_sessions(limit=300, archived="active") or {}
    sessions_archived_resp = cp.list_platform_ai_sessions(limit=300, archived="archived") or {}
    pipelines_resp = cp.list_platform_ai_pipelines() or {}
    workers = cp.list_workers() or []
    models = cp.list_models() or []
    api_keys = cp.list_keys() or []
    projects = cp.list_projects() or []
    bots = cp.list_bots() or []
    sessions_active = _as_list(sessions_active_resp.get("sessions"))
    sessions_archived = _as_list(sessions_archived_resp.get("sessions"))
    pipelines = _as_list(pipelines_resp.get("pipelines"))
    error = None
    if sessions_active_resp is None and sessions_archived_resp is None and pipelines_resp is None:
        error = "Control plane unavailable"
    return render_template(
        "platform_ai.html",
        sessions_active=sessions_active,
        sessions_archived=sessions_archived,
        pipelines=pipelines,
        workers=workers,
        models=models,
        api_keys=api_keys,
        projects=projects,
        bots=bots,
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
            projects=[],
            bots=[],
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
    projects = cp.list_projects() or []
    bots = cp.list_bots() or []
    return render_template(
        "platform_ai_session.html",
        session=session,
        messages=messages,
        events=events,
        context_files=_session_context_files(session),
        message_files=_session_message_files(session),
        pipeline=pipeline,
        suites=suites,
        suite_runs=suite_runs,
        projects=projects,
        bots=bots,
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
    archived = str(request.args.get("archived") or "active").strip() or "active"
    limit = _safe_int(request.args.get("limit"), 100, min_value=1, max_value=2000)
    data = cp.list_platform_ai_sessions(
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        mode=mode,
        archived=archived,
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


@bp.get("/api/platform-ai/sessions/<session_id>/export")
@login_required
def api_export_platform_ai_session(session_id: str):
    cp = get_cp_client()
    data = cp.export_platform_ai_session(session_id)
    if data is None:
        return _cp_error_response(cp, "failed to export platform ai session")
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


@bp.post("/api/platform-ai/sessions/<session_id>/context-files")
@login_required
def api_upload_platform_ai_context_files(session_id: str):
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return _cp_error_response(cp, "failed to load platform ai session")

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "at least one file is required"}), 400
    root = _upload_root()
    session_dir = root / secure_filename(str(session_id))
    session_dir.mkdir(parents=True, exist_ok=True)

    existing = _session_context_files(session)
    saved_rows: List[Dict[str, Any]] = []
    for file_storage in files:
        if file_storage is None:
            continue
        original_name = str(file_storage.filename or "").strip()
        safe_name = secure_filename(original_name) or f"file-{len(existing) + len(saved_rows) + 1}.bin"
        target = session_dir / safe_name
        suffix = 1
        while target.exists():
            stem = target.stem
            ext = target.suffix
            target = session_dir / f"{stem}({suffix}){ext}"
            suffix += 1
        file_storage.save(target)
        stat = target.stat()
        saved_rows.append(
            {
                "id": f"ctx-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{len(saved_rows)+1}",
                "name": original_name or target.name,
                "path": str(target),
                "size_bytes": int(stat.st_size),
                "content_type": str(file_storage.mimetype or "").strip() or None,
                "uploaded_at": _now_iso(),
            }
        )
    if not saved_rows:
        return jsonify({"error": "no files were saved"}), 400

    merged = existing + saved_rows
    patched = cp.patch_platform_ai_session(session_id, {"metadata": {"context_files": merged}})
    if patched is None:
        return _cp_error_response(cp, "failed to persist context files")
    cp.post_platform_ai_message(
        session_id,
        {
            "role": "operator",
            "content": f"Attached {len(saved_rows)} context file(s) for this session.",
            "metadata": {"source": "context_upload", "files": saved_rows},
        },
    )
    return jsonify({"session_id": session_id, "files": saved_rows, "total_files": len(merged)})


@bp.post("/api/platform-ai/sessions/<session_id>/message-files")
@login_required
def api_upload_platform_ai_message_files(session_id: str):
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return _cp_error_response(cp, "failed to load platform ai session")

    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "at least one file is required"}), 400
    root = _upload_root()
    session_dir = root / secure_filename(str(session_id)) / "messages"
    session_dir.mkdir(parents=True, exist_ok=True)

    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    existing = metadata.get("message_files") if isinstance(metadata.get("message_files"), list) else []
    saved_rows: List[Dict[str, Any]] = []
    for file_storage in files:
        if file_storage is None:
            continue
        original_name = str(file_storage.filename or "").strip()
        file_id = f"msg-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{len(saved_rows)+1}"
        safe_name = secure_filename(original_name) or f"{file_id}.bin"
        target = session_dir / safe_name
        suffix = 1
        while target.exists():
            stem = target.stem
            ext = target.suffix
            target = session_dir / f"{stem}({suffix}){ext}"
            suffix += 1
        file_storage.save(target)
        stat = target.stat()
        saved_rows.append(
            {
                "id": file_id,
                "name": original_name or target.name,
                "path": str(target),
                "size_bytes": int(stat.st_size),
                "content_type": str(file_storage.mimetype or "").strip() or None,
                "uploaded_at": _now_iso(),
                "url": f"/api/platform-ai/sessions/{secure_filename(str(session_id))}/files/{file_id}",
            }
        )
    if not saved_rows:
        return jsonify({"error": "no files were saved"}), 400

    merged = list(existing) + saved_rows
    patched = cp.patch_platform_ai_session(session_id, {"metadata": {"message_files": merged}})
    if patched is None:
        return _cp_error_response(cp, "failed to persist message files")
    return jsonify({"session_id": session_id, "files": saved_rows, "total_files": len(merged)})


@bp.get("/api/platform-ai/sessions/<session_id>/files/<file_id>")
@login_required
def api_get_platform_ai_session_file(session_id: str, file_id: str):
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return _cp_error_response(cp, "failed to load platform ai session")
    wanted = str(file_id or "").strip()
    if not wanted:
        abort(404)
    candidates = _session_context_files(session) + _session_message_files(session)
    match = next((row for row in candidates if str(row.get("id") or "").strip() == wanted), None)
    if match is None:
        abort(404)
    safe_path = _safe_session_file_path(session_id, str(match.get("path") or ""))
    if safe_path is None:
        abort(404)
    mimetype = str(match.get("content_type") or "").strip() or None
    return send_file(safe_path, mimetype=mimetype, as_attachment=False, download_name=str(match.get("name") or safe_path.name))


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


@bp.post("/api/platform-ai/sessions/<session_id>/bot-test-run")
@login_required
def api_run_platform_ai_bot_test(session_id: str):
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return _cp_error_response(cp, "failed to load platform ai session")

    body = request.get_json(silent=True) or {}
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    target_bot_id = str(body.get("target_bot_id") or metadata.get("target_bot_id") or "").strip()
    if not target_bot_id:
        return jsonify({"error": "target_bot_id is required for bot test runs"}), 400
    prompt = str(body.get("prompt") or "").strip() or f"Run isolated bot quality test for {target_bot_id}."
    suite_id = str(body.get("suite_id") or "").strip() or None
    operator_id = str(body.get("operator_id") or "").strip() or None
    wait_for_terminal = bool(body.get("wait_for_terminal", True))
    max_wait_seconds = max(1.0, float(body.get("max_wait_seconds") or 300.0))
    poll_interval_seconds = max(0.2, float(body.get("poll_interval_seconds") or 1.0))

    orchestration_id = str(uuid.uuid4())
    task = cp.create_task_full(
        target_bot_id,
        {"instruction": prompt},
        metadata={"source": "platform_ai_bot_test", "orchestration_id": orchestration_id, "target_bot_id": target_bot_id},
    )
    if task is None:
        return _cp_error_response(cp, "failed to launch isolated bot test task")

    terminal_statuses = {"completed", "failed", "cancelled", "canceled", "retried"}
    sampled_tasks: List[Dict[str, Any]] = []
    if wait_for_terminal:
        deadline = time.monotonic() + max_wait_seconds
        while True:
            listed = cp.list_tasks(orchestration_id=orchestration_id, limit=200) or []
            sampled_tasks = listed if isinstance(listed, list) else []
            if sampled_tasks and all(str(item.get("status") or "").strip().lower() in terminal_statuses for item in sampled_tasks):
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_seconds)

    patched = cp.patch_platform_ai_session(
        session_id,
        {
            "orchestration_id": orchestration_id,
            "target_bot_id": target_bot_id,
            "metadata": {
                "last_bot_test_orchestration_id": orchestration_id,
                "last_bot_test_task_id": task.get("id"),
                "target_bot_id": target_bot_id,
            },
        },
    )
    if patched is None:
        return _cp_error_response(cp, "failed to update session context after bot test launch")

    if not suite_id:
        designed = cp.design_platform_ai_quality_suite(
            session_id,
            {
                "name": f"{target_bot_id} Bot Quality Suite",
                "orchestration_id": orchestration_id,
                "include_default_tests": True,
                "metadata": {"source": "platform_ai_bot_test", "target_bot_id": target_bot_id},
            },
        )
        if designed is None:
            return _cp_error_response(cp, "failed to design bot quality suite")
        designed_suite = designed.get("suite") if isinstance(designed.get("suite"), dict) else {}
        suite_id = str(designed_suite.get("id") or "").strip()
        if not suite_id:
            return jsonify({"error": "bot quality suite design returned no suite id"}), 502

    run_payload: Dict[str, Any] = {
        "orchestration_id": orchestration_id,
        "wait_for_terminal": False,
        "metadata": {"source": "platform_ai_bot_test", "target_bot_id": target_bot_id},
    }
    if operator_id:
        run_payload["operator_id"] = operator_id
    run_result = cp.run_platform_ai_quality_suite(suite_id, run_payload)
    if run_result is None:
        return _cp_error_response(cp, "failed to run bot quality suite")

    return jsonify(
        {
            "session_id": session_id,
            "target_bot_id": target_bot_id,
            "orchestration_id": orchestration_id,
            "task": task,
            "sampled_tasks": sampled_tasks,
            "suite_id": suite_id,
            "suite_run": run_result,
        }
    )
