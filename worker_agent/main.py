import asyncio
import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from worker_agent.api import capabilities, health, infer
from worker_agent.gpu_monitor import get_gpu_info
from shared.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

WORKER_CONFIG_PATH = os.environ.get("WORKER_CONFIG_PATH", "config/workers/local_worker.yaml")
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "15"))
CONTROL_PLANE_API_TOKEN = os.environ.get("CONTROL_PLANE_API_TOKEN", "").strip()


def _cp_headers() -> dict:
    if not CONTROL_PLANE_API_TOKEN:
        return {}
    return {"X-Nexus-API-Key": CONTROL_PLANE_API_TOKEN}


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

    app.state.worker_config = worker_config

    # Register with control plane
    worker_id = worker_config.get("id", "unknown")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{CONTROL_PLANE_URL}/v1/workers",
                json=worker_config,
                headers=_cp_headers(),
            )
            response.raise_for_status()
            logger.info("Registered with control plane as %s", worker_id)
    except Exception as e:
        logger.warning("Could not register with control plane: %s", e)

    # Background heartbeat
    heartbeat_task = asyncio.create_task(
        _send_heartbeats(worker_id)
    )

    logger.info("NexusAI Worker Agent started (id=%s)", worker_id)
    yield

    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass
    logger.info("NexusAI Worker Agent stopped")


async def _send_heartbeats(worker_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            gpu_info = get_gpu_info()
            gpu_util = [
                (g["memory_used"] / g["memory_total"] * 100) if g["memory_total"] > 0 else 0.0
                for g in gpu_info
            ]
            metrics = {"gpu_utilization": gpu_util} if gpu_util else {}
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{CONTROL_PLANE_URL}/v1/workers/{worker_id}/heartbeat",
                    json={"metrics": metrics} if metrics else {},
                    headers=_cp_headers(),
                )
        except Exception as e:
            logger.warning("Heartbeat failed: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(
        title="NexusAI Worker Agent",
        version="0.1.0",
        lifespan=lifespan,
    )

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
