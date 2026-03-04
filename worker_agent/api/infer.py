import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from worker_agent.backends import (
    cli_backend,
    ollama_backend,
    openai_backend,
    claude_backend,
    gemini_backend,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["infer"])


class InferRequest(BaseModel):
    model: str
    provider: str
    messages: List[Dict[str, Any]]
    params: Optional[Dict[str, Any]] = None
    gpu_id: Optional[str] = None
    command: Optional[str] = None


@router.post("/infer")
async def infer(request: Request, body: InferRequest) -> dict:
    params = body.params or {}
    worker_config = getattr(request.app.state, "worker_config", {})
    ollama_host = worker_config.get("ollama_host", "http://localhost:11434")

    try:
        if body.provider == "ollama":
            return await ollama_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                host=ollama_host,
            )
        elif body.provider == "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            return await openai_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=api_key,
            )
        elif body.provider == "claude":
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            return await claude_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=api_key,
            )
        elif body.provider == "gemini":
            api_key = os.environ.get("GEMINI_API_KEY", "")
            return await gemini_backend.infer(
                model=body.model,
                messages=body.messages,
                params=params,
                api_key=api_key,
            )
        elif body.provider == "cli":
            command = body.command or body.model
            return await cli_backend.infer(command=command, params=params)
        else:
            raise HTTPException(
                status_code=400, detail=f"Unsupported provider: {body.provider}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Inference failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
