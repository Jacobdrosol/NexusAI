"""Authentication blueprint — login, logout, setup wizard."""
from __future__ import annotations

from datetime import datetime, timezone

import bcrypt
from flask import (
    Blueprint,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required, login_user, logout_user
from flask_wtf import FlaskForm
from wtforms import EmailField, PasswordField, StringField
from wtforms.validators import DataRequired, Email, EqualTo, Length

from dashboard.db import get_db
from dashboard.models import User

bp = Blueprint("auth", __name__)


# ── Forms ──────────────────────────────────────────────────────────────────────

class LoginForm(FlaskForm):
    """Email + password login form."""

    email = EmailField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])


class SetupForm(FlaskForm):
    """First-run admin account creation form."""

    email = EmailField("Admin email", validators=[DataRequired(), Email()])
    password = PasswordField(
        "Password",
        validators=[DataRequired(), Length(min=8)],
    )
    confirm = PasswordField(
        "Confirm password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match")],
    )


# ── Routes ─────────────────────────────────────────────────────────────────────

@bp.get("/setup")
def setup_get():
    """Show the first-run setup wizard (redirect away if users already exist)."""
    db = get_db()
    try:
        if db.query(User).count() > 0:
            return redirect(url_for("auth.login_get"))
    finally:
        db.close()
    return render_template("setup.html", form=SetupForm())


@bp.post("/setup")
def setup_post():
    """Handle admin account creation on first run."""
    db = get_db()
    try:
        if db.query(User).count() > 0:
            return redirect(url_for("auth.login_get"))
    finally:
        db.close()

    form = SetupForm()
    if not form.validate_on_submit():
        return render_template("setup.html", form=form), 400

    email = form.email.data.strip().lower()
    pw_hash = bcrypt.hashpw(form.password.data.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    try:
        if db.query(User).filter_by(email=email).first():
            flash("That email is already registered.", "error")
            return render_template("setup.html", form=form), 409
        admin = User(
            email=email,
            password_hash=pw_hash,
            role="admin",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        login_user(admin)
        return redirect(url_for("main.index"))
    finally:
        db.close()


@bp.get("/login")
def login_get():
    """Show the login form (redirect to setup if no users exist)."""
    db = get_db()
    try:
        if db.query(User).count() == 0:
            return redirect(url_for("auth.setup_get"))
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
        user = db.query(User).filter_by(email=email).first()
    finally:
        db.close()

    if not user or not user.is_active:
        flash("Invalid email or password.", "error")
        return render_template("login.html", form=form), 401

    if not bcrypt.checkpw(form.password.data.encode(), user.password_hash.encode()):
        flash("Invalid email or password.", "error")
        return render_template("login.html", form=form), 401

    login_user(user, remember=False)
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
