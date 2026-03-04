import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from shared.exceptions import TaskNotFoundError
from shared.models import Task, TaskError, TaskMetadata

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT,
    payload    TEXT,
    metadata   TEXT,
    status     TEXT,
    result     TEXT,
    error      TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""


class TaskManager:
    def __init__(self, scheduler: Any, db_path: Optional[str] = None) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._scheduler = scheduler
        self._db_ready = False
        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            if db_url.startswith("sqlite:///"):
                self._db_path = db_url[len("sqlite:///"):]
            else:
                self._db_path = _DEFAULT_DB_PATH

    async def _ensure_db(self) -> None:
        """Lazily initialise the SQLite tasks table and load existing rows."""
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(_CREATE_TASKS)
                await db.commit()
                async with db.execute("SELECT * FROM tasks") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        task = Task(
                            id=row["id"],
                            bot_id=row["bot_id"],
                            payload=json.loads(row["payload"]) if row["payload"] else {},
                            metadata=(
                                TaskMetadata(**json.loads(row["metadata"]))
                                if row["metadata"]
                                else None
                            ),
                            status=row["status"],
                            result=json.loads(row["result"]) if row["result"] else None,
                            error=(
                                TaskError(**json.loads(row["error"]))
                                if row["error"]
                                else None
                            ),
                            created_at=row["created_at"],
                            updated_at=row["updated_at"],
                        )
                        self._tasks[task.id] = task
            self._db_ready = True

    async def _persist_task(self, task: Task) -> None:
        """Upsert *task* into the SQLite tasks table."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO tasks
                    (id, bot_id, payload, metadata, status, result, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status     = excluded.status,
                    result     = excluded.result,
                    error      = excluded.error,
                    updated_at = excluded.updated_at
                """,
                (
                    task.id,
                    task.bot_id,
                    json.dumps(task.payload),
                    json.dumps(task.metadata.model_dump()) if task.metadata else None,
                    task.status,
                    json.dumps(task.result) if task.result is not None else None,
                    json.dumps(task.error.model_dump()) if task.error else None,
                    task.created_at,
                    task.updated_at,
                ),
            )
            await db.commit()

    async def create_task(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[TaskMetadata] = None,
    ) -> Task:
        await self._ensure_db()
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
        await self._persist_task(task)
        asyncio.create_task(self._run_task(task_id))
        return task

    async def get_task(self, task_id: str) -> Task:
        await self._ensure_db()
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            return self._tasks[task_id]

    async def list_tasks(self) -> List[Task]:
        await self._ensure_db()
        async with self._lock:
            return list(self._tasks.values())

    async def update_status(
        self,
        task_id: str,
        status: str,
        result: Optional[Any] = None,
        error: Optional[TaskError] = None,
    ) -> None:
        await self._ensure_db()
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
            updated_task = self._tasks[task_id]
        await self._persist_task(updated_task)

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
