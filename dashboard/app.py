"""Flask application factory for the NexusAI dashboard."""
from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, logout_user
from flask_wtf.csrf import CSRFProtect

from dashboard.db import get_db, init_db
from dashboard.models import User

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
_DATA_DIR = Path(__file__).parent.parent / "data"


def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=str(_TEMPLATES_DIR),
        static_folder=str(_STATIC_DIR),
    )

    # Secret key — must be set via env var in production
    app.config["SECRET_KEY"] = os.environ.get(
        "NEXUSAI_SECRET_KEY", "dev-secret-change-in-production"
    )
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=60)
    slow_request_threshold = float(os.environ.get("DASHBOARD_SLOW_REQUEST_SECONDS", "1.5"))

    # Initialise extensions
    csrf = CSRFProtect(app)
    login_manager = LoginManager(app)
    login_manager.login_view = "auth.login_get"  # type: ignore[assignment]
    login_manager.login_message = ""  # suppress the default "please log in" flash
    login_manager.login_message_category = "error"

    @login_manager.user_loader
    def load_user(user_id: str):
        """Load a user from the database by ID."""
        db = get_db()
        try:
            return db.get(User, int(user_id))
        finally:
            db.close()

    # Initialise database
    init_db()

    # Initialise settings singleton so dashboard/settings.py can use it
    from shared.settings_manager import SettingsManager as _SM
    _SM.instance(db_path=str(_DATA_DIR / "nexusai.db"))

    # Register blueprints
    from dashboard.auth import api_login_post, api_logout_post, bp as auth_bp
    from dashboard.onboarding import bp as onboarding_bp
    from dashboard.routes.bots import bp as bots_bp
    from dashboard.routes.chat import bp as chat_bp
    from dashboard.routes.connections import bp as connections_bp
    from dashboard.routes.events import bp as events_bp
    from dashboard.routes.pipelines import bp as pipelines_bp
    from dashboard.routes.platform_ai import bp as platform_ai_bp
    from dashboard.routes.projects import bp as projects_bp
    from dashboard.routes.tasks import bp as tasks_bp
    from dashboard.routes.users import bp as users_bp
    from dashboard.routes.vault import bp as vault_bp
    from dashboard.routes.workers import bp as workers_bp
    from dashboard.settings import bp as settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(workers_bp)
    app.register_blueprint(bots_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(projects_bp)
    app.register_blueprint(pipelines_bp)
    app.register_blueprint(platform_ai_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(connections_bp)
    app.register_blueprint(vault_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(settings_bp)

    # Exempt SSE from CSRF (it's GET-only)
    csrf.exempt(events_bp)
    csrf.exempt(api_login_post)
    csrf.exempt(api_logout_post)

    # Main blueprint for overview + root redirect
    from flask import Blueprint

    main_bp = Blueprint("main", __name__)

    def _cp_list_tasks_safe(cp, **kwargs):
        try:
            return cp.list_tasks(**kwargs)
        except TypeError:
            return cp.list_tasks()

    @main_bp.get("/")
    @login_required
    def index():
        """Overview / home page."""
        from dashboard.bot_launch import launchable_bots
        from dashboard.cp_client import get_cp_client
        from dashboard.models import User

        cp = get_cp_client()
        cp_health_ok = cp.health()
        cp_workers = cp.list_workers()
        cp_bots = cp.list_bots()
        cp_projects = cp.list_projects()
        cp_tasks = _cp_list_tasks_safe(cp, limit=200, include_content=False)
        cp_endpoint_checks = cp.probe_paths(
            ["/health", "/v1/projects", "/v1/bots", "/v1/workers"]
        )
        cp_auth_ok = (
            cp_workers is not None and cp_bots is not None and cp_projects is not None
        )
        cp_available = cp_auth_ok or cp_tasks is not None
        overview_launch_bots = launchable_bots(cp_bots or [], surface="overview")

        if cp_available:
            workers = cp_workers or []
            bots = cp_bots or []
            tasks = cp_tasks or []
            total_workers = len(workers)
            online_workers = sum(1 for w in workers if w.get("status") == "online")
            offline_workers = sum(1 for w in workers if w.get("status") == "offline")
            active_bots = sum(1 for b in bots if b.get("enabled"))
            queued = sum(1 for t in tasks if t.get("status") == "queued")
            blocked = sum(1 for t in tasks if t.get("status") == "blocked")
            running = sum(1 for t in tasks if t.get("status") == "running")
            completed = sum(1 for t in tasks if t.get("status") == "completed")
            failed = sum(1 for t in tasks if t.get("status") == "failed")
            retried = sum(1 for t in tasks if t.get("status") == "retried")
            cancelled = sum(1 for t in tasks if t.get("status") == "cancelled")

            worker_health = []
            for w in workers[:12]:
                m = w.get("metrics") or {}
                gpu_util = m.get("gpu_utilization") or []
                gpu_avg = (
                    (sum(float(x) for x in gpu_util) / len(gpu_util))
                    if isinstance(gpu_util, list) and gpu_util
                    else 0.0
                )
                worker_health.append(
                    {
                        "id": w.get("id"),
                        "name": w.get("name") or w.get("id"),
                        "status": w.get("status", "offline"),
                        "enabled": bool(w.get("enabled", True)),
                        "load": float(m.get("load") or 0.0),
                        "queue_depth": int(m.get("queue_depth") or 0),
                        "gpu_avg": float(gpu_avg),
                    }
                )

            recent_activity = []
            sorted_tasks = sorted(
                tasks,
                key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""),
                reverse=True,
            )
            for t in sorted_tasks[:12]:
                recent_activity.append(
                    {
                        "id": t.get("id"),
                        "status": t.get("status"),
                        "bot_id": t.get("bot_id"),
                        "updated_at": t.get("updated_at") or t.get("created_at") or "",
                    }
                )
        else:
            db = get_db()
            try:
                from dashboard.models import Bot, Task, Worker

                workers = db.query(Worker).all()
                total_workers = db.query(Worker).count()
                online_workers = db.query(Worker).filter(Worker.status == "online").count()
                offline_workers = db.query(Worker).filter(Worker.status == "offline").count()
                active_bots = db.query(Bot).filter(Bot.enabled.is_(True)).count()
                queued = db.query(Task).filter(Task.status == "queued").count()
                blocked = db.query(Task).filter(Task.status == "blocked").count()
                running = db.query(Task).filter(Task.status == "running").count()
                completed = db.query(Task).filter(Task.status == "completed").count()
                failed = db.query(Task).filter(Task.status == "failed").count()
                retried = db.query(Task).filter(Task.status == "retried").count()
                cancelled = db.query(Task).filter(Task.status == "cancelled").count()

                worker_health = []
                for w in workers[:12]:
                    m = w.metrics_as_dict()
                    gpu_util = m.get("gpu_utilization") or []
                    gpu_avg = (
                        (sum(float(x) for x in gpu_util) / len(gpu_util))
                        if isinstance(gpu_util, list) and gpu_util
                        else 0.0
                    )
                    worker_health.append(
                        {
                            "id": w.id,
                            "name": w.name,
                            "status": w.status,
                            "enabled": bool(w.enabled),
                            "load": float(m.get("load") or 0.0),
                            "queue_depth": int(m.get("queue_depth") or 0),
                            "gpu_avg": float(gpu_avg),
                        }
                    )

                task_rows = db.query(Task).order_by(Task.updated_at.desc()).limit(12).all()
                recent_activity = []
                for t in task_rows:
                    recent_activity.append(
                        {
                            "id": t.id,
                            "status": t.status,
                            "bot_id": t.bot_id,
                            "updated_at": t.updated_at.isoformat() if t.updated_at else "",
                        }
                    )
            finally:
                db.close()

        db = get_db()
        try:
            admin_count = db.query(User).filter(User.role == "admin", User.is_active.is_(True)).count()
            user_count = db.query(User).filter(User.is_active.is_(True)).count()
        finally:
            db.close()

        secret_key = (os.environ.get("NEXUSAI_SECRET_KEY", "") or "").strip()
        cp_token = (os.environ.get("CONTROL_PLANE_API_TOKEN", "") or "").strip()
        cp_url = (
            str(getattr(cp, "base_url", "") or "").strip()
            or (os.environ.get("CONTROL_PLANE_URL", "") or "").strip()
        )
        dashboard_secret_ok = bool(secret_key and secret_key != "dev-secret-change-in-production")
        cp_token_ok = bool(cp_token)
        dashboard_cloud_policy = (os.environ.get("NEXUSAI_CLOUD_CONTEXT_POLICY", "") or "").strip().lower()
        worker_cloud_policy = (os.environ.get("NEXUS_WORKER_CLOUD_CONTEXT_POLICY", "") or "").strip().lower()
        safe_cloud_policies = {"block", "redact"}
        cloud_policy_ok = (
            dashboard_cloud_policy in safe_cloud_policies
            and worker_cloud_policy in safe_cloud_policies
        )

        cp_auth_detail = "Control plane API is reachable and authenticated."
        if not cp_health_ok:
            cp_auth_detail = (
                "Control plane health check failed. Verify CONTROL_PLANE_URL and service reachability."
            )
        elif not cp_auth_ok:
            cp_auth_detail = cp.unavailable_reason()

        setup_checklist = [
            {
                "key": "dashboard-session-secret",
                "label": "Dashboard session secret configured",
                "ok": dashboard_secret_ok,
                "required": True,
                "detail": (
                    "NEXUSAI_SECRET_KEY is set."
                    if dashboard_secret_ok
                    else "Set NEXUSAI_SECRET_KEY to a long random value before shared use."
                ),
                "href": url_for("settings.settings_page"),
                "cta": "Review Settings",
            },
            {
                "key": "control-plane-config",
                "label": "Control plane URL and token configured",
                "ok": bool(cp_url and cp_token_ok),
                "required": True,
                "detail": (
                    f"CONTROL_PLANE_URL is {cp_url}."
                    if cp_url and cp_token_ok
                    else "Set CONTROL_PLANE_URL and CONTROL_PLANE_API_TOKEN in the runtime environment."
                ),
                "href": url_for("settings.settings_page"),
                "cta": "Open Settings",
            },
            {
                "key": "control-plane-health-auth",
                "label": "Control plane health and auth",
                "ok": cp_health_ok and cp_auth_ok,
                "required": True,
                "detail": cp_auth_detail,
                "href": url_for("projects.projects_page"),
                "cta": "Open Projects",
            },
            {
                "key": "safe-cloud-context-policy",
                "label": "Safe cloud context policy",
                "ok": cloud_policy_ok,
                "required": True,
                "detail": (
                    f"Dashboard={dashboard_cloud_policy or 'unset'}, worker={worker_cloud_policy or 'unset'}."
                    if cloud_policy_ok
                    else "Set both NEXUSAI_CLOUD_CONTEXT_POLICY and NEXUS_WORKER_CLOUD_CONTEXT_POLICY to block or redact."
                ),
                "href": url_for("settings.settings_page"),
                "cta": "Review Settings",
            },
            {
                "key": "admin-account-ready",
                "label": "Admin account ready",
                "ok": admin_count > 0,
                "required": True,
                "detail": (
                    f"{admin_count} active admin account(s) available."
                    if admin_count > 0
                    else "Complete onboarding or create an admin user."
                ),
                "href": url_for("settings.settings_page"),
                "cta": "Open Settings",
            },
            {
                "key": "worker-registration",
                "label": "Worker registration",
                "ok": total_workers > 0,
                "required": False,
                "detail": (
                    f"{total_workers} worker(s) registered."
                    if total_workers > 0
                    else "Register at least one worker before bot/task UAT."
                ),
                "href": url_for("workers.workers_page"),
                "cta": "Open Workers",
            },
            {
                "key": "bot-configuration",
                "label": "Bot configuration",
                "ok": active_bots > 0,
                "required": False,
                "detail": (
                    f"{active_bots} enabled bot(s) configured."
                    if active_bots > 0
                    else "Create at least one bot with a valid backend chain."
                ),
                "href": url_for("bots.bots_page"),
                "cta": "Open Bots",
            },
            {
                "key": "project-bootstrap",
                "label": "Project bootstrap",
                "ok": len(cp_projects or []) > 0,
                "required": False,
                "detail": (
                    f"{len(cp_projects or [])} project(s) visible from control plane."
                    if cp_projects is not None
                    else "Projects check is blocked until control-plane auth succeeds."
                ),
                "href": url_for("projects.projects_page"),
                "cta": "Open Projects",
            },
            {
                "key": "user-access-validation",
                "label": "User access validation",
                "ok": user_count > 0,
                "required": False,
                "detail": f"{user_count} active user account(s) available.",
                "href": url_for("settings.settings_page"),
                "cta": "Open Settings",
            },
        ]
        setup_required_total = sum(1 for item in setup_checklist if item["required"])
        setup_required_complete = sum(
            1 for item in setup_checklist if item["required"] and item["ok"]
        )
        setup_recommended_total = sum(1 for item in setup_checklist if not item["required"])
        setup_recommended_complete = sum(
            1 for item in setup_checklist if not item["required"] and item["ok"]
        )

        system_alerts = []
        if not cp_available:
            system_alerts.append(
                {
                    "level": "warning",
                    "message": "Control plane is unavailable; overview is using local fallback data.",
                }
            )
        if offline_workers > 0:
            system_alerts.append(
                {"level": "warning", "message": f"{offline_workers} worker(s) are offline."}
            )
        if failed > 0:
            system_alerts.append(
                {"level": "error", "message": f"{failed} task(s) are currently in failed state."}
            )
        if not system_alerts:
            system_alerts.append({"level": "info", "message": "No critical alerts. System is stable."})

        quick_links = [
            {"label": "Open Tasks", "href": url_for("tasks.tasks_page")},
            {"label": "Manage Workers", "href": url_for("workers.workers_page")},
            {"label": "Configure Bots", "href": url_for("bots.bots_page")},
            {"label": "Project Dashboard", "href": url_for("projects.projects_page")},
            {"label": "Chat Workspace", "href": url_for("chat.chat_page")},
            {"label": "Vault Browser", "href": url_for("vault.vault_page")},
        ]

        return render_template(
            "index.html",
            stats={
                "workers_total": total_workers,
                "workers_online": online_workers,
                "workers_offline": offline_workers,
                "bots_active": active_bots,
                "tasks_queued": queued,
                "tasks_blocked": blocked,
                "tasks_running": running,
                "tasks_completed": completed,
                "tasks_failed": failed,
                "tasks_retried": retried,
                "tasks_cancelled": cancelled,
            },
            worker_health=worker_health,
            recent_activity=recent_activity,
            launchable_bots=overview_launch_bots,
            system_alerts=system_alerts,
            quick_links=quick_links,
            setup_checklist=setup_checklist,
            setup_required_total=setup_required_total,
            setup_required_complete=setup_required_complete,
            setup_recommended_total=setup_recommended_total,
            setup_recommended_complete=setup_recommended_complete,
            cp_endpoint_checks=cp_endpoint_checks,
        )

    app.register_blueprint(main_bp)

    @app.before_request
    def _track_request_start():
        g._request_start_ts = time.perf_counter()

    @app.after_request
    def _log_slow_requests(response):
        start = getattr(g, "_request_start_ts", None)
        if start is None:
            return response
        elapsed = time.perf_counter() - start
        if elapsed >= slow_request_threshold:
            app.logger.warning(
                "slow_request method=%s path=%s status=%s elapsed_s=%.3f",
                request.method,
                request.path,
                response.status_code,
                elapsed,
            )
        return response

    @app.before_request
    def enforce_session_inactivity_timeout():
        if request.endpoint == "static":
            return None
        if not current_user.is_authenticated:
            return None

        timeout_minutes = 60
        try:
            timeout_raw = _SM.instance().get("session_timeout_minutes", 60)
            timeout_minutes = int(timeout_raw)
        except Exception:
            timeout_minutes = 60
        timeout_minutes = max(1, timeout_minutes)
        app.permanent_session_lifetime = timedelta(minutes=timeout_minutes)

        now_ts = int(time.time())
        last_ts = int(session.get("last_activity_ts", now_ts))
        if (now_ts - last_ts) > timeout_minutes * 60:
            logout_user()
            session.clear()
            return redirect(url_for("auth.login_get"))

        session.permanent = True
        session["last_activity_ts"] = now_ts
        return None

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"})

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("DASHBOARD_PORT", "5000")),
        debug=False,
    )
