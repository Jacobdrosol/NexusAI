from fastapi import APIRouter, Request

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
async def capabilities(request: Request) -> dict:
    worker_config = getattr(request.app.state, "worker_config", {})
    declared_capabilities = worker_config.get("capabilities", [])
    if not isinstance(declared_capabilities, list):
        declared_capabilities = []
    return {
        "worker_id": worker_config.get("id", "unknown"),
        "configured_capabilities": declared_capabilities,
        "declared_capabilities": declared_capabilities,
        "capability_attestation": {
            "configured_cli_tools": [],
            "installed_cli_tools": [],
            "enabled_cli_tools": [],
            "unavailable_cli_tools": [],
            "discarded_declared_tool_capabilities": 0,
        },
    }
