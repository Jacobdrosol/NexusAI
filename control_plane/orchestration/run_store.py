from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from shared.models import Task

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS orchestration_runs (
    id TEXT PRIMARY KEY,
    assignment_id TEXT NOT NULL,
    orchestration_id TEXT,
    conversation_id TEXT,
    project_id TEXT,
    pm_bot_id TEXT,
    instruction TEXT,
    state TEXT NOT NULL,
    graph_snapshot TEXT NOT NULL,
    node_overrides TEXT NOT NULL,
    lineage_parent_run_id TEXT,
    spliced_from_node_id TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_RUN_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_orchestration_runs_assignment ON orchestration_runs(assignment_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_orchestration_runs_orchestration ON orchestration_runs(orchestration_id)",
    "CREATE INDEX IF NOT EXISTS idx_orchestration_runs_conversation ON orchestration_runs(conversation_id, created_at)",
)

_CREATE_STATE_LOG = """
CREATE TABLE IF NOT EXISTS orchestration_run_state_log (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    previous_state TEXT,
    new_state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'system',
    created_at TEXT NOT NULL
)
"""

_CREATE_JOIN_STATE = """
CREATE TABLE IF NOT EXISTS orchestration_join_state (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    join_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    expected_branch_count INTEGER NOT NULL DEFAULT 0,
    resolved_branch_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_EXTRA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_orch_state_log_run ON orchestration_run_state_log(run_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_join_state_run ON orchestration_join_state(run_id, join_id)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    db_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    if db_url.startswith("sqlite:///"):
        return db_url[len("sqlite:///") :]
    return _DEFAULT_DB_PATH


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _json_loads(raw: Any, default: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _task_is_skip(task: Task) -> bool:
    result = task.result
    if not isinstance(result, dict):
        return False
    outcome = str(result.get("outcome") or result.get("status") or "").strip().lower()
    failure_type = str(result.get("failure_type") or "").strip().lower()
    return outcome == "skip" or failure_type in {"skip", "not_applicable", "not-applicable", "n/a"}


def _node_status_from_task(task: Task) -> str:
    status = str(task.status or "").strip().lower()
    if status == "completed":
        return "skipped" if _task_is_skip(task) else "succeeded"
    if status == "failed" or status == "retried":
        return "failed"
    if status == "cancelled":
        return "canceled"
    if status == "running":
        return "running"
    if status == "blocked":
        return "blocked"
    return "queued"


def _aggregate_node_status(statuses: List[str]) -> str:
    if not statuses:
        return "queued"
    priority = {
        "failed": 90,
        "canceled": 80,
        "running": 70,
        "blocked": 60,
        "queued": 50,
        "ready": 40,
        "succeeded": 30,
        "skipped": 20,
    }
    ranked = sorted(statuses, key=lambda item: priority.get(str(item or "").strip().lower(), 0), reverse=True)
    if all(str(item).lower() in {"succeeded", "skipped"} for item in statuses):
        if all(str(item).lower() == "skipped" for item in statuses):
            return "skipped"
        return "succeeded"
    return str(ranked[0] or "queued").strip().lower()


class OrchestrationRunStore:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._ready = False

    async def _ensure_db(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                await db.execute(_CREATE_RUNS)
                for statement in _CREATE_RUN_INDEXES:
                    await db.execute(statement)
                await db.execute(_CREATE_STATE_LOG)
                await db.execute(_CREATE_JOIN_STATE)
                for statement in _CREATE_EXTRA_INDEXES:
                    await db.execute(statement)
                # Add new columns to orchestration_runs (idempotent)
                await self._ensure_column(db, "orchestration_runs", "orch_state", "TEXT NOT NULL DEFAULT 'running'")
                await self._ensure_column(db, "orchestration_runs", "stall_signature", "TEXT")
                await self._ensure_column(db, "orchestration_runs", "stall_ticks", "INTEGER NOT NULL DEFAULT 0")
                await self._ensure_column(db, "orchestration_runs", "template_id", "TEXT")
                await self._ensure_column(db, "orchestration_runs", "binding_id", "TEXT")
                await self._ensure_column(db, "orchestration_runs", "run_contract_json", "TEXT")
                await self._ensure_column(db, "orchestration_runs", "completion_report_json", "TEXT")
                await db.commit()
            self._ready = True

    @staticmethod
    async def _ensure_column(db: aiosqlite.Connection, table: str, column: str, column_def: str) -> None:
        """Add a column to a table if it doesn't already exist."""
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            rows = await cursor.fetchall()
        existing = {str(r[1]) for r in rows}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_def}")

    async def create_run(
        self,
        *,
        conversation_id: str,
        project_id: Optional[str],
        pm_bot_id: str,
        instruction: str,
        graph_snapshot: Dict[str, Any],
        node_overrides: Dict[str, Any],
        assignment_id: Optional[str] = None,
        lineage_parent_run_id: Optional[str] = None,
        spliced_from_node_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        now = _now_iso()
        run_id = str(uuid.uuid4())
        root_assignment_id = str(assignment_id or run_id).strip() or run_id
        payload = {
            "id": run_id,
            "assignment_id": root_assignment_id,
            "orchestration_id": None,
            "conversation_id": str(conversation_id or "").strip(),
            "project_id": str(project_id or "").strip() or None,
            "pm_bot_id": str(pm_bot_id or "").strip(),
            "instruction": str(instruction or "").strip(),
            "state": "queued",
            "graph_snapshot": graph_snapshot if isinstance(graph_snapshot, dict) else {"nodes": [], "edges": []},
            "node_overrides": node_overrides if isinstance(node_overrides, dict) else {},
            "lineage_parent_run_id": str(lineage_parent_run_id or "").strip() or None,
            "spliced_from_node_id": str(spliced_from_node_id or "").strip() or None,
            "archived": False,
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": now,
            "updated_at": now,
        }
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    INSERT INTO orchestration_runs (
                        id, assignment_id, orchestration_id, conversation_id, project_id, pm_bot_id,
                        instruction, state, graph_snapshot, node_overrides, lineage_parent_run_id,
                        spliced_from_node_id, archived, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["id"],
                        payload["assignment_id"],
                        None,
                        payload["conversation_id"],
                        payload["project_id"],
                        payload["pm_bot_id"],
                        payload["instruction"],
                        payload["state"],
                        _json_dumps(payload["graph_snapshot"]),
                        _json_dumps(payload["node_overrides"]),
                        payload["lineage_parent_run_id"],
                        payload["spliced_from_node_id"],
                        0,
                        _json_dumps(payload["metadata"]),
                        payload["created_at"],
                        payload["updated_at"],
                    ),
                )
                await db.commit()
        return payload

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM orchestration_runs WHERE id = ? LIMIT 1", (rid,)) as cursor:
                row = await cursor.fetchone()
        return self._row_to_payload(row)

    async def get_run_by_orchestration(self, orchestration_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        oid = str(orchestration_id or "").strip()
        if not oid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM orchestration_runs
                WHERE orchestration_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (oid,),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_payload(row)

    async def get_latest_run_for_assignment(self, assignment_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        aid = str(assignment_id or "").strip()
        if not aid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM orchestration_runs
                WHERE assignment_id = ?
                ORDER BY archived ASC, created_at DESC
                LIMIT 1
                """,
                (aid,),
            ) as cursor:
                row = await cursor.fetchone()
        return self._row_to_payload(row)

    async def list_lineage(self, run_id: str) -> List[Dict[str, Any]]:
        await self._ensure_db()
        current = await self.get_run(run_id)
        if current is None:
            return []
        assignment_id = str(current.get("assignment_id") or "").strip()
        if not assignment_id:
            return [current]
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT * FROM orchestration_runs
                WHERE assignment_id = ?
                ORDER BY created_at ASC
                """,
                (assignment_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [item for item in (self._row_to_payload(row) for row in rows) if item is not None]

    async def bind_orchestration_id(self, run_id: str, orchestration_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        rid = str(run_id or "").strip()
        oid = str(orchestration_id or "").strip()
        if not rid or not oid:
            return await self.get_run(rid)
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    """
                    UPDATE orchestration_runs
                    SET orchestration_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (oid, _now_iso(), rid),
                )
                await db.commit()
        return await self.get_run(rid)

    async def archive_run(self, run_id: str) -> None:
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    "UPDATE orchestration_runs SET archived = 1, updated_at = ? WHERE id = ?",
                    (_now_iso(), rid),
                )
                await db.commit()

    async def sync_graph_from_tasks(self, run_id: str, tasks: List[Task]) -> Optional[Dict[str, Any]]:
        run = await self.get_run(run_id)
        if run is None:
            return None
        graph = run.get("graph_snapshot") if isinstance(run.get("graph_snapshot"), dict) else {"nodes": [], "edges": []}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
        node_map: Dict[str, Dict[str, Any]] = {}
        for raw in nodes:
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("id") or "").strip()
            if not node_id:
                continue
            node = dict(raw)
            node.setdefault("status", "queued")
            node_map[node_id] = node

        by_node_id: Dict[str, List[str]] = {}
        dynamic_nodes: List[Dict[str, Any]] = []
        dynamic_edges: List[Dict[str, Any]] = []
        for task in tasks:
            status = _node_status_from_task(task)
            step_id = str(task.metadata.step_id if task.metadata else "").strip()
            bot_id = str(task.bot_id or "").strip()
            target_node = step_id or bot_id
            if target_node:
                by_node_id.setdefault(target_node, []).append(status)
            task_node_id = f"task:{task.id}"
            dynamic_nodes.append(
                {
                    "id": task_node_id,
                    "title": str(task.payload.get("title") if isinstance(task.payload, dict) else "") or task.id,
                    "bot_id": bot_id,
                    "status": status,
                    "kind": "task_attempt",
                    "task_id": task.id,
                    "depends_on": list(task.depends_on or []),
                }
            )
            for dep in task.depends_on or []:
                dep_id = str(dep or "").strip()
                if dep_id:
                    dynamic_edges.append({"source": f"task:{dep_id}", "target": task_node_id, "kind": "dependency"})

        for node_id, statuses in by_node_id.items():
            if node_id in node_map:
                node_map[node_id]["status"] = _aggregate_node_status(statuses)

        merged_nodes = list(node_map.values()) + dynamic_nodes
        merged_edges = edges + dynamic_edges
        # Heuristic state kept in legacy `state` column for backwards compat.
        heuristic_state = _aggregate_node_status([str(item.get("status") or "queued") for item in merged_nodes]) if merged_nodes else "queued"
        updated_graph = {"nodes": merged_nodes, "edges": merged_edges}

        # GraphCompletenessEvaluator is the authoritative source for orch_state
        # and completion_report_json.  Fall back to heuristic if unavailable.
        task_dicts = [
            t.model_dump() if hasattr(t, "model_dump") else (t if isinstance(t, dict) else {})
            for t in tasks
        ]
        evaluator_orch_state: Optional[str] = None
        completion_report_str: Optional[str] = None
        try:
            from control_plane.orchestration.graph_completeness import GraphCompletenessEvaluator
            _ev = GraphCompletenessEvaluator.for_pm_software_delivery()
            _report = _ev.evaluate(graph=updated_graph, tasks=task_dicts)
            evaluator_orch_state = str(_report.orchestration_state or "").strip() or None
            completion_report_str = _json_dumps(_report.to_dict())
        except Exception:
            pass
        orch_state = evaluator_orch_state or heuristic_state

        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT orch_state FROM orchestration_runs WHERE id = ? LIMIT 1", (run_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                previous_orch_state = str(row["orch_state"] or "") if row else None
                now = _now_iso()
                await db.execute(
                    """
                    UPDATE orchestration_runs
                    SET graph_snapshot = ?, state = ?, orch_state = ?,
                        completion_report_json = COALESCE(?, completion_report_json),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (_json_dumps(updated_graph), heuristic_state, orch_state,
                     completion_report_str, now, run_id),
                )
                # Log the state transition for auditability.
                if previous_orch_state and previous_orch_state != orch_state:
                    log_id = str(uuid.uuid4())
                    await db.execute(
                        """INSERT INTO orchestration_run_state_log
                           (id, run_id, previous_state, new_state, reason, actor, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (log_id, run_id, previous_orch_state, orch_state, "graph_sync", "system", now),
                    )
                await db.commit()
        return await self.get_run(run_id)

    async def create_splice_child(
        self,
        *,
        run_id: str,
        spliced_from_node_id: str,
        node_overrides: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        current = await self.get_run(run_id)
        if current is None:
            raise ValueError(f"run not found: {run_id}")
        assignment_id = str(current.get("assignment_id") or "").strip() or str(current.get("id") or "").strip()
        parent_overrides = current.get("node_overrides") if isinstance(current.get("node_overrides"), dict) else {}
        merged_overrides = dict(parent_overrides)
        if isinstance(node_overrides, dict):
            for key, value in node_overrides.items():
                merged_overrides[str(key)] = value

        child = await self.create_run(
            conversation_id=str(current.get("conversation_id") or ""),
            project_id=current.get("project_id"),
            pm_bot_id=str(current.get("pm_bot_id") or ""),
            instruction=str(current.get("instruction") or ""),
            graph_snapshot=current.get("graph_snapshot") if isinstance(current.get("graph_snapshot"), dict) else {"nodes": [], "edges": []},
            node_overrides=merged_overrides,
            assignment_id=assignment_id,
            lineage_parent_run_id=str(current.get("id") or "").strip(),
            spliced_from_node_id=str(spliced_from_node_id or "").strip(),
            metadata=metadata or {},
        )
        await self.archive_run(str(current.get("id") or "").strip())
        return child

    async def update_orch_state(
        self,
        run_id: str,
        new_state: str,
        *,
        reason: str = "",
        actor: str = "system",
    ) -> Optional[Dict[str, Any]]:
        """Transition orchestration state with history logging."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return None
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT orch_state FROM orchestration_runs WHERE id = ? LIMIT 1", (rid,)
                ) as cursor:
                    row = await cursor.fetchone()
                previous_state = str(row["orch_state"] or "") if row else None
                now = _now_iso()
                await db.execute(
                    "UPDATE orchestration_runs SET orch_state = ?, updated_at = ? WHERE id = ?",
                    (new_state, now, rid),
                )
                log_id = str(uuid.uuid4())
                await db.execute(
                    """INSERT INTO orchestration_run_state_log
                       (id, run_id, previous_state, new_state, reason, actor, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (log_id, rid, previous_state, new_state, reason or "", actor or "system", now),
                )
                await db.commit()
        return await self.get_run(rid)

    async def get_orch_state(self, run_id: str) -> Optional[str]:
        """Get current orchestration state for a run."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT orch_state FROM orchestration_runs WHERE id = ? LIMIT 1", (rid,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return str(row["orch_state"] or "running")

    async def cancel_orchestration(
        self,
        run_id: str,
        *,
        reason: str = "operator_cancelled",
        actor: str = "operator",
    ) -> Dict[str, Any]:
        """
        Cancel an orchestration: update state to failed_terminal, log it.
        Returns {"cancelled": True, "run_id": run_id, "previous_state": ..., "reason": ...}
        """
        await self._ensure_db()
        rid = str(run_id or "").strip()
        previous_state = await self.get_orch_state(rid)
        await self.update_orch_state(rid, "failed_terminal", reason=reason, actor=actor)
        return {
            "cancelled": True,
            "run_id": rid,
            "previous_state": previous_state,
            "reason": reason,
        }

    async def update_stall_tracking(
        self,
        run_id: str,
        *,
        stall_signature: str,
    ) -> Dict[str, Any]:
        """
        Update stall detection tracking for a run.
        If new signature matches stored, increment stall_ticks.
        If different, reset to 0 and store new signature.
        Returns {"stall_ticks": int, "signature_changed": bool}
        """
        await self._ensure_db()
        rid = str(run_id or "").strip()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT stall_signature, stall_ticks FROM orchestration_runs WHERE id = ? LIMIT 1", (rid,)
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    return {"stall_ticks": 0, "signature_changed": False}
                current_sig = str(row["stall_signature"] or "")
                current_ticks = int(row["stall_ticks"] or 0)
                if current_sig == stall_signature:
                    new_ticks = current_ticks + 1
                    signature_changed = False
                else:
                    new_ticks = 0
                    signature_changed = True
                await db.execute(
                    "UPDATE orchestration_runs SET stall_signature = ?, stall_ticks = ?, updated_at = ? WHERE id = ?",
                    (stall_signature, new_ticks, _now_iso(), rid),
                )
                await db.commit()
        return {"stall_ticks": new_ticks, "signature_changed": signature_changed}

    async def update_completion_report(
        self,
        run_id: str,
        *,
        report: Dict[str, Any],
    ) -> None:
        """Store the latest GraphCompletenessEvaluator report on the run."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    "UPDATE orchestration_runs SET completion_report_json = ?, updated_at = ? WHERE id = ?",
                    (_json_dumps(report), _now_iso(), rid),
                )
                await db.commit()

    async def upsert_join_state(
        self,
        run_id: str,
        join_id: str,
        *,
        status: str,
        expected_branch_count: int = 0,
        resolved_branch_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Upsert join gate state for a run."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        jid = str(join_id or "").strip()
        now = _now_iso()
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT id FROM orchestration_join_state WHERE run_id = ? AND join_id = ? LIMIT 1",
                    (rid, jid),
                ) as cursor:
                    existing = await cursor.fetchone()
                if existing:
                    await db.execute(
                        """UPDATE orchestration_join_state
                           SET status = ?, expected_branch_count = ?, resolved_branch_count = ?,
                               metadata_json = ?, updated_at = ?
                           WHERE run_id = ? AND join_id = ?""",
                        (status, expected_branch_count, resolved_branch_count,
                         _json_dumps(metadata or {}), now, rid, jid),
                    )
                else:
                    await db.execute(
                        """INSERT INTO orchestration_join_state
                           (id, run_id, join_id, status, expected_branch_count, resolved_branch_count,
                            metadata_json, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (str(uuid.uuid4()), rid, jid, status, expected_branch_count,
                         resolved_branch_count, _json_dumps(metadata or {}), now, now),
                    )
                await db.commit()
        result = await self.get_join_state(rid, jid)
        return result or {}

    async def get_join_state(self, run_id: str, join_id: str) -> Optional[Dict[str, Any]]:
        """Get join gate state."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        jid = str(join_id or "").strip()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM orchestration_join_state WHERE run_id = ? AND join_id = ? LIMIT 1",
                (rid, jid),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_join_state(row)

    async def list_join_states(self, run_id: str) -> List[Dict[str, Any]]:
        """List all join states for a run."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM orchestration_join_state WHERE run_id = ? ORDER BY join_id ASC",
                (rid,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._row_to_join_state(r) for r in rows]

    def _row_to_join_state(self, row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "id": str(row["id"]),
            "run_id": str(row["run_id"] or ""),
            "join_id": str(row["join_id"] or ""),
            "status": str(row["status"] or "waiting"),
            "expected_branch_count": int(row["expected_branch_count"] or 0),
            "resolved_branch_count": int(row["resolved_branch_count"] or 0),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    async def store_run_contract(self, run_id: str, contract: Dict[str, Any]) -> None:
        """Store the RunContract on the run."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return
        async with self._lock:
            async with open_sqlite(self._db_path) as db:
                await db.execute(
                    "UPDATE orchestration_runs SET run_contract_json = ?, updated_at = ? WHERE id = ?",
                    (_json_dumps(contract), _now_iso(), rid),
                )
                await db.commit()

    async def get_run_contract(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get the RunContract for a run."""
        await self._ensure_db()
        rid = str(run_id or "").strip()
        if not rid:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT run_contract_json FROM orchestration_runs WHERE id = ? LIMIT 1", (rid,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return _json_loads(row["run_contract_json"], None)

    def _row_to_payload(self, row: Optional[aiosqlite.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "assignment_id": str(row["assignment_id"] or ""),
            "orchestration_id": str(row["orchestration_id"] or "") or None,
            "conversation_id": str(row["conversation_id"] or ""),
            "project_id": str(row["project_id"] or "") or None,
            "pm_bot_id": str(row["pm_bot_id"] or ""),
            "instruction": str(row["instruction"] or ""),
            "state": str(row["state"] or "queued"),
            "graph_snapshot": _json_loads(row["graph_snapshot"], {"nodes": [], "edges": []}),
            "node_overrides": _json_loads(row["node_overrides"], {}),
            "lineage_parent_run_id": str(row["lineage_parent_run_id"] or "") or None,
            "spliced_from_node_id": str(row["spliced_from_node_id"] or "") or None,
            "archived": bool(row["archived"]),
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }


def status_from_tasks(tasks: List[Task]) -> Tuple[str, Dict[str, Any]]:
    statuses = [_node_status_from_task(task) for task in tasks]
    aggregate = _aggregate_node_status(statuses)
    return aggregate, {"task_count": len(tasks), "statuses": statuses}
