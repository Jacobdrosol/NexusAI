from typing import Any, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=List[Task])
async def list_tasks(
    request: Request,
    orchestration_id: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    bot_id: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> List[Task]:
    task_manager = request.app.state.task_manager
    statuses = [part.strip() for part in str(status or "").split(",") if part.strip()]
    return await task_manager.list_tasks(
        orchestration_id=orchestration_id,
        statuses=statuses or None,
        bot_id=bot_id,
        limit=limit,
    )


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
