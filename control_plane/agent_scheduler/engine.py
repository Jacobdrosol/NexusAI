from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

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


class AgentScheduleEngine:
    def __init__(
        self,
        *,
        assignment_service: Any,
        task_manager: Any,
        db_path: Optional[str] = None,
    ) -> None:
        self._assignment_service = assignment_service
        self._task_manager = task_manager
        self._db_path = db_path or _db_path()
        self._ready = False
        self._tick_lock = asyncio.Lock()

    async def _ensure_db(self) -> None:
        if self._ready:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        async with open_sqlite(self._db_path) as db:
            await db.execute(_CREATE_SCHEDULES)
            await db.execute(_CREATE_SCHEDULE_RUNS)
            for statement in _CREATE_INDEXES:
                await db.execute(statement)
            await db.commit()
        self._ready = True

    async def create_schedule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now()
        cron_expression = str(payload.get("cron_expression") or "").strip()
        timezone_name = str(payload.get("timezone") or "UTC").strip() or "UTC"
        next_run = _next_run_time(cron_expression, timezone_name, after=now)
        schedule = {
            "id": str(uuid.uuid4()),
            "name": str(payload.get("name") or "").strip() or "Scheduled Agent",
            "status": str(payload.get("status") or "active").strip().lower(),
            "cron_expression": cron_expression,
            "timezone": timezone_name,
            "prompt": str(payload.get("prompt") or "").strip(),
            "target_bot_id": str(payload.get("target_bot_id") or "").strip() or None,
            "assignment_pm_bot_id": str(payload.get("assignment_pm_bot_id") or "").strip() or None,
            "conversation_id": str(payload.get("conversation_id") or "").strip() or None,
            "project_id": str(payload.get("project_id") or "").strip() or None,
            "node_overrides": payload.get("node_overrides") if isinstance(payload.get("node_overrides"), dict) else {},
            "retry_max": max(0, int(payload.get("retry_max", 2))),
            "retry_backoff_seconds": max(1, int(payload.get("retry_backoff_seconds", 30))),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
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
            "retry_max",
            "retry_backoff_seconds",
        ):
            if key in patch:
                merged[key] = patch[key]
        if "node_overrides" in patch and isinstance(patch.get("node_overrides"), dict):
            merged["node_overrides"] = patch["node_overrides"]
        if "metadata" in patch and isinstance(patch.get("metadata"), dict):
            next_meta = dict(merged.get("metadata") or {})
            next_meta.update(patch["metadata"])
            merged["metadata"] = next_meta

        merged["cron_expression"] = str(merged.get("cron_expression") or "").strip()
        merged["timezone"] = str(merged.get("timezone") or "UTC").strip() or "UTC"
        merged["next_run_at"] = _iso(
            _next_run_time(merged["cron_expression"], merged["timezone"], after=_now())
        )
        merged["updated_at"] = _iso(_now())

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
                    max(0, int(merged.get("retry_max") or 0)),
                    max(1, int(merged.get("retry_backoff_seconds") or 30)),
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
        run = await self._create_run(schedule, scheduled_for=_iso(_now()), manual=True)
        await self._dispatch_run(schedule, run)
        return run

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
                        (now_iso, _iso(next_run), _iso(_now()), schedule["id"]),
                    )
                await db.commit()
        runs: List[Dict[str, Any]] = []
        for schedule in due:
            run = await self._create_run(schedule, scheduled_for=schedule.get("last_scheduled_at") or _iso(_now()), manual=False)
            runs.append(run)
            await self._dispatch_run(schedule, run)
        return runs

    async def _create_run(self, schedule: Dict[str, Any], *, scheduled_for: str, manual: bool) -> Dict[str, Any]:
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
            "created_at": _iso(_now()),
            "manual": manual,
        }
        async with open_sqlite(self._db_path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO agent_schedule_runs (
                        id, schedule_id, dedupe_key, scheduled_for, started_at, finished_at,
                        status, orchestration_id, task_id, error_json, attempt, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        run["created_at"],
                    ),
                )
            except Exception:
                await db.rollback()
                existing = await self._get_run_by_dedupe(run["dedupe_key"])
                if existing is not None:
                    return existing
                raise
            await db.commit()
        return run

    async def _get_run_by_dedupe(self, dedupe_key: str) -> Optional[Dict[str, Any]]:
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM agent_schedule_runs WHERE dedupe_key = ? LIMIT 1",
                (dedupe_key,),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_run(row) if row is not None else None

    async def _dispatch_run(self, schedule: Dict[str, Any], run: Dict[str, Any]) -> None:
        await self._set_run_status(run["id"], "running", started_at=_iso(_now()))
        try:
            result = await self._dispatch_schedule(schedule)
            await self._set_run_status(
                run["id"],
                "completed",
                finished_at=_iso(_now()),
                orchestration_id=str(result.get("orchestration_id") or "") or None,
                task_id=str(result.get("task_id") or "") or None,
            )
            await self._update_schedule_last_run(schedule["id"], status="completed")
        except Exception as exc:
            await self._set_run_status(
                run["id"],
                "failed",
                finished_at=_iso(_now()),
                error={"message": str(exc)},
            )
            await self._update_schedule_last_run(schedule["id"], status="failed")

    async def _dispatch_schedule(self, schedule: Dict[str, Any]) -> Dict[str, Any]:
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
            )
            return {
                "orchestration_id": assignment.get("orchestration_id"),
                "assignment_id": assignment.get("assignment_id"),
                "run_id": assignment.get("run_id"),
            }
        target_bot_id = str(schedule.get("target_bot_id") or "").strip()
        if target_bot_id and prompt:
            task = await self._task_manager.create_task(
                bot_id=target_bot_id,
                payload={
                    "instruction": prompt,
                    "source": "agent_schedule",
                    "schedule_id": str(schedule.get("id") or ""),
                    "project_id": str(schedule.get("project_id") or "").strip() or None,
                    "node_overrides": schedule.get("node_overrides") if isinstance(schedule.get("node_overrides"), dict) else {},
                },
            )
            return {"task_id": task.id}
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
    ) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                UPDATE agent_schedule_runs
                SET status = ?, started_at = COALESCE(?, started_at), finished_at = COALESCE(?, finished_at),
                    orchestration_id = COALESCE(?, orchestration_id), task_id = COALESCE(?, task_id),
                    error_json = COALESCE(?, error_json)
                WHERE id = ?
                """,
                (
                    status,
                    started_at,
                    finished_at,
                    orchestration_id,
                    task_id,
                    _json_dump(error) if error is not None else None,
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
            "retry_max": int(row["retry_max"] or 0),
            "retry_backoff_seconds": int(row["retry_backoff_seconds"] or 30),
            "metadata": _json_load(row["metadata_json"], {}),
            "last_scheduled_at": str(row["last_scheduled_at"] or "") or None,
            "next_run_at": str(row["next_run_at"] or "") or None,
            "last_run_at": str(row["last_run_at"] or "") or None,
            "last_run_status": str(row["last_run_status"] or "") or None,
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def _row_to_run(self, row: Any) -> Dict[str, Any]:
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
            "created_at": str(row["created_at"]),
        }
