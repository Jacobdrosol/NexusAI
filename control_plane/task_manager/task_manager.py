import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from shared.exceptions import TaskNotFoundError
from shared.models import BotRun, BotRunArtifact, Task, TaskError, TaskMetadata
from shared.settings_manager import SettingsManager
from control_plane.scheduler.dependency_engine import DependencyEngine

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")
_TASKS_TABLE = "cp_tasks"
_TASK_DEPENDENCIES_TABLE = "cp_task_dependencies"
_BOT_RUNS_TABLE = "cp_bot_runs"
_BOT_RUN_ARTIFACTS_TABLE = "cp_bot_run_artifacts"

_CREATE_TASKS = f"""
CREATE TABLE IF NOT EXISTS {_TASKS_TABLE} (
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

_CREATE_TASK_DEPENDENCIES = f"""
CREATE TABLE IF NOT EXISTS {_TASK_DEPENDENCIES_TABLE} (
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
)
"""

_CREATE_BOT_RUNS = f"""
CREATE TABLE IF NOT EXISTS {_BOT_RUNS_TABLE} (
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

_CREATE_BOT_RUN_ARTIFACTS = f"""
CREATE TABLE IF NOT EXISTS {_BOT_RUN_ARTIFACTS_TABLE} (
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


def _summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = sorted(str(key) for key in payload.keys())
        return f"Object with keys: {', '.join(keys[:12])}" if keys else "Empty object"
    if isinstance(payload, list):
        return f"List with {len(payload)} item(s)"
    return str(type(payload).__name__)


def _parse_iso_dt(value: Optional[str]) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_json_payload(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("empty output")
    candidates = [raw]
    fence_matches = re.findall(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(match.strip() for match in fence_matches if match.strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        for start_char in ("{", "["):
            start = candidate.find(start_char)
            if start < 0:
                continue
            try:
                parsed, end = decoder.raw_decode(candidate[start:])
                if candidate[start + end :].strip():
                    continue
                return parsed
            except json.JSONDecodeError:
                continue
    raise ValueError("no valid JSON object or array found")


def _lookup_payload_path(payload: Any, path: str) -> Any:
    current: Any = payload
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            continue
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _split_transform_expr_list(expr: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    for char in str(expr or ""):
        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth = max(0, depth - 1)
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _resolve_transform_value(expr: str, payload: Any, notes: list[str]) -> Any:
    mode = "value"
    raw_expr = str(expr or "").strip()
    if raw_expr.startswith("json:"):
        mode = "json"
        raw_expr = raw_expr[5:].strip()
    if raw_expr.startswith("render:"):
        render_path = raw_expr[len("render:") :].strip()
        if render_path.startswith("payload."):
            render_path = render_path[8:].strip()
        rendered = _transform_template_value(_lookup_payload_path(payload, render_path), payload, notes)
        if mode == "json":
            return rendered
        return rendered
    if raw_expr.startswith("coalesce:"):
        candidates = _split_transform_expr_list(raw_expr[len("coalesce:") :])
        for candidate in candidates:
            value = _resolve_transform_value(("json:" + candidate) if mode == "json" else candidate, payload, notes)
            if value not in (None, "", [], {}):
                return value
        return None
    path = raw_expr
    if path.startswith("payload."):
        path = path[8:].strip()
    value = _lookup_payload_path(payload, path)
    if mode == "json":
        if value in (None, ""):
            return [] if path.endswith("_json") else None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(str(value))
        except json.JSONDecodeError:
            notes.append(f"Could not parse JSON field: {path}")
            return None
    return value


def _transform_template_value(template: Any, payload: Any, notes: list[str]) -> Any:
    if isinstance(template, dict):
        return {str(key): _transform_template_value(value, payload, notes) for key, value in template.items()}
    if isinstance(template, list):
        return [_transform_template_value(item, payload, notes) for item in template]
    if not isinstance(template, str):
        return template

    raw = template.strip()
    if raw.startswith("{{") and raw.endswith("}}"):
        expr = raw[2:-2].strip()
        return _resolve_transform_value(expr, payload, notes)
    return template


def _is_empty_contract_value(value: Any) -> bool:
    return value in (None, "", [], {})


def _missing_payload_fields(payload: Any, fields: list[str]) -> list[str]:
    if not isinstance(payload, dict):
        return [str(field) for field in fields]
    missing: list[str] = []
    for field in fields:
        path = str(field).strip()
        if not path:
            continue
        if _lookup_payload_path(payload, path) is None:
            missing.append(path)
    return missing


def _empty_payload_fields(payload: Any, fields: list[str]) -> list[str]:
    if not isinstance(payload, dict):
        return [str(field) for field in fields]
    empty: list[str] = []
    for field in fields:
        path = str(field).strip()
        if not path:
            continue
        if _is_empty_contract_value(_lookup_payload_path(payload, path)):
            empty.append(path)
    return empty


def _looks_like_flat_launch_payload(payload: Any, required_fields: list[str]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = {str(key).strip() for key in payload.keys() if str(key).strip()}
    if len(keys) < 3:
        return False
    required = [str(field).strip() for field in required_fields if str(field).strip()]
    if not required:
        return False
    if any(field in keys for field in required):
        return False
    has_serialized_fields = any(key.endswith("_json") for key in keys)
    has_brief_like_fields = bool(keys.intersection({"topic", "subject", "scope", "audience", "language", "level"}))
    return has_serialized_fields and has_brief_like_fields


def _payload_satisfies_output_contract(payload: Any, required_fields: list[str], non_empty_fields: list[str]) -> bool:
    if not isinstance(payload, dict):
        return False
    if required_fields:
        missing = [field for field in required_fields if str(field) not in payload]
        if missing:
            return False
    if non_empty_fields:
        empty_fields = [
            str(field)
            for field in non_empty_fields
            if _is_empty_contract_value(_lookup_payload_path(payload, str(field)))
        ]
        if empty_fields:
            return False
    return bool(required_fields or non_empty_fields)


def _merge_with_contract_defaults(value: Any, defaults: Any) -> Any:
    if defaults is None:
        return value
    if _is_empty_contract_value(value):
        return defaults
    if isinstance(value, dict) and isinstance(defaults, dict):
        merged: dict[str, Any] = dict(value)
        for key, default_value in defaults.items():
            current_value = merged.get(key)
            if key not in merged:
                merged[key] = default_value
            else:
                merged[key] = _merge_with_contract_defaults(current_value, default_value)
        return merged
    if isinstance(value, list) and isinstance(defaults, list):
        return value if len(value) > 0 else defaults
    return value


def _result_usage(task: Task) -> dict[str, Any]:
    if not isinstance(task.result, dict):
        return {}
    usage = task.result.get("usage")
    return usage if isinstance(usage, dict) else {}


def _usage_summary(usage: dict[str, Any]) -> dict[str, Any]:
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and "input_tokens" in usage:
        prompt = usage.get("input_tokens")
    if completion is None and "output_tokens" in usage:
        completion = usage.get("output_tokens")
    if prompt is None and "promptTokenCount" in usage:
        prompt = usage.get("promptTokenCount")
    if completion is None and "candidatesTokenCount" in usage:
        completion = usage.get("candidatesTokenCount")

    total = usage.get("total_tokens")
    if total is None:
        try:
            total = int(prompt or 0) + int(completion or 0)
        except (TypeError, ValueError):
            total = None

    summary = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }
    for key, value in usage.items():
        if key not in summary:
            summary[key] = value
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _execution_summary(task: Task) -> dict[str, Any]:
    created = _parse_iso_dt(task.created_at)
    updated = _parse_iso_dt(task.updated_at)
    duration_ms = None
    if created is not None and updated is not None:
        duration_ms = max(0, int((updated - created).total_seconds() * 1000))

    usage_summary = _usage_summary(_result_usage(task))
    metadata = task.metadata.model_dump() if task.metadata else {}
    return {
        "task_id": task.id,
        "bot_id": task.bot_id,
        "status": task.status,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "duration_ms": duration_ms,
        "metadata": metadata,
        "usage": usage_summary,
        "has_result": task.result is not None,
        "has_error": task.error is not None,
    }


def _execution_report_markdown(task: Task) -> str:
    summary = _execution_summary(task)
    usage = summary.get("usage") or {}
    lines = [
        f"# Execution Report: {task.bot_id}",
        "",
        f"- Task ID: {task.id}",
        f"- Status: {task.status}",
        f"- Created: {task.created_at}",
        f"- Updated: {task.updated_at}",
        f"- Duration (ms): {summary.get('duration_ms') if summary.get('duration_ms') is not None else '—'}",
        f"- Project: {summary['metadata'].get('project_id') or '—'}",
        f"- Source: {summary['metadata'].get('source') or '—'}",
        f"- Orchestration: {summary['metadata'].get('orchestration_id') or '—'}",
    ]
    if usage:
        lines.extend(
            [
                "",
                "## Token Usage",
                f"- Prompt Tokens: {usage.get('prompt_tokens', '—')}",
                f"- Completion Tokens: {usage.get('completion_tokens', '—')}",
                f"- Total Tokens: {usage.get('total_tokens', '—')}",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def _settings_int(name: str, default: int) -> int:
    try:
        return int(SettingsManager.instance().get(name, default))
    except Exception:
        return default


def _settings_float(name: str, default: float) -> float:
    try:
        return float(SettingsManager.instance().get(name, default))
    except Exception:
        return default


def _is_retryable_error_message(message: str) -> bool:
    normalized = str(message or "").strip().lower()
    if not normalized:
        return False
    retryable_markers = [
        "timeout",
        "timed out",
        "readtimeout",
        "connecttimeout",
        "no valid json object or array found",
        "output contract requires structured json output",
        "output contract missing required fields",
        "output contract requires a json object",
        "output contract requires a json array",
        "internal server error",
        "server error",
        "bad gateway",
        "service unavailable",
        "gateway timeout",
        "too many requests",
        "rate limit",
        "connection reset",
        "temporarily unavailable",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    ]
    return any(marker in normalized for marker in retryable_markers)


def _task_report_markdown(task: Task) -> str:
    execution = _execution_summary(task)
    usage = execution.get("usage") or {}
    lines = [
        f"# Run Report: {task.bot_id}",
        "",
        f"- Status: {task.status}",
        f"- Task ID: {task.id}",
        f"- Created: {task.created_at}",
        f"- Updated: {task.updated_at}",
        f"- Duration (ms): {execution.get('duration_ms') if execution.get('duration_ms') is not None else '—'}",
    ]
    if task.metadata:
        if task.metadata.project_id:
            lines.append(f"- Project: {task.metadata.project_id}")
        if task.metadata.source:
            lines.append(f"- Source: {task.metadata.source}")
        if task.metadata.parent_task_id:
            lines.append(f"- Triggered By Task: {task.metadata.parent_task_id}")
        if task.metadata.trigger_rule_id:
            lines.append(f"- Trigger Rule: {task.metadata.trigger_rule_id}")
        if task.metadata.trigger_depth is not None:
            lines.append(f"- Trigger Depth: {task.metadata.trigger_depth}")
        if task.metadata.orchestration_id:
            lines.append(f"- Orchestration ID: {task.metadata.orchestration_id}")

    lines.extend(["", "## Payload", _summarize_payload(task.payload)])
    if usage:
        lines.extend(
            [
                "",
                "## Usage",
                f"- Prompt Tokens: {usage.get('prompt_tokens', '—')}",
                f"- Completion Tokens: {usage.get('completion_tokens', '—')}",
                f"- Total Tokens: {usage.get('total_tokens', '—')}",
            ]
        )

    if task.error is not None:
        lines.extend(
            [
                "",
                "## Error",
                f"- Message: {task.error.message}",
                f"- Code: {task.error.code or '—'}",
            ]
        )
    elif task.result is not None:
        lines.append("")
        lines.append("## Result")
        if isinstance(task.result, dict):
            report = task.result.get("report")
            if isinstance(report, str) and report.strip():
                lines.append(report.strip())
            else:
                result_keys = sorted(str(key) for key in task.result.keys())
                lines.append(f"Result keys: {', '.join(result_keys[:20])}" if result_keys else "Empty result object")
            explicit_artifacts = task.result.get("artifacts")
            if isinstance(explicit_artifacts, list):
                lines.append("")
                lines.append(f"Artifacts reported by bot: {len(explicit_artifacts)}")
        else:
            lines.append(str(task.result))
    return "\n".join(lines).strip() + "\n"


class TaskManager:
    def __init__(self, scheduler: Any, db_path: Optional[str] = None, bot_registry: Optional[Any] = None) -> None:
        self._tasks: Dict[str, Task] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._scheduler = scheduler
        self._bot_registry = bot_registry
        self._db_ready = False
        self._running_task_ids: set[str] = set()
        self._runner_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._max_concurrency = max(1, int(os.environ.get("NEXUSAI_TASK_MAX_CONCURRENCY", "4")))
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
                    f"SELECT task_id, depends_on_task_id FROM {_TASK_DEPENDENCIES_TABLE}"
                ) as dep_cursor:
                    dep_rows = await dep_cursor.fetchall()
                    for dep_row in dep_rows:
                        dep_map.setdefault(dep_row["task_id"], []).append(dep_row["depends_on_task_id"])

                async with db.execute(f"SELECT * FROM {_TASKS_TABLE}") as cursor:
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
        """Ensure new table columns exist for upgraded installations."""
        table_columns = {
            _TASKS_TABLE: {
                "bot_id": "TEXT",
                "payload": "TEXT",
                "metadata": "TEXT",
                "depends_on": "TEXT",
                "status": "TEXT",
                "result": "TEXT",
                "error": "TEXT",
                "created_at": "TEXT",
                "updated_at": "TEXT",
            },
            _BOT_RUNS_TABLE: {
                "payload": "TEXT",
                "metadata": "TEXT",
                "result": "TEXT",
                "error": "TEXT",
                "triggered_by_task_id": "TEXT",
                "trigger_rule_id": "TEXT",
                "started_at": "TEXT",
                "completed_at": "TEXT",
            },
            _BOT_RUN_ARTIFACTS_TABLE: {
                "content": "TEXT",
                "path": "TEXT",
                "metadata": "TEXT",
            },
        }
        for table_name, columns in table_columns.items():
            async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
                existing = await cursor.fetchall()
            existing_names = {row[1] for row in existing}
            for column_name, column_type in columns.items():
                if column_name in existing_names:
                    continue
                await db.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )

    async def _persist_task(self, task: Task) -> None:
        """Upsert *task* into the SQLite tasks table."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_tasks
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
        completed_at = task.updated_at if task.status in {"completed", "failed", "cancelled"} else None
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_bot_runs
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
                    started_at = COALESCE(cp_bot_runs.started_at, excluded.started_at),
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
                INSERT INTO cp_bot_run_artifacts
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
            ),
            BotRunArtifact(
                id=f"{task.id}:report",
                run_id=task.id,
                task_id=task.id,
                bot_id=task.bot_id,
                kind="note",
                label="Run Report",
                content=_task_report_markdown(task),
                metadata={
                    "status": task.status,
                    "project_id": getattr(task.metadata, "project_id", None),
                    "source": getattr(task.metadata, "source", None),
                },
                created_at=now,
            ),
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
        execution_summary = _execution_summary(task)
        artifacts.append(
            BotRunArtifact(
                id=f"{task.id}:execution-report",
                run_id=task.id,
                task_id=task.id,
                bot_id=task.bot_id,
                kind="note",
                label="Execution Report",
                content=_execution_report_markdown(task),
                metadata=execution_summary,
                created_at=now,
            )
        )
        usage = _usage_summary(_result_usage(task))
        if usage:
            artifacts.append(
                BotRunArtifact(
                    id=f"{task.id}:usage",
                    run_id=task.id,
                    task_id=task.id,
                    bot_id=task.bot_id,
                    kind="note",
                    label="Usage Report",
                    content=json.dumps(usage, indent=2, sort_keys=True),
                    metadata=usage,
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
            await db.execute(f"DELETE FROM {_TASK_DEPENDENCIES_TABLE} WHERE task_id = ?", (task.id,))
            for dep_id in task.depends_on:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO cp_task_dependencies (task_id, depends_on_task_id)
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
        await self._validate_task_payload(bot_id, payload, metadata=metadata)
        dependencies = depends_on or []
        async with self._lock:
            for dep_id in dependencies:
                if dep_id not in self._tasks:
                    raise TaskNotFoundError(f"Dependency task not found: {dep_id}")
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        initial_status = "blocked" if dependencies else "queued"
        if metadata is not None and not metadata.workflow_root_task_id:
            metadata = metadata.model_copy(update={"workflow_root_task_id": task_id})
        elif metadata is None:
            metadata = TaskMetadata(workflow_root_task_id=task_id)
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
            await self._schedule_ready_tasks()
        return task

    async def get_task(self, task_id: str) -> Task:
        await self._ensure_db()
        async with self._lock:
            if task_id not in self._tasks:
                raise TaskNotFoundError(f"Task not found: {task_id}")
            return self._tasks[task_id]

    async def retry_task(self, task_id: str, payload_override: Any = None) -> Task:
        await self._ensure_db()
        original = await self.get_task(task_id)
        metadata = original.metadata or TaskMetadata()
        retry_attempt = int(metadata.retry_attempt or 0) + 1
        next_metadata = metadata.model_copy(
            update={
                "retry_attempt": retry_attempt,
                "original_task_id": metadata.original_task_id or original.id,
                "retry_of_task_id": original.id,
                "source": "manual_retry",
            }
        )
        return await self.create_task(
            bot_id=original.bot_id,
            payload=original.payload if payload_override is None else payload_override,
            metadata=next_metadata,
            depends_on=[],
        )

    async def cancel_task(self, task_id: str) -> Task:
        await self._ensure_db()
        task = await self.get_task(task_id)
        if task.status in {"completed", "failed", "cancelled"}:
            return task

        runner: Optional[asyncio.Task[Any]] = None
        async with self._lock:
            runner = self._runner_tasks.get(task_id)

        if task.status in {"queued", "blocked"} or runner is None:
            cancelled_error = TaskError(message="Task cancelled by operator", code="cancelled")
            await self.update_status(task_id, "cancelled", error=cancelled_error)
            return await self.get_task(task_id)

        runner.cancel()
        return await self.get_task(task_id)

    async def list_tasks(
        self,
        orchestration_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        bot_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Task]:
        await self._ensure_db()
        async with self._lock:
            tasks = list(self._tasks.values())
        if orchestration_id:
            tasks = [
                t
                for t in tasks
                if t.metadata and t.metadata.orchestration_id == orchestration_id
            ]
        if statuses:
            wanted = {str(status).strip().lower() for status in statuses if str(status).strip()}
            tasks = [t for t in tasks if t.status.lower() in wanted]
        if bot_id:
            tasks = [t for t in tasks if str(t.bot_id) == str(bot_id)]
        tasks.sort(key=lambda task: (task.updated_at or "", task.created_at or ""), reverse=True)
        if limit is not None:
            tasks = tasks[: max(1, int(limit))]
        return tasks

    async def list_bot_runs(self, bot_id: str, limit: int = 50) -> List[BotRun]:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM cp_bot_runs
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

    async def list_bot_run_artifacts(
        self,
        bot_id: str,
        limit: int = 100,
        task_id: Optional[str] = None,
        include_content: bool = True,
    ) -> List[BotRunArtifact]:
        await self._ensure_db()
        sql = f"""
                SELECT * FROM {_BOT_RUN_ARTIFACTS_TABLE}
                WHERE bot_id = ?
            """
        params: List[Any] = [bot_id]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, tuple(params)) as cursor:
                rows = await cursor.fetchall()
        return [
            BotRunArtifact(
                id=row["id"],
                run_id=row["run_id"],
                task_id=row["task_id"],
                bot_id=row["bot_id"],
                kind=row["kind"],
                label=row["label"],
                content=row["content"] if include_content else None,
                path=row["path"],
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                created_at=row["created_at"],
            )
            for row in rows
        ]

    async def get_bot_run_artifact(self, bot_id: str, artifact_id: str) -> BotRunArtifact:
        await self._ensure_db()
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT * FROM {_BOT_RUN_ARTIFACTS_TABLE}
                WHERE bot_id = ? AND id = ?
                LIMIT 1
                """,
                (bot_id, artifact_id),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise TaskNotFoundError(f"Artifact not found: {artifact_id}")
        return BotRunArtifact(
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

    async def _bot_output_contract(self, bot_id: str) -> dict[str, Any]:
        if self._bot_registry is None:
            return {}
        try:
            bot = await self._bot_registry.get(bot_id)
        except Exception:
            return {}
        routing_rules = getattr(bot, "routing_rules", None)
        if isinstance(routing_rules, dict):
            contract = routing_rules.get("output_contract")
            if isinstance(contract, dict):
                return contract
        return {}

    async def _bot_input_contract(self, bot_id: str) -> dict[str, Any]:
        if self._bot_registry is None:
            return {}
        try:
            bot = await self._bot_registry.get(bot_id)
        except Exception:
            return {}
        routing_rules = getattr(bot, "routing_rules", None)
        if isinstance(routing_rules, dict):
            contract = routing_rules.get("input_contract")
            if isinstance(contract, dict):
                return contract
        return {}

    async def _validate_task_payload(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[TaskMetadata] = None,
    ) -> None:
        contract = await self._bot_input_contract(bot_id)
        if not contract:
            return
        enabled = bool(contract.get("enabled", True))
        if not enabled:
            return

        payload_format = str(contract.get("format") or "any").strip().lower()
        required_fields = contract.get("required_fields") if isinstance(contract.get("required_fields"), list) else []
        non_empty_fields = contract.get("non_empty_fields") if isinstance(contract.get("non_empty_fields"), list) else []
        form_fields = contract.get("form_fields") if isinstance(contract.get("form_fields"), list) else []
        default_payload = contract.get("default_payload") if isinstance(contract.get("default_payload"), dict) else {}

        if payload_format == "json_object" and not isinstance(payload, dict):
            raise ValueError("input contract requires a JSON object payload")
        if payload_format == "json_array" and not isinstance(payload, list):
            raise ValueError("input contract requires a JSON array payload")

        validate_before_transform = bool(contract.get("validate_before_transform", False))
        output_mode = await self._bot_output_contract_mode(bot_id)
        has_input_transform = await self._bot_has_enabled_input_transform(bot_id)
        is_intake_role = await self._bot_is_intake_role(bot_id)
        has_launch_form_contract = bool(form_fields) or bool(default_payload)
        is_saved_launch_entry = await self._is_saved_launch_entry(bot_id, metadata)
        looks_like_flat_launch_payload = _looks_like_flat_launch_payload(payload, [str(field) for field in required_fields])
        if not validate_before_transform and (
            output_mode == "payload_transform"
            or has_input_transform
            or is_intake_role
            or has_launch_form_contract
            or is_saved_launch_entry
            or looks_like_flat_launch_payload
        ):
            return

        if required_fields:
            missing = _missing_payload_fields(payload, [str(field) for field in required_fields])
            if missing:
                raise ValueError(f"input contract missing required fields: {', '.join(missing)}")
        if non_empty_fields:
            empty = _empty_payload_fields(payload, [str(field) for field in non_empty_fields])
            if empty:
                raise ValueError(f"input contract requires non-empty fields: {', '.join(empty)}")

    async def _bot_output_contract_mode(self, bot_id: str) -> str:
        contract = await self._bot_output_contract(bot_id)
        return str(contract.get("mode") or "model_output").strip().lower()

    async def _bot_has_enabled_input_transform(self, bot_id: str) -> bool:
        bot = await self._bot_registry.get(bot_id)
        routing_rules = getattr(bot, "routing_rules", None)
        if not isinstance(routing_rules, dict):
            return False
        config = routing_rules.get("input_transform")
        return isinstance(config, dict) and bool(config.get("enabled", False)) and config.get("template") is not None

    async def _bot_is_intake_role(self, bot_id: str) -> bool:
        bot = await self._bot_registry.get(bot_id)
        bot_id_value = str(getattr(bot, "id", bot_id) or bot_id).strip().lower()
        if bot_id_value.endswith("intake") or bot_id_value.endswith("-intake"):
            return True
        role = str(getattr(bot, "role", "") or "").strip().lower()
        if role.endswith("intake") or role.endswith("-intake"):
            return True
        name = str(getattr(bot, "name", "") or "").strip().lower()
        return name.endswith("intake")

    async def _is_saved_launch_entry(self, bot_id: str, metadata: Optional[TaskMetadata]) -> bool:
        if metadata is None:
            return False
        source = str(metadata.source or "").strip().lower()
        if source not in {"saved_launch", "saved_launch_pipeline", "manual_retry"}:
            return False
        if metadata.parent_task_id or metadata.trigger_rule_id:
            return False
        return True

    async def _normalize_task_result(self, task: Task, result: Any) -> Any:
        contract = await self._bot_output_contract(task.bot_id)
        if not contract:
            return result

        enabled = bool(contract.get("enabled", True))
        mode = str(contract.get("mode") or "model_output").strip().lower()
        output_format = str(contract.get("format") or "any").strip().lower()
        required_fields = contract.get("required_fields") if isinstance(contract.get("required_fields"), list) else []
        non_empty_fields = contract.get("non_empty_fields") if isinstance(contract.get("non_empty_fields"), list) else []
        defaults_template = contract.get("defaults_template") if isinstance(contract.get("defaults_template"), dict) else None
        fallback_mode = str(contract.get("fallback_mode") or "").strip().lower()
        if fallback_mode not in {"disabled", "missing_only", "parse_failure", "parse_failure_or_missing"}:
            fallback_mode = "parse_failure_or_missing" if defaults_template is not None else "disabled"
        allow_parse_failure_fallback = defaults_template is not None and fallback_mode in {"parse_failure", "parse_failure_or_missing"}
        allow_missing_backfill = defaults_template is not None and fallback_mode in {"missing_only", "parse_failure_or_missing"}
        if (
            not enabled
            or (
                output_format == "any"
                and not required_fields
                and not non_empty_fields
                and defaults_template is None
                and mode != "payload_transform"
            )
        ):
            return result

        if mode == "payload_transform":
            if _payload_satisfies_output_contract(task.payload, [str(field) for field in required_fields], [str(field) for field in non_empty_fields]):
                normalized = task.payload
            else:
                template = contract.get("template")
                if template is None:
                    raise ValueError("output contract payload transform requires a template")
                notes: list[str] = []
                transformed = _transform_template_value(template, task.payload, notes)
                if isinstance(transformed, dict) and "normalization_notes" in transformed:
                    existing = transformed.get("normalization_notes")
                    if not isinstance(existing, list):
                        existing = []
                    transformed["normalization_notes"] = existing + notes
                normalized = transformed
        else:
            normalized = result

        raw_text = ""
        if mode != "payload_transform":
            if isinstance(result, dict):
                for key in ("output", "content", "text", "result"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        raw_text = value
                        break
            elif isinstance(result, str):
                raw_text = result

        if mode != "payload_transform" and (output_format in {"json_object", "json_array"} or required_fields):
            parsed = None
            if isinstance(result, (dict, list)) and output_format == "json_array" and isinstance(result, list):
                parsed = result
            elif raw_text:
                try:
                    parsed = _extract_json_payload(raw_text)
                except ValueError:
                    if allow_parse_failure_fallback:
                        default_notes: list[str] = ["Model output was not parseable JSON; fell back to defaults template."]
                        parsed = _transform_template_value(defaults_template, task.payload, default_notes)
                        if isinstance(parsed, dict):
                            existing_notes = parsed.get("normalization_notes")
                            if not isinstance(existing_notes, list):
                                existing_notes = []
                            parsed["normalization_notes"] = existing_notes + default_notes
                    else:
                        raise
            elif isinstance(result, dict) and output_format in {"json_object", "any"} and required_fields:
                parsed = result
            if parsed is None:
                raise ValueError("output contract requires structured JSON output")
            normalized = parsed

        if allow_missing_backfill:
            default_notes: list[str] = []
            normalized_defaults = _transform_template_value(defaults_template, task.payload, default_notes)
            if isinstance(normalized_defaults, dict):
                normalized = _merge_with_contract_defaults(normalized, normalized_defaults)
                if default_notes and isinstance(normalized, dict):
                    existing_notes = normalized.get("normalization_notes")
                    if not isinstance(existing_notes, list):
                        existing_notes = []
                    normalized["normalization_notes"] = existing_notes + default_notes

        if output_format == "json_object" and not isinstance(normalized, dict):
            raise ValueError("output contract requires a JSON object")
        if output_format == "json_array" and not isinstance(normalized, list):
            raise ValueError("output contract requires a JSON array")

        if required_fields:
            if not isinstance(normalized, dict):
                raise ValueError("required fields can only be validated on JSON objects")
            missing = [field for field in required_fields if str(field) not in normalized]
            if missing:
                raise ValueError(f"output contract missing required fields: {', '.join(str(field) for field in missing)}")
        if non_empty_fields:
            if not isinstance(normalized, dict):
                raise ValueError("non-empty fields can only be validated on JSON objects")
            empty_fields = [
                str(field)
                for field in non_empty_fields
                if _is_empty_contract_value(_lookup_payload_path(normalized, str(field)))
            ]
            if empty_fields:
                raise ValueError(
                    "output contract requires non-empty fields: "
                    + ", ".join(empty_fields)
                )

        if isinstance(result, dict) and isinstance(normalized, dict):
            if "usage" in result and "usage" not in normalized:
                normalized["usage"] = result.get("usage")
            if "artifacts" in result and "artifacts" not in normalized:
                normalized["artifacts"] = result.get("artifacts")
        return normalized

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
        if status in {"completed", "failed", "cancelled"}:
            await self._record_artifacts_for_task(updated_task)
            if status != "cancelled":
                await self._dispatch_triggers(updated_task)
            await self._try_unblock_tasks()

    async def _run_task(self, task_id: str) -> None:
        try:
            await self.update_status(task_id, "running")
            task = await self.get_task(task_id)
            mode = await self._bot_output_contract_mode(task.bot_id)
            if mode == "payload_transform":
                raw_result = {"deterministic_transform": True}
            else:
                raw_result = await self._scheduler.schedule(task)
            result = await self._normalize_task_result(task, raw_result)
            await self.update_status(task_id, "completed", result=result)
        except asyncio.CancelledError:
            logger.info("Task %s cancelled", task_id)
            task_error = TaskError(message="Task cancelled by operator", code="cancelled")
            await self.update_status(task_id, "cancelled", error=task_error)
            raise
        except Exception as e:
            logger.error("Task %s failed: %s", task_id, e)
            task = await self.get_task(task_id)
            task_error = TaskError(message=str(e))
            if await self._requeue_for_retry(task, task_error):
                logger.info("Task %s queued for automatic retry", task_id)
            else:
                await self.update_status(task_id, "failed", error=task_error)
        finally:
            async with self._lock:
                self._running_task_ids.discard(task_id)
                self._runner_tasks.pop(task_id, None)
            await self._schedule_ready_tasks()

    async def _requeue_for_retry(self, task: Task, task_error: TaskError) -> bool:
        max_retries = max(0, _settings_int("max_task_retries", 3))
        if max_retries <= 0:
            return False
        if not _is_retryable_error_message(task_error.message):
            return False

        metadata = task.metadata or TaskMetadata()
        current_attempt = int(metadata.retry_attempt or 0)
        if current_attempt >= max_retries:
            return False

        delay_seconds = max(0.0, _settings_float("task_retry_delay", 5.0))
        next_attempt = current_attempt + 1

        async def _delayed_requeue() -> None:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            now = datetime.now(timezone.utc).isoformat()
            async with self._lock:
                existing = self._tasks.get(task.id)
                if existing is None:
                    return
                next_metadata = (existing.metadata or TaskMetadata()).model_copy(
                    update={
                        "retry_attempt": next_attempt,
                        "original_task_id": (existing.metadata.original_task_id if existing.metadata and existing.metadata.original_task_id else existing.id),
                        "retry_of_task_id": existing.id,
                        "source": "auto_retry",
                    }
                )
                self._tasks[task.id] = existing.model_copy(
                    update={
                        "status": "queued",
                        "metadata": next_metadata,
                        "error": task_error,
                        "updated_at": now,
                    }
                )
                updated = self._tasks[task.id]
            await self._persist_task(updated)
            await self._upsert_bot_run(updated)
            await self._schedule_ready_tasks()

        asyncio.create_task(_delayed_requeue())
        return True

    async def _try_unblock_tasks(self) -> None:
        """Move ready blocked tasks into queued state and schedule them."""
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
        if ready_ids:
            await self._schedule_ready_tasks()

    async def _schedule_ready_tasks(self) -> None:
        async with self._lock:
            available_slots = max(0, self._max_concurrency - len(self._running_task_ids))
            if available_slots <= 0:
                return
            queued = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.status == "queued" and task.id not in self._running_task_ids
                ),
                key=lambda item: (item.created_at or "", item.updated_at or ""),
            )
            selected = queued[:available_slots]
            for task in selected:
                self._running_task_ids.add(task.id)

        for task in selected:
            runner = asyncio.create_task(self._run_task(task.id))
            async with self._lock:
                self._runner_tasks[task.id] = runner

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
        max_depth = max(1, _settings_int("bot_trigger_max_depth", 20))
        if trigger_depth >= max_depth:
            logger.warning("Skipping bot triggers for task %s due to depth cap %s", task.id, max_depth)
            return

        event = "task_completed" if task.status == "completed" else "task_failed"
        for trigger in workflow.triggers:
            try:
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
                payloads = self._build_trigger_payloads(task, trigger)
                if not payloads:
                    logger.warning("Skipping trigger %s for task %s because no payloads were produced", trigger.id, task.id)
                    continue
                next_metadata = TaskMetadata(
                    user_id=metadata.user_id if trigger.inherit_metadata else None,
                    project_id=metadata.project_id if trigger.inherit_metadata else None,
                    source="bot_trigger",
                    priority=metadata.priority if trigger.inherit_metadata else None,
                    conversation_id=metadata.conversation_id if trigger.inherit_metadata else None,
                    orchestration_id=metadata.orchestration_id if trigger.inherit_metadata else None,
                    pipeline_name=metadata.pipeline_name if trigger.inherit_metadata else None,
                    pipeline_entry_bot_id=metadata.pipeline_entry_bot_id if trigger.inherit_metadata else None,
                    parent_task_id=task.id,
                    trigger_rule_id=trigger.id,
                    trigger_depth=trigger_depth + 1,
                    workflow_root_task_id=metadata.workflow_root_task_id or task.id,
                )
                if self._trigger_uses_join(trigger):
                    await self._dispatch_join_trigger(task, trigger, target_bot_id, payloads, next_metadata)
                    continue
                for payload in payloads:
                    await self.create_task(
                        bot_id=target_bot_id,
                        payload=payload,
                        metadata=next_metadata,
                    )
            except Exception as exc:
                logger.exception(
                    "Trigger %s failed while dispatching task %s to %s",
                    getattr(trigger, "id", ""),
                    task.id,
                    getattr(trigger, "target_bot_id", ""),
                )
                await self._record_trigger_dispatch_error(
                    source_task=task,
                    trigger_id=str(getattr(trigger, "id", "") or ""),
                    target_bot_id=str(getattr(trigger, "target_bot_id", "") or ""),
                    message=str(exc),
                )

    async def _record_trigger_dispatch_error(
        self,
        source_task: Task,
        trigger_id: str,
        target_bot_id: str,
        message: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._upsert_artifact(
            BotRunArtifact(
                id=f"{source_task.id}:trigger-error:{trigger_id or 'unknown'}",
                run_id=source_task.id,
                task_id=source_task.id,
                bot_id=source_task.bot_id,
                kind="error",
                label="Trigger Dispatch Error",
                content=json.dumps(
                    {
                        "trigger_id": trigger_id,
                        "target_bot_id": target_bot_id,
                        "message": message,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                metadata={
                    "trigger_id": trigger_id,
                    "target_bot_id": target_bot_id,
                    "message": message,
                },
                created_at=now,
            )
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
            notes: list[str] = []
            transformed = _transform_template_value(payload_template, base_payload, notes)
            merged = dict(base_payload)
            if isinstance(transformed, dict):
                merged.update(transformed)
            else:
                merged["payload_template_error"] = "Trigger payload template did not resolve to a JSON object."
            if notes:
                merged["trigger_template_notes"] = notes
            return merged
        if payload_template is not None:
            notes: list[str] = []
            transformed = _transform_template_value(payload_template, base_payload, notes)
            if notes and isinstance(transformed, dict):
                transformed["trigger_template_notes"] = notes
            return transformed
        return base_payload

    def _build_trigger_payloads(self, task: Task, trigger: Any) -> List[Any]:
        payload = self._build_trigger_payload(task, trigger)
        fan_out_field = str(getattr(trigger, "fan_out_field", "") or "").strip()
        if not fan_out_field:
            return [payload]
        if not isinstance(payload, dict):
            return [payload]
        items = self._lookup_result_field(payload, fan_out_field)
        if not isinstance(items, list):
            return []
        alias = str(getattr(trigger, "fan_out_alias", "") or "").strip() or "item"
        index_alias = str(getattr(trigger, "fan_out_index_alias", "") or "").strip() or "item_index"
        total = len(items)
        payloads: List[Any] = []
        for idx, item in enumerate(items):
            next_payload = dict(payload)
            next_payload[alias] = item
            next_payload[index_alias] = idx
            next_payload["fanout_count"] = total
            payloads.append(next_payload)
        return payloads

    def _trigger_uses_join(self, trigger: Any) -> bool:
        return bool(str(getattr(trigger, "join_expected_field", "") or "").strip())

    async def _dispatch_join_trigger(
        self,
        task: Task,
        trigger: Any,
        target_bot_id: str,
        payloads: List[Any],
        next_metadata: TaskMetadata,
    ) -> None:
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            aggregate_payload = await self._build_join_payload(task, trigger, target_bot_id, payload)
            if aggregate_payload is None:
                continue
            join_step_id = self._join_step_id(task, trigger, payload)
            if not join_step_id:
                continue
            existing = await self._find_task_by_step_id(join_step_id, target_bot_id)
            if existing is not None:
                continue
            aggregate_metadata = next_metadata.model_copy(update={"step_id": join_step_id})
            await self.create_task(
                bot_id=target_bot_id,
                payload=aggregate_payload,
                metadata=aggregate_metadata,
            )

    async def _build_join_payload(
        self,
        task: Task,
        trigger: Any,
        target_bot_id: str,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        expected_field = str(getattr(trigger, "join_expected_field", "") or "").strip()
        items_alias = str(getattr(trigger, "join_items_alias", "") or "").strip() or "items"
        result_field = str(getattr(trigger, "join_result_field", "") or "").strip()
        result_items_alias = str(getattr(trigger, "join_result_items_alias", "") or "").strip() or "join_result_items"
        sort_field = str(getattr(trigger, "join_sort_field", "") or "").strip()

        expected_raw = self._lookup_result_field(payload, expected_field)
        try:
            expected_count = max(1, int(expected_raw))
        except (TypeError, ValueError):
            logger.warning("Skipping join trigger %s for task %s because expected count is invalid: %r", trigger.id, task.id, expected_raw)
            return None

        group_value = self._lookup_result_field(payload, group_field) if group_field else None
        sibling_payloads = self._collect_join_payloads(task, trigger, group_field, group_value)
        if len(sibling_payloads) < expected_count:
            return None

        if sort_field:
            sibling_payloads.sort(key=lambda item: self._sortable_join_value(self._lookup_result_field(item, sort_field)))

        aggregate_payload = dict(payload)
        aggregate_payload[items_alias] = sibling_payloads[:expected_count]
        aggregate_payload["join_results"] = [
            item.get("source_result")
            for item in aggregate_payload[items_alias]
            if isinstance(item, dict) and item.get("source_result") is not None
        ]
        if result_field:
            aggregate_payload[result_items_alias] = [
                self._lookup_result_field(item, result_field)
                for item in aggregate_payload[items_alias]
                if isinstance(item, dict) and self._lookup_result_field(item, result_field) is not None
            ]
        aggregate_payload["join_task_ids"] = [
            str(item.get("source_task_id"))
            for item in aggregate_payload[items_alias]
            if isinstance(item, dict) and item.get("source_task_id")
        ]
        aggregate_payload["join_group"] = group_value
        aggregate_payload["join_count"] = len(aggregate_payload[items_alias])
        aggregate_payload["join_expected_count"] = expected_count
        aggregate_payload["join_target_bot_id"] = target_bot_id
        return aggregate_payload

    def _collect_join_payloads(
        self,
        task: Task,
        trigger: Any,
        group_field: str,
        group_value: Any,
    ) -> List[Dict[str, Any]]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        event = "task_completed" if task.status == "completed" else "task_failed"
        matches: List[Dict[str, Any]] = []
        for candidate in self._tasks.values():
            candidate_meta = candidate.metadata or TaskMetadata()
            if candidate.bot_id != task.bot_id:
                continue
            if candidate.status != task.status:
                continue
            if (candidate_meta.workflow_root_task_id or candidate.id) != root_id:
                continue
            if metadata.orchestration_id != candidate_meta.orchestration_id:
                continue
            if metadata.project_id != candidate_meta.project_id:
                continue
            if trigger.event != event:
                continue
            if trigger.condition == "has_result" and candidate.result is None:
                continue
            if trigger.condition == "has_error" and candidate.error is None:
                continue
            if not self._trigger_matches_result(candidate, trigger):
                continue
            candidate_payload = self._build_trigger_payload(candidate, trigger)
            if not isinstance(candidate_payload, dict):
                continue
            candidate_group_value = self._lookup_result_field(candidate_payload, group_field) if group_field else None
            if group_field and candidate_group_value != group_value:
                continue
            matches.append(candidate_payload)
        return matches

    def _join_step_id(self, task: Task, trigger: Any, payload: Dict[str, Any]) -> Optional[str]:
        metadata = task.metadata or TaskMetadata()
        root_id = metadata.workflow_root_task_id or task.id
        group_field = str(getattr(trigger, "join_group_field", "") or "").strip()
        group_value = self._lookup_result_field(payload, group_field) if group_field else "__all__"
        try:
            group_token = json.dumps(group_value, sort_keys=True)
        except TypeError:
            group_token = str(group_value)
        normalized_group = re.sub(r"[^a-zA-Z0-9:_-]+", "-", group_token).strip("-") or "group"
        return f"join:{task.bot_id}:{trigger.id}:{root_id}:{normalized_group}"

    async def _find_task_by_step_id(self, step_id: str, bot_id: str) -> Optional[Task]:
        await self._ensure_db()
        async with self._lock:
            for task in self._tasks.values():
                if task.bot_id != bot_id:
                    continue
                metadata = task.metadata or TaskMetadata()
                if metadata.step_id == step_id:
                    return task
        return None

    def _sortable_join_value(self, value: Any) -> Any:
        if isinstance(value, (int, float, str)):
            return value
        if value is None:
            return ""
        return json.dumps(value, sort_keys=True, default=str)

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
        if raw.startswith("{{") and raw.endswith("}}"):
            expr = raw[2:-2].strip()
            if expr.startswith("result.") and isinstance(task.result, dict):
                resolved = self._lookup_result_field(task.result, expr[7:].strip())
                return str(resolved).strip() or None if resolved is not None else None
            if expr.startswith("payload.") and isinstance(task.payload, dict):
                resolved = self._lookup_result_field(task.payload, expr[8:].strip())
                return str(resolved).strip() or None if resolved is not None else None
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
