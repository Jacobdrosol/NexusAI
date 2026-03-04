from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shared.exceptions import BotNotFoundError, TaskNotFoundError
from shared.models import Task, TaskMetadata

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


class CreateTaskRequest(BaseModel):
    bot_id: str
    payload: Any
    metadata: Optional[TaskMetadata] = None


@router.post("", response_model=Task)
async def create_task(request: Request, body: CreateTaskRequest) -> Task:
    task_manager = request.app.state.task_manager
    try:
        task = await task_manager.create_task(
            bot_id=body.bot_id,
            payload=body.payload,
            metadata=body.metadata,
        )
        return task
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=List[Task])
async def list_tasks(request: Request) -> List[Task]:
    task_manager = request.app.state.task_manager
    return await task_manager.list_tasks()


@router.get("/{task_id}", response_model=Task)
async def get_task(task_id: str, request: Request) -> Task:
    task_manager = request.app.state.task_manager
    try:
        return await task_manager.get_task(task_id)
    except TaskNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
