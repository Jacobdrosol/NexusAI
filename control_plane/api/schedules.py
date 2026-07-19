from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
from control_plane.agent_scheduler.engine import (
    _DEFAULT_SCHEDULE_RETRY_BACKOFF_SECONDS,
    _DEFAULT_SCHEDULE_RETRY_MAX,
    _MAX_SCHEDULE_RETRY_BACKOFF_SECONDS,
    _MAX_SCHEDULE_RETRY_MAX,
    _MIN_SCHEDULE_RETRY_BACKOFF_SECONDS,
)
from control_plane.schedule_safety import (
    ScheduleAutonomySafetyError,
    require_schedule_autonomy_safety,
    require_schedule_runtime_readiness,
)


router = APIRouter(prefix="/v1/schedules", tags=["schedules"])


async def _require_schedule_target_ready(
    request: Request,
    schedule: Dict[str, Any],
    *,
    only_when_active: bool,
) -> None:
    if only_when_active and str(schedule.get("status") or "").strip().lower() != "active":
        return
    try:
        await require_schedule_runtime_readiness(
            schedule,
            bot_registry=request.app.state.bot_registry,
            worker_registry=request.app.state.worker_registry,
            connection_resolver=request.app.state.connection_resolver,
            worker_probe_store=request.app.state.worker_probe_store,
            key_vault=request.app.state.key_vault,
            model_registry=request.app.state.model_registry,
        )
    except ScheduleAutonomySafetyError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc


async def _require_schedule_autonomy_safety(
    request: Request,
    schedule: Dict[str, Any],
    *,
    only_when_active: bool,
) -> None:
    try:
        await require_schedule_autonomy_safety(
            schedule,
            bot_registry=request.app.state.bot_registry,
            only_when_active=only_when_active,
        )
    except ScheduleAutonomySafetyError as exc:
        raise HTTPException(status_code=409, detail=exc.as_detail()) from exc


class CreateScheduleRequest(BaseModel):
    name: str
    cron_expression: str
    timezone: str = "UTC"
    prompt: str
    status: str = "paused"
    target_bot_id: Optional[str] = None
    assignment_pm_bot_id: Optional[str] = None
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    node_overrides: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    retry_max: int = Field(
        default=_DEFAULT_SCHEDULE_RETRY_MAX,
        ge=0,
        le=_MAX_SCHEDULE_RETRY_MAX,
    )
    retry_backoff_seconds: int = Field(
        default=_DEFAULT_SCHEDULE_RETRY_BACKOFF_SECONDS,
        ge=_MIN_SCHEDULE_RETRY_BACKOFF_SECONDS,
        le=_MAX_SCHEDULE_RETRY_BACKOFF_SECONDS,
    )
    overlap_policy: Literal["forbid", "allow"] = "forbid"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class UpdateScheduleRequest(BaseModel):
    name: Optional[str] = None
    cron_expression: Optional[str] = None
    timezone: Optional[str] = None
    prompt: Optional[str] = None
    status: Optional[str] = None
    target_bot_id: Optional[str] = None
    assignment_pm_bot_id: Optional[str] = None
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    node_overrides: Optional[Dict[str, Any]] = None
    task_payload: Optional[Dict[str, Any]] = None
    retry_max: Optional[int] = Field(default=None, ge=0, le=_MAX_SCHEDULE_RETRY_MAX)
    retry_backoff_seconds: Optional[int] = Field(
        default=None,
        ge=_MIN_SCHEDULE_RETRY_BACKOFF_SECONDS,
        le=_MAX_SCHEDULE_RETRY_BACKOFF_SECONDS,
    )
    overlap_policy: Optional[Literal["forbid", "allow"]] = None
    metadata: Optional[Dict[str, Any]] = None


@router.get("")
async def list_schedules(
    request: Request,
    limit: int = 100,
    status: Optional[str] = None,
    target_bot_id: Optional[str] = None,
) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        schedules = await engine.list_schedules(
            limit=limit,
            status=status,
            target_bot_id=target_bot_id,
        )
        return {"schedules": schedules}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("")
async def create_schedule(request: Request, body: CreateScheduleRequest) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        payload = body.model_dump()
        await _require_schedule_target_ready(request, payload, only_when_active=True)
        await _require_schedule_autonomy_safety(request, payload, only_when_active=True)
        schedule = await engine.create_schedule(payload)
        await record_audit_event(request, action="schedules.create", resource=f"schedule:{schedule['id']}")
        return {"schedule": schedule}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, request: Request, body: UpdateScheduleRequest) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    patch = {key: value for key, value in body.model_dump().items() if value is not None}
    try:
        current = await engine.get_schedule(schedule_id)
        if current is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        candidate = {**current, **patch}
        await _require_schedule_target_ready(request, candidate, only_when_active=True)
        await _require_schedule_autonomy_safety(request, candidate, only_when_active=True)
        schedule = await engine.update_schedule(schedule_id, patch)
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        await record_audit_event(request, action="schedules.update", resource=f"schedule:{schedule_id}")
        return {"schedule": schedule}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{schedule_id}")
async def get_schedule(schedule_id: str, request: Request) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    schedule = await engine.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"schedule": schedule}


@router.post("/{schedule_id}/trigger")
async def trigger_schedule(schedule_id: str, request: Request) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        schedule = await engine.get_schedule(schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        await _require_schedule_target_ready(request, schedule, only_when_active=False)
        await _require_schedule_autonomy_safety(request, schedule, only_when_active=False)
        run = await engine.trigger_schedule(schedule_id)
        await record_audit_event(request, action="schedules.trigger", resource=f"schedule:{schedule_id}")
        return {"run": run}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{schedule_id}/preview")
async def preview_schedule(schedule_id: str, request: Request) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        schedule = await engine.get_schedule(schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        await _require_schedule_autonomy_safety(request, schedule, only_when_active=False)
        preview = await engine.preview_schedule_payload(schedule_id)
        await record_audit_event(request, action="schedules.preview", resource=f"schedule:{schedule_id}")
        return preview
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{schedule_id}/runs")
async def list_schedule_runs(schedule_id: str, request: Request, limit: int = 50) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        runs = await engine.list_runs(schedule_id, limit=limit)
        return {"schedule_id": schedule_id, "runs": runs}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
