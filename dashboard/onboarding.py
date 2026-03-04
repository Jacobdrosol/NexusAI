"""Onboarding wizard routes for NexusAI dashboard.

Implements a multi-step first-run setup flow accessible at ``/setup``.
The wizard is only available while no admin user exists in the database;
once an admin is present every ``/setup`` request redirects to ``/login``.

Wizard steps
------------
1. Welcome
2. Create Admin Account
3. Control Plane & Worker Setup
4. Branding / Customisation
5. Review & Finish (with optional docker-compose.yml download)
"""

import re
import textwrap
from typing import Any, Dict, Optional

import bcrypt
from flask import (
    Blueprint,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from dashboard.models import (
    admin_exists,
    create_user,
    create_worker,
    get_setting,
    init_db,
    set_setting,
)

bp = Blueprint("onboarding", __name__)

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _setup_redirect():
    """Return a redirect to ``/login`` (admin already set up)."""
    return redirect(url_for("auth.login_get"))


def _get_wizard() -> Dict[str, Any]:
    """Return the current wizard session dict, creating it if necessary."""
    if "wizard" not in session:
        session["wizard"] = {}
    return session["wizard"]  # type: ignore[return-value]


# ------------------------------------------------------------------
# GET /setup  →  redirect to current step
# ------------------------------------------------------------------


@bp.get("/setup")
def setup_root():
    """Redirect to the correct wizard step (or ``/login`` if already configured)."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard()
    step = max(1, min(wizard.get("step", 1), 5))
    step_endpoints = {
        1: "onboarding.step1_get",
        2: "onboarding.step2_get",
        3: "onboarding.step3_get",
        4: "onboarding.step4_get",
        5: "onboarding.step5_get",
    }
    return redirect(url_for(step_endpoints[step]))


# ------------------------------------------------------------------
# Step 1 — Welcome
# ------------------------------------------------------------------


@bp.get("/setup/step1")
def step1_get():
    """Render the Welcome step."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard()
    wizard["step"] = 1
    session["wizard"] = wizard
    return render_template("setup/step1_welcome.html", current_step=1)


@bp.post("/setup/step1")
def step1_post():
    """Advance from the Welcome step to Step 2."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard()
    wizard["step"] = 2
    session["wizard"] = wizard
    return redirect(url_for("onboarding.step2_get"))


# ------------------------------------------------------------------
# Step 2 — Create Admin Account
# ------------------------------------------------------------------


@bp.get("/setup/step2")
def step2_get():
    """Render the Create Admin Account step."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 2:
        return redirect(url_for("onboarding.step1_get"))
    return render_template("setup/step2_admin.html", current_step=2)


@bp.post("/setup/step2")
def step2_post():
    """Validate and persist the admin account, then advance to Step 3."""
    init_db()
    if admin_exists():
        return _setup_redirect()

    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    errors: Dict[str, str] = {}
    if not _EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid e-mail address."
    if len(password) < 8:
        errors["password"] = "Password must be at least 8 characters."
    if password != confirm_password:
        errors["confirm_password"] = "Passwords do not match."

    if errors:
        return render_template(
            "setup/step2_admin.html",
            current_step=2,
            errors=errors,
            email=email,
        ), 400

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    create_user(email=email, hashed_password=hashed)

    wizard = _get_wizard()
    wizard["step"] = 3
    wizard["admin_email"] = email
    session["wizard"] = wizard
    return redirect(url_for("onboarding.step3_get"))


# ------------------------------------------------------------------
# Step 3 — Control Plane & Worker Setup
# ------------------------------------------------------------------


@bp.get("/setup/step3")
def step3_get():
    """Render the Control Plane & Worker Setup step."""
    init_db()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 3:
        return redirect(url_for("onboarding.step2_get"))
    return render_template(
        "setup/step3_worker.html",
        current_step=3,
        cp_host=wizard.get("cp_host", "localhost"),
        cp_port=wizard.get("cp_port", 8080),
    )


@bp.post("/setup/step3")
def step3_post():
    """Persist control-plane settings and optional first worker, then advance."""
    init_db()
    cp_host = request.form.get("cp_host", "localhost").strip()
    cp_port_str = request.form.get("cp_port", "8080")
    worker_name = request.form.get("worker_name", "").strip() or None
    worker_host = request.form.get("worker_host", "").strip() or None
    worker_port_str = request.form.get("worker_port", "")
    skip_worker = request.form.get("skip_worker")

    errors: Dict[str, str] = {}
    try:
        cp_port = int(cp_port_str)
    except (ValueError, TypeError):
        cp_port = 0

    if not cp_host:
        errors["cp_host"] = "Control Plane host is required."
    if not (1 <= cp_port <= 65535):
        errors["cp_port"] = "Port must be between 1 and 65535."

    add_worker = skip_worker is None
    worker_port: Optional[int] = None
    if add_worker and worker_name:
        if not worker_host:
            errors["worker_host"] = "Worker host is required."
        try:
            worker_port = int(worker_port_str)
        except (ValueError, TypeError):
            worker_port = None
        if worker_port is None or not (1 <= worker_port <= 65535):
            errors["worker_port"] = "Worker port must be between 1 and 65535."

    if errors:
        return render_template(
            "setup/step3_worker.html",
            current_step=3,
            errors=errors,
            cp_host=cp_host,
            cp_port=cp_port,
            worker_name=worker_name or "",
            worker_host=worker_host or "",
            worker_port=worker_port if worker_port is not None else worker_port_str,
        ), 400

    set_setting("cp_host", cp_host)
    set_setting("cp_port", str(cp_port))

    wizard = _get_wizard()
    wizard["cp_host"] = cp_host
    wizard["cp_port"] = cp_port

    if add_worker and worker_name and worker_host and worker_port is not None:
        create_worker(name=worker_name, host=worker_host, port=worker_port)
        wizard["worker_name"] = worker_name
        wizard["worker_host"] = worker_host
        wizard["worker_port"] = worker_port

    wizard["step"] = 4
    session["wizard"] = wizard
    return redirect(url_for("onboarding.step4_get"))


# ------------------------------------------------------------------
# Step 4 — Branding / Customisation
# ------------------------------------------------------------------


@bp.get("/setup/step4")
def step4_get():
    """Render the Branding / Customisation step."""
    init_db()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 4:
        return redirect(url_for("onboarding.step3_get"))
    return render_template(
        "setup/step4_branding.html",
        current_step=4,
        site_name=wizard.get("site_name", "NexusAI"),
        tagline=wizard.get("tagline", ""),
    )


@bp.post("/setup/step4")
def step4_post():
    """Persist branding settings and advance to the final review step."""
    init_db()
    site_name = request.form.get("site_name", "NexusAI").strip()
    tagline = request.form.get("tagline", "").strip()

    errors: Dict[str, str] = {}
    if not site_name:
        errors["site_name"] = "Site name cannot be empty."

    if errors:
        return render_template(
            "setup/step4_branding.html",
            current_step=4,
            errors=errors,
            site_name=site_name,
            tagline=tagline,
        ), 400

    set_setting("site_name", site_name)
    set_setting("tagline", tagline)

    wizard = _get_wizard()
    wizard["site_name"] = site_name
    wizard["tagline"] = tagline
    wizard["step"] = 5
    session["wizard"] = wizard
    return redirect(url_for("onboarding.step5_get"))


# ------------------------------------------------------------------
# Step 5 — Review & Finish
# ------------------------------------------------------------------


@bp.get("/setup/step5")
def step5_get():
    """Render the Review & Finish step."""
    init_db()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 5:
        return redirect(url_for("onboarding.step4_get"))
    return render_template(
        "setup/step5_finish.html",
        current_step=5,
        wizard=wizard,
    )


@bp.post("/setup/step5")
def step5_post():
    """Complete the wizard and redirect to the login page."""
    session.pop("wizard", None)
    return redirect(url_for("auth.login_get"))


# ------------------------------------------------------------------
# Docker Compose download
# ------------------------------------------------------------------


@bp.get("/setup/download-compose")
def download_compose():
    """Generate and return a ``docker-compose.yml`` for the configured stack."""
    cp_host = get_setting("cp_host", "localhost") or "localhost"
    cp_port = get_setting("cp_port", "8080") or "8080"
    site_name = get_setting("site_name", "NexusAI") or "NexusAI"

    wizard = _get_wizard()
    worker_name = wizard.get("worker_name")
    worker_host = wizard.get("worker_host")
    worker_port = wizard.get("worker_port")

    worker_service = ""
    worker_depends = ""
    if worker_name:
        worker_service = textwrap.dedent(
            f"""
  nexusai-worker:
    image: nexusai/worker:latest
    # Worker: {worker_name} ({worker_host}:{worker_port})
    environment:
      - WORKER_HOST={worker_host}
      - WORKER_PORT={worker_port}
      # Add additional secrets/tokens here
    volumes:
      - ./data:/app/data
    restart: unless-stopped
"""
        )
        worker_depends = "    depends_on:\n      - nexusai-worker\n"

    compose = textwrap.dedent(
        f"""\
# docker-compose.yml — generated by {site_name} setup wizard
# Review and add any secrets before running in production.

version: '3.9'

services:
  nexusai-control-plane:
    image: nexusai/control-plane:latest
    ports:
      - "{cp_port}:{cp_port}"
    environment:
      - CONTROL_PLANE_HOST={cp_host}
      - CONTROL_PLANE_PORT={cp_port}
      # Add API keys / secrets here — never commit real values!
    volumes:
      - ./data:/app/data
    restart: unless-stopped
{worker_service}
  nexusai-dashboard:
    image: nexusai/dashboard:latest
    ports:
      - "8080:8080"
    environment:
      - CONTROL_PLANE_URL=http://nexusai-control-plane:{cp_port}
      # SESSION_SECRET_KEY=changeme
    volumes:
      - ./data:/app/data
    depends_on:
      - nexusai-control-plane
{worker_depends}    restart: unless-stopped

volumes:
  data:
"""
    )

    response = make_response(compose)
    response.headers["Content-Type"] = "text/yaml"
    response.headers["Content-Disposition"] = "attachment; filename=docker-compose.yml"
    return response
