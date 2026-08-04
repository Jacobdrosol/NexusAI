from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("memory", __name__)

# Future expansion: replace this constant with a selected per-user profile once
# imported/manual/auto-generated memory profiles become first-class records.
_DEFAULT_PROFILE_ID = "default"


def _current_memory_user_id() -> str:
    return str(getattr(current_user, "email", "") or getattr(current_user, "id", "") or "").strip()


def _cp_error_response(cp: Any, fallback: str = "control plane unavailable") -> tuple[Any, int]:
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = ""
    status_code = None
    if isinstance(err, dict):
        detail = str(err.get("detail") or "").strip()
        raw_code = err.get("status_code")
        if isinstance(raw_code, int) and 400 <= raw_code <= 599:
            status_code = raw_code
    return jsonify({"error": detail or fallback}), (status_code or 502)


@bp.get("/memory")
@login_required
def memory_page() -> str:
    cp = get_cp_client()
    items = cp.list_memory_profile_items(
        user_id=_current_memory_user_id(),
        profile_id=_DEFAULT_PROFILE_ID,
        limit=200,
    )
    error = None
    if items is None:
        error = cp.unavailable_reason()
        items = []
    return render_template(
        "memory.html",
        items=items,
        profile_id=_DEFAULT_PROFILE_ID,
        error=error,
    )


@bp.get("/api/memory/items")
@login_required
def api_list_memory_items():
    cp = get_cp_client()
    query = str(request.args.get("query") or "").strip() or None
    raw_limit = request.args.get("limit", "200")
    try:
        limit = max(1, min(int(raw_limit), 500))
    except (TypeError, ValueError):
        limit = 200
    result = cp.list_memory_profile_items(
        user_id=_current_memory_user_id(),
        profile_id=_DEFAULT_PROFILE_ID,
        limit=limit,
        query=query,
    )
    if result is None:
        return _cp_error_response(cp)
    return jsonify({"items": result})


@bp.post("/api/memory/items")
@login_required
def api_create_memory_item():
    cp = get_cp_client()
    data: dict[str, Any] = request.get_json(force=True) or {}
    content = str(data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    result = cp.create_memory_profile_item(
        {
            "user_id": _current_memory_user_id(),
            "profile_id": _DEFAULT_PROFILE_ID,
            "content": content,
            "role": "assistant" if data.get("role") == "assistant" else "user",
            "metadata": {"source": "manual"},
        }
    )
    if result is None:
        return _cp_error_response(cp, "failed to create memory item")
    return jsonify(result), 201


@bp.put("/api/memory/items/<item_id>")
@login_required
def api_update_memory_item(item_id: str):
    cp = get_cp_client()
    data: dict[str, Any] = request.get_json(force=True) or {}
    content = str(data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content is required"}), 400
    result = cp.update_memory_profile_item(
        item_id,
        {
            "user_id": _current_memory_user_id(),
            "profile_id": _DEFAULT_PROFILE_ID,
            "content": content,
            "role": "assistant" if data.get("role") == "assistant" else "user",
            "metadata": {"source": "manual"},
        },
    )
    if result is None:
        return _cp_error_response(cp, "failed to update memory item")
    return jsonify(result)


@bp.delete("/api/memory/items/<item_id>")
@login_required
def api_delete_memory_item(item_id: str):
    cp = get_cp_client()
    ok = cp.delete_memory_profile_item(item_id, user_id=_current_memory_user_id())
    if not ok:
        return _cp_error_response(cp, "failed to delete memory item")
    return "", 204
