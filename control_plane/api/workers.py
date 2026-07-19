import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from control_plane.audit.utils import record_audit_event
from control_plane.schedule_payload_sources import fleet_health_summary
from control_plane.worker_probe import (
    WorkerProbeError,
    autonomous_worker_probe_max_age_seconds,
    probe_worker,
    verify_worker_inference,
)
from shared.exceptions import WorkerNotFoundError
from shared.models import Worker, WorkerMetrics

router = APIRouter(prefix="/v1/workers", tags=["workers"])
logger = logging.getLogger(__name__)

_REGISTRATION_PROBE_DELAY_SECONDS = 2.0
_PROBE_REFRESH_RETRY_SECONDS = 30.0
_PROBE_REFRESH_FRACTION = 0.75


class HeartbeatRequest(BaseModel):
    metrics: Optional[WorkerMetrics] = None


class VerifyInferenceRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None


def _worker_dependency_detail(
    *,
    worker_id: str,
    dependent_bots: List[Any],
    active_schedules: List[Dict[str, Any]],
) -> Dict[str, Any]:
    bot_rows = [
        {
            "id": str(bot.id or ""),
            "name": str(bot.name or ""),
            "enabled": bool(bot.enabled),
            "project_id": str(bot.project_id or "") or None,
        }
        for bot in dependent_bots
    ]
    return {
        "worker_id": worker_id,
        "dependent_bots": bot_rows,
        "enabled_bot_ids": [row["id"] for row in bot_rows if row["enabled"]],
        "active_schedules": [
            {
                "id": str(schedule.get("id") or ""),
                "name": str(schedule.get("name") or ""),
                "target_bot_id": str(schedule.get("target_bot_id") or ""),
                "project_id": str(schedule.get("project_id") or "") or None,
            }
            for schedule in active_schedules
        ],
        "can_disable": not any(row["enabled"] for row in bot_rows),
        "can_delete": not bot_rows,
    }


async def _worker_dependencies(request: Request, worker_id: str) -> Dict[str, Any]:
    """Return bounded bot and active-schedule dependencies for one worker."""
    safe_worker_id = str(worker_id or "").strip()
    bots = await request.app.state.bot_registry.list()
    dependent_bots = [
        bot
        for bot in bots
        if any(str(getattr(backend, "worker_id", "") or "").strip() == safe_worker_id for backend in bot.backends)
    ]
    dependent_bot_ids = {str(bot.id or "").strip() for bot in dependent_bots}
    active_schedules: List[Dict[str, Any]] = []
    if dependent_bot_ids:
        schedules = await request.app.state.agent_schedule_engine.list_schedules(limit=500, status="active")
        active_schedules = [
            schedule
            for schedule in schedules
            if str(schedule.get("target_bot_id") or "").strip() in dependent_bot_ids
        ]
    return _worker_dependency_detail(
        worker_id=safe_worker_id,
        dependent_bots=dependent_bots,
        active_schedules=active_schedules,
    )


def _raise_worker_dependency_error(*, reason_code: str, message: str, dependencies: Dict[str, Any]) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "reason_code": reason_code,
            "message": message,
            "dependencies": dependencies,
        },
    )


async def _refresh_registered_worker_probe(
    request: Request,
    worker_id: str,
    *,
    delay_seconds: float = _REGISTRATION_PROBE_DELAY_SECONDS,
) -> None:
    """Refresh persisted, non-mutating worker readiness evidence."""
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    try:
        worker = await request.app.state.worker_registry.get(worker_id)
        result = await probe_worker(worker)
        await request.app.state.worker_probe_store.record(result)
    except WorkerNotFoundError:
        return
    except Exception as exc:
        logger.warning("Worker runtime probe failed for %s: %s", worker_id, exc)


def _worker_probe_refresh_due(probe: Any, *, now: datetime | None = None) -> bool:
    """Refresh before scheduled-dispatch evidence expires, with a short retry for failed probes."""
    if not isinstance(probe, dict):
        return True
    checked_at = str(probe.get("checked_at") or "").strip()
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = (current - checked.astimezone(timezone.utc)).total_seconds()
    if age_seconds < -30:
        return True
    refresh_after = max(15.0, autonomous_worker_probe_max_age_seconds() * _PROBE_REFRESH_FRACTION)
    if str(probe.get("probe_status") or "").strip().lower() != "ready":
        return age_seconds >= min(_PROBE_REFRESH_RETRY_SECONDS, refresh_after)
    return age_seconds >= refresh_after


def _queue_worker_probe_refresh(request: Request, worker_id: str) -> None:
    """Coalesce heartbeat-triggered re-probes so one slow worker cannot create a task storm."""
    tasks = getattr(request.app.state, "worker_probe_refresh_tasks", None)
    if not isinstance(tasks, dict):
        tasks = {}
        request.app.state.worker_probe_refresh_tasks = tasks
    existing = tasks.get(worker_id)
    if existing is not None and not existing.done():
        return
    try:
        task = asyncio.create_task(
            _refresh_registered_worker_probe(request, worker_id, delay_seconds=0)
        )
    except RuntimeError as exc:
        logger.warning("Worker probe refresh could not start for %s: %s", worker_id, exc)
        return
    tasks[worker_id] = task

    def _clear_completed(completed_task: asyncio.Task) -> None:
        if tasks.get(worker_id) is completed_task:
            tasks.pop(worker_id, None)

    task.add_done_callback(_clear_completed)


@router.post("", response_model=Worker)
async def register_worker(request: Request, worker: Worker) -> Worker:
    worker_registry = request.app.state.worker_registry
    await worker_registry.register(worker)
    await worker_registry.update_status(worker.id, "online")
    asyncio.create_task(_refresh_registered_worker_probe(request, worker.id))
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


@router.get("/probes")
async def list_registered_worker_probes(request: Request) -> dict:
    """Return persisted read-only runtime evidence for every currently registered worker."""
    worker_registry = request.app.state.worker_registry
    workers = await worker_registry.list()
    stored = await request.app.state.worker_probe_store.list_for_workers(
        [worker.id for worker in workers]
    )
    probes: list[dict] = []
    for worker in workers:
        probe = stored.get(worker.id)
        if probe is None:
            probe = {
                "worker_id": worker.id,
                "probe_status": "unknown",
                "checked_at": None,
                "dispatch_eligible": False,
                "checks": [],
            }
        probes.append(probe)
    return {"probes": probes, "count": len(probes)}


@router.get("/fleet-summary")
async def get_fleet_summary(request: Request) -> dict:
    """Return a bounded, non-secret operational snapshot for manager workflows."""
    return await fleet_health_summary(
        worker_registry=request.app.state.worker_registry,
        worker_probe_store=request.app.state.worker_probe_store,
        bot_registry=request.app.state.bot_registry,
        task_manager=request.app.state.task_manager,
        schedule_engine=request.app.state.agent_schedule_engine,
    )


@router.get("/{worker_id}", response_model=Worker)
async def get_worker(worker_id: str, request: Request) -> Worker:
    worker_registry = request.app.state.worker_registry
    try:
        return await worker_registry.get(worker_id)
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{worker_id}/dependencies")
async def get_worker_dependencies(worker_id: str, request: Request) -> Dict[str, Any]:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.get(worker_id)
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return await _worker_dependencies(request, worker_id)


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


@router.post("/{worker_id}/verify-inference")
async def verify_registered_worker_inference(
    worker_id: str,
    request: Request,
    body: VerifyInferenceRequest | None = None,
) -> dict:
    """Run a fixed, no-context completion check against one declared worker LLM."""
    worker_registry = request.app.state.worker_registry
    try:
        worker = await worker_registry.get(worker_id)
    except WorkerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        result = await verify_worker_inference(
            worker,
            provider=body.provider if body else None,
            model=body.model if body else None,
        )
    except WorkerProbeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await record_audit_event(
        request,
        action="workers.verify_inference",
        resource=f"worker:{worker.id}",
        status=str(result["verification_status"]),
        details={
            "provider": result["provider"],
            "model": result["model"],
            "latency_ms": result["latency_ms"],
            "output_length": result["output_length"],
        },
    )
    return result


@router.put("/{worker_id}", response_model=Worker)
async def update_worker(worker_id: str, request: Request, worker: Worker) -> Worker:
    worker_registry = request.app.state.worker_registry
    try:
        current = await worker_registry.get(worker_id)
        if str(worker.id or "").strip() != str(worker_id or "").strip():
            raise HTTPException(
                status_code=409,
                detail={
                    "reason_code": "worker_id_immutable",
                    "message": "Worker ID cannot be changed through an update.",
                },
            )
        if bool(current.enabled) and not bool(worker.enabled):
            dependencies = await _worker_dependencies(request, worker_id)
            if not bool(dependencies["can_disable"]):
                _raise_worker_dependency_error(
                    reason_code="worker_disable_blocked",
                    message="Disable dependent bots before disabling this worker.",
                    dependencies=dependencies,
                )
        await worker_registry.update(worker_id, worker)
        return await worker_registry.get(worker_id)
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{worker_id}")
async def remove_worker(worker_id: str, request: Request) -> dict:
    worker_registry = request.app.state.worker_registry
    try:
        await worker_registry.get(worker_id)
        dependencies = await _worker_dependencies(request, worker_id)
        if not bool(dependencies["can_delete"]):
            _raise_worker_dependency_error(
                reason_code="worker_delete_blocked",
                message="Remove or repoint every dependent bot before deleting this worker.",
                dependencies=dependencies,
            )
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
        try:
            probe = await request.app.state.worker_probe_store.get(worker_id)
            if _worker_probe_refresh_due(probe):
                _queue_worker_probe_refresh(request, worker_id)
        except Exception as exc:
            logger.warning("Worker probe freshness check failed for %s: %s", worker_id, exc)
        return {"status": "ok"}
    except WorkerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
