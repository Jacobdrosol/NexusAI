import os
import shutil

from fastapi import APIRouter, Request

router = APIRouter(tags=["capabilities"])

_PROVIDER_CREDENTIAL_ENV = {
    "ollama_cloud": "OLLAMA_API_KEY",
    "openai": "OPENAI_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
}
_AUTH_REQUIRED_CLI_TOOLS = frozenset({"claude", "codex"})


def _unique_tool_names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted(
        {
            str(value).strip().lower()
            for value in values
            if isinstance(value, str) and str(value).strip()
        }
    )


def _configured_cli_tools(worker_config: dict) -> list[str]:
    """Return CLI tools explicitly configured or declared by the worker."""
    tooling = worker_config.get("tooling")
    tooling = tooling if isinstance(tooling, dict) else {}
    tools = _unique_tool_names(tooling.get("cli_tools"))
    for capability in worker_config.get("capabilities") or []:
        if not isinstance(capability, dict):
            continue
        if str(capability.get("type") or "").strip().lower() != "tool":
            continue
        if str(capability.get("provider") or "").strip().lower() != "cli":
            continue
        tools.extend(_unique_tool_names(capability.get("models")))
    return sorted(set(tools))


def capability_attestation(worker_config: dict) -> dict[str, object]:
    """Report executable and authentication readiness without exposing credentials."""
    configured_cli_tools = _configured_cli_tools(worker_config)
    installed_cli_tools = sorted(tool for tool in configured_cli_tools if shutil.which(tool))
    unavailable_cli_tools = sorted(set(configured_cli_tools) - set(installed_cli_tools))
    tooling = worker_config.get("tooling")
    tooling = tooling if isinstance(tooling, dict) else {}
    authenticated_cli_tools = set(_unique_tool_names(tooling.get("authenticated_cli_tools")))
    auth_required_cli_tools = sorted(
        tool for tool in installed_cli_tools if tool in _AUTH_REQUIRED_CLI_TOOLS
    )
    unauthenticated_cli_tools = sorted(
        tool for tool in auth_required_cli_tools if tool not in authenticated_cli_tools
    )
    enabled_cli_tools = sorted(set(installed_cli_tools) - set(unauthenticated_cli_tools))
    declared_capabilities = worker_config.get("capabilities", [])
    if not isinstance(declared_capabilities, list):
        declared_capabilities = []
    return {
        "configured_cli_tools": configured_cli_tools,
        "installed_cli_tools": installed_cli_tools,
        "enabled_cli_tools": enabled_cli_tools,
        "unavailable_cli_tools": unavailable_cli_tools,
        "auth_required_cli_tools": auth_required_cli_tools,
        "unauthenticated_cli_tools": unauthenticated_cli_tools,
        "discarded_declared_tool_capabilities": 0,
        "provider_credentials": _provider_credentials(declared_capabilities),
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
        "capability_attestation": capability_attestation(worker_config),
    }
