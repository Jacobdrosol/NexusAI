import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from nexus_worker.services.inference import run_inference

router = APIRouter(tags=["infer"])


class InferStreamRequest(BaseModel):
    model: str
    provider: str
    messages: List[Dict[str, Any]]
    params: Optional[Dict[str, Any]] = None
    command: Optional[str] = None


@router.post("/infer/stream")
async def infer_stream(request: Request, body: InferStreamRequest) -> StreamingResponse:
    cfg = getattr(request.app.state, "worker_config", {})

    async def event_gen() -> AsyncGenerator[str, None]:
        try:
            result = await run_inference(
                provider=body.provider,
                model=body.model,
                messages=body.messages,
                params=body.params or {},
                worker_config=cfg,
                command=body.command or "",
            )
            text = str(result.get("output", ""))
            chunk_size = 80
            for i in range(0, len(text), chunk_size):
                chunk = text[i : i + chunk_size]
                yield f"event: token\ndata: {json.dumps({'text': chunk})}\n\n"
            yield f"event: final\ndata: {json.dumps(result)}\n\n"
            yield "event: done\ndata: {}\n\n"
        except HTTPException as e:
            payload = {"error": e.detail, "status_code": e.status_code}
            yield f"event: error\ndata: {json.dumps(payload)}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

