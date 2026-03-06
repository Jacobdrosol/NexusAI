from fastapi import APIRouter, Request

from nexus_worker.hardware.detector import detect_hardware_profile
from nexus_worker.hardware.model_advisor import compatibility_for_models, estimate_eta_seconds
from nexus_worker.manager.cli_tools import discover_cli_tools
from nexus_worker.manager.local_models import discover_local_models

router = APIRouter(tags=["capabilities"])


@router.get("/capabilities")
async def capabilities(request: Request) -> dict:
    cfg = getattr(request.app.state, "worker_config", {})
    hardware = detect_hardware_profile()
    local = await discover_local_models(cfg)
    cli_tools = discover_cli_tools()
    model_names = [m.get("name", "") for m in local.get("models", []) if m.get("name")]
    compat = compatibility_for_models(model_names, hardware)
    eta_examples = [
        {
            "model": m,
            "tokens": 1200,
            "eta_seconds": round(estimate_eta_seconds(1200, m, hardware), 2),
        }
        for m in model_names[:8]
    ]
    return {
        "worker_id": cfg.get("id", "unknown"),
        "configured_capabilities": cfg.get("capabilities", []),
        "hardware_profile": hardware,
        "local_models": local,
        "cli_tools": cli_tools,
        "compatibility": compat,
        "eta_examples": eta_examples,
    }
