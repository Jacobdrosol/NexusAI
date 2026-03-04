from fastapi import APIRouter, Request

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
async def capabilities(request: Request) -> dict:
    worker_config = getattr(request.app.state, "worker_config", {})
    return {
        "worker_id": worker_config.get("id", "unknown"),
        "capabilities": worker_config.get("capabilities", []),
    }
