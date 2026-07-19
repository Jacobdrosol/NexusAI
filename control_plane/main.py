import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from control_plane.agent_scheduler.engine import AgentScheduleEngine
from control_plane.api import (
    assignments,
    audit,
    bot_blueprints,
    bots,
    chat,
    database,
    keys,
    models_catalog,
    orchestration,
    platform_ai,
    projects,
    schedules,
    tasks,
    vault,
    workers,
)
from control_plane.audit.audit_log import AuditLog
from control_plane.chat.chat_manager import ChatManager
from control_plane.chat.pm_orchestrator import PMOrchestrator
from control_plane.connections.resolver import ConnectionResolver
from control_plane.github.webhook_store import GitHubWebhookStore
from control_plane.keys.key_vault import KeyVault
from control_plane.observability import install_observability
from control_plane.orchestration.assignment_service import AssignmentService
from control_plane.orchestration.run_store import OrchestrationRunStore
try:
    from control_plane.orchestration.template_store import OrchestrationTemplateStore as _OrchTemplateStore
    _HAS_TEMPLATE_STORE = True
except ImportError:
    _HAS_TEMPLATE_STORE = False
from control_plane.orchestration_workspace_store import OrchestrationWorkspaceStore
from control_plane.platform_ai.session_store import PlatformAISessionStore
from control_plane.platform_ai.runtime import PlatformAISessionRuntime
from control_plane.registry.bot_registry import BotRegistry
from control_plane.registry.model_registry import ModelRegistry
from control_plane.registry.project_registry import ProjectRegistry
from control_plane.repo_workspace_usage_store import RepoWorkspaceUsageStore
from control_plane.registry.worker_registry import WorkerRegistry
from control_plane.scheduler.scheduler import Scheduler
from control_plane.schedule_safety import (
    require_schedule_autonomy_safety,
    require_schedule_runtime_readiness,
)
from control_plane.schedule_payload_sources import materialize_system_schedule_payload
from control_plane.task_manager.task_manager import TaskManager
from control_plane.vault.mcp_broker import MCPBroker
from control_plane.vault.vault_manager import VaultManager
from control_plane.worker_probe import probe_worker
from control_plane.worker_probe_store import WorkerProbeStore
from shared.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("NEXUS_CONFIG_PATH", "config/nexus_config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load main config
    config = ConfigLoader.load_config(CONFIG_PATH)
    cp_cfg = config.get("control_plane", {})

    # Setup logging
    log_cfg = config.get("logging", {})
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("level", "INFO"), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Initialize registries
    worker_registry = WorkerRegistry()
    bot_registry = BotRegistry()
    project_registry = ProjectRegistry()
    model_registry = ModelRegistry()
    key_vault = KeyVault()
    chat_manager = ChatManager()
    vault_manager = VaultManager()
    mcp_broker = MCPBroker(vault_manager)
    github_webhook_store = GitHubWebhookStore()
    audit_log = AuditLog()
    repo_workspace_usage_store = RepoWorkspaceUsageStore()
    orchestration_workspace_store = OrchestrationWorkspaceStore()
    worker_probe_store = WorkerProbeStore()

    # Load from YAML configs
    workers_dir = cp_cfg.get("workers_config_dir", "config/workers")
    bots_dir = cp_cfg.get("bots_config_dir", "config/bots")
    worker_configs = ConfigLoader.load_all_from_dir(workers_dir)
    bot_configs = ConfigLoader.load_all_from_dir(bots_dir)

    if cp_cfg.get("seed_workers_from_config", False):
        await worker_registry.seed_from_configs(
            worker_configs,
            force=cp_cfg.get("force_seed_workers_from_config", False),
        )
    worker_ids = set(await worker_registry.get_worker_ids())
    if cp_cfg.get("seed_bots_from_config", False):
        force_seed = cp_cfg.get("force_seed_bots_from_config", False)
        await bot_registry.seed_from_configs(bot_configs, worker_ids, force=force_seed)

    # Initialize scheduler and task manager
    connection_resolver = ConnectionResolver()
    scheduler = Scheduler(
        bot_registry,
        worker_registry,
        key_vault=key_vault,
        model_registry=model_registry,
        project_registry=project_registry,
        connection_resolver=connection_resolver,
        worker_probe_store=worker_probe_store,
    )
    task_manager = TaskManager(
        scheduler,
        bot_registry=bot_registry,
        orchestration_workspace_store=orchestration_workspace_store,
        connection_resolver=connection_resolver,
    )
    pm_orchestrator = PMOrchestrator(
        bot_registry=bot_registry,
        scheduler=scheduler,
        task_manager=task_manager,
        chat_manager=chat_manager,
        orchestration_workspace_store=orchestration_workspace_store,
    )
    orchestration_run_store = OrchestrationRunStore()
    orchestration_template_store = _OrchTemplateStore() if _HAS_TEMPLATE_STORE else None
    assignment_service = AssignmentService(
        chat_manager=chat_manager,
        bot_registry=bot_registry,
        task_manager=task_manager,
        pm_orchestrator=pm_orchestrator,
        run_store=orchestration_run_store,
        connection_resolver=connection_resolver,
    )
    platform_ai_session_store = PlatformAISessionStore()
    platform_ai_runtime = PlatformAISessionRuntime(
        platform_ai_session_store,
        assignment_service=assignment_service,
        run_store=orchestration_run_store,
        task_manager=task_manager,
        bot_registry=bot_registry,
        scheduler=scheduler,
        worker_registry=worker_registry,
        connection_resolver=connection_resolver,
        worker_probe_store=worker_probe_store,
        key_vault=key_vault,
        model_registry=model_registry,
    )
    async def _schedule_dispatch_guard(schedule: dict) -> None:
        await require_schedule_autonomy_safety(
            schedule,
            bot_registry=bot_registry,
            only_when_active=False,
        )
        await require_schedule_runtime_readiness(
            schedule,
            bot_registry=bot_registry,
            worker_registry=worker_registry,
            connection_resolver=connection_resolver,
            worker_probe_store=worker_probe_store,
            key_vault=key_vault,
            model_registry=model_registry,
        )

    async def _schedule_payload_materializer(schedule: dict) -> dict:
        return await materialize_system_schedule_payload(
            schedule,
            worker_registry=worker_registry,
            worker_probe_store=worker_probe_store,
            bot_registry=bot_registry,
            task_manager=task_manager,
            schedule_engine=agent_schedule_engine,
        )

    agent_schedule_engine = AgentScheduleEngine(
        assignment_service=assignment_service,
        task_manager=task_manager,
        autonomy_guard=_schedule_dispatch_guard,
        payload_materializer=_schedule_payload_materializer,
    )

    # Store on app state
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
    app.state.repo_workspace_usage_store = repo_workspace_usage_store
    app.state.orchestration_workspace_store = orchestration_workspace_store
    app.state.worker_probe_store = worker_probe_store
    app.state.scheduler = scheduler
    app.state.task_manager = task_manager
    app.state.pm_orchestrator = pm_orchestrator
    app.state.connection_resolver = connection_resolver
    app.state.orchestration_run_store = orchestration_run_store
    app.state.orchestration_template_store = orchestration_template_store
    app.state.assignment_service = assignment_service
    app.state.platform_ai_session_store = platform_ai_session_store
    app.state.platform_ai_runtime = platform_ai_runtime
    app.state.agent_schedule_engine = agent_schedule_engine
    app.state.config = config
    app.state.control_plane_api_token = os.environ.get("CONTROL_PLANE_API_TOKEN", "").strip()

    # Background heartbeat checker
    heartbeat_timeout = cp_cfg.get("heartbeat_timeout_seconds", 30)
    heartbeat_task = asyncio.create_task(
        _heartbeat_checker(worker_registry, heartbeat_timeout)
    )
    schedule_tick_seconds = max(2, int(cp_cfg.get("agent_schedule_tick_seconds", 10)))
    schedule_task = asyncio.create_task(
        _agent_schedule_loop(agent_schedule_engine, tick_seconds=schedule_tick_seconds)
    )
    try:
        configured_probe_interval = int(cp_cfg.get("worker_probe_interval_seconds", 120))
    except (TypeError, ValueError):
        configured_probe_interval = 120
    probe_interval_seconds = min(3600, max(15, configured_probe_interval))
    worker_probe_task = asyncio.create_task(
        _worker_probe_loop(
            worker_registry,
            worker_probe_store,
            interval_seconds=probe_interval_seconds,
        )
    )

    logger.info("NexusAI Control Plane started")
    yield

    heartbeat_task.cancel()
    schedule_task.cancel()
    worker_probe_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    try:
        await schedule_task
    except asyncio.CancelledError:
        pass
    try:
        await worker_probe_task
    except asyncio.CancelledError:
        pass
    logger.info("NexusAI Control Plane stopped")


async def _heartbeat_checker(worker_registry: WorkerRegistry, timeout_seconds: int) -> None:
    while True:
        await asyncio.sleep(10)
        try:
            workers = await worker_registry.list()
            now = datetime.now(timezone.utc)
            for worker in workers:
                last_hb = await worker_registry.get_last_heartbeat(worker.id)
                if last_hb is None:
                    continue
                elapsed = (now - last_hb).total_seconds()
                if elapsed > timeout_seconds and worker.status == "online":
                    logger.warning(
                        "Worker %s heartbeat timeout (%.1fs), marking offline",
                        worker.id,
                        elapsed,
                    )
                    await worker_registry.update_status(worker.id, "offline")
        except Exception as e:
            logger.error("Heartbeat checker error: %s", e)


async def _agent_schedule_loop(agent_schedule_engine: AgentScheduleEngine, *, tick_seconds: int) -> None:
    while True:
        await asyncio.sleep(max(2, int(tick_seconds)))
        try:
            await agent_schedule_engine.tick_once()
        except Exception as e:
            logger.error("Agent schedule tick error: %s", e)


async def _worker_probe_loop(
    worker_registry: WorkerRegistry,
    worker_probe_store: WorkerProbeStore,
    *,
    interval_seconds: int,
) -> None:
    """Continuously record runtime evidence without changing worker state."""
    while True:
        # Let actively managed workers register before probing their runtime.
        # Registration also queues a short delayed probe, so live readiness
        # recovers promptly while the periodic loop handles ongoing drift.
        await asyncio.sleep(max(15, int(interval_seconds)))
        try:
            workers = await worker_registry.list()
            for worker in workers:
                try:
                    result = await probe_worker(worker)
                    await worker_probe_store.record(result)
                except Exception as exc:
                    logger.warning("Worker probe failed for %s: %s", worker.id, exc)
        except Exception as exc:
            logger.error("Worker probe loop error: %s", exc)


def create_app() -> FastAPI:
    app = FastAPI(
        title="NexusAI Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )

    install_observability(app)

    app.include_router(tasks.router)
    app.include_router(bot_blueprints.router)
    app.include_router(bots.router)
    app.include_router(workers.router)
    app.include_router(projects.router)
    app.include_router(keys.router)
    app.include_router(models_catalog.router)
    app.include_router(chat.router)
    app.include_router(assignments.router)
    app.include_router(platform_ai.router)
    app.include_router(orchestration.router)
    app.include_router(schedules.router)
    app.include_router(vault.router)
    app.include_router(audit.router)
    app.include_router(database.router)

    @app.middleware("http")
    async def control_plane_auth_middleware(request: Request, call_next):
        token = (getattr(request.app.state, "control_plane_api_token", "") or "").strip()
        if not token:
            return await call_next(request)

        path = request.url.path
        if path in {"/health", "/docs", "/redoc", "/openapi.json"}:
            return await call_next(request)
        if request.method.upper() == "POST" and re.fullmatch(r"/v1/bots/[^/]+/trigger", path):
            return await call_next(request)

        header_token = (request.headers.get("X-Nexus-API-Key", "") or "").strip()
        auth_header = (request.headers.get("Authorization", "") or "").strip()
        bearer = ""
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()

        if header_token == token or bearer == token:
            return await call_next(request)

        return JSONResponse(status_code=401, content={"detail": "unauthorized"})

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    config = ConfigLoader.load_config(CONFIG_PATH)
    cp_cfg = config.get("control_plane", {})
    uvicorn.run(
        "control_plane.main:app",
        host=cp_cfg.get("host", "0.0.0.0"),
        port=cp_cfg.get("port", 8000),
        reload=False,
    )
