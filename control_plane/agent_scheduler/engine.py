from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from shared.models import TaskMetadata

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")
_DEFAULT_SCHEDULE_RETRY_MAX = 2
_MAX_SCHEDULE_RETRY_MAX = 5
_DEFAULT_SCHEDULE_RETRY_BACKOFF_SECONDS = 30
_MIN_SCHEDULE_RETRY_BACKOFF_SECONDS = 5
_MAX_SCHEDULE_RETRY_BACKOFF_SECONDS = 3600
_DEFAULT_SCHEDULE_OVERLAP_POLICY = "forbid"
_SCHEDULE_OVERLAP_POLICIES = {"forbid", "allow"}
_DEFAULT_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE = 500
_MIN_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE = 1
_MAX_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE = 10_000
_DEFAULT_SCHEDULE_TERMINAL_RUN_PRUNE_BATCH_SIZE = 250
_MAX_SCHEDULE_TERMINAL_RUN_PRUNE_BATCH_SIZE = 1_000
_TERMINAL_SCHEDULE_RUN_STATUSES = ("cancelled", "completed", "failed", "retried", "skipped")

_CREATE_SCHEDULES = """
CREATE TABLE IF NOT EXISTS agent_schedules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    timezone TEXT NOT NULL,
    prompt TEXT NOT NULL,
    target_bot_id TEXT,
    assignment_pm_bot_id TEXT,
    conversation_id TEXT,
    project_id TEXT,
    node_overrides_json TEXT NOT NULL DEFAULT '{}',
    retry_max INTEGER NOT NULL DEFAULT 2,
    retry_backoff_seconds INTEGER NOT NULL DEFAULT 30,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    last_scheduled_at TEXT,
    next_run_at TEXT,
    last_run_at TEXT,
    last_run_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_SCHEDULE_RUNS = """
CREATE TABLE IF NOT EXISTS agent_schedule_runs (
    id TEXT PRIMARY KEY,
    schedule_id TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL,
    orchestration_id TEXT,
    task_id TEXT,
    error_json TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    retry_not_before TEXT,
    manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_agent_schedules_due ON agent_schedules(status, next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_schedule_runs_schedule ON agent_schedule_runs(schedule_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_schedule_runs_dedupe ON agent_schedule_runs(dedupe_key)",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_load(raw: Any, default: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _schedule_task_payload(schedule: Dict[str, Any]) -> Dict[str, Any]:
    direct_payload = schedule.get("task_payload")
    if isinstance(direct_payload, dict):
        return dict(direct_payload)
    metadata = schedule.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("task_payload"), dict):
        return dict(metadata["task_payload"])
    return {}


def _db_path() -> str:
    db_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    return _DEFAULT_DB_PATH


def _parse_list_field(token: str, lower: int, upper: int) -> Set[int]:
    values: Set[int] = set()
    piece = str(token or "").strip()
    if not piece:
        raise ValueError("empty cron token")
    if piece == "*":
        return set(range(lower, upper + 1))
    for part in piece.split(","):
        value = part.strip()
        if not value:
            continue
        if value.startswith("*/"):
            interval = int(value[2:])
            if interval <= 0:
                raise ValueError("cron interval must be > 0")
            values.update(range(lower, upper + 1, interval))
            continue
        if "-" in value:
            start_raw, end_raw = value.split("-", 1)
            start = int(start_raw)
            end = int(end_raw)
            if start > end:
                raise ValueError("cron range start must be <= end")
            if start < lower or end > upper:
                raise ValueError("cron range out of bounds")
            values.update(range(start, end + 1))
            continue
        number = int(value)
        if number < lower or number > upper:
            raise ValueError("cron value out of bounds")
        values.add(number)
    if not values:
        raise ValueError("cron field resolved to empty set")
    return values


@dataclass
class _CronSpec:
    minutes: Set[int]
    hours: Set[int]
    days: Set[int]
    months: Set[int]
    weekdays: Set[int]


def _parse_cron(expr: str) -> _CronSpec:
    parts = [part.strip() for part in str(expr or "").split() if part.strip()]
    if len(parts) != 5:
        raise ValueError("cron_expression must have 5 fields: minute hour day month weekday")
    minutes = _parse_list_field(parts[0], 0, 59)
    hours = _parse_list_field(parts[1], 0, 23)
    days = _parse_list_field(parts[2], 1, 31)
    months = _parse_list_field(parts[3], 1, 12)
    weekdays = _parse_list_field(parts[4].replace("7", "0"), 0, 6)
    return _CronSpec(minutes=minutes, hours=hours, days=days, months=months, weekdays=weekdays)


def _cron_weekday(dt: datetime) -> int:
    # Python Monday=0; cron Sunday=0.
    return (dt.weekday() + 1) % 7


def _next_run_time(expr: str, timezone_name: str, *, after: Optional[datetime] = None) -> datetime:
    spec = _parse_cron(expr)
    tz = ZoneInfo(str(timezone_name or "UTC").strip() or "UTC")
    base = (after or _now()).astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(0, 525600 * 2):  # scan up to 2 years
        if (
            base.minute in spec.minutes
            and base.hour in spec.hours
            and base.day in spec.days
            and base.month in spec.months
            and _cron_weekday(base) in spec.weekdays
        ):
            return base.astimezone(timezone.utc)
        base = base + timedelta(minutes=1)
    raise ValueError("could not compute next run for cron expression within 2 years")


def _normalize_schedule_status(value: Any) -> str:
    status = str(value or "paused").strip().lower()
    if status not in {"active", "paused"}:
        raise ValueError("schedule status must be 'active' or 'paused'")
    return status


def _normalize_schedule_overlap_policy(value: Any) -> str:
    policy = str(value or _DEFAULT_SCHEDULE_OVERLAP_POLICY).strip().lower()
    if policy not in _SCHEDULE_OVERLAP_POLICIES:
        raise ValueError("overlap_policy must be 'forbid' or 'allow'")
    return policy


def _normalize_retry_settings(retry_max: Any, retry_backoff_seconds: Any) -> tuple[int, int]:
    """Validate bounded retry settings before they become persisted schedule policy."""
    if isinstance(retry_max, bool) or isinstance(retry_backoff_seconds, bool):
        raise ValueError("retry settings must be integers")
    try:
        normalized_retry_max = int(retry_max)
        normalized_backoff_seconds = int(retry_backoff_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("retry settings must be integers") from exc
    if not 0 <= normalized_retry_max <= _MAX_SCHEDULE_RETRY_MAX:
        raise ValueError(f"retry_max must be between 0 and {_MAX_SCHEDULE_RETRY_MAX}")
    if not _MIN_SCHEDULE_RETRY_BACKOFF_SECONDS <= normalized_backoff_seconds <= _MAX_SCHEDULE_RETRY_BACKOFF_SECONDS:
        raise ValueError(
            "retry_backoff_seconds must be between "
            f"{_MIN_SCHEDULE_RETRY_BACKOFF_SECONDS} and {_MAX_SCHEDULE_RETRY_BACKOFF_SECONDS}"
        )
    return normalized_retry_max, normalized_backoff_seconds


def _terminal_run_retention_per_schedule(value: Optional[int] = None) -> int:
    """Resolve a bounded history retention setting without accepting unsafe values."""
    raw_value: Any = value
    if raw_value is None:
        raw_value = os.environ.get(
            "NEXUSAI_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE",
            _DEFAULT_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE,
        )
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        parsed_value = _DEFAULT_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE
    return min(
        _MAX_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE,
        max(_MIN_SCHEDULE_TERMINAL_RUN_RETENTION_PER_SCHEDULE, parsed_value),
    )


def _terminal_run_prune_batch_size(value: Optional[int] = None) -> int:
    """Bound each maintenance transaction so schedule cleanup cannot monopolize SQLite."""
    raw_value: Any = value
    if raw_value is None:
        raw_value = _DEFAULT_SCHEDULE_TERMINAL_RUN_PRUNE_BATCH_SIZE
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        parsed_value = _DEFAULT_SCHEDULE_TERMINAL_RUN_PRUNE_BATCH_SIZE
    return min(_MAX_SCHEDULE_TERMINAL_RUN_PRUNE_BATCH_SIZE, max(1, parsed_value))


def _validate_schedule_dispatch(
    *,
    prompt: str,
    target_bot_id: Optional[str],
    assignment_pm_bot_id: Optional[str],
    conversation_id: Optional[str],
) -> None:
    if not prompt:
        raise ValueError("prompt is required")

    has_target_bot = bool(target_bot_id)
    has_assignment_pm = bool(assignment_pm_bot_id)
    has_conversation = bool(conversation_id)
    if has_assignment_pm != has_conversation:
        raise ValueError("assignment schedules require both assignment_pm_bot_id and conversation_id")
    if has_target_bot == has_assignment_pm:
        raise ValueError("schedule requires exactly one dispatch target")


class AgentScheduleEngine:
    def __init__(
        self,
        *,
        assignment_service: Any,
        task_manager: Any,
        db_path: Optional[str] = None,
        autonomy_guard: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
        payload_materializer: Optional[Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
        terminal_run_retention_per_schedule: Optional[int] = None,
        terminal_run_prune_batch_size: Optional[int] = None,
    ) -> None:
        self._assignment_service = assignment_service
        self._task_manager = task_manager
        self._db_path = db_path or _db_path()
        self._autonomy_guard = autonomy_guard
        self._payload_materializer = payload_materializer
        self._terminal_run_retention_per_schedule = _terminal_run_retention_per_schedule(
            terminal_run_retention_per_schedule
        )
        self._terminal_run_prune_batch_size = _terminal_run_prune_batch_size(terminal_run_prune_batch_size)
        self._ready = False
        self._tick_lock = asyncio.Lock()

    async def _ensure_db(self) -> None:
        if self._ready:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_sqlite(self._db_path) as db:
            await db.execute(_CREATE_SCHEDULES)
            await db.execute(_CREATE_SCHEDULE_RUNS)
            async with db.execute("PRAGMA table_info(agent_schedule_runs)") as cursor:
                run_columns = {str(row[1]) for row in await cursor.fetchall()}
            if "retry_not_before" not in run_columns:
                await db.execute("ALTER TABLE agent_schedule_runs ADD COLUMN retry_not_before TEXT")
            if "manual" not in run_columns:
                await db.execute("ALTER TABLE agent_schedule_runs ADD COLUMN manual INTEGER NOT NULL DEFAULT 0")
            for statement in _CREATE_INDEXES:
                await db.execute(statement)
            await db.commit()
        self._ready = True

    async def create_schedule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now()
        cron_expression = str(payload.get("cron_expression") or "").strip()
        timezone_name = str(payload.get("timezone") or "UTC").strip() or "UTC"
        prompt = str(payload.get("prompt") or "").strip()
        target_bot_id = str(payload.get("target_bot_id") or "").strip() or None
        assignment_pm_bot_id = str(payload.get("assignment_pm_bot_id") or "").strip() or None
        conversation_id = str(payload.get("conversation_id") or "").strip() or None
        if not cron_expression:
            raise ValueError("cron_expression is required")
        _validate_schedule_dispatch(
            prompt=prompt,
            target_bot_id=target_bot_id,
            assignment_pm_bot_id=assignment_pm_bot_id,
            conversation_id=conversation_id,
        )
        next_run = _next_run_time(cron_expression, timezone_name, after=now)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata = dict(metadata)
        overlap_policy = _normalize_schedule_overlap_policy(
            payload.get("overlap_policy", metadata.get("overlap_policy"))
        )
        metadata["overlap_policy"] = overlap_policy
        task_payload = payload.get("task_payload") if isinstance(payload.get("task_payload"), dict) else {}
        if "task_payload" in payload:
            metadata["task_payload"] = task_payload
        retry_max, retry_backoff_seconds = _normalize_retry_settings(
            payload.get("retry_max", _DEFAULT_SCHEDULE_RETRY_MAX),
            payload.get("retry_backoff_seconds", _DEFAULT_SCHEDULE_RETRY_BACKOFF_SECONDS),
        )
        schedule = {
            "id": str(uuid.uuid4()),
            "name": str(payload.get("name") or "").strip() or "Scheduled Agent",
            "status": _normalize_schedule_status(payload.get("status")),
            "cron_expression": cron_expression,
            "timezone": timezone_name,
            "prompt": prompt,
            "target_bot_id": target_bot_id,
            "assignment_pm_bot_id": assignment_pm_bot_id,
            "conversation_id": conversation_id,
            "project_id": str(payload.get("project_id") or "").strip() or None,
            "node_overrides": payload.get("node_overrides") if isinstance(payload.get("node_overrides"), dict) else {},
            "task_payload": task_payload,
            "retry_max": retry_max,
            "retry_backoff_seconds": retry_backoff_seconds,
            "overlap_policy": overlap_policy,
            "metadata": metadata,
            "last_scheduled_at": None,
            "next_run_at": _iso(next_run),
            "last_run_at": None,
            "last_run_status": None,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO agent_schedules (
                    id, name, status, cron_expression, timezone, prompt, target_bot_id, assignment_pm_bot_id,
                    conversation_id, project_id, node_overrides_json, retry_max, retry_backoff_seconds,
                    metadata_json, last_scheduled_at, next_run_at, last_run_at, last_run_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule["id"],
                    schedule["name"],
                    schedule["status"],
                    schedule["cron_expression"],
                    schedule["timezone"],
                    schedule["prompt"],
                    schedule["target_bot_id"],
                    schedule["assignment_pm_bot_id"],
                    schedule["conversation_id"],
                    schedule["project_id"],
                    _json_dump(schedule["node_overrides"]),
                    schedule["retry_max"],
                    schedule["retry_backoff_seconds"],
                    _json_dump(schedule["metadata"]),
                    schedule["last_scheduled_at"],
                    schedule["next_run_at"],
                    schedule["last_run_at"],
                    schedule["last_run_status"],
                    schedule["created_at"],
                    schedule["updated_at"],
                ),
            )
            await db.commit()
        return schedule

    async def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        sid = str(schedule_id or "").strip()
        if not sid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM agent_schedules WHERE id = ? LIMIT 1", (sid,)) as cursor:
                row = await cursor.fetchone()
        return self._row_to_schedule(row)

    async def list_schedules(
        self,
        *,
        limit: int = 100,
        status: Optional[str] = None,
        target_bot_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 500))
        filters: List[str] = []
        values: List[Any] = []
        normalized_status = str(status or "").strip().lower()
        if normalized_status:
            normalized_status = _normalize_schedule_status(normalized_status)
            filters.append("status = ?")
            values.append(normalized_status)
        normalized_bot_id = str(target_bot_id or "").strip()
        if normalized_bot_id:
            filters.append("target_bot_id = ?")
            values.append(normalized_bot_id)
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT * FROM agent_schedules
            {where_clause}
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, next_run_at ASC, created_at DESC
            LIMIT ?
        """
        values.append(safe_limit)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, tuple(values)) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_schedule(row) for row in rows if row is not None]

    async def update_schedule(self, schedule_id: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        schedule = await self.get_schedule(schedule_id)
        if schedule is None:
            return None
        merged = dict(schedule)
        for key in (
            "name",
            "status",
            "cron_expression",
            "timezone",
            "prompt",
            "target_bot_id",
            "assignment_pm_bot_id",
            "conversation_id",
            "project_id",
            "task_payload",
            "retry_max",
            "retry_backoff_seconds",
            "overlap_policy",
        ):
            if key in patch:
                merged[key] = patch[key]
        if "node_overrides" in patch and isinstance(patch.get("node_overrides"), dict):
            merged["node_overrides"] = patch["node_overrides"]
        if "metadata" in patch and isinstance(patch.get("metadata"), dict):
            next_meta = dict(merged.get("metadata") or {})
            next_meta.update(patch["metadata"])
            merged["metadata"] = next_meta
        if "task_payload" in patch and isinstance(patch.get("task_payload"), dict):
            next_meta = dict(merged.get("metadata") or {})
            next_meta["task_payload"] = dict(patch["task_payload"])
            merged["metadata"] = next_meta
        merged["task_payload"] = _schedule_task_payload(merged)
        merged["overlap_policy"] = _normalize_schedule_overlap_policy(merged.get("overlap_policy"))
        merged_metadata = dict(merged.get("metadata") or {})
        merged_metadata["overlap_policy"] = merged["overlap_policy"]
        merged["metadata"] = merged_metadata

        merged["status"] = _normalize_schedule_status(merged.get("status"))
        merged["cron_expression"] = str(merged.get("cron_expression") or "").strip()
        merged["timezone"] = str(merged.get("timezone") or "UTC").strip() or "UTC"
        merged["prompt"] = str(merged.get("prompt") or "").strip()
        merged["target_bot_id"] = str(merged.get("target_bot_id") or "").strip() or None
        merged["assignment_pm_bot_id"] = str(merged.get("assignment_pm_bot_id") or "").strip() or None
        merged["conversation_id"] = str(merged.get("conversation_id") or "").strip() or None
        _validate_schedule_dispatch(
            prompt=merged["prompt"],
            target_bot_id=merged["target_bot_id"],
            assignment_pm_bot_id=merged["assignment_pm_bot_id"],
            conversation_id=merged["conversation_id"],
        )
        merged["next_run_at"] = _iso(
            _next_run_time(merged["cron_expression"], merged["timezone"], after=_now())
        )
        merged["updated_at"] = _iso(_now())
        retry_max, retry_backoff_seconds = _normalize_retry_settings(
            merged.get("retry_max", _DEFAULT_SCHEDULE_RETRY_MAX),
            merged.get("retry_backoff_seconds", _DEFAULT_SCHEDULE_RETRY_BACKOFF_SECONDS),
        )

        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                UPDATE agent_schedules
                SET name=?, status=?, cron_expression=?, timezone=?, prompt=?, target_bot_id=?,
                    assignment_pm_bot_id=?, conversation_id=?, project_id=?, node_overrides_json=?,
                    retry_max=?, retry_backoff_seconds=?, metadata_json=?, next_run_at=?, updated_at=?
                WHERE id=?
                """,
                (
                    str(merged.get("name") or ""),
                    str(merged.get("status") or "active"),
                    merged["cron_expression"],
                    merged["timezone"],
                    str(merged.get("prompt") or ""),
                    str(merged.get("target_bot_id") or "") or None,
                    str(merged.get("assignment_pm_bot_id") or "") or None,
                    str(merged.get("conversation_id") or "") or None,
                    str(merged.get("project_id") or "") or None,
                    _json_dump(merged.get("node_overrides") or {}),
                    retry_max,
                    retry_backoff_seconds,
                    _json_dump(merged.get("metadata") or {}),
                    merged["next_run_at"],
                    merged["updated_at"],
                    schedule_id,
                ),
            )
            await db.commit()
        return await self.get_schedule(schedule_id)

    async def trigger_schedule(self, schedule_id: str) -> Dict[str, Any]:
        schedule = await self.get_schedule(schedule_id)
        if schedule is None:
            raise ValueError("schedule not found")
        run, created = await self._create_run(schedule, scheduled_for=_iso(_now()), manual=True)
        if created:
            await self._dispatch_run(schedule, run)
        elif run.get("status") == "skipped":
            await self._update_schedule_last_run(schedule["id"], status="skipped")
        return run

    async def preview_schedule_payload(self, schedule_id: str) -> Dict[str, Any]:
        """Resolve a schedule's bounded payload without creating a task or run record."""
        schedule = await self.get_schedule(schedule_id)
        if schedule is None:
            raise ValueError("schedule not found")
        payload = _schedule_task_payload(schedule)
        if self._payload_materializer is not None:
            generated_payload = await self._payload_materializer(schedule)
            if not isinstance(generated_payload, dict):
                raise ValueError("schedule payload materializer must return an object")
            payload.update(generated_payload)
        return {"schedule": schedule, "task_payload": payload}

    async def list_runs(self, schedule_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 500))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM agent_schedule_runs
                WHERE schedule_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (str(schedule_id or "").strip(), safe_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_run(row) for row in rows]

    async def tick_once(self) -> List[Dict[str, Any]]:
        await self._ensure_db()
        await self._reconcile_task_runs()
        await self._retry_failed_dispatch_runs()
        await self._prune_terminal_runs()
        due: List[Dict[str, Any]] = []
        async with self._tick_lock:
            now_iso = _iso(_now())
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    """
                    SELECT * FROM agent_schedules
                    WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?
                    ORDER BY next_run_at ASC
                    LIMIT 20
                    """,
                    (now_iso,),
                ) as cursor:
                    rows = await cursor.fetchall()
                due = [self._row_to_schedule(row) for row in rows if row is not None]
                for schedule in due:
                    scheduled_for = str(schedule.get("next_run_at") or now_iso)
                    next_run = _next_run_time(
                        str(schedule.get("cron_expression") or ""),
                        str(schedule.get("timezone") or "UTC"),
                        after=_now(),
                    )
                    await db.execute(
                        """
                        UPDATE agent_schedules
                        SET last_scheduled_at = ?, next_run_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (scheduled_for, _iso(next_run), _iso(_now()), schedule["id"]),
                    )
                    schedule["last_scheduled_at"] = scheduled_for
                await db.commit()
        runs: List[Dict[str, Any]] = []
        for schedule in due:
            run, created = await self._create_run(
                schedule,
                scheduled_for=schedule.get("last_scheduled_at") or _iso(_now()),
                manual=False,
            )
            runs.append(run)
            if created:
                await self._dispatch_run(schedule, run)
            elif run.get("status") == "skipped":
                await self._update_schedule_last_run(schedule["id"], status="skipped")
        return runs

    async def _prune_terminal_runs(self) -> int:
        """Keep recent terminal history while preserving all work that can still run or reconcile."""
        placeholders = ", ".join("?" for _ in _TERMINAL_SCHEDULE_RUN_STATUSES)
        pruned = 0
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                f"""
                SELECT DISTINCT schedule_id
                FROM agent_schedule_runs
                WHERE status IN ({placeholders})
                """,
                _TERMINAL_SCHEDULE_RUN_STATUSES,
            ) as cursor:
                schedule_rows = await cursor.fetchall()
            for schedule_row in schedule_rows:
                schedule_id = str(schedule_row["schedule_id"] or "").strip()
                if not schedule_id:
                    continue
                async with db.execute(
                    f"""
                    SELECT id
                    FROM agent_schedule_runs
                    WHERE schedule_id = ? AND status IN ({placeholders})
                    ORDER BY created_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (
                        schedule_id,
                        *_TERMINAL_SCHEDULE_RUN_STATUSES,
                        self._terminal_run_prune_batch_size,
                        self._terminal_run_retention_per_schedule,
                    ),
                ) as cursor:
                    rows = await cursor.fetchall()
                if not rows:
                    continue
                await db.executemany(
                    "DELETE FROM agent_schedule_runs WHERE id = ?",
                    [(str(row["id"]),) for row in rows],
                )
                pruned += len(rows)
            await db.commit()
        return pruned

    async def _create_run(
        self,
        schedule: Dict[str, Any],
        *,
        scheduled_for: str,
        manual: bool,
    ) -> tuple[Dict[str, Any], bool]:
        overlap_policy = _normalize_schedule_overlap_policy(schedule.get("overlap_policy"))
        created_at = _iso(_now())
        run = {
            "id": str(uuid.uuid4()),
            "schedule_id": str(schedule.get("id") or ""),
            "dedupe_key": f"{schedule.get('id')}|{scheduled_for}",
            "scheduled_for": scheduled_for,
            "started_at": None,
            "finished_at": None,
            "status": "queued",
            "orchestration_id": None,
            "task_id": None,
            "error": None,
            "attempt": 0,
            "created_at": created_at,
            "manual": bool(manual),
        }
        async with open_sqlite(self._db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_schedule_runs WHERE dedupe_key = ? LIMIT 1",
                (run["dedupe_key"],),
            ) as cursor:
                existing_row = await cursor.fetchone()
            if existing_row is not None:
                await db.commit()
                return self._row_to_run(existing_row), False

            if overlap_policy == "forbid":
                async with db.execute(
                    """
                    SELECT id, status FROM agent_schedule_runs
                    WHERE schedule_id = ? AND status IN ('queued', 'running')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (run["schedule_id"],),
                ) as cursor:
                    active_run = await cursor.fetchone()
                if active_run is not None:
                    active_run_id = str(active_run["id"])
                    run["status"] = "skipped"
                    run["finished_at"] = _iso(_now())
                    run["error"] = {
                        "reason": "overlap_prevented",
                        "message": "Skipped because a previous run for this schedule is still active.",
                        "active_run_id": active_run_id,
                    }
                    await db.execute(
                        """
                        INSERT INTO agent_schedule_runs (
                            id, schedule_id, dedupe_key, scheduled_for, started_at, finished_at,
                            status, orchestration_id, task_id, error_json, attempt, retry_not_before, created_at,
                            manual
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run["id"],
                            run["schedule_id"],
                            run["dedupe_key"],
                            run["scheduled_for"],
                            None,
                            run["finished_at"],
                            run["status"],
                            None,
                            None,
                            _json_dump(run["error"]),
                            0,
                            None,
                            run["created_at"],
                            int(run["manual"]),
                        ),
                    )
                    await db.commit()
                    return run, False

            cursor = await db.execute(
                """
                    INSERT OR IGNORE INTO agent_schedule_runs (
                        id, schedule_id, dedupe_key, scheduled_for, started_at, finished_at,
                        status, orchestration_id, task_id, error_json, attempt, retry_not_before, created_at,
                        manual
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run["id"],
                    run["schedule_id"],
                    run["dedupe_key"],
                    run["scheduled_for"],
                    None,
                    None,
                    run["status"],
                    None,
                    None,
                    None,
                    0,
                    None,
                    run["created_at"],
                    int(run["manual"]),
                ),
            )
            await db.commit()
        if cursor.rowcount == 1:
            return run, True

        existing = await self._get_run_by_dedupe(run["dedupe_key"])
        if existing is not None:
            return existing, False
        raise RuntimeError("schedule run was not created and no matching dedupe record exists")

    async def _get_run_by_dedupe(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_schedule_runs WHERE dedupe_key = ? LIMIT 1",
                (dedupe_key,),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_run(row) if row is not None else None

    async def _get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_schedule_runs WHERE id = ? LIMIT 1",
                (str(run_id or "").strip(),),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_run(row) if row is not None else None

    async def _dispatch_run(self, schedule: Dict[str, Any], run: Dict[str, Any]) -> None:
        await self._set_run_status(run["id"], "running", started_at=_iso(_now()))
        try:
            result = await self._dispatch_schedule(schedule)
            task_id = str(result.get("task_id") or "").strip() or None
            if task_id:
                await self._set_run_status(
                    run["id"],
                    "running",
                    task_id=task_id,
                    orchestration_id=str(result.get("orchestration_id") or "") or None,
                )
                await self._update_schedule_last_run(schedule["id"], status="running")
                return
            await self._set_run_status(
                run["id"],
                "completed",
                finished_at=_iso(_now()),
                orchestration_id=str(result.get("orchestration_id") or "") or None,
            )
            await self._update_schedule_last_run(schedule["id"], status="completed")
        except Exception as exc:
            retry_not_before = self._dispatch_retry_not_before(schedule, run)
            error = {"message": str(exc)}
            if retry_not_before is not None:
                error["retry"] = {
                    "next_attempt": int(run.get("attempt") or 0) + 1,
                    "retry_not_before": retry_not_before,
                }
            await self._set_run_status(
                run["id"],
                "failed",
                finished_at=_iso(_now()),
                error=error,
                retry_not_before=retry_not_before,
            )
            await self._update_schedule_last_run(schedule["id"], status="failed")

    def _dispatch_retry_not_before(
        self,
        schedule: Dict[str, Any],
        run: Dict[str, Any],
    ) -> Optional[str]:
        """Retry only failures that happened before a task could be created."""
        if str(run.get("task_id") or "").strip():
            return None
        try:
            retry_max = min(_MAX_SCHEDULE_RETRY_MAX, max(0, int(schedule.get("retry_max") or 0)))
            attempt = max(0, int(run.get("attempt") or 0))
            configured_backoff_seconds = int(
                schedule.get("retry_backoff_seconds") or _DEFAULT_SCHEDULE_RETRY_BACKOFF_SECONDS
            )
            backoff_seconds = min(
                _MAX_SCHEDULE_RETRY_BACKOFF_SECONDS,
                max(_MIN_SCHEDULE_RETRY_BACKOFF_SECONDS, configured_backoff_seconds),
            )
        except (TypeError, ValueError):
            return None
        if attempt >= retry_max:
            return None
        return _iso(_now() + timedelta(seconds=backoff_seconds))

    async def _retry_failed_dispatch_runs(self) -> None:
        """Retry bounded failures where no task was ever handed to a worker."""
        now_iso = _iso(_now())
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT runs.*
                FROM agent_schedule_runs AS runs
                JOIN agent_schedules AS schedules ON schedules.id = runs.schedule_id
                WHERE runs.status = 'failed'
                  AND (runs.task_id IS NULL OR runs.task_id = '')
                  AND runs.retry_not_before IS NOT NULL
                  AND runs.retry_not_before <= ?
                  AND runs.attempt < schedules.retry_max
                  AND schedules.status = 'active'
                ORDER BY runs.retry_not_before ASC
                LIMIT 100
                """,
                (now_iso,),
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            run = self._row_to_run(row)
            schedule = await self.get_schedule(run["schedule_id"])
            if schedule is None:
                continue
            claimed = await self._claim_failed_dispatch_retry(
                run_id=run["id"],
                retry_max=max(0, int(schedule.get("retry_max") or 0)),
                now_iso=now_iso,
            )
            if claimed is not None:
                await self._dispatch_run(schedule, claimed)

    async def _claim_failed_dispatch_retry(
        self,
        *,
        run_id: str,
        retry_max: int,
        now_iso: str,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim one due retry so scheduler replicas cannot replay it twice."""
        async with open_sqlite(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE agent_schedule_runs
                SET status = 'queued', started_at = NULL, finished_at = NULL,
                    error_json = NULL, retry_not_before = NULL, attempt = attempt + 1
                WHERE id = ?
                  AND status = 'failed'
                  AND (task_id IS NULL OR task_id = '')
                  AND retry_not_before IS NOT NULL
                  AND retry_not_before <= ?
                  AND attempt < ?
                """,
                (str(run_id or "").strip(), now_iso, max(0, retry_max)),
            )
            await db.commit()
        if cursor.rowcount != 1:
            return None
        return await self._get_run(run_id)

    async def _reconcile_task_runs(self) -> None:
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM agent_schedule_runs
                WHERE status = 'running' AND task_id IS NOT NULL AND task_id != ''
                ORDER BY created_at ASC
                LIMIT 100
                """
            ) as cursor:
                rows = await cursor.fetchall()

        terminal_statuses = {"completed", "failed", "cancelled", "retried"}
        for row in rows:
            run = self._row_to_run(row)
            if run is None:
                continue
            try:
                task = await self._task_manager.get_task(str(run["task_id"]))
            except Exception as exc:
                await self._set_run_status(
                    run["id"],
                    "failed",
                    finished_at=_iso(_now()),
                    error={"message": f"Scheduled task lookup failed: {exc}"},
                )
                await self._update_schedule_last_run(run["schedule_id"], status="failed")
                continue

            task_status = str(getattr(task, "status", "") or "").strip().lower()
            if task_status not in terminal_statuses:
                continue
            if task_status == "completed":
                await self._set_run_status(
                    run["id"],
                    "completed",
                    finished_at=_iso(_now()),
                )
                await self._update_schedule_last_run(run["schedule_id"], status="completed")
                continue

            task_error = getattr(task, "error", None)
            detail = getattr(task_error, "message", None) or f"Scheduled task finished with status {task_status}"
            await self._set_run_status(
                run["id"],
                "failed",
                finished_at=_iso(_now()),
                error={"message": str(detail), "task_status": task_status},
            )
            await self._update_schedule_last_run(run["schedule_id"], status="failed")

    async def _dispatch_schedule(self, schedule: Dict[str, Any]) -> Dict[str, Any]:
        if self._autonomy_guard is not None:
            await self._autonomy_guard(schedule)
        prompt = str(schedule.get("prompt") or "").strip()
        pm_bot_id = str(schedule.get("assignment_pm_bot_id") or "").strip()
        conversation_id = str(schedule.get("conversation_id") or "").strip()
        if pm_bot_id and conversation_id and prompt:
            assignment = await self._assignment_service.create_assignment(
                conversation_id=conversation_id,
                instruction=prompt,
                pm_bot_id=pm_bot_id,
                run_id=None,
                node_overrides=schedule.get("node_overrides") if isinstance(schedule.get("node_overrides"), dict) else {},
                context_items=[],
                task_source="agent_schedule",
            )
            return {
                "orchestration_id": assignment.get("orchestration_id"),
                "assignment_id": assignment.get("assignment_id"),
                "run_id": assignment.get("run_id"),
            }
        target_bot_id = str(schedule.get("target_bot_id") or "").strip()
        if target_bot_id and prompt:
            task_payload = _schedule_task_payload(schedule)
            if self._payload_materializer is not None:
                generated_payload = await self._payload_materializer(schedule)
                if not isinstance(generated_payload, dict):
                    raise ValueError("schedule payload materializer must return an object")
                task_payload.update(generated_payload)
            task = await self._task_manager.create_task(
                bot_id=target_bot_id,
                payload={
                    **task_payload,
                    "instruction": prompt,
                    "source": "agent_schedule",
                    "schedule_id": str(schedule.get("id") or ""),
                    "project_id": str(schedule.get("project_id") or "").strip() or None,
                    "node_overrides": schedule.get("node_overrides") if isinstance(schedule.get("node_overrides"), dict) else {},
                },
                metadata=TaskMetadata(
                    source="agent_schedule",
                    project_id=str(schedule.get("project_id") or "").strip() or None,
                ),
            )
            task_metadata = getattr(task, "metadata", None)
            orchestration_id = (
                str(task_metadata.get("orchestration_id") or "").strip()
                if isinstance(task_metadata, dict)
                else str(getattr(task_metadata, "orchestration_id", "") or "").strip()
            )
            return {"task_id": task.id, "orchestration_id": orchestration_id or None}
        raise ValueError("schedule requires either (assignment_pm_bot_id + conversation_id) or target_bot_id with prompt")

    async def _set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        task_id: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
        retry_not_before: Optional[str] = None,
    ) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                UPDATE agent_schedule_runs
                SET status = ?, started_at = COALESCE(?, started_at), finished_at = COALESCE(?, finished_at),
                    orchestration_id = COALESCE(?, orchestration_id), task_id = COALESCE(?, task_id),
                    error_json = COALESCE(?, error_json), retry_not_before = COALESCE(?, retry_not_before)
                WHERE id = ?
                """,
                (
                    status,
                    started_at,
                    finished_at,
                    orchestration_id,
                    task_id,
                    _json_dump(error) if error is not None else None,
                    retry_not_before,
                    run_id,
                ),
            )
            await db.commit()

    async def _update_schedule_last_run(self, schedule_id: str, *, status: str) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                UPDATE agent_schedules
                SET last_run_at = ?, last_run_status = ?, updated_at = ?
                WHERE id = ?
                """,
                (_iso(_now()), status, _iso(_now()), schedule_id),
            )
            await db.commit()

    def _row_to_schedule(self, row: Any) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        metadata = _json_load(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        overlap_policy = _normalize_schedule_overlap_policy(metadata.get("overlap_policy"))
        metadata["overlap_policy"] = overlap_policy
        return {
            "id": str(row["id"]),
            "name": str(row["name"] or ""),
            "status": str(row["status"] or "active"),
            "cron_expression": str(row["cron_expression"] or ""),
            "timezone": str(row["timezone"] or "UTC"),
            "prompt": str(row["prompt"] or ""),
            "target_bot_id": str(row["target_bot_id"] or "") or None,
            "assignment_pm_bot_id": str(row["assignment_pm_bot_id"] or "") or None,
            "conversation_id": str(row["conversation_id"] or "") or None,
            "project_id": str(row["project_id"] or "") or None,
            "node_overrides": _json_load(row["node_overrides_json"], {}),
            "task_payload": _schedule_task_payload({"metadata": metadata}),
            "retry_max": int(row["retry_max"] or 0),
            "retry_backoff_seconds": int(row["retry_backoff_seconds"] or 30),
            "overlap_policy": overlap_policy,
            "metadata": metadata,
            "last_scheduled_at": str(row["last_scheduled_at"] or "") or None,
            "next_run_at": str(row["next_run_at"] or "") or None,
            "last_run_at": str(row["last_run_at"] or "") or None,
            "last_run_status": str(row["last_run_status"] or "") or None,
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def _row_to_run(self, row: Any) -> Dict[str, Any]:
        row_keys = set(row.keys()) if hasattr(row, "keys") else set()
        return {
            "id": str(row["id"]),
            "schedule_id": str(row["schedule_id"]),
            "dedupe_key": str(row["dedupe_key"]),
            "scheduled_for": str(row["scheduled_for"]),
            "started_at": str(row["started_at"] or "") or None,
            "finished_at": str(row["finished_at"] or "") or None,
            "status": str(row["status"] or "queued"),
            "orchestration_id": str(row["orchestration_id"] or "") or None,
            "task_id": str(row["task_id"] or "") or None,
            "error": _json_load(row["error_json"], None),
            "attempt": int(row["attempt"] or 0),
            "retry_not_before": str(row["retry_not_before"] or "") or None,
            "manual": bool(row["manual"]) if "manual" in row_keys else False,
            "created_at": str(row["created_at"]),
        }
