"""Authentication blueprint — login and logout."""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import EmailField, PasswordField
from wtforms.validators import DataRequired, Email

import bcrypt
from sqlalchemy import func

from dashboard.db import get_db
from dashboard.models import User

bp = Blueprint("auth", __name__)


def _authenticate_user(email: str, password: str):
    normalized_email = str(email or "").strip().lower()
    db = get_db()
    try:
        user = db.query(User).filter(func.lower(User.email) == normalized_email).first()
    finally:
        db.close()

    if not user or not user.is_active:
        return None
    if not bcrypt.checkpw(str(password or "").encode(), user.password_hash.encode()):
        return None
    return user


def _login_success_payload(user: User) -> dict[str, object]:
    return {
        "ok": True,
        "user": {
            "id": user.get_id(),
            "email": user.email,
            "role": user.role,
        },
    }


# ── Forms ──────────────────────────────────────────────────────────────────────

class LoginForm(FlaskForm):
    """Email + password login form."""

    email = EmailField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.get("/login")
def login_get():
    """Show the login form (redirect to setup wizard if no users exist)."""
    db = get_db()
    try:
        if db.query(User).count() == 0:
            return redirect(url_for("onboarding.index"))
    finally:
        db.close()
    return render_template("login.html", form=LoginForm())


@bp.post("/login")
def login_post():
    """Handle login form submission."""
    form = LoginForm()
    if not form.validate_on_submit():
        return render_template("login.html", form=form), 400

    user = _authenticate_user(form.email.data, form.password.data)
    if user is None:
        flash("Invalid email or password.", "error")
        return render_template("login.html", form=form), 401

    login_user(user, remember=False)
    import time
    session["last_activity_ts"] = int(time.time())
    next_url = request.args.get("next") or ""
    # Prevent open-redirect: only allow safe relative paths (no scheme, no netloc)
    from urllib.parse import urlparse
    parsed = urlparse(next_url)
    if not next_url or parsed.scheme or parsed.netloc:
        next_url = url_for("main.index")
    return redirect(next_url)


@bp.post("/api/auth/login")
def api_login_post():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    if not email or not password:
        return jsonify({"error": "email and password are required"}), 400
    user = _authenticate_user(email, password)
    if user is None:
        return jsonify({"error": "invalid email or password"}), 401
    login_user(user, remember=False)
    import time
    session["last_activity_ts"] = int(time.time())
    return jsonify(_login_success_payload(user))


@bp.get("/api/auth/session")
def api_session_status():
    if not current_user.is_authenticated:
        return jsonify({"authenticated": False}), 401
    return jsonify(
        {
            "authenticated": True,
            "user": {
                "id": current_user.get_id(),
                "email": getattr(current_user, "email", ""),
                "role": getattr(current_user, "role", ""),
            },
        }
    )


@bp.post("/api/auth/logout")
@login_required
def api_logout_post():
    logout_user()
    session.clear()
    return jsonify({"ok": True})


@bp.get("/logout")
@login_required
def logout():
    """Log out the current user."""
    logout_user()
    return redirect(url_for("auth.login_get"))
