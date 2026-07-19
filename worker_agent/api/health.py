from fastapi import APIRouter, Request

from worker_agent.api.capabilities import capability_attestation

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    worker_config = getattr(request.app.state, "worker_config", {})
    worker_config = worker_config if isinstance(worker_config, dict) else {}
    return {
        "status": "ok",
        "worker_id": worker_config.get("id", "unknown"),
        "enabled_cli_tools": capability_attestation(worker_config)["enabled_cli_tools"],
    }
