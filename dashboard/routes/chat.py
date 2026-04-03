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
from shared.chat_attachments import CHAT_ATTACHMENT_MAX_FILES, CHAT_ATTACHMENT_MAX_TOTAL_BYTES

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


def _humanize_bot_id(bot_id: str) -> str:
    raw = str(bot_id or "").strip()
    if not raw:
        return "Unknown Bot"
    cleaned = raw.replace("_", " ").replace("-", " ").strip()
    parts = [part for part in cleaned.split() if part]
    if not parts:
        return raw
    return " ".join(part.upper() if part.lower() in {"pm", "ui", "qc", "db"} else part.capitalize() for part in parts)


def _is_failed_pm_run_message(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if str(metadata.get("mode") or "").strip() not in {"pm_run_report", "assign_summary", "assign_pending"}:
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


def _cp_list_messages_safe(cp: Any, conversation_id: str, *, limit: int | None) -> Any:
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
        try:
            model_catalog = cp.list_models() or []
        except Exception:
            model_catalog = []

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
                messages = _normalize_message_rows(
                    _cp_list_messages_safe(cp, selected_id, limit=None) or []
                )
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
            model_catalog=model_catalog,
            chat_attachment_limits={
                "max_files": CHAT_ATTACHMENT_MAX_FILES,
                "max_total_bytes": CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
            },
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
            model_catalog=[],
            chat_attachment_limits={
                "max_files": CHAT_ATTACHMENT_MAX_FILES,
                "max_total_bytes": CHAT_ATTACHMENT_MAX_TOTAL_BYTES,
            },
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
    attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    if not conversation_id or (not content and not attachments):
        return jsonify({"error": "conversation_id and either content or attachments are required"}), 400
    cp = get_cp_client()

    resp = cp.post_message(
        conversation_id,
        {
            "content": content,
            "bot_id": data.get("bot_id"),
            "attachments": data.get("attachments") or [],
            "context_items": data.get("context_items"),
            "context_item_ids": data.get("context_item_ids"),
            "include_project_context": data.get("include_project_context", False),
            "use_workspace_tools": data.get("use_workspace_tools", False),
        },
    )
    if resp is None:
        return _cp_error_response(cp, "chat message failed")
    return jsonify(resp)


@bp.post("/api/chat/assignments/preview")
@login_required
def api_assignment_preview():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    instruction = str(data.get("instruction") or "").strip()
    pm_bot_id = str(data.get("pm_bot_id") or data.get("bot_id") or "").strip()
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400
    if not pm_bot_id:
        return jsonify({"error": "pm_bot_id is required"}), 400
    cp = get_cp_client()
    preview = cp.preview_assignment(
        {
            "conversation_id": conversation_id,
            "instruction": instruction,
            "pm_bot_id": pm_bot_id,
            "node_overrides": data.get("node_overrides") if isinstance(data.get("node_overrides"), dict) else {},
        }
    )
    if preview is None:
        return _cp_error_response(cp, "assignment preview failed")
    return jsonify(preview)


@bp.post("/api/chat/assignments")
@login_required
def api_create_assignment():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = str(data.get("conversation_id") or "").strip()
    instruction = str(data.get("instruction") or "").strip()
    pm_bot_id = str(data.get("pm_bot_id") or data.get("bot_id") or "").strip()
    if not conversation_id:
        return jsonify({"error": "conversation_id is required"}), 400
    if not instruction:
        return jsonify({"error": "instruction is required"}), 400
    if not pm_bot_id:
        return jsonify({"error": "pm_bot_id is required"}), 400
    cp = get_cp_client()
    created = cp.create_assignment(
        {
            "conversation_id": conversation_id,
            "instruction": instruction,
            "pm_bot_id": pm_bot_id,
            "run_id": data.get("run_id"),
            "node_overrides": data.get("node_overrides") if isinstance(data.get("node_overrides"), dict) else {},
            "context_items": data.get("context_items") if isinstance(data.get("context_items"), list) else [],
        }
    )
    if created is None:
        return _cp_error_response(cp, "assignment create failed")
    return jsonify(created)


@bp.get("/api/chat/assignments/<assignment_id>/graph")
@login_required
def api_assignment_graph(assignment_id: str):
    cp = get_cp_client()
    graph = cp.get_assignment_graph(assignment_id)
    if graph is None:
        return _cp_error_response(cp, "assignment graph failed")
    return jsonify(graph)


@bp.post("/api/chat/assignments/<assignment_id>/splice")
@login_required
def api_assignment_splice(assignment_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    from_node_id = str(data.get("from_node_id") or "").strip()
    if not from_node_id:
        return jsonify({"error": "from_node_id is required"}), 400
    cp = get_cp_client()
    result = cp.splice_assignment(
        assignment_id,
        {
            "from_node_id": from_node_id,
            "node_overrides": data.get("node_overrides") if isinstance(data.get("node_overrides"), dict) else {},
            "context_items": data.get("context_items") if isinstance(data.get("context_items"), list) else [],
        },
    )
    if result is None:
        return _cp_error_response(cp, "assignment splice failed")
    return jsonify(result)


@bp.post("/api/chat/assignments/<assignment_id>/nodes/<node_id>/rerun")
@login_required
def api_assignment_rerun_node(assignment_id: str, node_id: str):
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    result = cp.rerun_assignment_node(assignment_id, node_id, payload=data.get("payload"))
    if result is None:
        return _cp_error_response(cp, "assignment node rerun failed")
    return jsonify(result)


@bp.get("/api/chat/assignments/<assignment_id>/lineage")
@login_required
def api_assignment_lineage(assignment_id: str):
    cp = get_cp_client()
    result = cp.list_assignment_lineage(assignment_id)
    if result is None:
        return _cp_error_response(cp, "assignment lineage unavailable")
    return jsonify(result)


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


@bp.post("/api/chat/assignments/review")
@login_required
def api_review_assignment_files():
    data: dict[str, Any] = request.get_json(force=True) or {}
    orchestration_id = (data.get("orchestration_id") or "").strip()
    project_id = (data.get("project_id") or "").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required"}), 400
    if not project_id:
        return jsonify({"error": "project_id is required"}), 400

    cp = get_cp_client()
    result = cp.review_project_assignment_files(
        project_id=project_id,
        orchestration_id=orchestration_id,
        include_content=bool(data.get("include_content", True)),
        max_content_chars=int(data.get("max_content_chars", 20000) or 20000),
        diff_context_lines=int(data.get("diff_context_lines", 3) or 3),
    )
    if result is None:
        return _cp_error_response(cp, "assignment review failed")
    return jsonify(result)


@bp.get("/api/chat/conversations/<conversation_id>/messages")
@login_required
def api_list_messages(conversation_id: str):
    cp = get_cp_client()
    raw_limit = request.args.get("limit")
    try:
        limit = max(1, int(raw_limit)) if raw_limit is not None else None
    except Exception:
        limit = None
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
    attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    if not conversation_id or (not content and not attachments):
        return jsonify({"error": "conversation_id and either content or attachments are required"}), 400

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
        "attachments": data.get("attachments") or [],
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
                for line in upstream.iter_lines(decode_unicode=True, keepempty_lines=True):
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
    assignment_graph: Dict[str, Any] = {}
    if hasattr(cp, "get_assignment_graph_by_orchestration"):
        try:
            fetched = cp.get_assignment_graph_by_orchestration(orchestration_id)
        except Exception:
            fetched = None
        if isinstance(fetched, dict):
            assignment_graph = fetched

    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, include_content=False)
    if tasks is None:
        fallback_tasks = assignment_graph.get("tasks") if isinstance(assignment_graph.get("tasks"), list) else None
        if fallback_tasks is None:
            return jsonify({"error": "control plane unavailable"}), 502
        tasks = fallback_tasks

    scoped_tasks = [task for task in tasks if isinstance(task, dict)]
    assignment_id = str(assignment_graph.get("assignment_id") or "").strip()
    run_id = str(assignment_graph.get("run_id") or "").strip()
    run_state = str(assignment_graph.get("state") or "").strip()
    node_overrides = (
        assignment_graph.get("node_overrides")
        if isinstance(assignment_graph.get("node_overrides"), dict)
        else {}
    )
    task_by_id = {
        str(task.get("id") or "").strip(): task
        for task in scoped_tasks
        if str(task.get("id") or "").strip()
    }
    bot_cache: Dict[str, Dict[str, Any]] = {}
    bot_name_map: Dict[str, str] = {}
    reference_graph: Dict[str, Any] | None = None
    pipeline_entry_bot_id = ""

    def _bot_doc(bot_id: str) -> Dict[str, Any] | None:
        normalized = str(bot_id or "").strip()
        if not normalized:
            return None
        if normalized in bot_cache:
            return bot_cache[normalized]
        try:
            bot_doc = cp.get_bot(normalized)
        except Exception:
            bot_doc = None
        bot_cache[normalized] = bot_doc if isinstance(bot_doc, dict) else {}
        return bot_cache[normalized] or None

    def _metadata_value(task: Dict[str, Any], key: str) -> str:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        return str(metadata.get(key) or "").strip()

    root_candidates = []
    for task in scoped_tasks:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        source = str(metadata.get("source") or "").strip().lower()
        parent_task_id = str(metadata.get("parent_task_id") or "").strip()
        if source in {"chat_assign", "auto_retry"} or not parent_task_id:
            root_candidates.append(task)
    for task in root_candidates + scoped_tasks:
        for key in ("pipeline_entry_bot_id", "pm_bot_id", "root_pm_bot_id"):
            candidate = _metadata_value(task, key)
            if candidate:
                pipeline_entry_bot_id = candidate
                break
        if pipeline_entry_bot_id:
            break
        candidate_bot_id = str(task.get("bot_id") or "").strip()
        if candidate_bot_id:
            pipeline_entry_bot_id = candidate_bot_id
            break

    ordered_bot_ids: List[str] = []

    def _add_bot_id(bot_id: str) -> None:
        normalized = str(bot_id or "").strip()
        if normalized and normalized not in ordered_bot_ids:
            ordered_bot_ids.append(normalized)

    _add_bot_id(pipeline_entry_bot_id)
    for task in scoped_tasks:
        _add_bot_id(str(task.get("bot_id") or "").strip())

    for bot_id in ordered_bot_ids:
        bot_doc = _bot_doc(bot_id)
        if bot_doc is None:
            bot_name_map[bot_id] = _humanize_bot_id(bot_id)
            continue
        bot_name_map[bot_id] = str(bot_doc.get("name") or _humanize_bot_id(bot_id))
        workflow = bot_doc.get("workflow") if isinstance(bot_doc.get("workflow"), dict) else {}
        candidate_graph = workflow.get("reference_graph") if isinstance(workflow, dict) else None
        if (
            reference_graph is None
            and isinstance(candidate_graph, dict)
            and candidate_graph.get("nodes")
            and (
                not pipeline_entry_bot_id
                or str(candidate_graph.get("entry_bot_id") or "").strip() == pipeline_entry_bot_id
                or bot_id == pipeline_entry_bot_id
            )
        ):
            reference_graph = candidate_graph
    if reference_graph is None:
        for bot_id in ordered_bot_ids:
            bot_doc = _bot_doc(bot_id)
            workflow = bot_doc.get("workflow") if isinstance(bot_doc.get("workflow"), dict) else {}
            candidate_graph = workflow.get("reference_graph") if isinstance(workflow, dict) else None
            if isinstance(candidate_graph, dict) and candidate_graph.get("nodes"):
                reference_graph = candidate_graph
                break

    reference_nodes = (
        reference_graph.get("nodes")
        if isinstance(reference_graph, dict) and isinstance(reference_graph.get("nodes"), list)
        else []
    )
    reference_node_by_bot = {
        str(node.get("bot_id") or "").strip(): node
        for node in reference_nodes
        if isinstance(node, dict) and str(node.get("bot_id") or "").strip()
    }
    stage_order = [
        str(node.get("bot_id") or "").strip()
        for node in reference_nodes
        if isinstance(node, dict) and str(node.get("bot_id") or "").strip()
    ]
    if not stage_order:
        stage_order = [
            "pm-orchestrator",
            "pm-research-analyst",
            "pm-engineer",
            "pm-coder",
            "pm-tester",
            "pm-security-reviewer",
            "pm-database-engineer",
            "pm-ui-tester",
            "pm-final-qc",
        ]
    for bot_id in ordered_bot_ids:
        if bot_id and bot_id not in stage_order:
            stage_order.append(bot_id)

    root_node_id = f"orchestrator::{orchestration_id}"
    synthetic_root_bot_id = pipeline_entry_bot_id or "pm-orchestrator"
    synthetic_root_name = bot_name_map.get(synthetic_root_bot_id, _humanize_bot_id(synthetic_root_bot_id))
    synthetic_root_stage_kind = str((reference_node_by_bot.get(synthetic_root_bot_id) or {}).get("stage_kind") or "entry")
    has_explicit_entry_task = any(
        str((task.get("metadata") or {}).get("source") or "").strip().lower() in {"chat_assign", "auto_retry"}
        for task in scoped_tasks
    )
    is_chat_assignment = any(
        str((task.get("metadata") or {}).get("source") or "").strip().lower() in {"chat_assign", "auto_retry", "bot_trigger"}
        for task in scoped_tasks
    )

    nodes = []
    edges = []
    if scoped_tasks and is_chat_assignment and not has_explicit_entry_task:
        nodes.append(
            {
                "id": root_node_id,
                "title": synthetic_root_name,
                "step_id": synthetic_root_bot_id,
                "status": "completed",
                "bot_id": synthetic_root_bot_id,
                "display_name": synthetic_root_name,
                "stage_key": synthetic_root_bot_id,
                "stage_kind": synthetic_root_stage_kind,
                "depends_on": [],
                "synthetic": True,
                "details": {
                    "task_id": root_node_id,
                    "run_id": root_node_id,
                    "title": synthetic_root_name,
                    "bot_id": synthetic_root_bot_id,
                    "bot_name": synthetic_root_name,
                    "status": "completed",
                    "source": "synthetic_root",
                    "step_id": synthetic_root_bot_id,
                    "trigger_rule_id": "",
                    "trigger_depth": 0,
                    "parent_task_id": "",
                    "join_task_ids": [],
                    "fanout_id": "",
                    "fanout_branch_key": "",
                },
            }
        )

    for t in scoped_tasks:
        task_id = str(t.get("id"))
        metadata = t.get("metadata") or {}
        payload = t.get("payload") if isinstance(t.get("payload"), dict) else {}
        step_id = str(metadata.get("step_id") or "").strip()
        bot_id_label = str(t.get("bot_id") or "").strip()
        bot_name = bot_name_map.get(bot_id_label, _humanize_bot_id(bot_id_label))
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
        trigger_rule_id = str(metadata.get("trigger_rule_id") or "").strip()
        fanout_id = str(metadata.get("fanout_id") or payload.get("fanout_id") or "").strip()
        fanout_branch_key = str(metadata.get("fanout_branch_key") or payload.get("fanout_branch_key") or "").strip()
        join_task_ids = [str(jid) for jid in (payload.get("join_task_ids") or []) if str(jid).strip()]
        retry_of_task_id = str(metadata.get("retry_of_task_id") or "").strip()
        retried_by_task_id = str(metadata.get("retried_by_task_id") or "").strip()
        branch_index = None
        for raw_value in (
            payload.get("workstream_index"),
            payload.get("research_step_index"),
            metadata.get("workstream_index"),
            metadata.get("research_step_index"),
        ):
            try:
                if raw_value is None or str(raw_value).strip() == "":
                    continue
                branch_index = int(raw_value)
                break
            except Exception:
                continue
        lane_key = fanout_branch_key or (str(branch_index) if branch_index is not None else "")

        if not depends_on:
            if join_task_ids:
                # Show all joined sibling tasks as dependencies (fan-in)
                depends_on = join_task_ids
            elif source == "bot_trigger" and parent_task_id:
                depends_on = [parent_task_id]
            elif source in {"chat_assign", "auto_retry"} and is_chat_assignment and not has_explicit_entry_task:
                depends_on = [root_node_id]

        nodes.append(
            {
                "id": task_id,
                "title": title,
                "step_id": step_id,
                "status": t.get("status"),
                "bot_id": bot_id_label,
                "display_name": bot_name,
                "stage_key": bot_id_label,
                "stage_kind": str((reference_node_by_bot.get(bot_id_label) or {}).get("stage_kind") or ""),
                "branch_index": branch_index,
                "lane_key": lane_key,
                "depends_on": depends_on,
                "status_variant": str(t.get("status") or "queued").strip().lower() or "queued",
                "is_rerouted": False,
                "is_retried": bool(
                    str(source) in {"auto_retry", "manual_retry"}
                    or retry_of_task_id
                    or retried_by_task_id
                    or str(t.get("status") or "").strip().lower() == "retried"
                ),
                "details": {
                    "task_id": task_id,
                    "run_id": task_id,
                    "title": title,
                    "bot_id": bot_id_label,
                    "bot_name": bot_name,
                    "status": t.get("status"),
                    "source": source,
                    "step_id": step_id,
                    "trigger_rule_id": trigger_rule_id,
                    "trigger_depth": metadata.get("trigger_depth"),
                    "parent_task_id": parent_task_id,
                    "join_task_ids": join_task_ids,
                    "fanout_id": fanout_id,
                    "fanout_branch_key": fanout_branch_key,
                    "workstream_index": payload.get("workstream_index"),
                    "research_step_index": payload.get("research_step_index"),
                    "lane_key": lane_key,
                    "retry_of_task_id": retry_of_task_id,
                    "retried_by_task_id": retried_by_task_id,
                    "created_at": t.get("created_at"),
                    "synthetic": False,
                },
            }
        )
        for dep in depends_on:
            edges.append({"from": str(dep), "to": task_id})

    stage_index = {stage_id: index for index, stage_id in enumerate(stage_order)}
    node_by_id = {str(node.get("id") or ""): node for node in nodes if str(node.get("id") or "")}
    for node in nodes:
        if bool(node.get("synthetic")):
            continue
        details = node.get("details") if isinstance(node.get("details"), dict) else {}
        parent_task_id = str(details.get("parent_task_id") or "").strip()
        source = str(details.get("source") or "").strip().lower()
        parent_task = task_by_id.get(parent_task_id)
        parent_bot_id = str((parent_task or {}).get("bot_id") or "").strip()
        current_stage = stage_index.get(str(node.get("stage_key") or node.get("bot_id") or "").strip(), -1)
        parent_stage = stage_index.get(parent_bot_id, -1)
        is_rerouted = source == "bot_trigger" and parent_stage >= 0 and current_stage >= 0 and current_stage < parent_stage
        node["is_rerouted"] = is_rerouted
        if is_rerouted:
            node["status_variant"] = "rerouted"
        elif bool(node.get("is_retried")):
            node["status_variant"] = "retried"
        else:
            node["status_variant"] = str(node.get("status") or "queued").strip().lower() or "queued"
        details["route_state"] = "sent_back_and_reran" if is_rerouted else ("retried" if bool(node.get("is_retried")) else "")
        details["parent_bot_id"] = parent_bot_id
        node["details"] = details

    return jsonify(
        {
            "orchestration_id": orchestration_id,
            "assignment_id": assignment_id or None,
            "run_id": run_id or None,
            "state": run_state or None,
            "node_overrides": node_overrides,
            "nodes": nodes,
            "edges": edges,
            "stage_order": stage_order,
            "reference_graph": reference_graph or {},
        }
    )


@bp.get("/api/chat/orchestrations/<orchestration_id>/recap")
@login_required
def api_orchestration_recap(orchestration_id: str):
    cp = get_cp_client()
    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, include_content=True)
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502
    recap = _assignment_full_recap(orchestration_id, tasks)
    return jsonify({"orchestration_id": orchestration_id, "task_count": len(tasks), "recap": recap})
