from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/v1/schedules", tags=["schedules"])


class CreateScheduleRequest(BaseModel):
    name: str
    cron_expression: str
    timezone: str = "UTC"
    prompt: str
    status: str = "active"
    target_bot_id: Optional[str] = None
    assignment_pm_bot_id: Optional[str] = None
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    node_overrides: Dict[str, Any] = Field(default_factory=dict)
    retry_max: int = 2
    retry_backoff_seconds: int = 30
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
    retry_max: Optional[int] = None
    retry_backoff_seconds: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


@router.post("")
async def create_schedule(request: Request, body: CreateScheduleRequest) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        schedule = await engine.create_schedule(body.model_dump())
        return {"schedule": schedule}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{schedule_id}")
async def update_schedule(schedule_id: str, request: Request, body: UpdateScheduleRequest) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    patch = {key: value for key, value in body.model_dump().items() if value is not None}
    try:
        schedule = await engine.update_schedule(schedule_id, patch)
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
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
        run = await engine.trigger_schedule(schedule_id)
        return {"run": run}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{schedule_id}/runs")
async def list_schedule_runs(schedule_id: str, request: Request, limit: int = 50) -> Dict[str, Any]:
    engine = request.app.state.agent_schedule_engine
    try:
        runs = await engine.list_runs(schedule_id, limit=limit)
        return {"schedule_id": schedule_id, "runs": runs}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
