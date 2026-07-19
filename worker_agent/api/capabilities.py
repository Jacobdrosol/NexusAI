import os

from fastapi import APIRouter, Request

router = APIRouter(tags=["capabilities"])

_PROVIDER_CREDENTIAL_ENV = {
    "ollama_cloud": "OLLAMA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _provider_credentials(declared_capabilities: list[object]) -> dict[str, bool]:
    """Report whether declared cloud providers have credentials without exposing them."""
    credentials: dict[str, bool] = {}
    for capability in declared_capabilities:
        if not isinstance(capability, dict):
            continue
        if str(capability.get("type") or "").strip().lower() != "llm":
            continue
        provider = str(capability.get("provider") or "").strip().lower()
        env_name = _PROVIDER_CREDENTIAL_ENV.get(provider)
        if env_name:
            credentials[provider] = bool(os.environ.get(env_name, "").strip())
    return credentials


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
            "provider_credentials": _provider_credentials(declared_capabilities),
        },
    }
