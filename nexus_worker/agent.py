import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Dict

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nexus_worker.api import capabilities, health, infer, infer_stream, models
from nexus_worker.hardware.detector import detect_hardware_profile
from shared.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

WORKER_CONFIG_PATH = os.environ.get("NEXUS_WORKER_CONFIG_PATH", "nexus_worker/config.yaml.example")
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8000")
CONTROL_PLANE_API_TOKEN = os.environ.get("CONTROL_PLANE_API_TOKEN", "").strip()
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "15"))


def _cp_headers() -> Dict[str, str]:
    if not CONTROL_PLANE_API_TOKEN:
        return {}
    return {"X-Nexus-API-Key": CONTROL_PLANE_API_TOKEN}


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        worker_config = ConfigLoader.load_yaml(WORKER_CONFIG_PATH)
    except Exception as e:
        logger.warning("Failed to load nexus_worker config from %s: %s", WORKER_CONFIG_PATH, e)
        worker_config = {
            "id": "nexus-worker-standalone",
            "name": "Nexus Worker Standalone",
            "host": "0.0.0.0",
            "port": 8010,
            "status": "offline",
            "enabled": True,
            "capabilities": [],
            "metrics": {},
        }

    app.state.worker_config = worker_config
    worker_id = worker_config.get("id", "nexus-worker-standalone")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{CONTROL_PLANE_URL}/v1/workers",
                json=worker_config,
                headers=_cp_headers(),
            )
            resp.raise_for_status()
            logger.info("nexus_worker registered with control plane as %s", worker_id)
    except Exception as e:
        logger.warning("nexus_worker registration failed: %s", e)

    heartbeat_task = asyncio.create_task(_send_heartbeats(worker_id))
    yield
    heartbeat_task.cancel()
    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass


async def _send_heartbeats(worker_id: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            hw = detect_hardware_profile()
            cpu = hw.get("cpu") or {}
            gpus = hw.get("gpus") or []
            gpu_util = [float(g.get("utilization_percent") or 0.0) for g in gpus]
            metrics = {
                "load": float(cpu.get("usage_percent") or 0.0),
                "gpu_utilization": gpu_util,
                "queue_depth": 0,
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                await client.post(
                    f"{CONTROL_PLANE_URL}/v1/workers/{worker_id}/heartbeat",
                    json={"metrics": metrics},
                    headers=_cp_headers(),
                )
        except Exception as e:
            logger.warning("nexus_worker heartbeat failed: %s", e)


def create_app() -> FastAPI:
    app = FastAPI(title="Nexus Worker", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(models.router)
    app.include_router(infer.router)
    app.include_router(infer_stream.router)

    @app.exception_handler(Exception)
    async def _generic_exception_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"error": type(exc).__name__, "detail": str(exc)})

    return app


app = create_app()

