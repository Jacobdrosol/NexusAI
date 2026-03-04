"""Flask application factory for the NexusAI dashboard."""
from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, url_for
from flask_login import LoginManager, login_required
from flask_wtf.csrf import CSRFProtect

from dashboard.db import get_db, init_db
from dashboard.models import User

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


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

    # Register blueprints
    from dashboard.auth import bp as auth_bp
    from dashboard.onboarding import bp as onboarding_bp
    from dashboard.routes.bots import bp as bots_bp
    from dashboard.routes.events import bp as events_bp
    from dashboard.routes.tasks import bp as tasks_bp
    from dashboard.routes.users import bp as users_bp
    from dashboard.routes.workers import bp as workers_bp
    from dashboard.settings import bp as settings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(workers_bp)
    app.register_blueprint(bots_bp)
    app.register_blueprint(tasks_bp)
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
        db = get_db()
        try:
            from dashboard.models import Bot, Task, Worker

            total_workers = db.query(Worker).count()
            online_workers = db.query(Worker).filter(Worker.status == "online").count()
            offline_workers = db.query(Worker).filter(Worker.status == "offline").count()
            active_bots = db.query(Bot).filter(Bot.enabled.is_(True)).count()
            queued = db.query(Task).filter(Task.status == "queued").count()
            running = db.query(Task).filter(Task.status == "running").count()
            completed = db.query(Task).filter(Task.status == "completed").count()
            failed = db.query(Task).filter(Task.status == "failed").count()
        finally:
            db.close()

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
        )

    app.register_blueprint(main_bp)

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
