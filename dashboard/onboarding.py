"""Onboarding wizard routes for NexusAI dashboard.

Implements a multi-step first-run setup flow accessible at ``/onboarding``.
The wizard is only available while no admin user exists in the database;
once an admin is present every ``/onboarding`` request redirects to ``/login``.

Wizard steps
------------
1. Welcome
2. Create Admin Account
3. LLM Backend Selection
4. Worker Node Setup (optional)
5. Complete
"""

import re
from typing import Any, Dict, Optional

import bcrypt
from flask import (
    Blueprint,
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
    init_db,
    set_setting,
)

bp = Blueprint("onboarding", __name__, url_prefix="/onboarding")

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_LLM_BACKENDS = ["ollama", "openai", "claude", "gemini"]


def _setup_redirect():
    """Return a redirect to ``/login`` (admin already set up)."""
    return redirect(url_for("auth.login_get"))


def _get_wizard() -> Dict[str, Any]:
    """Return the current wizard session dict, creating it if necessary."""
    if "wizard" not in session:
        session["wizard"] = {}
    return session["wizard"]  # type: ignore[return-value]


# ------------------------------------------------------------------
# GET /onboarding/  →  redirect to current step
# ------------------------------------------------------------------


@bp.get("/")
def index():
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


@bp.get("/step1")
def step1_get():
    """Render the Welcome step."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard()
    wizard["step"] = 1
    session["wizard"] = wizard
    return render_template("onboarding/step1_welcome.html", current_step=1)


@bp.post("/step1")
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


@bp.get("/step2")
def step2_get():
    """Render the Create Admin Account step."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 2:
        return redirect(url_for("onboarding.step1_get"))
    return render_template("onboarding/step2_admin.html", current_step=2)


@bp.post("/step2")
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
            "onboarding/step2_admin.html",
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
# Step 3 — LLM Backend Selection
# ------------------------------------------------------------------


@bp.get("/step3")
def step3_get():
    """Render the LLM Backend Selection step."""
    init_db()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 3:
        return redirect(url_for("onboarding.step2_get"))
    return render_template(
        "onboarding/step3_llm.html",
        current_step=3,
        llm_backend=wizard.get("llm_backend", "ollama"),
        llm_backends=_LLM_BACKENDS,
    )


@bp.post("/step3")
def step3_post():
    """Persist LLM backend selection and advance to Step 4."""
    init_db()
    llm_backend = request.form.get("llm_backend", "ollama").strip().lower()
    if llm_backend not in _LLM_BACKENDS:
        llm_backend = "ollama"

    set_setting("llm_backend", llm_backend)

    wizard = _get_wizard()
    wizard["llm_backend"] = llm_backend
    wizard["step"] = 4
    session["wizard"] = wizard
    return redirect(url_for("onboarding.step4_get"))


# ------------------------------------------------------------------
# Step 4 — Worker Node Setup
# ------------------------------------------------------------------


@bp.get("/step4")
def step4_get():
    """Render the Worker Node Setup step."""
    init_db()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 4:
        return redirect(url_for("onboarding.step3_get"))
    return render_template(
        "onboarding/step4_worker.html",
        current_step=4,
        worker_name=wizard.get("worker_name", ""),
        worker_host=wizard.get("worker_host", ""),
        worker_port=wizard.get("worker_port", ""),
    )


@bp.post("/step4")
def step4_post():
    """Optionally register the first worker node, then advance to Step 5."""
    init_db()
    worker_name = request.form.get("worker_name", "").strip() or None
    worker_host = request.form.get("worker_host", "").strip() or None
    worker_port_str = request.form.get("worker_port", "")
    skip_worker = request.form.get("skip_worker")

    errors: Dict[str, str] = {}
    worker_port: Optional[int] = None

    add_worker = skip_worker is None and bool(worker_name)
    if add_worker:
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
            "onboarding/step4_worker.html",
            current_step=4,
            errors=errors,
            worker_name=worker_name or "",
            worker_host=worker_host or "",
            worker_port=worker_port if worker_port is not None else worker_port_str,
        ), 400

    wizard = _get_wizard()
    if add_worker and worker_name and worker_host and worker_port is not None:
        create_worker(name=worker_name, host=worker_host, port=worker_port)
        wizard["worker_name"] = worker_name
        wizard["worker_host"] = worker_host
        wizard["worker_port"] = worker_port

    wizard["step"] = 5
    session["wizard"] = wizard
    return redirect(url_for("onboarding.step5_get"))


# ------------------------------------------------------------------
# Step 5 — Complete
# ------------------------------------------------------------------


@bp.get("/step5")
def step5_get():
    """Render the completion screen."""
    init_db()
    wizard = _get_wizard()
    if wizard.get("step", 1) < 5:
        return redirect(url_for("onboarding.step4_get"))
    return render_template(
        "onboarding/step5_complete.html",
        current_step=5,
        wizard=wizard,
    )


@bp.post("/step5")
def step5_post():
    """Complete the wizard and redirect to the login page."""
    session.pop("wizard", None)
    return redirect(url_for("auth.login_get"))
