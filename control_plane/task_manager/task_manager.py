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
from shared.models import BotRun, BotRunArtifact, Task, TaskError, TaskMetadata
from control_plane.scheduler.dependency_engine import DependencyEngine

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    bot_id     TEXT,
    payload    TEXT,
    metadata   TEXT,
    depends_on TEXT,
    status     TEXT,
    result     TEXT,
    error      TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""

_CREATE_TASK_DEPENDENCIES = """
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
)
"""

_CREATE_BOT_RUNS = """
CREATE TABLE IF NOT EXISTS bot_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    bot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT,
    metadata TEXT,
    result TEXT,
    error TEXT,
    triggered_by_task_id TEXT,
    trigger_rule_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
)
"""

_CREATE_BOT_RUN_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS bot_run_artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    content TEXT,
    path TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL
)
"""


class TaskManager:
    def __init__(self, scheduler: Any, db_path: Optional[str] = None, bot_registry: Optional[Any] = None) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._scheduler = scheduler
        self._bot_registry = bot_registry
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
                await db.execute(_CREATE_TASK_DEPENDENCIES)
                await db.execute(_CREATE_BOT_RUNS)
                await db.execute(_CREATE_BOT_RUN_ARTIFACTS)
                await self._migrate_tasks_table(db)
                await db.commit()
                dep_map: Dict[str, List[str]] = {}
                async with db.execute(
                    "SELECT task_id, depends_on_task_id FROM task_dependencies"
                ) as dep_cursor:
                    dep_rows = await dep_cursor.fetchall()
                    for dep_row in dep_rows:
                        dep_map.setdefault(dep_row["task_id"], []).append(dep_row["depends_on_task_id"])

                async with db.execute("SELECT * FROM tasks") as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        depends_on = []
                        if row["depends_on"]:
                            depends_on = json.loads(row["depends_on"])
                        elif row["id"] in dep_map:
                            depends_on = dep_map[row["id"]]
                        task = Task(
                            id=row["id"],
                            bot_id=row["bot_id"],
                            payload=json.loads(row["payload"]) if row["payload"] else {},
                            metadata=(
                                TaskMetadata(**json.loads(row["metadata"]))
                                if row["metadata"]
                                else None
                            ),
                            depends_on=depends_on,
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

    async def _migrate_tasks_table(self, db: aiosqlite.Connection) -> None:
        """Ensure new task columns exist for upgraded installations."""
        async with db.execute("PRAGMA table_info(tasks)") as cursor:
            columns = await cursor.fetchall()
            column_names = {row[1] for row in columns}
            if "depends_on" not in column_names:
                await db.execute("ALTER TABLE tasks ADD COLUMN depends_on TEXT")

    async def _persist_task(self, task: Task) -> None:
        """Upsert *task* into the SQLite tasks table."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO tasks
                    (id, bot_id, payload, metadata, depends_on, status, result, error,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    depends_on = excluded.depends_on,
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
                    json.dumps(task.depends_on),
                    task.status,
                    json.dumps(task.result) if task.result is not None else None,
                    json.dumps(task.error.model_dump()) if task.error else None,
                    task.created_at,
                    task.updated_at,
                ),
            )
            await db.commit()

    async def _upsert_bot_run(self, task: Task) -> None:
        metadata = task.metadata.model_dump() if task.metadata else None
        started_at = task.updated_at if task.status == "running" else None
        completed_at = task.updated_at if task.status in {"completed", "failed"} else None
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO bot_runs
                    (id, task_id, bot_id, status, payload, metadata, result, error,
                     triggered_by_task_id, trigger_rule_id, created_at, updated_at,
                     started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    metadata = excluded.metadata,
                    result = excluded.result,
                    error = excluded.error,
                    triggered_by_task_id = excluded.triggered_by_task_id,
                    trigger_rule_id = excluded.trigger_rule_id,
                    updated_at = excluded.updated_at,
                    started_at = COALESCE(bot_runs.started_at, excluded.started_at),
                    completed_at = excluded.completed_at
                """,
                (
                    task.id,
                    task.id,
                    task.bot_id,
                    task.status,
                    json.dumps(task.payload),
                    json.dumps(metadata) if metadata is not None else None,
                    json.dumps(task.result) if task.result is not None else None,
                    json.dumps(task.error.model_dump()) if task.error else None,
                    getattr(task.metadata, "parent_task_id", None),
                    getattr(task.metadata, "trigger_rule_id", None),
                    task.created_at,
                    task.updated_at,
                    started_at,
                    completed_at,
                ),
            )
            await db.commit()

    async def _upsert_artifact(self, artifact: BotRunArtifact) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO bot_run_artifacts
                    (id, run_id, task_id, bot_id, kind, label, content, path, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    label = excluded.label,
                    content = excluded.content,
                    path = excluded.path,
                    metadata = excluded.metadata,
                    created_at = excluded.created_at
                """,
                (
                    artifact.id,
                    artifact.run_id,
                    artifact.task_id,
                    artifact.bot_id,
                    artifact.kind,
                    artifact.label,
                    artifact.content,
                    artifact.path,
                    json.dumps(artifact.metadata),
                    artifact.created_at,
                ),
            )
            await db.commit()

    async def _record_artifacts_for_task(self, task: Task) -> None:
        now = datetime.now(timezone.utc).isoformat()
        artifacts: List[BotRunArtifact] = [
            BotRunArtifact(
                id=f"{task.id}:payload",
                run_id=task.id,
                task_id=task.id,
                bot_id=task.bot_id,
                kind="payload",
                label="Task Payload",
                content=json.dumps(task.payload, indent=2, sort_keys=True),
                metadata={},
                created_at=now,
            )
        ]
        if task.result is not None:
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:result",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="result",
                    label="Task Result",
                    content=json.dumps(task.result, indent=2, sort_keys=True),
                    metadata={},
                    created_at=now,
                )
            )
        if task.error is not None:
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:error",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="error",
                    label="Task Error",
                    content=json.dumps(task.error.model_dump(), indent=2, sort_keys=True),
                    metadata={},
                    created_at=now,
                )
            )

        explicit_artifacts = task.result.get("artifacts") if isinstance(task.result, dict) else None
        if isinstance(explicit_artifacts, list):
            for idx, item in enumerate(explicit_artifacts):
                if not isinstance(item, dict):
                    continue
                artifacts.append(
                    BotRunArtifact(
                        id=f"{task.id}:artifact:{idx}",
                        run_id=task.id,
                        task_id=task.id,
                        bot_id=task.bot_id,
                        kind="file" if item.get("path") else "note",
                        label=str(item.get("label") or item.get("name") or f"Artifact {idx + 1}"),
                        content=(item.get("content") if isinstance(item.get("content"), str) else json.dumps(item.get("content"), indent=2, sort_keys=True) if item.get("content") is not None else None),
                        path=item.get("path"),
                        metadata={k: v for k, v in item.items() if k not in {"label", "name", "content", "path"}},
                        created_at=now,
                    )
                )

        for artifact in artifacts:
            await self._upsert_artifact(artifact)

    async def _persist_dependencies(self, task: Task) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM task_dependencies WHERE task_id = ?", (task.id,))
            for dep_id in task.depends_on:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_task_id)
                    VALUES (?, ?)
                    """,
                    (task.id, dep_id),
                )
            await db.commit()

    async def create_task(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[TaskMetadata] = None,
        depends_on: Optional[List[str]] = None,
    ) -> Task:
        await self._ensure_db()
        dependencies = depends_on or []
        async with self._lock:
            for dep_id in dependencies:
                if dep_id not in self._tasks:
                    raise TaskNotFoundError(f"Dependency task not found: {dep_id}")
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        initial_status = "blocked" if dependencies else "queued"
        task = Task(
            id=task_id,
            bot_id=bot_id,
            payload=payload,
            metadata=metadata,
            depends_on=dependencies,
            status=initial_status,
            created_at=now,
            updated_at=now,
        )
        async with self._lock:
            self._tasks[task_id] = task
        await self._persist_task(task)
        await self._persist_dependencies(task)
        await self._upsert_bot_run(task)
        if task.status == "queued":
            asyncio.create_task(self._run_task(task_id))
        return task

    async def get_task(self, task_id: str) -> Task:
        await self._ensure_db()
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            return self._tasks[task_id]

    async def list_tasks(self, orchestration_id: Optional[str] = None) -> List[Task]:
        await self._ensure_db()
        async with self._lock:
            tasks = list(self._tasks.values())
        if not orchestration_id:
            return tasks
        return [
            t
            for t in tasks
            if t.metadata and t.metadata.orchestration_id == orchestration_id
        ]

    async def list_bot_runs(self, bot_id: str, limit: int = 50) -> List[BotRun]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM bot_runs
                WHERE bot_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (bot_id, max(1, int(limit))),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            BotRun(
                id=row["id"],
                task_id=row["task_id"],
                bot_id=row["bot_id"],
                status=row["status"],
                payload=json.loads(row["payload"]) if row["payload"] else {},
                metadata=TaskMetadata(**json.loads(row["metadata"])) if row["metadata"] else None,
                result=json.loads(row["result"]) if row["result"] else None,
                error=TaskError(**json.loads(row["error"])) if row["error"] else None,
                triggered_by_task_id=row["triggered_by_task_id"],
                trigger_rule_id=row["trigger_rule_id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
            )
            for row in rows
        ]

    async def list_bot_run_artifacts(self, bot_id: str, limit: int = 100) -> List[BotRunArtifact]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM bot_run_artifacts
                WHERE bot_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (bot_id, max(1, int(limit))),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            BotRunArtifact(
                id=row["id"],
                run_id=row["run_id"],
                task_id=row["task_id"],
                bot_id=row["bot_id"],
                kind=row["kind"],
                label=row["label"],
                content=row["content"],
                path=row["path"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                created_at=row["created_at"],
            )
            for row in rows
        ]

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
        await self._upsert_bot_run(updated_task)
        if status in {"completed", "failed"}:
            await self._record_artifacts_for_task(updated_task)
            await self._dispatch_triggers(updated_task)
            await self._try_unblock_tasks()

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

    async def _try_unblock_tasks(self) -> None:
        """Move ready blocked tasks into queued state and start them."""
        async with self._lock:
            ready_ids: List[str] = []
            for task_id, task in self._tasks.items():
                if task.status != "blocked":
                    continue
                if DependencyEngine.is_ready(task, self._tasks):
                    now = datetime.now(timezone.utc).isoformat()
                    self._tasks[task_id] = task.model_copy(
                        update={"status": "queued", "updated_at": now}
                    )
                    ready_ids.append(task_id)
            tasks_to_persist = [self._tasks[t_id] for t_id in ready_ids]

        for t in tasks_to_persist:
            await self._persist_task(t)
            await self._upsert_bot_run(t)
        for task_id in ready_ids:
            asyncio.create_task(self._run_task(task_id))

    async def _dispatch_triggers(self, task: Task) -> None:
        if self._bot_registry is None:
            return
        try:
            bot = await self._bot_registry.get(task.bot_id)
        except Exception:
            return
        workflow = getattr(bot, "workflow", None)
        if workflow is None or not workflow.triggers:
            return

        metadata = task.metadata or TaskMetadata()
        trigger_depth = int(metadata.trigger_depth or 0)
        max_depth = int(os.environ.get("NEXUSAI_BOT_TRIGGER_MAX_DEPTH", "6"))
        if trigger_depth >= max_depth:
            logger.warning("Skipping bot triggers for task %s due to depth cap %s", task.id, max_depth)
            return

        event = "task_completed" if task.status == "completed" else "task_failed"
        for trigger in workflow.triggers:
            if not trigger.enabled or trigger.event != event:
                continue
            if trigger.condition == "has_result" and task.result is None:
                continue
            if trigger.condition == "has_error" and task.error is None:
                continue
            if not self._trigger_matches_result(task, trigger):
                continue
            target_bot_id = self._resolve_trigger_target_bot_id(task, trigger.target_bot_id)
            if not target_bot_id:
                logger.warning("Skipping trigger %s for task %s because target bot could not be resolved", trigger.id, task.id)
                continue
            payload = self._build_trigger_payload(task, trigger)
            next_metadata = TaskMetadata(
                user_id=metadata.user_id if trigger.inherit_metadata else None,
                project_id=metadata.project_id if trigger.inherit_metadata else None,
                source="bot_trigger",
                priority=metadata.priority if trigger.inherit_metadata else None,
                conversation_id=metadata.conversation_id if trigger.inherit_metadata else None,
                orchestration_id=metadata.orchestration_id if trigger.inherit_metadata else None,
                parent_task_id=task.id,
                trigger_rule_id=trigger.id,
                trigger_depth=trigger_depth + 1,
            )
            await self.create_task(
                bot_id=target_bot_id,
                payload=payload,
                metadata=next_metadata,
            )

    def _build_trigger_payload(self, task: Task, trigger: Any) -> Any:
        payload_template = trigger.payload_template
        base_payload: Dict[str, Any] = {
            "source_bot_id": task.bot_id,
            "source_task_id": task.id,
            "source_status": task.status,
            "source_payload": task.payload,
            "source_result": task.result,
            "source_error": task.error.model_dump() if task.error else None,
            "instruction": f"Triggered by bot {task.bot_id} task {task.id}",
        }
        if isinstance(payload_template, dict):
            merged = dict(base_payload)
            merged.update(payload_template)
            return merged
        if payload_template is not None:
            return payload_template
        return base_payload

    def _resolve_trigger_target_bot_id(self, task: Task, target_bot_id: str) -> Optional[str]:
        raw = str(target_bot_id or "").strip()
        if not raw:
            return None
        if raw == "{{source_bot_id}}":
            if isinstance(task.payload, dict):
                source_bot_id = str(task.payload.get("source_bot_id") or "").strip()
                if source_bot_id:
                    return source_bot_id
            return None
        return raw

    def _trigger_matches_result(self, task: Task, trigger: Any) -> bool:
        field = str(getattr(trigger, "result_field", "") or "").strip()
        if not field:
            return True
        if not isinstance(task.result, dict):
            return False
        actual = self._lookup_result_field(task.result, field)
        expected = getattr(trigger, "result_equals", None)
        if expected is None:
            return actual is not None
        return str(actual) == str(expected)

    def _lookup_result_field(self, data: Dict[str, Any], field: str) -> Any:
        current: Any = data
        for part in str(field).split("."):
            key = part.strip()
            if not key or not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return current
