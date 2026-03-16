from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent / "data" / "nexusai.db")

_CREATE_REPO_WORKSPACE_RUNS = """
CREATE TABLE IF NOT EXISTS repo_workspace_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    command_json TEXT,
    details_json TEXT,
    metrics_json TEXT,
    wall_time_ms INTEGER,
    cpu_user_seconds REAL,
    cpu_system_seconds REAL,
    peak_rss_bytes INTEGER,
    io_read_bytes INTEGER,
    io_write_bytes INTEGER
)
"""


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


class RepoWorkspaceUsageStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
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
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(_CREATE_REPO_WORKSPACE_RUNS)
                await db.commit()
            self._db_ready = True

    async def record_run(
        self,
        *,
        project_id: str,
        action: str,
        status: str,
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        command: Optional[List[str]] = None,
        details: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        started = _parse_iso8601(started_at) or datetime.now(timezone.utc)
        finished = _parse_iso8601(finished_at)
        duration_ms: Optional[int] = None
        if finished is not None:
            duration_ms = max(0, int((finished - started).total_seconds() * 1000))
        metrics_obj = metrics if isinstance(metrics, dict) else {}
        row = {
            "id": str(uuid.uuid4()),
            "project_id": str(project_id),
            "action": str(action or "").strip() or "unknown",
            "status": str(status or "").strip() or "unknown",
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat() if finished is not None else None,
            "duration_ms": duration_ms,
            "command": command if isinstance(command, list) else [],
            "details": details if isinstance(details, dict) else {},
            "metrics": metrics_obj,
            "wall_time_ms": int(metrics_obj.get("wall_time_ms") or 0) if metrics_obj.get("wall_time_ms") is not None else None,
            "cpu_user_seconds": float(metrics_obj.get("cpu_user_seconds") or 0.0)
            if metrics_obj.get("cpu_user_seconds") is not None
            else None,
            "cpu_system_seconds": float(metrics_obj.get("cpu_system_seconds") or 0.0)
            if metrics_obj.get("cpu_system_seconds") is not None
            else None,
            "peak_rss_bytes": int(metrics_obj.get("peak_rss_bytes") or 0)
            if metrics_obj.get("peak_rss_bytes") is not None
            else None,
            "io_read_bytes": int(metrics_obj.get("io_read_bytes") or 0)
            if metrics_obj.get("io_read_bytes") is not None
            else None,
            "io_write_bytes": int(metrics_obj.get("io_write_bytes") or 0)
            if metrics_obj.get("io_write_bytes") is not None
            else None,
        }

        async with self._lock:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO repo_workspace_runs (
                        id, project_id, action, status, started_at, finished_at, duration_ms,
                        command_json, details_json, metrics_json,
                        wall_time_ms, cpu_user_seconds, cpu_system_seconds, peak_rss_bytes, io_read_bytes, io_write_bytes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["id"],
                        row["project_id"],
                        row["action"],
                        row["status"],
                        row["started_at"],
                        row["finished_at"],
                        row["duration_ms"],
                        json.dumps(row["command"]),
                        json.dumps(row["details"]),
                        json.dumps(row["metrics"]),
                        row["wall_time_ms"],
                        row["cpu_user_seconds"],
                        row["cpu_system_seconds"],
                        row["peak_rss_bytes"],
                        row["io_read_bytes"],
                        row["io_write_bytes"],
                    ),
                )
                await db.commit()
        return row

    async def list_runs(self, *, project_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 1000))
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, project_id, action, status, started_at, finished_at, duration_ms,
                       command_json, details_json, metrics_json
                FROM repo_workspace_runs
                WHERE project_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (str(project_id), safe_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            for key in ("command_json", "details_json", "metrics_json"):
                raw = data.get(key)
                try:
                    parsed = json.loads(raw) if raw else {}
                except Exception:
                    parsed = {}
                out_key = key.replace("_json", "")
                data[out_key] = parsed
                data.pop(key, None)
            out.append(data)
        return out

    async def summarize(
        self,
        *,
        project_id: str,
        since_hours: Optional[int] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        where = "WHERE project_id = ?"
        params: List[Any] = [str(project_id)]
        if since_hours is not None and int(since_hours) > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
            where += " AND started_at >= ?"
            params.append(cutoff.isoformat())

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""
                SELECT
                    COUNT(*) AS total_runs,
                    SUM(CASE WHEN status IN ('ok', 'no_changes', 'already_cloned') THEN 1 ELSE 0 END) AS success_runs,
                    SUM(CASE WHEN status IN ('ok', 'no_changes', 'already_cloned') THEN 0 ELSE 1 END) AS failed_runs,
                    COALESCE(SUM(duration_ms), 0) AS total_duration_ms,
                    COALESCE(SUM(cpu_user_seconds), 0) AS total_cpu_user_seconds,
                    COALESCE(SUM(cpu_system_seconds), 0) AS total_cpu_system_seconds,
                    COALESCE(MAX(peak_rss_bytes), 0) AS peak_rss_bytes_max,
                    COALESCE(SUM(io_read_bytes), 0) AS total_io_read_bytes,
                    COALESCE(SUM(io_write_bytes), 0) AS total_io_write_bytes
                FROM repo_workspace_runs
                {where}
                """,
                tuple(params),
            ) as cursor:
                totals = dict(await cursor.fetchone() or {})

            async with db.execute(
                f"""
                SELECT action, COUNT(*) AS runs,
                       COALESCE(SUM(duration_ms), 0) AS duration_ms,
                       COALESCE(SUM(cpu_user_seconds), 0) AS cpu_user_seconds,
                       COALESCE(SUM(cpu_system_seconds), 0) AS cpu_system_seconds,
                       COALESCE(MAX(peak_rss_bytes), 0) AS peak_rss_bytes_max
                FROM repo_workspace_runs
                {where}
                GROUP BY action
                ORDER BY runs DESC, action ASC
                """,
                tuple(params),
            ) as cursor:
                by_action_rows = [dict(row) for row in await cursor.fetchall()]

        return {
            "project_id": str(project_id),
            "since_hours": int(since_hours) if since_hours is not None else None,
            "totals": totals,
            "by_action": by_action_rows,
        }
