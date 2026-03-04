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
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pathlib import Path

from dashboard.models import (
    admin_exists,
    create_user,
    create_worker,
    get_setting,
    init_db,
    set_setting,
)

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _setup_redirect() -> RedirectResponse:
    """Return a redirect to ``/login`` (admin already set up)."""
    return RedirectResponse(url="/login", status_code=302)


def _render(
    request: Request,
    template: str,
    ctx: Optional[Dict[str, Any]] = None,
) -> HTMLResponse:
    """Render a setup template with common context merged in."""
    context: Dict[str, Any] = {"request": request}
    if ctx:
        context.update(ctx)
    return _templates.TemplateResponse(template, context)


def _get_wizard(request: Request) -> Dict[str, Any]:
    """Return the current wizard session dict, creating it if necessary."""
    if "wizard" not in request.session:
        request.session["wizard"] = {}
    return request.session["wizard"]  # type: ignore[return-value]


# ------------------------------------------------------------------
# GET /setup  →  redirect to current step
# ------------------------------------------------------------------


@router.get("/setup", response_class=RedirectResponse)
async def setup_root(request: Request) -> RedirectResponse:
    """Redirect to the correct wizard step (or ``/login`` if already configured)."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard(request)
    step = wizard.get("step", 1)
    return RedirectResponse(url=f"/setup/step{step}", status_code=302)


# ------------------------------------------------------------------
# Step 1 — Welcome
# ------------------------------------------------------------------


@router.get("/setup/step1", response_class=HTMLResponse)
async def step1_get(request: Request) -> HTMLResponse:
    """Render the Welcome step."""
    init_db()
    if admin_exists():
        return _setup_redirect()  # type: ignore[return-value]
    wizard = _get_wizard(request)
    wizard["step"] = 1
    request.session["wizard"] = wizard
    return _render(request, "setup/step1_welcome.html", {"current_step": 1})


@router.post("/setup/step1", response_class=RedirectResponse)
async def step1_post(request: Request) -> RedirectResponse:
    """Advance from the Welcome step to Step 2."""
    init_db()
    if admin_exists():
        return _setup_redirect()
    wizard = _get_wizard(request)
    wizard["step"] = 2
    request.session["wizard"] = wizard
    return RedirectResponse(url="/setup/step2", status_code=302)


# ------------------------------------------------------------------
# Step 2 — Create Admin Account
# ------------------------------------------------------------------


@router.get("/setup/step2", response_class=HTMLResponse)
async def step2_get(request: Request) -> HTMLResponse:
    """Render the Create Admin Account step."""
    init_db()
    if admin_exists():
        return _setup_redirect()  # type: ignore[return-value]
    wizard = _get_wizard(request)
    if wizard.get("step", 1) < 2:
        return RedirectResponse(url="/setup/step1", status_code=302)  # type: ignore[return-value]
    return _render(request, "setup/step2_admin.html", {"current_step": 2})


@router.post("/setup/step2", response_class=HTMLResponse)
async def step2_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
) -> Any:
    """Validate and persist the admin account, then advance to Step 3."""
    init_db()
    if admin_exists():
        return _setup_redirect()

    errors: Dict[str, str] = {}
    if not _EMAIL_RE.match(email):
        errors["email"] = "Please enter a valid e-mail address."
    if len(password) < 8:
        errors["password"] = "Password must be at least 8 characters."
    if password != confirm_password:
        errors["confirm_password"] = "Passwords do not match."

    if errors:
        return _render(
            request,
            "setup/step2_admin.html",
            {
                "current_step": 2,
                "errors": errors,
                "email": email,
            },
        )

    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    create_user(email=email, hashed_password=hashed)

    wizard = _get_wizard(request)
    wizard["step"] = 3
    wizard["admin_email"] = email
    request.session["wizard"] = wizard
    return RedirectResponse(url="/setup/step3", status_code=302)


# ------------------------------------------------------------------
# Step 3 — Control Plane & Worker Setup
# ------------------------------------------------------------------


@router.get("/setup/step3", response_class=HTMLResponse)
async def step3_get(request: Request) -> HTMLResponse:
    """Render the Control Plane & Worker Setup step."""
    init_db()
    if admin_exists() and _get_wizard(request).get("step", 1) < 3:
        return _setup_redirect()  # type: ignore[return-value]
    wizard = _get_wizard(request)
    if wizard.get("step", 1) < 3:
        return RedirectResponse(url="/setup/step2", status_code=302)  # type: ignore[return-value]
    return _render(
        request,
        "setup/step3_worker.html",
        {
            "current_step": 3,
            "cp_host": wizard.get("cp_host", "localhost"),
            "cp_port": wizard.get("cp_port", 8080),
        },
    )


@router.post("/setup/step3", response_class=HTMLResponse)
async def step3_post(
    request: Request,
    cp_host: str = Form("localhost"),
    cp_port: int = Form(8080),
    worker_name: Optional[str] = Form(None),
    worker_host: Optional[str] = Form(None),
    worker_port: Optional[int] = Form(None),
    skip_worker: Optional[str] = Form(None),
) -> Any:
    """Persist control-plane settings and optional first worker, then advance."""
    init_db()
    errors: Dict[str, str] = {}

    if not cp_host.strip():
        errors["cp_host"] = "Control Plane host is required."
    if not (1 <= cp_port <= 65535):
        errors["cp_port"] = "Port must be between 1 and 65535."

    add_worker = skip_worker is None  # "Skip for now" sets skip_worker
    if add_worker and worker_name:
        if not worker_host or not worker_host.strip():
            errors["worker_host"] = "Worker host is required."
        if worker_port is None or not (1 <= worker_port <= 65535):
            errors["worker_port"] = "Worker port must be between 1 and 65535."

    if errors:
        return _render(
            request,
            "setup/step3_worker.html",
            {
                "current_step": 3,
                "errors": errors,
                "cp_host": cp_host,
                "cp_port": cp_port,
                "worker_name": worker_name,
                "worker_host": worker_host,
                "worker_port": worker_port,
            },
        )

    set_setting("cp_host", cp_host.strip())
    set_setting("cp_port", str(cp_port))

    wizard = _get_wizard(request)
    wizard["cp_host"] = cp_host.strip()
    wizard["cp_port"] = cp_port

    if add_worker and worker_name and worker_name.strip():
        if not worker_host or worker_port is None:
            # Should have been caught by validation above; guard defensively.
            return RedirectResponse(url="/setup/step3", status_code=302)
        create_worker(
            name=worker_name.strip(),
            host=worker_host.strip(),
            port=worker_port,
        )
        wizard["worker_name"] = worker_name.strip()
        wizard["worker_host"] = worker_host.strip()
        wizard["worker_port"] = worker_port

    wizard["step"] = 4
    request.session["wizard"] = wizard
    return RedirectResponse(url="/setup/step4", status_code=302)


# ------------------------------------------------------------------
# Step 4 — Branding / Customisation
# ------------------------------------------------------------------


@router.get("/setup/step4", response_class=HTMLResponse)
async def step4_get(request: Request) -> HTMLResponse:
    """Render the Branding / Customisation step."""
    init_db()
    wizard = _get_wizard(request)
    if wizard.get("step", 1) < 4:
        return RedirectResponse(url="/setup/step3", status_code=302)  # type: ignore[return-value]
    return _render(
        request,
        "setup/step4_branding.html",
        {
            "current_step": 4,
            "site_name": wizard.get("site_name", "NexusAI"),
            "tagline": wizard.get("tagline", ""),
        },
    )


@router.post("/setup/step4", response_class=HTMLResponse)
async def step4_post(
    request: Request,
    site_name: str = Form("NexusAI"),
    tagline: str = Form(""),
) -> Any:
    """Persist branding settings and advance to the final review step."""
    init_db()
    errors: Dict[str, str] = {}
    if not site_name.strip():
        errors["site_name"] = "Site name cannot be empty."

    if errors:
        return _render(
            request,
            "setup/step4_branding.html",
            {
                "current_step": 4,
                "errors": errors,
                "site_name": site_name,
                "tagline": tagline,
            },
        )

    set_setting("site_name", site_name.strip())
    set_setting("tagline", tagline.strip())

    wizard = _get_wizard(request)
    wizard["site_name"] = site_name.strip()
    wizard["tagline"] = tagline.strip()
    wizard["step"] = 5
    request.session["wizard"] = wizard
    return RedirectResponse(url="/setup/step5", status_code=302)


# ------------------------------------------------------------------
# Step 5 — Review & Finish
# ------------------------------------------------------------------


@router.get("/setup/step5", response_class=HTMLResponse)
async def step5_get(request: Request) -> HTMLResponse:
    """Render the Review & Finish step."""
    init_db()
    wizard = _get_wizard(request)
    if wizard.get("step", 1) < 5:
        return RedirectResponse(url="/setup/step4", status_code=302)  # type: ignore[return-value]
    return _render(
        request,
        "setup/step5_finish.html",
        {
            "current_step": 5,
            "wizard": wizard,
        },
    )


@router.post("/setup/step5", response_class=RedirectResponse)
async def step5_post(request: Request) -> RedirectResponse:
    """Complete the wizard and redirect to the login page."""
    request.session.pop("wizard", None)
    return RedirectResponse(url="/login", status_code=302)


# ------------------------------------------------------------------
# Docker Compose download
# ------------------------------------------------------------------


@router.get("/setup/download-compose")
async def download_compose(request: Request) -> Response:
    """Generate and return a ``docker-compose.yml`` for the configured stack."""
    cp_host = get_setting("cp_host", "localhost") or "localhost"
    cp_port = get_setting("cp_port", "8080") or "8080"
    site_name = get_setting("site_name", "NexusAI") or "NexusAI"

    wizard = _get_wizard(request)
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

    return Response(
        content=compose,
        media_type="text/yaml",
        headers={
            "Content-Disposition": "attachment; filename=docker-compose.yml"
        },
    )
