from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

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
    usage: Optional[Dict[str, Any]] = None


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
    tasks = await task_manager.list_tasks(
        orchestration_id=orchestration_id,
        statuses=statuses or None,
        bot_id=bot_id,
        limit=limit,
    )
    if include_content:
        return tasks
    return [_task_summary(task) for task in tasks]


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, request: Request) -> Task:
    task_manager = request.app.state.task_manager
    try:
        return await task_manager.get_task(task_id)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


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
async def cancel_task(task_id: str, request: Request) -> Task:
    task_manager = request.app.state.task_manager
    try:
        return await task_manager.cancel_task(task_id)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
