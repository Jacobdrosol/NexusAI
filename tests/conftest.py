"""Shared pytest fixtures for NexusAI test suite."""
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

# ── Control plane ────────────────────────────────────────────────────────────

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def cp_app(tmp_path):
    """Create a control plane FastAPI app with empty registries (no YAML loading)."""
    from control_plane.registry.worker_registry import WorkerRegistry
    from control_plane.registry.bot_registry import BotRegistry
    from control_plane.scheduler.scheduler import Scheduler
    from control_plane.task_manager.task_manager import TaskManager
    from fastapi import FastAPI
    from control_plane.api import bots, tasks, workers as workers_api

    app = FastAPI(title="NexusAI Control Plane Test")
    app.include_router(tasks.router)
    app.include_router(bots.router)
    app.include_router(workers_api.router)

    worker_registry = WorkerRegistry()
    bot_registry = BotRegistry()
    scheduler = Scheduler(bot_registry, worker_registry)
    task_manager = TaskManager(scheduler, db_path=str(tmp_path / "tasks.db"))

    app.state.worker_registry = worker_registry
    app.state.bot_registry = bot_registry
    app.state.scheduler = scheduler
    app.state.task_manager = task_manager

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


@pytest_asyncio.fixture
async def cp_client(cp_app):
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        yield client


# ── Dashboard ────────────────────────────────────────────────────────────────

@pytest.fixture
def dashboard_app(tmp_path, monkeypatch):
    """Create a Flask dashboard app using a temporary SQLite database."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("NEXUSAI_SECRET_KEY", "test-secret-key")
    # Reload the db module so it picks up the new DATABASE_URL
    import importlib
    import dashboard.db as db_module
    importlib.reload(db_module)
    import dashboard.app as app_module
    importlib.reload(app_module)
    from dashboard.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app


@pytest.fixture
def dashboard_client(dashboard_app):
    with dashboard_app.test_client() as client:
        yield client
