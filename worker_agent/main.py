import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from worker_agent.api import capabilities, health, infer
from worker_agent.gpu_monitor import get_gpu_info
from worker_agent.observability import install_observability
from shared.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

WORKER_CONFIG_PATH = os.environ.get("WORKER_CONFIG_PATH", "config/workers/local_worker.yaml")
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "15"))
CONTROL_PLANE_API_TOKEN = os.environ.get("CONTROL_PLANE_API_TOKEN", "").strip()


def _positive_float_env(name: str) -> float | None:
    raw_value = str(os.environ.get(name, "") or "").strip()
    if not raw_value:
        return None
    try:
        value = float(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid %s value for worker runtime registration", name)
        return None
    if value <= 0:
        logger.warning("Ignoring non-positive %s value for worker runtime registration", name)
        return None
    return value


def _positive_int_env(name: str) -> int | None:
    raw_value = str(os.environ.get(name, "") or "").strip()
    if not raw_value:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        logger.warning("Ignoring invalid %s value for worker runtime registration", name)
        return None
    if value <= 0:
        logger.warning("Ignoring non-positive %s value for worker runtime registration", name)
        return None
    return value


def _with_runtime_limits_from_environment(worker_config: dict[str, Any]) -> dict[str, Any]:
    """Declare container limits to the control plane without exposing host state."""
    configured_limits = worker_config.get("runtime_limits")
    runtime_limits = dict(configured_limits) if isinstance(configured_limits, dict) else {}

    cpus = _positive_float_env("NEXUSAI_WORKER_RUNTIME_CPUS")
    if cpus is not None:
        runtime_limits["cpus"] = cpus

    memory_limit = str(os.environ.get("NEXUSAI_WORKER_RUNTIME_MEMORY_LIMIT", "") or "").strip()
    if memory_limit:
        runtime_limits["memory_limit"] = memory_limit

    memory_reservation = str(
        os.environ.get("NEXUSAI_WORKER_RUNTIME_MEMORY_RESERVATION", "") or ""
    ).strip()
    if memory_reservation:
        runtime_limits["memory_reservation"] = memory_reservation

    pids_limit = _positive_int_env("NEXUSAI_WORKER_RUNTIME_PIDS_LIMIT")
    if pids_limit is not None:
        runtime_limits["pids_limit"] = pids_limit

    if not runtime_limits:
        return worker_config
    return {**worker_config, "runtime_limits": runtime_limits}


def _cp_headers() -> dict:
    if not CONTROL_PLANE_API_TOKEN:
        return {}
    return {"X-Nexus-API-Key": CONTROL_PLANE_API_TOKEN}


async def _register_with_control_plane(worker_config: dict, client: httpx.AsyncClient) -> bool:
    worker_id = str(worker_config.get("id") or "unknown")
    try:
        response = await client.post(
            f"{CONTROL_PLANE_URL}/v1/workers",
            json=worker_config,
            headers=_cp_headers(),
        )
        response.raise_for_status()
        logger.info("Registered with control plane as %s", worker_id)
        return True
    except Exception as exc:
        logger.warning("Could not register with control plane as %s: %s", worker_id, exc)
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # Load worker config
    try:
        worker_config = ConfigLoader.load_yaml(WORKER_CONFIG_PATH)
    except Exception as e:
        logger.warning("Failed to load worker config from %s: %s", WORKER_CONFIG_PATH, e)
        worker_config = {"id": "unknown-worker", "name": "Unknown Worker", "host": "localhost", "port": 8080, "capabilities": []}
    worker_config = _with_runtime_limits_from_environment(worker_config)

    app.state.worker_config = worker_config

    worker_id = worker_config.get("id", "unknown")
    async with httpx.AsyncClient(timeout=10.0) as client:
        await _register_with_control_plane(worker_config, client)

    # Background heartbeat
    heartbeat_task = asyncio.create_task(_send_heartbeats(worker_id, app))

    logger.info("NexusAI Worker Agent started (id=%s)", worker_id)
    yield

    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    logger.info("NexusAI Worker Agent stopped")


async def _send_heartbeats(worker_id: str, app: FastAPI) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            gpu_info = get_gpu_info()
            gpu_util = [
                (g["memory_used"] / g["memory_total"] * 100) if g["memory_total"] > 0 else 0.0
                for g in gpu_info
            ]
            queue_depth = int(getattr(app.state, "inference_inflight", 0) or 0)
            metrics = {"queue_depth": queue_depth}
            if gpu_util:
                metrics["gpu_utilization"] = gpu_util
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{CONTROL_PLANE_URL}/v1/workers/{worker_id}/heartbeat",
                    json={"metrics": metrics},
                    headers=_cp_headers(),
                )
                if response.status_code == 404:
                    worker_config = getattr(app.state, "worker_config", {})
                    if isinstance(worker_config, dict):
                        await _register_with_control_plane(worker_config, client)
                    continue
                response.raise_for_status()
        except Exception as e:
            logger.warning("Heartbeat failed: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(
        title="NexusAI Worker Agent",
        version="0.1.0",
        lifespan=lifespan,
    )
    install_observability(app)

    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(infer.router)

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"error": type(exc).__name__, "detail": str(exc)},
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    worker_config = {}
    try:
        worker_config = ConfigLoader.load_yaml(WORKER_CONFIG_PATH)
    except Exception:
        pass

    uvicorn.run(
        "worker_agent.main:app",
        host=worker_config.get("host", "0.0.0.0"),
        port=worker_config.get("port", 8080),
        reload=False,
    )
