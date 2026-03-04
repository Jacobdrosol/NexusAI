"""Chat dashboard page and proxy endpoints."""
from __future__ import annotations

import os
from typing import Any, Dict, Iterable

import requests
from flask import Blueprint, Response, jsonify, render_template, request, stream_with_context
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("chat", __name__)


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
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(created), 201


@bp.post("/api/chat/messages")
@login_required
def api_send_message():
    data: dict[str, Any] = request.get_json(force=True) or {}
    conversation_id = (data.get("conversation_id") or "").strip()
    content = (data.get("content") or "").strip()
    if not conversation_id or not content:
        return jsonify({"error": "conversation_id and content are required"}), 400
    cp = get_cp_client()

    # Inline assignment shortcut:
    #   @assign Build auth tests
    if content.lower().startswith("@assign"):
        assignment_payload = content[len("@assign"):].strip()
        if not assignment_payload:
            return jsonify({"error": "assignment content is required after @assign"}), 400
        bots = cp.list_bots() or []
        pm_bot = next(
            (
                b
                for b in bots
                if str(b.get("role", "")).lower()
                in {"pm", "project_manager", "project manager"}
            ),
            None,
        )
        target_bot_id = data.get("bot_id") or (pm_bot.get("id") if pm_bot else None)
        if not target_bot_id:
            return jsonify({"error": "no PM bot available for assignment"}), 400
        task = cp.create_task_full(
            bot_id=str(target_bot_id),
            payload={"instruction": assignment_payload, "source": "chat_assign"},
            metadata={"source": "chat", "conversation_id": conversation_id},
        )
        if task is None:
            return jsonify({"error": "task assignment failed"}), 502
        return jsonify({"assigned_task": task, "mode": "assign"})

    resp = cp.post_message(
        conversation_id,
        {
            "content": content,
            "bot_id": data.get("bot_id"),
            "context_items": data.get("context_items"),
        },
    )
    if resp is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(resp)


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
    }

    def generate() -> Iterable[str]:
        try:
            with requests.post(
                stream_url, json=payload, stream=True, timeout=120
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
