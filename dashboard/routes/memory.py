from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

import bcrypt

from dashboard.cp_client import get_cp_client
from dashboard.db import get_db
from dashboard.models import User

bp = Blueprint("memory", __name__)

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


def _selected_profile_id() -> str:
    return str(request.args.get("profile_id") or request.form.get("profile_id") or _DEFAULT_PROFILE_ID).strip() or _DEFAULT_PROFILE_ID


@bp.get("/memory")
@login_required
def memory_page() -> str:
    cp = get_cp_client()
    user_id = _current_memory_user_id()
    profile_id = _selected_profile_id()
    items = cp.list_memory_profile_items(
        user_id=user_id,
        profile_id=profile_id,
        limit=200,
    )
    profiles_result = cp.list_memory_profiles(user_id=user_id)
    profiles = (profiles_result or {}).get("profiles", []) if profiles_result else []
    selected_profile = next((p for p in profiles if p.get("id") == profile_id), None)
    error = None
    if items is None:
        error = cp.unavailable_reason()
        items = []
    return render_template(
        "memory.html",
        items=items,
        profile_id=profile_id,
        profiles=profiles,
        selected_profile=selected_profile,
        error=error,
    )


@bp.get("/api/memory/profiles")
@login_required
def api_list_memory_profiles():
    cp = get_cp_client()
    result = cp.list_memory_profiles(user_id=_current_memory_user_id())
    if result is None:
        return _cp_error_response(cp)
    return jsonify(result)


@bp.post("/api/memory/profiles")
@login_required
def api_create_memory_profile():
    cp = get_cp_client()
    data: dict[str, Any] = request.get_json(force=True) or {}
    profile_id = str(data.get("profile_id") or "").strip()
    name = str(data.get("name") or "").strip()
    if not profile_id or not name:
        return jsonify({"error": "profile_id and name are required"}), 400
    result = cp.create_memory_profile(
        _current_memory_user_id(),
        profile_id=profile_id,
        name=name,
        description=str(data.get("description") or "").strip() or None,
        source=str(data.get("source") or "manual").strip() or "manual",
        enabled=bool(data.get("enabled", True)),
    )
    if result is None:
        return _cp_error_response(cp, "failed to create memory profile")
    return jsonify(result), 201


@bp.put("/api/memory/profiles/<profile_id>")
@login_required
def api_update_memory_profile(profile_id: str):
    cp = get_cp_client()
    data: dict[str, Any] = request.get_json(force=True) or {}
    kwargs: dict[str, Any] = {}
    if "name" in data:
        kwargs["name"] = str(data.get("name") or "").strip()
    if "description" in data:
        kwargs["description"] = str(data.get("description") or "").strip() or None
    if "enabled" in data:
        kwargs["enabled"] = bool(data.get("enabled"))
    result = cp.update_memory_profile(_current_memory_user_id(), profile_id, **kwargs)
    if result is None:
        return _cp_error_response(cp, "failed to update memory profile")
    return jsonify(result)


@bp.delete("/api/memory/profiles/<profile_id>")
@login_required
def api_delete_memory_profile(profile_id: str):
    cp = get_cp_client()
    ok = cp.delete_memory_profile(_current_memory_user_id(), profile_id)
    if not ok:
        return _cp_error_response(cp, "failed to delete memory profile")
    return "", 204


@bp.post("/api/memory/profiles/<profile_id>/clear")
@login_required
def api_clear_memory_profile(profile_id: str):
    """Clear all items in a memory profile (password + phrase required).

    Requires the signed-in user's password AND the literal phrase
    "DELETE" before any profile items are removed.
    """
    data: dict[str, Any] = request.get_json(force=True) or {}
    password = str(data.get("password") or "")
    confirm_phrase = str(data.get("confirm_phrase") or "").strip()

    if not password:
        return jsonify({"error": "password is required"}), 400
    if confirm_phrase != "DELETE":
        return jsonify({"error": "confirmation phrase must be 'DELETE'"}), 400

    # Verify the signed-in user's password before allowing a destructive clear.
    email = str(getattr(current_user, "email", "") or "").strip().lower()
    db = get_db()
    try:
        user = db.query(User).filter(User.email == email).first()
    finally:
        db.close()
    if user is None or not bcrypt.checkpw(str(password).encode(), user.password_hash.encode()):
        return jsonify({"error": "incorrect password"}), 403

    cp = get_cp_client()
    result = cp.clear_memory_profile(_current_memory_user_id(), profile_id, confirm_phrase)
    if result is None:
        return _cp_error_response(cp, "failed to clear memory profile")
    return jsonify(result)


@bp.get("/api/memory/profiles/<profile_id>/count")
@login_required
def api_count_memory_profile(profile_id: str):
    cp = get_cp_client()
    result = cp.count_memory_profile(_current_memory_user_id(), profile_id)
    if result is None:
        return _cp_error_response(cp, "failed to count memory profile items")
    return jsonify(result)


@bp.get("/api/memory/items")
@login_required
def api_list_memory_items():
    cp = get_cp_client()
    query = str(request.args.get("query") or "").strip() or None
    profile_id = _selected_profile_id()
    raw_limit = request.args.get("limit", "200")
    try:
        limit = max(1, min(int(raw_limit), 500))
    except (TypeError, ValueError):
        limit = 200
    result = cp.list_memory_profile_items(
        user_id=_current_memory_user_id(),
        profile_id=profile_id,
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
    profile_id = str(data.get("profile_id") or _DEFAULT_PROFILE_ID).strip() or _DEFAULT_PROFILE_ID
    result = cp.create_memory_profile_item(
        {
            "user_id": _current_memory_user_id(),
            "profile_id": profile_id,
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
    profile_id = str(data.get("profile_id") or _DEFAULT_PROFILE_ID).strip() or _DEFAULT_PROFILE_ID
    result = cp.update_memory_profile_item(
        item_id,
        {
            "user_id": _current_memory_user_id(),
            "profile_id": profile_id,
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