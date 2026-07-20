import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from worker_agent.backends import (
    ollama_backend,
    ollama_cloud_backend,
    openai_backend,
    claude_backend,
    gemini_backend,
)
from worker_agent.request_auth import require_worker_request_token

logger = logging.getLogger(__name__)
router = APIRouter(tags=["infer"])


class InferRequest(BaseModel):
    model: str
    provider: str
    messages: List[Dict[str, Any]]
    params: Optional[Dict[str, Any]] = None
    gpu_id: Optional[str] = None
    command: Optional[str] = None


_SUPPORTED_PROVIDERS = {"ollama", "ollama_cloud", "openai", "claude", "gemini", "cli"}


def _is_declared_model(worker_config: Dict[str, Any], provider: str, model: str) -> bool:
    """Allow inference only for a model explicitly declared by this worker."""
    requested_provider = str(provider or "").strip().lower()
    requested_model = str(model or "").strip().lower()
    capabilities = worker_config.get("capabilities", [])
    if not isinstance(capabilities, list) or not requested_provider or not requested_model:
        return False

    for capability in capabilities:
        if not isinstance(capability, dict):
            continue
        if str(capability.get("type") or "").strip().lower() != "llm":
            continue
        if str(capability.get("provider") or "").strip().lower() != requested_provider:
            continue
        models = capability.get("models", [])
        if isinstance(models, list) and any(
            str(declared_model or "").strip().lower() == requested_model
            for declared_model in models
        ):
            return True
    return False


@router.post("/infer")
async def infer(request: Request, body: InferRequest) -> dict:
    params = body.params or {}
    worker_config = getattr(request.app.state, "worker_config", {})
    require_worker_request_token(request, worker_config)
    provider = str(body.provider or "").strip().lower()
    if provider not in _SUPPORTED_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {body.provider}")
    if provider == "cli":
        raise HTTPException(
            status_code=403,
            detail="Legacy worker CLI execution is disabled; use an isolated worker node.",
        )
    if not _is_declared_model(worker_config, provider, body.model):
        raise HTTPException(
            status_code=403,
            detail="Requested provider/model is not declared for this worker.",
        )
    ollama_host = worker_config.get("ollama_host", "http://localhost:11434")
    request.app.state.inference_inflight = int(
        getattr(request.app.state, "inference_inflight", 0) or 0
    ) + 1

    try:
        if provider == "ollama":
            return await ollama_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                host=ollama_host,
            )
        elif provider == "ollama_cloud":
            return await ollama_cloud_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=os.environ.get("OLLAMA_API_KEY", ""),
                base_url=os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api"),
            )
        elif provider == "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            return await openai_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=api_key,
            )
        elif provider == "claude":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            return await claude_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=api_key,
            )
        elif provider == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY", "")
            return await gemini_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=api_key,
            )
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported provider: {provider}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Inference failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        request.app.state.inference_inflight = max(
            0, int(getattr(request.app.state, "inference_inflight", 1)) - 1
        )
