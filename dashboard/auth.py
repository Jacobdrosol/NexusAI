"""Authentication blueprint — login and logout."""
from __future__ import annotations

from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import EmailField, PasswordField
from wtforms.validators import DataRequired, Email

import bcrypt
from sqlalchemy import func

from dashboard.db import get_db
from dashboard.models import User

bp = Blueprint("auth", __name__)


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

    email = form.email.data.strip().lower()
    db = get_db()
    try:
        user = db.query(User).filter(func.lower(User.email) == email).first()
    finally:
        db.close()

    if not user or not user.is_active:
        flash("Invalid email or password.", "error")
        return render_template("login.html", form=form), 401

    if not bcrypt.checkpw(form.password.data.encode(), user.password_hash.encode()):
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


@bp.get("/logout")
@login_required
def logout():
    """Log out the current user."""
    logout_user()
    return redirect(url_for("auth.login_get"))
