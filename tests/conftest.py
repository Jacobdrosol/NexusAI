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
    from control_plane.keys.key_vault import KeyVault
    from control_plane.chat.chat_manager import ChatManager
    from control_plane.chat.pm_orchestrator import PMOrchestrator
    from control_plane.github.webhook_store import GitHubWebhookStore
    from control_plane.audit.audit_log import AuditLog
    from control_plane.registry.model_registry import ModelRegistry
    from control_plane.registry.project_registry import ProjectRegistry
    from control_plane.scheduler.scheduler import Scheduler
    from control_plane.task_manager.task_manager import TaskManager
    from control_plane.vault.mcp_broker import MCPBroker
    from control_plane.vault.vault_manager import VaultManager
    from control_plane.observability import install_observability
    from fastapi import FastAPI
    from control_plane.api import audit, bots, chat, keys, models_catalog, projects, tasks, vault, workers as workers_api

    app = FastAPI(title="NexusAI Control Plane Test")
    install_observability(app)
    app.include_router(tasks.router)
    app.include_router(bots.router)
    app.include_router(workers_api.router)
    app.include_router(projects.router)
    app.include_router(keys.router)
    app.include_router(models_catalog.router)
    app.include_router(chat.router)
    app.include_router(vault.router)
    app.include_router(audit.router)

    worker_registry = WorkerRegistry()
    bot_registry = BotRegistry()
    project_registry = ProjectRegistry(db_path=str(tmp_path / "projects.db"))
    model_registry = ModelRegistry(db_path=str(tmp_path / "models.db"))
    key_vault = KeyVault(db_path=str(tmp_path / "keys.db"), master_key="test-master-key")
    chat_manager = ChatManager(db_path=str(tmp_path / "chat.db"))
    vault_manager = VaultManager(db_path=str(tmp_path / "vault.db"))
    mcp_broker = MCPBroker(vault_manager=vault_manager)
    github_webhook_store = GitHubWebhookStore(db_path=str(tmp_path / "github_webhooks.db"))
    audit_log = AuditLog(db_path=str(tmp_path / "audit.db"))
    scheduler = Scheduler(
        bot_registry,
        worker_registry,
        key_vault=key_vault,
        model_registry=model_registry,
    )
    task_manager = TaskManager(scheduler, db_path=str(tmp_path / "tasks.db"))
    pm_orchestrator = PMOrchestrator(
        bot_registry=bot_registry,
        scheduler=scheduler,
        task_manager=task_manager,
        chat_manager=chat_manager,
    )

    app.state.worker_registry = worker_registry
    app.state.bot_registry = bot_registry
    app.state.project_registry = project_registry
    app.state.model_registry = model_registry
    app.state.key_vault = key_vault
    app.state.chat_manager = chat_manager
    app.state.vault_manager = vault_manager
    app.state.mcp_broker = mcp_broker
    app.state.github_webhook_store = github_webhook_store
    app.state.audit_log = audit_log
    app.state.scheduler = scheduler
    app.state.task_manager = task_manager
    app.state.pm_orchestrator = pm_orchestrator
    app.state.control_plane_api_token = ""

    @app.middleware("http")
    async def _auth_middleware(request, call_next):
        token = (getattr(request.app.state, "control_plane_api_token", "") or "").strip()
        if not token:
            return await call_next(request)
        if request.url.path in {"/health", "/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)
        header_token = (request.headers.get("X-Nexus-API-Key", "") or "").strip()
        auth_header = (request.headers.get("Authorization", "") or "").strip()
        bearer = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        if header_token == token or bearer == token:
            return await call_next(request)
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

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
