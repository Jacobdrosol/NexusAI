from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from control_plane.audit.utils import record_audit_event
from control_plane.worker_probe import probe_worker
from shared.exceptions import WorkerNotFoundError
from shared.models import Worker, WorkerMetrics

router = APIRouter(prefix="/v1/workers", tags=["workers"])


class HeartbeatRequest(BaseModel):
    metrics: Optional[WorkerMetrics] = None


@router.post("", response_model=Worker)
async def register_worker(request: Request, worker: Worker) -> Worker:
    worker_registry = request.app.state.worker_registry
    await worker_registry.register(worker)
    await worker_registry.update_status(worker.id, "online")
    return await worker_registry.get(worker.id)


@router.post("/provision", response_model=Worker, status_code=201)
async def provision_worker(request: Request, worker: Worker) -> Worker:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.provision(worker)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await record_audit_event(request, action="workers.provision", resource=f"worker:{worker.id}")
    return await worker_registry.get(worker.id)


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


@router.post("/{worker_id}/probe")
async def probe_registered_worker(worker_id: str, request: Request) -> dict:
    """Perform a non-mutating runtime and capability probe for one worker."""
    worker_registry = request.app.state.worker_registry
    try:
        worker = await worker_registry.get(worker_id)
    except WorkerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = await probe_worker(worker)
    await request.app.state.worker_probe_store.record(result)
    await record_audit_event(
        request,
        action="workers.probe",
        resource=f"worker:{worker.id}",
        status=str(result["probe_status"]),
        details={"checks": result["checks"]},
    )
    return result


@router.get("/{worker_id}/probe")
async def get_registered_worker_probe(worker_id: str, request: Request) -> dict:
    """Return the latest stored, read-only runtime probe result for one worker."""
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.get(worker_id)
    except WorkerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    result = await request.app.state.worker_probe_store.get(worker_id)
    if result is None:
        return {
            "worker_id": worker_id,
            "probe_status": "unknown",
            "checked_at": None,
            "dispatch_eligible": False,
            "checks": [],
        }
    return result


@router.put("/{worker_id}", response_model=Worker)
async def update_worker(worker_id: str, request: Request, worker: Worker) -> Worker:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.update(worker_id, worker)
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
async def heartbeat(worker_id: str, request: Request, body: Optional[HeartbeatRequest] = None) -> dict:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.update_heartbeat(worker_id)
        if body and body.metrics:
            await worker_registry.update_metrics(worker_id, body.metrics)
        return {"status": "ok"}
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
