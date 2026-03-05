from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    cfg = getattr(request.app.state, "worker_config", {})
    return {"status": "ok", "worker_id": cfg.get("id", "unknown")}

