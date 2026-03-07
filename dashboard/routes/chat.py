"""Chat dashboard page and proxy endpoints."""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable

import requests
from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("chat", __name__)


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


@bp.get("/chat")
@login_required
def chat_page() -> str:
    cp = get_cp_client()
    conversations = cp.list_conversations() or []
    bots = cp.list_bots() or []
    vault_items = cp.list_vault_items(limit=50) or []
    selected_id = request.args.get("conversation_id")
    selected = None
    messages = []
    if selected_id:
        for c in conversations:
            if c.get("id") == selected_id:
                selected = c
                break
        messages = cp.list_messages(selected_id) or []
    return render_template(
        "chat.html",
        conversations=conversations,
        selected_conversation=selected,
        messages=messages,
        bots=bots,
        vault_items=vault_items,
        error=None,
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
            "scope": data.get("scope", "global"),
            "default_bot_id": data.get("default_bot_id"),
            "default_model_id": data.get("default_model_id"),
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
    }

    def generate() -> Iterable[str]:
        try:
            with requests.post(
                stream_url,
                json=payload,
                headers=_stream_cp_headers(cp),
                stream=True,
                timeout=120,
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
