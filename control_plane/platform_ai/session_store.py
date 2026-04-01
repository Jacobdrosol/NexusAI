from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS platform_ai_sessions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    assignment_id TEXT,
    run_id TEXT,
    orchestration_id TEXT,
    operator_id TEXT,
    privileged INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS platform_ai_events (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_TEST_SUITES = """
CREATE TABLE IF NOT EXISTS platform_ai_test_suites (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    pipeline_bot_id TEXT,
    assignment_id TEXT,
    run_id TEXT,
    orchestration_id TEXT,
    suite_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_TEST_RUNS = """
CREATE TABLE IF NOT EXISTS platform_ai_test_runs (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL,
    session_id TEXT,
    pipeline_bot_id TEXT,
    status TEXT NOT NULL,
    assignment_id TEXT,
    run_id TEXT,
    orchestration_id TEXT,
    score REAL NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    completed_at TEXT
)
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS platform_ai_messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_sessions_assignment ON platform_ai_sessions(assignment_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_events_session ON platform_ai_events(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_messages_session ON platform_ai_messages(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_test_suites_session ON platform_ai_test_suites(session_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_test_suites_assignment ON platform_ai_test_suites(assignment_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_test_suites_pipeline ON platform_ai_test_suites(pipeline_bot_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_test_runs_suite ON platform_ai_test_runs(suite_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_platform_ai_test_runs_pipeline ON platform_ai_test_runs(pipeline_bot_id, created_at)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    db_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    return _DEFAULT_DB_PATH


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _loads(raw: Any, default: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


class PlatformAISessionStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._ready = False

    async def _ensure_db(self) -> None:
        if self._ready:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        now = _now()
        async with open_sqlite(self._db_path) as db:
            await db.execute(_CREATE_SESSIONS)
            await db.execute(_CREATE_EVENTS)
            await db.execute(_CREATE_MESSAGES)
            await db.execute(_CREATE_TEST_SUITES)
            await db.execute(_CREATE_TEST_RUNS)
            await self._ensure_column(db, "platform_ai_test_suites", "pipeline_bot_id", "TEXT")
            await self._ensure_column(db, "platform_ai_test_runs", "pipeline_bot_id", "TEXT")
            # Auto-managed pipeline test sessions should not stay active across restarts.
            await db.execute(
                """
                UPDATE platform_ai_sessions
                SET status = 'paused', updated_at = ?
                WHERE mode = 'pipeline_tuner'
                  AND status = 'active'
                  AND (operator_id IS NULL OR TRIM(operator_id) = '')
                  AND (
                    metadata_json LIKE '%"source":"pipeline_test_modal"%'
                    OR metadata_json LIKE '%"source": "pipeline_test_modal"%'
                    OR metadata_json LIKE '%"source":"pipeline_suite_api"%'
                    OR metadata_json LIKE '%"source": "pipeline_suite_api"%'
                  )
                """,
                (now,),
            )
            for statement in _CREATE_INDEXES:
                await db.execute(statement)
            await db.commit()
        self._ready = True

    async def _ensure_column(self, db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        existing = {str(row[1]) for row in rows if len(row) >= 2}
        if column in existing:
            return
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def create_session(
        self,
        *,
        mode: str,
        status: str = "active",
        assignment_id: Optional[str] = None,
        run_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        privileged: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now()
        session_id = str(uuid.uuid4())
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_ai_sessions (
                    id, mode, status, assignment_id, run_id, orchestration_id, operator_id,
                    privileged, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(mode or "").strip(),
                    str(status or "active").strip() or "active",
                    str(assignment_id or "").strip() or None,
                    str(run_id or "").strip() or None,
                    str(orchestration_id or "").strip() or None,
                    str(operator_id or "").strip() or None,
                    1 if privileged else 0,
                    _dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            await db.commit()
        session = await self.get_session(session_id)
        assert session is not None
        await self.append_event(session_id, "session_created", {"mode": mode, "privileged": bool(privileged)})
        return session

    async def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        sid = str(session_id or "").strip()
        if not sid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM platform_ai_sessions WHERE id = ? LIMIT 1", (sid,)) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "mode": str(row["mode"] or ""),
            "status": str(row["status"] or "active"),
            "assignment_id": str(row["assignment_id"] or "") or None,
            "run_id": str(row["run_id"] or "") or None,
            "orchestration_id": str(row["orchestration_id"] or "") or None,
            "operator_id": str(row["operator_id"] or "") or None,
            "privileged": bool(row["privileged"]),
            "metadata": _loads(row["metadata_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    async def list_sessions(
        self,
        *,
        assignment_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        mode: Optional[str] = None,
        archived: str = "active",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 50000))
        clauses: List[str] = []
        params: List[Any] = []
        if str(assignment_id or "").strip():
            clauses.append("assignment_id = ?")
            params.append(str(assignment_id or "").strip())
        if str(orchestration_id or "").strip():
            clauses.append("orchestration_id = ?")
            params.append(str(orchestration_id or "").strip())
        if str(mode or "").strip():
            clauses.append("mode = ?")
            params.append(str(mode or "").strip())
        archived_mode = str(archived or "active").strip().lower()
        if archived_mode == "archived":
            clauses.append("status = ?")
            params.append("archived")
        elif archived_mode == "active":
            clauses.append("status != ?")
            params.append("archived")
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        query = (
            "SELECT * FROM platform_ai_sessions "
            f"{where_sql} "
            "ORDER BY updated_at DESC "
            "LIMIT ?"
        )
        params.append(safe_limit)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "mode": str(row["mode"] or ""),
                "status": str(row["status"] or "active"),
                "assignment_id": str(row["assignment_id"] or "") or None,
                "run_id": str(row["run_id"] or "") or None,
                "orchestration_id": str(row["orchestration_id"] or "") or None,
                "operator_id": str(row["operator_id"] or "") or None,
                "privileged": bool(row["privileged"]),
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        ]

    async def update_session(
        self,
        session_id: str,
        *,
        status: Optional[str] = None,
        assignment_id: Optional[str] = None,
        run_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        current = await self.get_session(session_id)
        if current is None:
            return None
        merged_metadata = dict(current.get("metadata") or {})
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        next_status = str(status or current.get("status") or "active").strip() or "active"
        next_assignment_id = str(assignment_id or current.get("assignment_id") or "").strip() or None
        next_run_id = str(run_id or current.get("run_id") or "").strip() or None
        next_orchestration_id = str(orchestration_id or current.get("orchestration_id") or "").strip() or None
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                UPDATE platform_ai_sessions
                SET status = ?, assignment_id = ?, run_id = ?, orchestration_id = ?, metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    next_status,
                    next_assignment_id,
                    next_run_id,
                    next_orchestration_id,
                    _dumps(merged_metadata),
                    _now(),
                    session_id,
                ),
            )
            await db.commit()
        return await self.get_session(session_id)

    async def append_event(self, session_id: str, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        await self._ensure_db()
        event = {
            "id": str(uuid.uuid4()),
            "session_id": str(session_id or "").strip(),
            "event_type": str(event_type or "").strip() or "event",
            "payload": payload if isinstance(payload, dict) else {},
            "created_at": _now(),
        }
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_ai_events (id, session_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event["id"],
                    event["session_id"],
                    event["event_type"],
                    _dumps(event["payload"]),
                    event["created_at"],
                ),
            )
            await db.commit()
        return event

    async def list_events(self, session_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 50000))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, session_id, event_type, payload_json, created_at
                FROM platform_ai_events
                WHERE session_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (str(session_id or "").strip(), safe_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "session_id": str(row["session_id"]),
                "event_type": str(row["event_type"]),
                "payload": _loads(row["payload_json"], {}),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    async def append_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        message = {
            "id": str(uuid.uuid4()),
            "session_id": str(session_id or "").strip(),
            "role": str(role or "operator").strip() or "operator",
            "content": str(content or "").strip(),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": _now(),
        }
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_ai_messages (id, session_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message["id"],
                    message["session_id"],
                    message["role"],
                    message["content"],
                    _dumps(message["metadata"]),
                    message["created_at"],
                ),
            )
            await db.commit()
        return message

    async def list_messages(self, session_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 50000))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, session_id, role, content, metadata_json, created_at
                FROM platform_ai_messages
                WHERE session_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (str(session_id or "").strip(), safe_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "session_id": str(row["session_id"]),
                "role": str(row["role"] or "operator"),
                "content": str(row["content"] or ""),
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": str(row["created_at"] or ""),
            }
            for row in rows
        ]

    async def create_test_suite(
        self,
        *,
        session_id: str,
        name: str,
        suite: Dict[str, Any],
        status: str = "active",
        pipeline_bot_id: Optional[str] = None,
        assignment_id: Optional[str] = None,
        run_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        suite_id = str(uuid.uuid4())
        now = _now()
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_ai_test_suites (
                    id, session_id, name, status, pipeline_bot_id, assignment_id, run_id, orchestration_id,
                    suite_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    suite_id,
                    str(session_id or "").strip(),
                    str(name or "").strip() or "Platform AI Suite",
                    str(status or "active").strip() or "active",
                    str(pipeline_bot_id or "").strip() or None,
                    str(assignment_id or "").strip() or None,
                    str(run_id or "").strip() or None,
                    str(orchestration_id or "").strip() or None,
                    _dumps(suite),
                    _dumps(metadata or {}),
                    now,
                    now,
                ),
            )
            await db.commit()
        created = await self.get_test_suite(suite_id)
        assert created is not None
        return created

    async def get_test_suite(self, suite_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        safe_suite_id = str(suite_id or "").strip()
        if not safe_suite_id:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM platform_ai_test_suites WHERE id = ? LIMIT 1",
                (safe_suite_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "session_id": str(row["session_id"] or ""),
            "name": str(row["name"] or ""),
            "status": str(row["status"] or "active"),
            "pipeline_bot_id": str(row["pipeline_bot_id"] or "") or None,
            "assignment_id": str(row["assignment_id"] or "") or None,
            "run_id": str(row["run_id"] or "") or None,
            "orchestration_id": str(row["orchestration_id"] or "") or None,
            "suite": _loads(row["suite_json"], {}),
            "metadata": _loads(row["metadata_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    async def list_test_suites(
        self,
        *,
        session_id: Optional[str] = None,
        pipeline_bot_id: Optional[str] = None,
        assignment_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 50000))
        clauses: List[str] = []
        params: List[Any] = []
        if str(session_id or "").strip():
            clauses.append("session_id = ?")
            params.append(str(session_id or "").strip())
        if str(pipeline_bot_id or "").strip():
            clauses.append("pipeline_bot_id = ?")
            params.append(str(pipeline_bot_id or "").strip())
        if str(assignment_id or "").strip():
            clauses.append("assignment_id = ?")
            params.append(str(assignment_id or "").strip())
        if str(orchestration_id or "").strip():
            clauses.append("orchestration_id = ?")
            params.append(str(orchestration_id or "").strip())
        where_sql = ""
        if clauses:
            where_sql = "WHERE " + " AND ".join(clauses)
        query = (
            "SELECT * FROM platform_ai_test_suites "
            f"{where_sql} "
            "ORDER BY created_at DESC "
            "LIMIT ?"
        )
        params.append(safe_limit)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "session_id": str(row["session_id"] or ""),
                "name": str(row["name"] or ""),
                "status": str(row["status"] or "active"),
                "pipeline_bot_id": str(row["pipeline_bot_id"] or "") or None,
                "assignment_id": str(row["assignment_id"] or "") or None,
                "run_id": str(row["run_id"] or "") or None,
                "orchestration_id": str(row["orchestration_id"] or "") or None,
                "suite": _loads(row["suite_json"], {}),
                "metadata": _loads(row["metadata_json"], {}),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
            }
            for row in rows
        ]

    async def create_test_run(
        self,
        *,
        suite_id: str,
        session_id: Optional[str] = None,
        pipeline_bot_id: Optional[str] = None,
        assignment_id: Optional[str] = None,
        run_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        status: str = "running",
        score: float = 0.0,
        result: Optional[Dict[str, Any]] = None,
        completed: bool = False,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        run_ref = str(uuid.uuid4())
        now = _now()
        completed_at = now if completed else None
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO platform_ai_test_runs (
                    id, suite_id, session_id, pipeline_bot_id, status, assignment_id, run_id, orchestration_id,
                    score, result_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_ref,
                    str(suite_id or "").strip(),
                    str(session_id or "").strip() or None,
                    str(pipeline_bot_id or "").strip() or None,
                    str(status or "running").strip() or "running",
                    str(assignment_id or "").strip() or None,
                    str(run_id or "").strip() or None,
                    str(orchestration_id or "").strip() or None,
                    float(score or 0.0),
                    _dumps(result or {}),
                    now,
                    completed_at,
                ),
            )
            await db.commit()
        created = await self.get_test_run(run_ref)
        assert created is not None
        return created

    async def complete_test_run(
        self,
        run_id: str,
        *,
        status: str,
        score: float,
        result: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return None
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                UPDATE platform_ai_test_runs
                SET status = ?, score = ?, result_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    str(status or "failed").strip() or "failed",
                    float(score or 0.0),
                    _dumps(result or {}),
                    _now(),
                    safe_run_id,
                ),
            )
            await db.commit()
        return await self.get_test_run(safe_run_id)

    async def get_test_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        safe_run_id = str(run_id or "").strip()
        if not safe_run_id:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM platform_ai_test_runs WHERE id = ? LIMIT 1",
                (safe_run_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "suite_id": str(row["suite_id"] or ""),
            "session_id": str(row["session_id"] or "") or None,
            "pipeline_bot_id": str(row["pipeline_bot_id"] or "") or None,
            "status": str(row["status"] or "running"),
            "assignment_id": str(row["assignment_id"] or "") or None,
            "run_id": str(row["run_id"] or "") or None,
            "orchestration_id": str(row["orchestration_id"] or "") or None,
            "score": float(row["score"] or 0.0),
            "result": _loads(row["result_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "completed_at": str(row["completed_at"] or "") or None,
        }

    async def list_test_runs(self, suite_id: str, *, limit: int = 200) -> List[Dict[str, Any]]:
        await self._ensure_db()
        safe_suite_id = str(suite_id or "").strip()
        safe_limit = max(1, min(int(limit), 50000))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM platform_ai_test_runs
                WHERE suite_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_suite_id, safe_limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            {
                "id": str(row["id"]),
                "suite_id": str(row["suite_id"] or ""),
                "session_id": str(row["session_id"] or "") or None,
                "pipeline_bot_id": str(row["pipeline_bot_id"] or "") or None,
                "status": str(row["status"] or "running"),
                "assignment_id": str(row["assignment_id"] or "") or None,
                "run_id": str(row["run_id"] or "") or None,
                "orchestration_id": str(row["orchestration_id"] or "") or None,
                "score": float(row["score"] or 0.0),
                "result": _loads(row["result_json"], {}),
                "created_at": str(row["created_at"] or ""),
                "completed_at": str(row["completed_at"] or "") or None,
            }
            for row in rows
        ]

    async def export_session_bundle(self, session_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        session = await self.get_session(session_id)
        if session is None:
            return None
        safe_session_id = str(session.get("id") or "").strip()
        if not safe_session_id:
            return None

        events = await self.list_events(safe_session_id, limit=50000)
        messages = await self.list_messages(safe_session_id, limit=50000)
        suites = await self.list_test_suites(session_id=safe_session_id, limit=50000)
        suite_runs: Dict[str, List[Dict[str, Any]]] = {}
        all_runs: List[Dict[str, Any]] = []
        for suite in suites:
            suite_id = str(suite.get("id") or "").strip()
            if not suite_id:
                continue
            runs = await self.list_test_runs(suite_id, limit=50000)
            suite_runs[suite_id] = runs
            all_runs.extend(runs)

        return {
            "exported_at": _now(),
            "session": session,
            "messages": messages,
            "events": events,
            "test_suites": suites,
            "test_runs": all_runs,
            "suite_runs": suite_runs,
        }
