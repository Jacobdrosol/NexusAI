"""Users blueprint — admin user management page + API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import bcrypt
from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from dashboard.db import get_db
from dashboard.models import User

bp = Blueprint("users", __name__)


def _user_to_dict(u: User) -> dict[str, Any]:
    """Serialise a User ORM row to a safe plain dict (no password_hash)."""
    return {
        "id": u.id,
        "email": u.email,
        "role": u.role,
        "is_active": u.is_active,
        "created_at": u.created_at.isoformat() if u.created_at else "",
    }


def _require_admin() -> None:
    """Abort with 403 if the current user is not an admin."""
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


@bp.get("/users")
@login_required
def users_page() -> str:
    """Render the user management page (admin only)."""
    _require_admin()
    db = get_db()
    try:
        users = db.query(User).order_by(User.created_at).all()
        return render_template("users.html", users=[_user_to_dict(u) for u in users])
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/users")
@login_required
def api_list_users():
    """List all users (admin only)."""
    _require_admin()
    db = get_db()
    try:
        users = db.query(User).order_by(User.created_at).all()
        return jsonify([_user_to_dict(u) for u in users])
    finally:
        db.close()


@bp.post("/api/users")
@login_required
def api_create_user():
    """Create (invite) a new user (admin only)."""
    _require_admin()
    data: dict[str, Any] = request.get_json(force=True) or {}
    email: str = data.get("email", "").strip().lower()
    password: str = data.get("password", "")
    role: str = data.get("role", "user")
    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400
    if role not in ("admin", "user"):
        return jsonify({"error": "role must be 'admin' or 'user'"}), 400
    db = get_db()
    try:
        if db.query(User).filter_by(email=email).first():
            return jsonify({"error": "email already registered"}), 409
        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = User(
            email=email,
            password_hash=pw_hash,
            role=role,
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return jsonify(_user_to_dict(user)), 201
    finally:
        db.close()


@bp.put("/api/users/<int:user_id>")
@login_required
def api_update_user(user_id: int):
    """Update a user — admin can change role/active; any user can reset own password."""
    db = get_db()
    try:
        user = db.get(User, user_id)
        if not user:
            return jsonify({"error": "not found"}), 404
        data: dict[str, Any] = request.get_json(force=True) or {}
        is_admin = current_user.role == "admin"
        is_self = current_user.id == user_id
        if not is_admin and not is_self:
            return jsonify({"error": "forbidden"}), 403
        if is_admin:
            if "role" in data and data["role"] in ("admin", "user"):
                user.role = data["role"]
            if "is_active" in data:
                user.is_active = bool(data["is_active"])
        if "password" in data and data["password"]:
            user.password_hash = bcrypt.hashpw(
                data["password"].encode(), bcrypt.gensalt()
            ).decode()
        db.commit()
        db.refresh(user)
        return jsonify(_user_to_dict(user))
    finally:
        db.close()


@bp.delete("/api/users/<int:user_id>")
@login_required
def api_delete_user(user_id: int):
    """Delete a user (admin only; cannot delete self)."""
    _require_admin()
    if current_user.id == user_id:
        return jsonify({"error": "cannot delete yourself"}), 400
    db = get_db()
    try:
        user = db.get(User, user_id)
        if not user:
            return jsonify({"error": "not found"}), 404
        db.delete(user)
        db.commit()
        return "", 204
    finally:
        db.close()
