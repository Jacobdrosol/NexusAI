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
logger = logging.getLogger(__name__)


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
                messages = _normalize_message_rows(cp.list_messages(selected_id) or [])
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
                    items_raw = cp.list_vault_items(namespace=namespace, project_id=pid, limit=120) or []
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
            vault_items_raw = cp.list_vault_items(limit=50) or []
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


@bp.get("/api/chat/conversations/<conversation_id>/messages")
@login_required
def api_list_messages(conversation_id: str):
    cp = get_cp_client()
    messages = cp.list_messages(conversation_id)
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


@bp.get("/api/chat/orchestrations/<orchestration_id>/graph")
@login_required
def api_orchestration_graph(orchestration_id: str):
    cp = get_cp_client()
    tasks = cp.list_tasks(orchestration_id=orchestration_id)
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502

    nodes = []
    edges = []
    for t in tasks:
        task_id = str(t.get("id"))
        payload = t.get("payload") or {}
        title = ""
        if isinstance(payload, dict):
            title = str(payload.get("title") or payload.get("instruction") or task_id)
        depends_on = t.get("depends_on") or []
        nodes.append(
            {
                "id": task_id,
                "title": title,
                "status": t.get("status"),
                "bot_id": t.get("bot_id"),
                "depends_on": depends_on,
            }
        )
        for dep in depends_on:
            edges.append({"from": str(dep), "to": task_id})

    return jsonify({"orchestration_id": orchestration_id, "nodes": nodes, "edges": edges})
