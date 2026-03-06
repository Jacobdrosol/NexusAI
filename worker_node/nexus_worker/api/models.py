from fastapi import APIRouter, Request

from nexus_worker.manager.local_models import discover_local_models

router = APIRouter(tags=["models"])


@router.get("/models/local")
async def local_models(request: Request) -> dict:
    cfg = getattr(request.app.state, "worker_config", {})
    return await discover_local_models(cfg)

