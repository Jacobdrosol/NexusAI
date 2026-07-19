from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from flask import Blueprint, Response, abort, jsonify, render_template, request, send_file, stream_with_context
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from dashboard.cp_client import get_cp_client
from dashboard.routes._sse_proxy import proxy_upstream_sse_lines


bp = Blueprint("platform_ai", __name__)
_UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024
_UPLOAD_REQUEST_OVERHEAD_BYTES = 8 * 1024 * 1024


class _UploadLimitExceeded(ValueError):
    """A Platform AI attachment exceeded a configured hard boundary."""


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


def _stream_cp_headers(cp) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    token = ""
    if hasattr(cp, "api_token"):
        token = str(getattr(cp, "api_token") or "").strip()
    if not token:
        token = (os.environ.get("CONTROL_PLANE_API_TOKEN", "") or "").strip()
    if token:
        headers["X-Nexus-API-Key"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _safe_int(value: Any, default: int, min_value: int = 1, max_value: int = 2000) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(min_value, min(max_value, parsed))


def _soft_threshold(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        parsed = int(raw)
    except Exception:
        return int(default)
    return parsed if parsed > 0 else int(default)


def _hard_upload_limit(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)


def _platform_ai_upload_limits() -> Dict[str, int]:
    return {
        "max_files": _hard_upload_limit("NEXUS_PLATFORM_AI_UPLOAD_MAX_FILES", 100),
        "max_file_bytes": _hard_upload_limit("NEXUS_PLATFORM_AI_UPLOAD_MAX_FILE_BYTES", 128 * 1024 * 1024),
        "max_total_bytes": _hard_upload_limit("NEXUS_PLATFORM_AI_UPLOAD_MAX_TOTAL_BYTES", 1024 * 1024 * 1024),
    }


def _stored_upload_bytes(rows: List[Dict[str, Any]]) -> int:
    total = 0
    for row in rows:
        try:
            total += max(0, int(row.get("size_bytes") or 0))
        except (AttributeError, TypeError, ValueError):
            continue
    return total


def _validate_upload_batch(
    *,
    existing_file_count: int,
    existing_total_bytes: int,
    submitted_file_count: int,
    declared_content_length: Optional[int],
    limits: Dict[str, int],
) -> None:
    if submitted_file_count < 1:
        raise _UploadLimitExceeded("at least one file is required")
    if existing_file_count + submitted_file_count > limits["max_files"]:
        raise _UploadLimitExceeded(
            f"attachment limit exceeded: at most {limits['max_files']} files are allowed per session"
        )
    if existing_total_bytes >= limits["max_total_bytes"]:
        raise _UploadLimitExceeded(
            f"attachment limit exceeded: session already uses the {limits['max_total_bytes']}-byte allowance"
        )
    remaining_total_bytes = limits["max_total_bytes"] - existing_total_bytes
    if declared_content_length is not None and declared_content_length > (
        remaining_total_bytes + _UPLOAD_REQUEST_OVERHEAD_BYTES
    ):
        raise _UploadLimitExceeded(
            "upload request exceeds the remaining session storage allowance"
        )


def _save_upload_limited(
    file_storage: Any,
    target: Path,
    *,
    max_file_bytes: int,
    max_total_remaining_bytes: int,
) -> int:
    """Stream one attachment to disk and remove it atomically on a cap breach."""
    if max_total_remaining_bytes <= 0:
        raise _UploadLimitExceeded("attachment limit exceeded: no session storage remains")
    written = 0
    try:
        with target.open("xb") as output:
            while True:
                chunk = file_storage.stream.read(_UPLOAD_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise _UploadLimitExceeded("attachment stream returned invalid data")
                written += len(chunk)
                if written > max_file_bytes:
                    raise _UploadLimitExceeded(
                        f"attachment exceeds the {max_file_bytes}-byte per-file limit"
                    )
                if written > max_total_remaining_bytes:
                    raise _UploadLimitExceeded("attachment exceeds the remaining session storage allowance")
                output.write(chunk)
        return written
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _cleanup_saved_uploads(rows: List[Dict[str, Any]]) -> None:
    for row in rows:
        path = str(row.get("path") or "").strip()
        if not path:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            continue


def _cleanup_empty_upload_dirs(session_dir: Path, root: Path) -> None:
    current = session_dir
    while current != root:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _upload_soft_warnings(*, file_count: int, total_bytes: int, label: str) -> List[str]:
    warnings: List[str] = []
    soft_max_files = _soft_threshold("NEXUS_PLATFORM_AI_UPLOAD_SOFT_MAX_FILES", 250)
    soft_max_bytes = _soft_threshold("NEXUS_PLATFORM_AI_UPLOAD_SOFT_MAX_BYTES", 5 * 1024 * 1024 * 1024)
    if file_count > soft_max_files:
        warnings.append(
            f"{label} library now has {file_count} files, above soft threshold {soft_max_files}. "
            "Uploads remain allowed; consider pruning older files."
        )
    if total_bytes > soft_max_bytes:
        warnings.append(
            f"{label} library now uses {total_bytes} bytes, above soft threshold {soft_max_bytes}. "
            "Uploads remain allowed; consider reducing pack size."
        )
    return warnings


def _as_list(value: Any) -> List[Dict[str, Any]]:
    return value if isinstance(value, list) else []


def _body_with_authenticated_operator(body: Any) -> Dict[str, Any]:
    """Use the dashboard login identity instead of client-supplied attribution."""
    result = dict(body) if isinstance(body, dict) else {}
    try:
        identity = str(current_user.get_id() or "").strip()
    except Exception:
        identity = ""
    if identity:
        result["operator_id"] = identity[:255]
    else:
        result.pop("operator_id", None)
    return result


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
                "relative_path": str(row.get("relative_path") or "").strip() or None,
                "path": path,
                "size_bytes": int(row.get("size_bytes") or 0),
                "content_type": str(row.get("content_type") or "").strip() or None,
                "uploaded_at": str(row.get("uploaded_at") or "").strip() or None,
                "url": f"/api/platform-ai/sessions/{secure_filename(str(session.get('id') or ''))}/files/{str(row.get('id') or '').strip()}",
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
                "relative_path": str(row.get("relative_path") or "").strip() or None,
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


def _sanitize_relative_path(raw: str) -> str:
    parts = [secure_filename(part) for part in str(raw or "").replace("\\", "/").split("/") if str(part).strip()]
    parts = [part for part in parts if part not in {"", ".", ".."}]
    return "/".join(parts)


@bp.get("/platform-ai")
@login_required
def platform_ai_page() -> str:
    cp = get_cp_client()
    sessions_active_resp = cp.list_platform_ai_sessions(limit=300, archived="active") or {}
    sessions_archived_resp = cp.list_platform_ai_sessions(limit=300, archived="archived") or {}
    pipelines_resp = cp.list_platform_ai_pipelines() or {}
    capabilities = cp.get_platform_ai_capabilities() or {}
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
        capabilities=capabilities,
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
            proposals=[],
            pipeline=None,
            suites=[],
            suite_runs=[],
            projects=[],
            bots=[],
            workers=[],
            error="Platform AI session not found or control plane unavailable.",
            active_page="platform_ai",
        )

    messages_resp = cp.list_platform_ai_messages(session_id, limit=400) or {}
    events_resp = cp.list_platform_ai_events(session_id, limit=600) or {}
    proposals_resp = cp.list_platform_ai_proposals(session_id, limit=100) or {}
    messages = _as_list(messages_resp.get("messages"))
    events = _as_list(events_resp.get("events"))
    proposals = _as_list(proposals_resp.get("proposals"))

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
    workers = cp.list_workers() or []
    return render_template(
        "platform_ai_session.html",
        session=session,
        messages=messages,
        events=events,
        proposals=proposals,
        context_files=_session_context_files(session),
        message_files=_session_message_files(session),
        pipeline=pipeline,
        suites=suites,
        suite_runs=suite_runs,
        projects=projects,
        bots=bots,
        workers=workers,
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
    body = _body_with_authenticated_operator(request.get_json(silent=True) or {})
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
    body = _body_with_authenticated_operator(request.get_json(silent=True) or {})
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


@bp.get("/api/platform-ai/sessions/<session_id>/proposals")
@login_required
def api_list_platform_ai_session_proposals(session_id: str):
    cp = get_cp_client()
    limit = _safe_int(request.args.get("limit"), 100, min_value=1, max_value=2000)
    data = cp.list_platform_ai_proposals(session_id, limit=limit)
    if data is None:
        return _cp_error_response(cp, "failed to list platform ai proposals")
    return jsonify(data)


@bp.post("/api/platform-ai/sessions/<session_id>/proposals/<proposal_id>/approve")
@login_required
def api_approve_platform_ai_session_proposal(session_id: str, proposal_id: str):
    cp = get_cp_client()
    body = _body_with_authenticated_operator(request.get_json(silent=True) or {})
    data = cp.approve_platform_ai_proposal(session_id, proposal_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to approve platform ai proposal")
    return jsonify(data)


@bp.post("/api/platform-ai/sessions/<session_id>/proposals/<proposal_id>/preflight")
@login_required
def api_preflight_platform_ai_session_proposal(session_id: str, proposal_id: str):
    cp = get_cp_client()
    body = _body_with_authenticated_operator(request.get_json(silent=True) or {})
    data = cp.preflight_platform_ai_proposal(session_id, proposal_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to preflight platform ai proposal")
    return jsonify(data)


@bp.post("/api/platform-ai/sessions/<session_id>/proposals/<proposal_id>/reject")
@login_required
def api_reject_platform_ai_session_proposal(session_id: str, proposal_id: str):
    cp = get_cp_client()
    body = _body_with_authenticated_operator(request.get_json(silent=True) or {})
    data = cp.reject_platform_ai_proposal(session_id, proposal_id, body)
    if data is None:
        return _cp_error_response(cp, "failed to reject platform ai proposal")
    return jsonify(data)


@bp.get("/api/platform-ai/sessions/<session_id>/messages/stream")
@login_required
def api_stream_platform_ai_session_messages(session_id: str):
    cp = get_cp_client()
    cp_base = cp.base_url if hasattr(cp, "base_url") else os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
    safe_session_id = requests.utils.quote(str(session_id), safe="")
    since = str(request.args.get("since") or "").strip()
    stream_url = f"{cp_base.rstrip('/')}/v1/platform-ai/sessions/{safe_session_id}/messages/stream"
    if since:
        stream_url += f"?since={requests.utils.quote(since, safe='')}"

    heartbeat_seconds = os.environ.get("PLATFORM_AI_STREAM_HEARTBEAT_SECONDS", "15")

    def _open_upstream():
        return requests.get(
            stream_url,
            headers=_stream_cp_headers(cp),
            stream=True,
            timeout=(10, None),
        )

    def generate():
        yield from proxy_upstream_sse_lines(_open_upstream, heartbeat_seconds=heartbeat_seconds)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


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

    existing = _session_context_files(session)
    existing_total_bytes = _stored_upload_bytes(existing)
    limits = _platform_ai_upload_limits()
    try:
        _validate_upload_batch(
            existing_file_count=len(existing),
            existing_total_bytes=existing_total_bytes,
            submitted_file_count=1,
            declared_content_length=request.content_length,
            limits=limits,
        )
    except _UploadLimitExceeded as exc:
        return jsonify({"error": str(exc)}), 413
    files = request.files.getlist("files")
    relative_paths = request.form.getlist("relative_paths")
    if not files:
        return jsonify({"error": "at least one file is required"}), 400
    root = _upload_root()
    session_dir = root / secure_filename(str(session_id))
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        _validate_upload_batch(
            existing_file_count=len(existing),
            existing_total_bytes=existing_total_bytes,
            submitted_file_count=len(files),
            declared_content_length=None,
            limits=limits,
        )
    except _UploadLimitExceeded as exc:
        return jsonify({"error": str(exc)}), 413
    saved_rows: List[Dict[str, Any]] = []
    try:
        for idx, file_storage in enumerate(files):
            if file_storage is None:
                continue
            original_name = str(file_storage.filename or "").strip()
            rel_hint = _sanitize_relative_path(relative_paths[idx] if idx < len(relative_paths) else "")
            safe_name = secure_filename(Path(rel_hint or original_name).name) or f"file-{len(existing) + len(saved_rows) + 1}.bin"
            rel_parent = _sanitize_relative_path(str(Path(rel_hint).parent)) if rel_hint else ""
            target_dir = session_dir / rel_parent if rel_parent else session_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / safe_name
            suffix = 1
            while target.exists():
                stem = target.stem
                ext = target.suffix
                target = target_dir / f"{stem}({suffix}){ext}"
                suffix += 1
            size_bytes = _save_upload_limited(
                file_storage,
                target,
                max_file_bytes=limits["max_file_bytes"],
                max_total_remaining_bytes=limits["max_total_bytes"] - existing_total_bytes - _stored_upload_bytes(saved_rows),
            )
            file_id = f"ctx-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{len(saved_rows)+1}"
            saved_rows.append(
                {
                    "id": file_id,
                    "name": original_name or target.name,
                    "relative_path": rel_hint or None,
                    "path": str(target),
                    "size_bytes": size_bytes,
                    "content_type": str(file_storage.mimetype or "").strip() or None,
                    "uploaded_at": _now_iso(),
                    "url": f"/api/platform-ai/sessions/{secure_filename(str(session_id))}/files/{file_id}",
                }
            )
    except _UploadLimitExceeded as exc:
        _cleanup_saved_uploads(saved_rows)
        _cleanup_empty_upload_dirs(session_dir, root)
        return jsonify({"error": str(exc)}), 413
    except Exception:
        _cleanup_saved_uploads(saved_rows)
        _cleanup_empty_upload_dirs(session_dir, root)
        raise
    if not saved_rows:
        return jsonify({"error": "no files were saved"}), 400

    merged = existing + saved_rows
    patched = cp.patch_platform_ai_session(session_id, {"metadata": {"context_files": merged}})
    if patched is None:
        return _cp_error_response(cp, "failed to persist context files")
    total_bytes = sum(int(row.get("size_bytes") or 0) for row in merged if isinstance(row, dict))
    warnings = _upload_soft_warnings(file_count=len(merged), total_bytes=total_bytes, label="Context")
    cp.post_platform_ai_message(
        session_id,
        {
            "role": "operator",
            "content": f"Attached {len(saved_rows)} context file(s) for this session.",
            "metadata": {"source": "context_upload", "files": saved_rows},
        },
    )
    return jsonify(
        {
            "session_id": session_id,
            "files": saved_rows,
            "total_files": len(merged),
            "total_bytes": total_bytes,
            "warnings": warnings,
        }
    )


@bp.post("/api/platform-ai/sessions/<session_id>/message-files")
@login_required
def api_upload_platform_ai_message_files(session_id: str):
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return _cp_error_response(cp, "failed to load platform ai session")

    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    existing = metadata.get("message_files") if isinstance(metadata.get("message_files"), list) else []
    existing_rows = [row for row in existing if isinstance(row, dict)]
    existing_total_bytes = _stored_upload_bytes(existing_rows)
    limits = _platform_ai_upload_limits()
    try:
        _validate_upload_batch(
            existing_file_count=len(existing_rows),
            existing_total_bytes=existing_total_bytes,
            submitted_file_count=1,
            declared_content_length=request.content_length,
            limits=limits,
        )
    except _UploadLimitExceeded as exc:
        return jsonify({"error": str(exc)}), 413
    files = request.files.getlist("files")
    relative_paths = request.form.getlist("relative_paths")
    if not files:
        return jsonify({"error": "at least one file is required"}), 400
    root = _upload_root()
    session_dir = root / secure_filename(str(session_id)) / "messages"
    session_dir.mkdir(parents=True, exist_ok=True)

    try:
        _validate_upload_batch(
            existing_file_count=len(existing_rows),
            existing_total_bytes=existing_total_bytes,
            submitted_file_count=len(files),
            declared_content_length=None,
            limits=limits,
        )
    except _UploadLimitExceeded as exc:
        return jsonify({"error": str(exc)}), 413
    saved_rows: List[Dict[str, Any]] = []
    try:
        for idx, file_storage in enumerate(files):
            if file_storage is None:
                continue
            original_name = str(file_storage.filename or "").strip()
            file_id = f"msg-{int(datetime.now(timezone.utc).timestamp() * 1000)}-{len(saved_rows)+1}"
            rel_hint = _sanitize_relative_path(relative_paths[idx] if idx < len(relative_paths) else "")
            safe_name = secure_filename(Path(rel_hint or original_name).name) or f"{file_id}.bin"
            rel_parent = _sanitize_relative_path(str(Path(rel_hint).parent)) if rel_hint else ""
            target_dir = session_dir / rel_parent if rel_parent else session_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / safe_name
            suffix = 1
            while target.exists():
                stem = target.stem
                ext = target.suffix
                target = target_dir / f"{stem}({suffix}){ext}"
                suffix += 1
            size_bytes = _save_upload_limited(
                file_storage,
                target,
                max_file_bytes=limits["max_file_bytes"],
                max_total_remaining_bytes=limits["max_total_bytes"] - existing_total_bytes - _stored_upload_bytes(saved_rows),
            )
            saved_rows.append(
                {
                    "id": file_id,
                    "name": original_name or target.name,
                    "relative_path": rel_hint or None,
                    "path": str(target),
                    "size_bytes": size_bytes,
                    "content_type": str(file_storage.mimetype or "").strip() or None,
                    "uploaded_at": _now_iso(),
                    "url": f"/api/platform-ai/sessions/{secure_filename(str(session_id))}/files/{file_id}",
                }
            )
    except _UploadLimitExceeded as exc:
        _cleanup_saved_uploads(saved_rows)
        _cleanup_empty_upload_dirs(session_dir, root)
        return jsonify({"error": str(exc)}), 413
    except Exception:
        _cleanup_saved_uploads(saved_rows)
        _cleanup_empty_upload_dirs(session_dir, root)
        raise
    if not saved_rows:
        return jsonify({"error": "no files were saved"}), 400

    merged = list(existing) + saved_rows
    patched = cp.patch_platform_ai_session(session_id, {"metadata": {"message_files": merged}})
    if patched is None:
        return _cp_error_response(cp, "failed to persist message files")
    total_bytes = sum(int(row.get("size_bytes") or 0) for row in merged if isinstance(row, dict))
    warnings = _upload_soft_warnings(file_count=len(merged), total_bytes=total_bytes, label="Message")
    return jsonify(
        {
            "session_id": session_id,
            "files": saved_rows,
            "total_files": len(merged),
            "total_bytes": total_bytes,
            "warnings": warnings,
        }
    )


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


@bp.delete("/api/platform-ai/sessions/<session_id>/files/<file_id>")
@login_required
def api_delete_platform_ai_session_file(session_id: str, file_id: str):
    cp = get_cp_client()
    session = cp.get_platform_ai_session(session_id)
    if session is None:
        return _cp_error_response(cp, "failed to load platform ai session")
    safe_file_id = str(file_id or "").strip()
    if not safe_file_id:
        return jsonify({"error": "file_id required"}), 400

    context_rows = _session_context_files(session)
    message_rows = _session_message_files(session)
    target_row = next((row for row in context_rows if str(row.get("id") or "") == safe_file_id), None)
    target_key = "context_files"
    if target_row is None:
        target_row = next((row for row in message_rows if str(row.get("id") or "") == safe_file_id), None)
        target_key = "message_files"
    if target_row is None:
        return jsonify({"error": "file not found"}), 404

    safe_path = _safe_session_file_path(session_id, str(target_row.get("path") or ""))
    if safe_path is not None:
        try:
            safe_path.unlink(missing_ok=True)
        except Exception:
            pass

    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    rows = metadata.get(target_key) if isinstance(metadata.get(target_key), list) else []
    updated_rows = [row for row in rows if isinstance(row, dict) and str(row.get("id") or "").strip() != safe_file_id]
    patched = cp.patch_platform_ai_session(session_id, {"metadata": {target_key: updated_rows}})
    if patched is None:
        return _cp_error_response(cp, "failed to persist file deletion")
    return jsonify({"session_id": session_id, "deleted_file_id": safe_file_id, "file_type": target_key})


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
