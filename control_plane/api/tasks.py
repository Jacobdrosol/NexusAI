from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
from shared.exceptions import BotNotFoundError, TaskNotFoundError
from shared.models import Task, TaskMetadata

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    bot_id: str
    payload: Any
    metadata: Optional[TaskMetadata] = None
    depends_on: Optional[List[str]] = None


class RetryTaskRequest(BaseModel):
    payload: Optional[Any] = None


class HistoryRetentionPurgeRequest(BaseModel):
    older_than_days: int = Field(default=90, ge=1, le=3650)
    statuses: List[str] = Field(default_factory=lambda: ["completed", "retried", "cancelled"], min_length=1)
    max_tasks: int = Field(default=500, ge=1, le=10_000)
    confirmation: str = Field(min_length=1, max_length=80)


class CancelOrchestrationRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class CancelTaskRequest(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=500)


class WorkDispatchHoldRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=200)
    manager_id: Optional[str] = Field(default=None, max_length=200)
    reason: Optional[str] = Field(default=None, max_length=500)
    operator_id: Optional[str] = Field(default=None, max_length=200)


class TaskListItem(BaseModel):
    id: str
    bot_id: str
    status: str
    created_at: str
    updated_at: str
    metadata: Optional[TaskMetadata] = None
    depends_on: List[str] = Field(default_factory=list)
    payload: Optional[Any] = None
    result: Optional[Any] = None
    error: Optional[Any] = None
    has_payload: Optional[bool] = None
    has_result: Optional[bool] = None
    has_error: Optional[bool] = None
    payload_type: Optional[str] = None
    result_type: Optional[str] = None
    error_type: Optional[str] = None
    error_summary: Optional[Dict[str, Any]] = None
    usage: Optional[Dict[str, Any]] = None


def _summarize_error(error: Any) -> Optional[Dict[str, Any]]:
    if error is None:
        return None
    if isinstance(error, dict):
        error_type = str(error.get("type") or error.get("error_type") or "dict").strip() or "dict"
        code = str(error.get("code") or error.get("error_code") or "").strip()
        message = str(error.get("message") or error.get("detail") or error.get("error") or "").strip()
    else:
        error_type = type(error).__name__
        code = ""
        message = str(error or "").strip()
    if len(message) > 240:
        message = f"{message[:237]}..."
    summary: Dict[str, Any] = {"type": error_type}
    if code:
        summary["code"] = code
    if message:
        summary["message"] = message
    return summary


def _task_summary(task: Task) -> Dict[str, Any]:
    payload = task.payload
    result = task.result
    error = task.error
    usage: Optional[Dict[str, Any]] = None
    if isinstance(result, dict):
        raw_usage = result.get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                "prompt_tokens": raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens"),
                "completion_tokens": raw_usage.get("completion_tokens") or raw_usage.get("output_tokens"),
                "total_tokens": raw_usage.get("total_tokens"),
            }
            if usage["total_tokens"] in (None, ""):
                try:
                    usage["total_tokens"] = int(usage.get("prompt_tokens") or 0) + int(
                        usage.get("completion_tokens") or 0
                    )
                except Exception:
                    usage["total_tokens"] = None
            usage = {key: value for key, value in usage.items() if value not in (None, "")} or None
    return {
        "id": task.id,
        "bot_id": task.bot_id,
        "status": task.status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "metadata": task.metadata.model_dump() if task.metadata else None,
        "depends_on": list(task.depends_on or []),
        "has_payload": payload is not None,
        "has_result": result is not None,
        "has_error": error is not None,
        "payload_type": type(payload).__name__ if payload is not None else None,
        "result_type": type(result).__name__ if result is not None else None,
        "error_type": type(error).__name__ if error is not None else None,
        "error_summary": _summarize_error(error),
        "usage": usage,
    }


@router.post("", response_model=Task)
async def create_task(request: Request, body: CreateTaskRequest) -> Task:
    task_manager = request.app.state.task_manager
    try:
        task = await task_manager.create_task(
            bot_id=body.bot_id,
            payload=body.payload,
            metadata=body.metadata,
            depends_on=body.depends_on,
        )
        return task
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history-retention/preview")
async def preview_history_retention(
    request: Request,
    older_than_days: int = Query(default=90, ge=1, le=3650),
    statuses: Optional[str] = Query(default=None),
) -> Dict[str, Any]:
    requested_statuses = [value.strip() for value in str(statuses or "").split(",") if value.strip()] or None
    try:
        return await request.app.state.task_manager.preview_history_retention(
            older_than_days=older_than_days,
            statuses=requested_statuses,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/history-retention/purge")
async def purge_history_retention(request: Request, body: HistoryRetentionPurgeRequest) -> Dict[str, Any]:
    try:
        result = await request.app.state.task_manager.purge_history_retention(
            older_than_days=body.older_than_days,
            statuses=body.statuses,
            max_tasks=body.max_tasks,
            confirmation=body.confirmation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit_event(
        request,
        action="tasks.history_retention_purge",
        resource="task_history",
        details={
            "older_than_days": body.older_than_days,
            "statuses": body.statuses,
            "max_tasks": body.max_tasks,
            "deleted_task_count": result.get("deleted_task_count", 0),
            "deleted_artifact_count": result.get("deleted_artifact_count", 0),
        },
    )
    return result


@router.get("", response_model=List[TaskListItem])
async def list_tasks(
    request: Request,
    orchestration_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    bot_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    include_content: bool = Query(default=True),
) -> List[TaskListItem]:
    task_manager = request.app.state.task_manager
    statuses = [part.strip() for part in str(status or "").split(",") if part.strip()]
    if not include_content:
        return await task_manager.list_task_summaries(
            orchestration_id=orchestration_id,
            statuses=statuses or None,
            bot_id=bot_id,
            limit=limit,
        )
    tasks = await task_manager.list_tasks(
        orchestration_id=orchestration_id,
        statuses=statuses or None,
        bot_id=bot_id,
        limit=limit,
    )
    return tasks


@router.get("/usage")
async def task_usage_summary(
    request: Request,
    hours: int = Query(default=24, ge=1, le=2160),
    limit_bots: int = Query(default=25, ge=1, le=250),
) -> Dict[str, Any]:
    task_manager = request.app.state.task_manager
    return await task_manager.summarize_token_usage(hours=hours, limit_bots=limit_bots)


@router.get("/work-dispatch-holds")
async def list_work_dispatch_holds(request: Request) -> Dict[str, Any]:
    task_manager = request.app.state.task_manager
    return await task_manager.list_work_dispatch_holds()


@router.post("/work-dispatch-holds")
async def set_work_dispatch_hold(request: Request, body: WorkDispatchHoldRequest) -> Dict[str, Any]:
    task_manager = request.app.state.task_manager
    try:
        result = await task_manager.set_work_dispatch_hold(
            project_id=body.project_id,
            manager_id=body.manager_id or "",
            reason=str(body.reason or "operator_hold").strip() or "operator_hold",
            created_by=str(body.operator_id or "operator").strip() or "operator",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    hold = result.get("hold") if isinstance(result, dict) else {}
    await record_audit_event(
        request,
        action="tasks.work_dispatch_hold.set",
        resource=f"work:{body.project_id}",
        details=hold if isinstance(hold, dict) else {},
    )
    return result


@router.post("/work-dispatch-holds/release")
async def release_work_dispatch_hold(request: Request, body: WorkDispatchHoldRequest) -> Dict[str, Any]:
    task_manager = request.app.state.task_manager
    try:
        result = await task_manager.release_work_dispatch_hold(
            project_id=body.project_id,
            manager_id=body.manager_id or "",
            released_by=str(body.operator_id or "operator").strip() or "operator",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit_event(
        request,
        action="tasks.work_dispatch_hold.release",
        resource=f"work:{body.project_id}",
        details={
            "project_id": body.project_id,
            "manager_id": body.manager_id or "",
            "status": result.get("status"),
        },
    )
    return result


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, request: Request) -> Task:
    task_manager = request.app.state.task_manager
    try:
        return await task_manager.get_task(task_id)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/orchestrations/{orchestration_id}/cancel")
async def cancel_orchestration(
    orchestration_id: str,
    request: Request,
    body: CancelOrchestrationRequest,
) -> Dict[str, Any]:
    """Stop all active work for one orchestration and prevent new trigger fan-out."""
    task_manager = request.app.state.task_manager
    safe_id = str(orchestration_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="orchestration_id required")
    try:
        result = await task_manager.cancel_orchestration(
            safe_id,
            reason=str(body.reason or "operator_cancelled").strip() or "operator_cancelled",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await record_audit_event(
        request,
        action="tasks.cancel_orchestration",
        resource=f"orchestration:{safe_id}",
        details={
            "reason": result["reason"],
            "cancelled_task_count": result["cancelled_task_count"],
            "task_count": result["task_count"],
        },
    )
    return result


@router.post("/{task_id}/retry", response_model=Task)
async def retry_task(task_id: str, request: Request, body: RetryTaskRequest) -> Task:
    task_manager = request.app.state.task_manager
    try:
        return await task_manager.retry_task(task_id, payload_override=body.payload)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{task_id}/cancel", response_model=Task)
async def cancel_task(task_id: str, request: Request, body: CancelTaskRequest | None = None) -> Task:
    task_manager = request.app.state.task_manager
    try:
        reason = str((body.reason if body else None) or "operator_cancelled").strip() or "operator_cancelled"
        return await task_manager.cancel_task(task_id, reason=reason)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
