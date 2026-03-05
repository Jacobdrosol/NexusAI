import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from nexus_worker.services.inference import run_inference

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
    cfg = getattr(request.app.state, "worker_config", {})
    try:
        return await run_inference(
            provider=body.provider,
            model=body.model,
            messages=body.messages,
            params=body.params or {},
            worker_config=cfg,
            command=body.command or "",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("nexus_worker inference failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

