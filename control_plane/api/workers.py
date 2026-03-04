from typing import List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from shared.exceptions import WorkerNotFoundError
from shared.models import Worker, WorkerMetrics

router = APIRouter(prefix="/v1/workers", tags=["workers"])


class HeartbeatRequest(BaseModel):
    metrics: WorkerMetrics | None = None


@router.post("", response_model=Worker)
async def register_worker(request: Request, worker: Worker) -> Worker:
    worker_registry = request.app.state.worker_registry
    await worker_registry.register(worker)
    updated = worker.model_copy(update={"status": "online"})
    await worker_registry.update_status(worker.id, "online")
    return updated


@router.get("", response_model=List[Worker])
async def list_workers(request: Request) -> List[Worker]:
    worker_registry = request.app.state.worker_registry
    return await worker_registry.list()


@router.get("/{worker_id}", response_model=Worker)
async def get_worker(worker_id: str, request: Request) -> Worker:
    worker_registry = request.app.state.worker_registry
    try:
        return await worker_registry.get(worker_id)
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{worker_id}")
async def remove_worker(worker_id: str, request: Request) -> dict:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.remove(worker_id)
        return {"message": f"Worker {worker_id} removed"}
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{worker_id}/heartbeat")
async def heartbeat(worker_id: str, request: Request, body: HeartbeatRequest | None = None) -> dict:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.update_heartbeat(worker_id)
        if body and body.metrics:
            await worker_registry.update_metrics(worker_id, body.metrics)
        return {"status": "ok"}
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
