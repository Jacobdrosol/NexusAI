"""Chat dashboard page and proxy endpoints."""
from __future__ import annotations

import os
import json
import logging
from typing import Any, Dict, Iterable

import requests
from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("chat", __name__)


def _task_sort_key(task: dict[str, Any]) -> tuple[int, int, str, str]:
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    try:
        step_number = int(payload.get("step_number") or 0)
    except Exception:
        step_number = 0
    try:
        trigger_depth = int(metadata.get("trigger_depth") or 0)
    except Exception:
        trigger_depth = 0
    return (step_number, trigger_depth, str(task.get("created_at") or ""), str(task.get("updated_at") or ""))


def _task_output_text(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("output", "content", "text", "result"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
        try:
            return json.dumps(result, indent=2, sort_keys=True, default=str)
        except Exception:
            return str(result)
    if result is None:
        return ""
    return str(result)


def _task_truncation_note(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    finish_reason = str(result.get("finish_reason") or "").strip().lower()
    if finish_reason in {"length", "max_tokens", "max_output_tokens", "token_limit", "max_new_tokens"}:
        return "Model output likely hit token limit and may be incomplete."
    usage = result.get("usage")
    if isinstance(usage, dict):
        try:
            if int(usage.get("completion_tokens") or 0) >= 4096:
                return "Model output may be truncated (completion_tokens reached 4096)."
        except Exception:
            return ""
    return ""


def _assignment_full_recap(orchestration_id: str, tasks: list[dict[str, Any]]) -> str:
    ordered = sorted([task for task in tasks if isinstance(task, dict)], key=_task_sort_key)
    lines: list[str] = [
        f"Assignment Full Recap ({len(ordered)} tasks):",
        f"Orchestration ID: {orchestration_id}",
        "",
    ]
    for task in ordered:
        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        bot_id = str(task.get("bot_id") or "unknown")
        workstream = str(payload.get("workstream") or payload.get("title") or "").strip()
        title = workstream or bot_id
        step_number = payload.get("step_number")
        step_count = payload.get("step_count")
        status = str(task.get("status") or "unknown")
        source = str(metadata.get("source") or "").strip()
        trigger_depth = metadata.get("trigger_depth")
        if step_number and step_count:
            step_label = f"Step {step_number}/{step_count}"
        elif trigger_depth is not None:
            step_label = f"Trigger depth {trigger_depth}"
        else:
            step_label = "Task"
        lines.extend(
            [
                f"{step_label}: {title}",
                f"- Status: {status}",
                f"- Bot: {bot_id}",
            ]
        )
        if source:
            lines.append(f"- Source: {source}")
        deliverables = payload.get("deliverables") if isinstance(payload.get("deliverables"), list) else []
        if deliverables:
            lines.append("- Deliverables:")
            for item in deliverables:
                lines.append(f"  - {item}")
        output = _task_output_text(task.get("result"))
        if output:
            lines.append("- Full Output:")
            lines.append(output)
        error = task.get("error")
        if isinstance(error, dict) and error.get("message"):
            lines.append(f"- Error: {error.get('message')}")
        note = _task_truncation_note(task.get("result"))
        if note:
            lines.append(f"- Note: {note}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"
logger = logging.getLogger(__name__)


def _is_failed_pm_run_message(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if str(metadata.get("mode") or "").strip() not in {"pm_run_report", "assign_summary"}:
        return False
    run_status = str(metadata.get("run_status") or "").strip().lower()
    ingest_allowed = metadata.get("ingest_allowed")
    return run_status == "failed" or ingest_allowed is False


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


def _stream_cp_headers(cp) -> dict[str, str]:
    headers: dict[str, str] = {}
    token = ""
    if hasattr(cp, "api_token"):
        token = str(getattr(cp, "api_token") or "").strip()
    if not token:
        token = (os.environ.get("CONTROL_PLANE_API_TOKEN", "") or "").strip()
    if token:
        headers["X-Nexus-API-Key"] = token
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, raw in value.items():
            safe[str(key)] = _json_safe(raw)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _normalize_bridge_project_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = [str(item or "").strip() for item in raw]
        return [value for value in values if value]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        # Legacy rows may store JSON text or a single project id.
        try:
            parsed = json.loads(text)
        except Exception:
            return [text]
        if isinstance(parsed, (list, tuple, set)):
            values = [str(item or "").strip() for item in parsed]
            return [value for value in values if value]
        if isinstance(parsed, str):
            value = parsed.strip()
            return [value] if value else []
        return []
    return []


def _normalize_conversation_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cid = str(raw.get("id") or "").strip()
    if not cid:
        return None
    normalized = dict(raw)
    normalized["id"] = cid
    title = str(raw.get("title") or "").strip()
    normalized["title"] = title or cid
    normalized["project_id"] = str(raw.get("project_id") or "").strip() or None
    normalized["scope"] = str(raw.get("scope") or "").strip() or "global"
    normalized["default_bot_id"] = str(raw.get("default_bot_id") or "").strip() or None
    normalized["default_model_id"] = str(raw.get("default_model_id") or "").strip() or None
    normalized["created_at"] = str(raw.get("created_at") or "").strip() or None
    normalized["updated_at"] = str(raw.get("updated_at") or "").strip() or None
    normalized["archived_at"] = str(raw.get("archived_at") or "").strip() or None
    normalized["bridge_project_ids"] = _normalize_bridge_project_ids(raw.get("bridge_project_ids"))
    normalized["tool_access_enabled"] = bool(raw.get("tool_access_enabled") or False)
    normalized["tool_access_filesystem"] = bool(raw.get("tool_access_filesystem") or False)
    normalized["tool_access_repo_search"] = bool(raw.get("tool_access_repo_search") or False)
    return normalized


def _normalize_conversation_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_conversation_row(row)
        if normalized is not None:
            result.append(normalized)
    return result


def _normalize_message_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    mid = str(raw.get("id") or "").strip()
    if not mid:
        return None
    normalized: dict[str, Any] = {
        "id": mid,
        "role": str(raw.get("role") or "").strip() or "assistant",
        "content": str(raw.get("content") or ""),
        "created_at": str(raw.get("created_at") or "").strip() or None,
        "metadata": None,
    }
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        normalized["metadata"] = _json_safe(metadata)
    elif isinstance(metadata, str):
        try:
            parsed = json.loads(metadata)
            normalized["metadata"] = _json_safe(parsed) if isinstance(parsed, dict) else None
        except Exception:
            normalized["metadata"] = None
    return normalized


def _normalize_message_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        normalized = _normalize_message_row(row)
        if normalized is not None:
            result.append(normalized)
    return result


def _normalize_vault_item_row(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    item_id = str(raw.get("id") or "").strip()
    if not item_id:
        return None
    metadata = raw.get("metadata")
    return {
        "id": item_id,
        "title": str(raw.get("title") or item_id).strip() or item_id,
        "namespace": str(raw.get("namespace") or "").strip() or None,
        "project_id": str(raw.get("project_id") or "").strip() or None,
        "content": str(raw.get("content") or ""),
        "created_at": str(raw.get("created_at") or "").strip() or None,
        "updated_at": str(raw.get("updated_at") or "").strip() or None,
        "metadata": _json_safe(metadata) if isinstance(metadata, dict) else None,
    }


def _normalize_vault_item_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("items", "results", "data"):
            candidate = raw.get(key)
            if isinstance(candidate, list):
                raw = candidate
                break
        else:
            return []
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for row in raw:
        normalized = _normalize_vault_item_row(row)
        if normalized is not None:
            result.append(normalized)
    return result


def _cp_list_messages_safe(cp: Any, conversation_id: str, *, limit: int) -> Any:
    try:
        return cp.list_messages(conversation_id, limit=limit)
    except TypeError:
        return cp.list_messages(conversation_id)


def _cp_list_vault_items_safe(
    cp: Any,
    *,
    namespace: str | None = None,
    project_id: str | None = None,
    limit: int = 100,
    include_content: bool = True,
) -> Any:
    try:
        return cp.list_vault_items(
            namespace=namespace,
            project_id=project_id,
            limit=limit,
            include_content=include_content,
        )
    except TypeError:
        return cp.list_vault_items(namespace=namespace, project_id=project_id, limit=limit)


def _cp_list_tasks_safe(cp: Any, **kwargs) -> Any:
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


@bp.get("/chat")
@login_required
def chat_page() -> str:
    cp = get_cp_client()
    page_error: str | None = None
    try:
        try:
            conversations = _normalize_conversation_rows(cp.list_conversations(archived="all") or [])
        except Exception:
            conversations = []
            page_error = "Conversation list is temporarily unavailable."

        try:
            bots = cp.list_bots() or []
        except Exception:
            bots = []

        try:
            projects = cp.list_projects() or []
        except Exception:
            projects = []

        selected_id = str(request.args.get("conversation_id") or "").strip()
        selected = None
        messages: list[dict[str, Any]] = []
        repo_context_items: list[dict[str, Any]] = []
        repo_context_sections: list[dict[str, Any]] = []
        repo_context_item_ids: list[str] = []
        if selected_id:
            for c in conversations:
                if c.get("id") == selected_id:
                    selected = c
                    break
            try:
                messages = _normalize_message_rows(_cp_list_messages_safe(cp, selected_id, limit=120) or [])
            except Exception:
                messages = []
                page_error = page_error or "Selected conversation messages could not be loaded."

        if selected:
            project_ids: list[str] = []
            project_id = str(selected.get("project_id") or "").strip()
            if project_id:
                project_ids.append(project_id)
            for bridged in selected.get("bridge_project_ids") or []:
                value = str(bridged or "").strip()
                if value and value not in project_ids:
                    project_ids.append(value)

            for pid in project_ids:
                namespace = f"project:{pid}:repo"
                if hasattr(cp, "get_project_github_context_sync_status"):
                    try:
                        status = cp.get_project_github_context_sync_status(pid) or {}
                        if isinstance(status, dict):
                            context_sync = status.get("context_sync") if isinstance(status.get("context_sync"), dict) else {}
                            ns = str(context_sync.get("namespace") or "").strip()
                            if ns:
                                namespace = ns
                    except Exception:
                        namespace = f"project:{pid}:repo"
                try:
                    items_raw = _cp_list_vault_items_safe(
                        cp,
                        namespace=namespace,
                        project_id=pid,
                        limit=30,
                        include_content=False,
                    ) or []
                except Exception:
                    items_raw = []
                items = _normalize_vault_item_rows(items_raw)
                if items:
                    repo_context_sections.append(
                        {
                            "project_id": pid,
                            "namespace": namespace,
                            "items": items,
                        }
                    )
                    repo_context_items.extend(items)
                    for item in items:
                        item_id = str(item.get("id") or "").strip()
                        if item_id and item_id not in repo_context_item_ids:
                            repo_context_item_ids.append(item_id)

        try:
            vault_items_raw = _cp_list_vault_items_safe(cp, limit=30, include_content=False) or []
        except Exception:
            vault_items_raw = []
        vault_items = _normalize_vault_item_rows(vault_items_raw)

        return render_template(
            "chat.html",
            conversations=[c for c in conversations if not c.get("archived_at")],
            archived_conversations=[c for c in conversations if c.get("archived_at")],
            selected_conversation=selected,
            messages=messages,
            bots=bots,
            projects=projects,
            vault_items=vault_items,
            repo_context_items=repo_context_items,
            repo_context_sections=repo_context_sections,
            repo_context_item_ids=repo_context_item_ids,
            error=page_error,
        )
    except Exception:
        logger.exception(
            "chat_page failed unexpectedly",
            extra={"conversation_id": str(request.args.get("conversation_id") or "").strip() or None},
        )
        return render_template(
            "chat.html",
            conversations=[],
            archived_conversations=[],
            selected_conversation=None,
            messages=[],
            bots=[],
            projects=[],
            vault_items=[],
            repo_context_items=[],
            repo_context_sections=[],
            repo_context_item_ids=[],
            error="Chat view is temporarily unavailable. Start a new chat or refresh.",
        )


@bp.post("/api/chat/conversations")
@login_required
def api_create_conversation():
    data: dict[str, Any] = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    cp = get_cp_client()
    created = cp.create_conversation(
        {
            "title": title,
            "project_id": data.get("project_id"),
            "bridge_project_ids": data.get("bridge_project_ids") or [],
            "scope": data.get("scope", "global"),
            "default_bot_id": data.get("default_bot_id"),
            "default_model_id": data.get("default_model_id"),
            "tool_access_enabled": bool(data.get("tool_access_enabled", False)),
            "tool_access_filesystem": bool(data.get("tool_access_filesystem", False)),
            "tool_access_repo_search": bool(data.get("tool_access_repo_search", False)),
        }
    )
    if created is None:
        return _cp_error_response(cp)
    return jsonify(created), 201


@bp.delete("/api/chat/conversations/<conversation_id>")
@login_required
def api_delete_conversation(conversation_id: str):
    cp = get_cp_client()
    ok = cp.delete_conversation(conversation_id)
    if not ok:
        return _cp_error_response(cp, "conversation delete failed")
    return "", 204


@bp.post("/api/chat/conversations/<conversation_id>/archive")
@login_required
def api_archive_conversation(conversation_id: str):
    cp = get_cp_client()
    archived = cp.archive_conversation(conversation_id)
    if archived is None:
        return _cp_error_response(cp, "conversation archive failed")
    return jsonify(archived)


@bp.post("/api/chat/conversations/<conversation_id>/restore")
@login_required
def api_restore_conversation(conversation_id: str):
    cp = get_cp_client()
    restored = cp.restore_conversation(conversation_id)
    if restored is None:
        return _cp_error_response(cp, "conversation restore failed")
    return jsonify(restored)


@bp.put("/api/chat/conversations/<conversation_id>/tool-access")
@login_required
def api_update_conversation_tool_access(conversation_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    updated = cp.update_conversation_tool_access(
        conversation_id=conversation_id,
        enabled=bool(data.get("enabled", False)),
        filesystem=bool(data.get("filesystem", False)),
        repo_search=bool(data.get("repo_search", False)),
    )
    if updated is None:
        return _cp_error_response(cp, "conversation tool access update failed")
    return jsonify(updated)


@bp.post("/api/chat/messages")
@login_required
def api_send_message():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    content = (data.get("content") or "").strip()
    if not conversation_id or not content:
        return jsonify({"error": "conversation_id and content are required"}), 400
    cp = get_cp_client()

    resp = cp.post_message(
        conversation_id,
        {
            "content": content,
            "bot_id": data.get("bot_id"),
            "context_items": data.get("context_items"),
            "context_item_ids": data.get("context_item_ids"),
            "include_project_context": data.get("include_project_context", False),
            "use_workspace_tools": data.get("use_workspace_tools", False),
        },
    )
    if resp is None:
        return _cp_error_response(cp, "chat message failed")
    return jsonify(resp)


@bp.post("/api/chat/assignments/apply")
@login_required
def api_apply_assignment_files():
    data: dict[str, Any] = request.get_json(force=True) or {}
    orchestration_id = (data.get("orchestration_id") or "").strip()
    project_id = (data.get("project_id") or "").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required"}), 400
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400

    cp = get_cp_client()
    result = cp.apply_project_assignment_to_repo_workspace(
        project_id=project_id,
        orchestration_id=orchestration_id,
        overwrite=bool(data.get("overwrite", True)),
    )
    if result is None:
        return _cp_error_response(cp, "assignment apply failed")
    return jsonify(result)


@bp.get("/api/chat/conversations/<conversation_id>/messages")
@login_required
def api_list_messages(conversation_id: str):
    cp = get_cp_client()
    raw_limit = request.args.get("limit", "120")
    try:
        limit = max(1, min(int(raw_limit), 1000))
    except Exception:
        limit = 120
    messages = _cp_list_messages_safe(cp, conversation_id, limit=limit)
    if messages is None:
        return _cp_error_response(cp, "chat messages unavailable")
    return jsonify(messages)


@bp.post("/api/chat/stream")
@login_required
def api_send_message_stream():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    content = (data.get("content") or "").strip()
    if not conversation_id or not content:
        return jsonify({"error": "conversation_id and content are required"}), 400

    cp = get_cp_client()
    cp_base = (
        cp.base_url
        if hasattr(cp, "base_url")
        else os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
    )
    stream_url = f"{cp_base.rstrip('/')}/v1/chat/conversations/{conversation_id}/stream"
    payload = {
        "content": content,
        "bot_id": data.get("bot_id"),
        "context_items": data.get("context_items"),
        "context_item_ids": data.get("context_item_ids"),
        "include_project_context": data.get("include_project_context", False),
        "use_workspace_tools": data.get("use_workspace_tools", False),
    }

    def generate() -> Iterable[str]:
        try:
            with requests.post(
                stream_url,
                json=payload,
                headers=_stream_cp_headers(cp),
                stream=True,
                timeout=(10, None),
            ) as upstream:
                upstream.raise_for_status()
                for line in upstream.iter_lines(decode_unicode=True):
                    if line is None:
                        continue
                    yield f"{line}\n"
        except Exception as e:
            escaped = str(e).replace('"', '\\"')
            yield "event: error\n"
            yield f'data: {{"error": "{escaped}"}}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@bp.post("/api/chat/ingest")
@login_required
def api_ingest_chat():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    namespace = (data.get("namespace") or "global").strip() or "global"
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400

    cp = get_cp_client()
    conversation = None
    for c in (cp.list_conversations() or []):
        if c.get("id") == conversation_id:
            conversation = c
            break
    messages = cp.list_messages(conversation_id)
    if not conversation or messages is None:
        return jsonify({"error": "conversation or messages unavailable"}), 502

    lines = []
    for m in messages:
        if isinstance(m, dict) and _is_failed_pm_run_message(m):
            continue
        lines.append(f"[{m.get('role', 'unknown')}] {m.get('content', '')}")
    content = "\n\n".join(lines)
    title = f"Chat: {conversation.get('title', conversation_id)}"
    ingested = cp.ingest_vault_item(
        {
            "title": title,
            "content": content,
            "namespace": namespace,
            "source_type": "chat",
            "source_ref": conversation_id,
            "metadata": {"conversation_id": conversation_id},
        }
    )
    if ingested is None:
        return jsonify({"error": "vault ingestion failed"}), 502
    return jsonify(ingested), 201


@bp.post("/api/chat/message-to-vault")
@login_required
def api_ingest_message_to_vault():
    data: Dict[str, Any] = request.get_json(force=True) or {}
    message = data.get("message") or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    namespace = (data.get("namespace") or "global").strip() or "global"
    if not isinstance(message, dict):
        return jsonify({"error": "message object is required"}), 400
    if _is_failed_pm_run_message(message):
        return jsonify({"error": "failed PM run reports cannot be ingested into the vault"}), 400
    content = str(message.get("content") or "").strip()
    if not content:
        return jsonify({"error": "message content is required"}), 400

    title = f"Chat Message: {message.get('role', 'unknown')}"
    cp = get_cp_client()
    ingested = cp.ingest_vault_item(
        {
            "title": title,
            "content": content,
            "namespace": namespace,
            "source_type": "chat",
            "source_ref": message.get("id"),
            "metadata": {
                "conversation_id": conversation_id or None,
                "role": message.get("role"),
                "bot_id": message.get("bot_id"),
            },
        }
    )
    if ingested is None:
        return jsonify({"error": "vault ingestion failed"}), 502
    return jsonify(ingested), 201


@bp.post("/api/chat/orchestrations/<orchestration_id>/mark-failed")
@login_required
def api_mark_pm_run_failed(orchestration_id: str):
    data: Dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400
    cp = get_cp_client()
    updated = cp.mark_pm_run_failed(conversation_id, orchestration_id)
    if updated is None:
        return _cp_error_response(cp, fallback="failed to mark PM run as failed")
    return jsonify(updated)


@bp.get("/api/chat/orchestrations/<orchestration_id>/graph")
@login_required
def api_orchestration_graph(orchestration_id: str):
    cp = get_cp_client()
    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, include_content=False)
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502

    scoped_tasks = [task for task in tasks if isinstance(task, dict)]
    root_node_id = f"orchestrator::{orchestration_id}"
    has_explicit_orchestrator = any(str(task.get("bot_id") or "").strip() == "pm-orchestrator" for task in scoped_tasks)
    is_chat_assignment = any(
        str((task.get("metadata") or {}).get("source") or "").strip().lower() in {"chat_assign", "auto_retry", "bot_trigger"}
        for task in scoped_tasks
    )

    nodes = []
    edges = []
    if scoped_tasks and is_chat_assignment and not has_explicit_orchestrator:
        nodes.append(
            {
                "id": root_node_id,
                "title": "PM Orchestrator",
                "step_id": "pm-orchestrator",
                "status": "completed",
                "bot_id": "pm-orchestrator",
                "depends_on": [],
                "synthetic": True,
            }
        )

    for t in scoped_tasks:
        task_id = str(t.get("id"))
        metadata = t.get("metadata") or {}
        payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
        step_id = str(metadata.get("step_id") or "").strip()
        bot_id_label = str(t.get("bot_id") or "").strip()
        title = str(
            payload.get("title")
            or payload.get("workstream")
            or metadata.get("pipeline_name")
            or step_id
            or bot_id_label
            or task_id
        )
        depends_on = [str(dep) for dep in (t.get("depends_on") or []) if str(dep).strip()]
        source = str(metadata.get("source") or "").strip().lower()
        parent_task_id = str(metadata.get("parent_task_id") or "").strip()

        # For join-triggered tasks, the payload carries all sibling task IDs so the
        # DAG can show the true fan-in instead of a single-parent edge.
        join_task_ids = [str(jid) for jid in (payload.get("join_task_ids") or []) if str(jid).strip()]

        if not depends_on:
            if join_task_ids:
                # Show all joined sibling tasks as dependencies (fan-in)
                depends_on = join_task_ids
            elif source == "bot_trigger" and parent_task_id:
                depends_on = [parent_task_id]
            elif source in {"chat_assign", "auto_retry"} and is_chat_assignment and not has_explicit_orchestrator:
                depends_on = [root_node_id]

        nodes.append(
            {
                "id": task_id,
                "title": title,
                "step_id": step_id,
                "status": t.get("status"),
                "bot_id": bot_id_label,
                "depends_on": depends_on,
            }
        )
        for dep in depends_on:
            edges.append({"from": str(dep), "to": task_id})

    return jsonify({"orchestration_id": orchestration_id, "nodes": nodes, "edges": edges})


@bp.get("/api/chat/orchestrations/<orchestration_id>/recap")
@login_required
def api_orchestration_recap(orchestration_id: str):
    cp = get_cp_client()
    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, include_content=True)
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502
    recap = _assignment_full_recap(orchestration_id, tasks)
    return jsonify({"orchestration_id": orchestration_id, "task_count": len(tasks), "recap": recap})
