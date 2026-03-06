"""Flask application factory for the NexusAI dashboard."""
from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
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
    from dashboard.auth import bp as auth_bp
    from dashboard.onboarding import bp as onboarding_bp
    from dashboard.routes.bots import bp as bots_bp
    from dashboard.routes.chat import bp as chat_bp
    from dashboard.routes.connections import bp as connections_bp
    from dashboard.routes.events import bp as events_bp
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
    app.register_blueprint(chat_bp)
    app.register_blueprint(connections_bp)
    app.register_blueprint(vault_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(events_bp)
    app.register_blueprint(settings_bp)

    # Exempt SSE from CSRF (it's GET-only)
    csrf.exempt(events_bp)

    # Main blueprint for overview + root redirect
    from flask import Blueprint

    main_bp = Blueprint("main", __name__)

    @main_bp.get("/")
    @login_required
    def index():
        """Overview / home page."""
        from dashboard.cp_client import get_cp_client

        cp = get_cp_client()
        cp_workers = cp.list_workers()
        cp_bots = cp.list_bots()
        cp_tasks = cp.list_tasks()
        cp_available = cp_workers is not None or cp_bots is not None or cp_tasks is not None

        if cp_available:
            workers = cp_workers or []
            bots = cp_bots or []
            tasks = cp_tasks or []
            total_workers = len(workers)
            online_workers = sum(1 for w in workers if w.get("status") == "online")
            offline_workers = sum(1 for w in workers if w.get("status") == "offline")
            active_bots = sum(1 for b in bots if b.get("enabled"))
            queued = sum(1 for t in tasks if t.get("status") == "queued")
            running = sum(1 for t in tasks if t.get("status") == "running")
            completed = sum(1 for t in tasks if t.get("status") == "completed")
            failed = sum(1 for t in tasks if t.get("status") == "failed")

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
                running = db.query(Task).filter(Task.status == "running").count()
                completed = db.query(Task).filter(Task.status == "completed").count()
                failed = db.query(Task).filter(Task.status == "failed").count()

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
                "tasks_running": running,
                "tasks_completed": completed,
                "tasks_failed": failed,
            },
            worker_health=worker_health,
            recent_activity=recent_activity,
            system_alerts=system_alerts,
            quick_links=quick_links,
        )

    @app.context_processor
    def inject_cp_status():
        from dashboard.cp_client import get_cp_client
        try:
            available = get_cp_client().health()
        except Exception:
            available = False
        return {"cp_available": available}

    app.register_blueprint(main_bp)

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
