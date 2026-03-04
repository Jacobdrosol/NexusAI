import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from shared.exceptions import TaskNotFoundError
from shared.models import Task, TaskError, TaskMetadata

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, scheduler: Any) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._scheduler = scheduler

    async def create_task(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[TaskMetadata] = None,
    ) -> Task:
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        task = Task(
            id=task_id,
            bot_id=bot_id,
            payload=payload,
            metadata=metadata,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks[task_id] = task
        asyncio.create_task(self._run_task(task_id))
        return task

    async def get_task(self, task_id: str) -> Task:
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            return self._tasks[task_id]

    async def list_tasks(self) -> List[Task]:
        async with self._lock:
            return list(self._tasks.values())

    async def update_status(
        self,
        task_id: str,
        status: str,
        result: Optional[Any] = None,
        error: Optional[TaskError] = None,
    ) -> None:
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            now = datetime.now(timezone.utc).isoformat()
            self._tasks[task_id] = self._tasks[task_id].model_copy(
                update={
                    "status": status,
                    "result": result,
                    "error": error,
                    "updated_at": now,
                }
            )

    async def _run_task(self, task_id: str) -> None:
        await self.update_status(task_id, "running")
        try:
            task = await self.get_task(task_id)
            result = await self._scheduler.schedule(task)
            await self.update_status(task_id, "completed", result=result)
        except Exception as e:
            logger.error("Task %s failed: %s", task_id, e)
            task_error = TaskError(message=str(e))
            await self.update_status(task_id, "failed", error=task_error)
