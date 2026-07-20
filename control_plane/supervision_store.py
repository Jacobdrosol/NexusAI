"""Persistent, approval-gated supervision evidence for autonomous worker portfolios."""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from shared.bot_policy import supervision_manager_config

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent / "data" / "nexusai.db")
_ALLOWED_ACTION_TYPES = frozenset({"pause_schedule", "hold_bot", "configuration_review"})
_ACTION_TARGET_TYPES = {
    "pause_schedule": "schedule",
    "hold_bot": "bot",
    "configuration_review": "bot",
}
_OVERALL_STATUSES = frozenset({"healthy", "attention", "blocked"})
_ACTION_STATUSES = frozenset({"pending", "applied", "approved", "rejected"})

_CREATE_HOLDS = """
CREATE TABLE IF NOT EXISTS cp_supervision_holds (
    bot_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    report_id TEXT,
    action_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_CREATE_REPORTS = """
CREATE TABLE IF NOT EXISTS cp_supervision_reports (
    id TEXT PRIMARY KEY,
    manager_bot_id TEXT NOT NULL,
    manager_task_id TEXT NOT NULL UNIQUE,
    project_id TEXT,
    overall_status TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""
_CREATE_ACTIONS = """
CREATE TABLE IF NOT EXISTS cp_supervision_actions (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL,
    manager_bot_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    rationale TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    decision_note TEXT,
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_CREATE_ACTION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_cp_supervision_actions_status_created
ON cp_supervision_actions (status, created_at DESC)
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    database_url = str(os.environ.get("DATABASE_URL") or "")
    if database_url.startswith("sqlite:///"):
        return database_url[len("sqlite:///") :]
    return _DEFAULT_DB_PATH


def _short_text(value: Any, *, limit: int) -> str:
    return str(value or "").strip().replace("\x00", "")[:limit]


def _short_list(value: Any, *, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        normalized = _short_text(item, limit=item_limit)
        if normalized:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _parse_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _normalized_portfolio(value: Any) -> list[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    entries: list[Dict[str, str]] = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        target_id = _short_text(item.get("target_id") or item.get("id"), limit=160)
        if not target_id:
            continue
        entries.append(
            {
                "target_type": _short_text(item.get("target_type") or "bot", limit=32),
                "target_id": target_id,
                "status": _short_text(item.get("status") or "attention", limit=32).lower(),
                "summary": _short_text(item.get("summary"), limit=800),
            }
        )
    return entries


def _normalized_portfolio_metrics(value: Any) -> Dict[str, Any]:
    """Keep a small scalar metric snapshot when a manager uses a map format.

    Portfolio entries remain the canonical per-bot report format.  This tolerant
    projection preserves aggregate counters from a manager without allowing it
    to persist arbitrary nested data or unbounded text.
    """
    if not isinstance(value, dict):
        return {}
    metrics: Dict[str, Any] = {}
    for key, raw_value in list(value.items())[:32]:
        metric_name = _short_text(key, limit=80)
        if not metric_name:
            continue
        if isinstance(raw_value, bool):
            metrics[metric_name] = raw_value
        elif isinstance(raw_value, int):
            metrics[metric_name] = raw_value
        elif isinstance(raw_value, float):
            metrics[metric_name] = raw_value
        elif isinstance(raw_value, str):
            metrics[metric_name] = _short_text(raw_value, limit=400)
    return metrics


class SupervisionStore:
    """Store manager reports, approval-gated actions, and enforced bot holds."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or _db_path()
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def _ensure_db(self) -> None:
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                await db.execute(_CREATE_HOLDS)
                await db.execute(_CREATE_REPORTS)
                await db.execute(_CREATE_ACTIONS)
                await db.execute(_CREATE_ACTION_INDEX)
                await db.commit()
            self._ready = True

    @staticmethod
    def _report_from_row(row: aiosqlite.Row) -> Dict[str, Any]:
        try:
            report = json.loads(row["report_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            report = {}
        return {
            "id": str(row["id"]),
            "manager_bot_id": str(row["manager_bot_id"]),
            "manager_task_id": str(row["manager_task_id"]),
            "project_id": str(row["project_id"] or "") or None,
            "overall_status": str(row["overall_status"]),
            "report": report if isinstance(report, dict) else {},
            "created_at": str(row["created_at"]),
        }

    @staticmethod
    def _action_from_row(row: aiosqlite.Row) -> Dict[str, Any]:
        try:
            evidence = json.loads(row["evidence_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = []
        return {
            "id": str(row["id"]),
            "report_id": str(row["report_id"]),
            "manager_bot_id": str(row["manager_bot_id"]),
            "action_type": str(row["action_type"]),
            "target_type": str(row["target_type"]),
            "target_id": str(row["target_id"]),
            "rationale": str(row["rationale"]),
            "evidence": evidence if isinstance(evidence, list) else [],
            "status": str(row["status"]),
            "decision_note": str(row["decision_note"] or "") or None,
            "decided_by": str(row["decided_by"] or "") or None,
            "decided_at": str(row["decided_at"] or "") or None,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _hold_from_row(row: aiosqlite.Row) -> Dict[str, Any]:
        return {
            "bot_id": str(row["bot_id"]),
            "reason": str(row["reason"]),
            "report_id": str(row["report_id"] or "") or None,
            "action_id": str(row["action_id"] or "") or None,
            "created_by": str(row["created_by"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    async def get_hold(self, bot_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        normalized_id = _short_text(bot_id, limit=160)
        if not normalized_id:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM cp_supervision_holds WHERE bot_id = ? LIMIT 1", (normalized_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return self._hold_from_row(row) if row is not None else None

    async def list_holds(self, *, limit: int = 200) -> list[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 500))
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM cp_supervision_holds ORDER BY updated_at DESC LIMIT ?", (safe_limit,)
            ) as cursor:
                rows = await cursor.fetchall()
        return [self._hold_from_row(row) for row in rows]

    async def hold_bot(
        self,
        bot_id: str,
        *,
        reason: str,
        created_by: str,
        report_id: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        await self._ensure_db()
        normalized_id = _short_text(bot_id, limit=160)
        normalized_reason = _short_text(reason, limit=2_000)
        if not normalized_id or not normalized_reason:
            raise ValueError("bot_id and a non-empty hold reason are required")
        now = _utc_now()
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_supervision_holds
                    (bot_id, reason, report_id, action_id, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    reason = excluded.reason,
                    report_id = excluded.report_id,
                    action_id = excluded.action_id,
                    created_by = excluded.created_by,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_id,
                    normalized_reason,
                    _short_text(report_id, limit=80) or None,
                    _short_text(action_id, limit=80) or None,
                    _short_text(created_by, limit=120) or "operator",
                    now,
                    now,
                ),
            )
            await db.commit()
        hold = await self.get_hold(normalized_id)
        if hold is None:
            raise RuntimeError("supervision hold was not persisted")
        return hold

    async def release_hold(self, bot_id: str) -> bool:
        await self._ensure_db()
        normalized_id = _short_text(bot_id, limit=160)
        if not normalized_id:
            return False
        async with open_sqlite(self._db_path) as db:
            cursor = await db.execute(
                "DELETE FROM cp_supervision_holds WHERE bot_id = ?", (normalized_id,)
            )
            await db.commit()
        return bool(cursor.rowcount)

    async def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        normalized_id = _short_text(report_id, limit=80)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM cp_supervision_reports WHERE id = ? LIMIT 1", (normalized_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return self._report_from_row(row) if row is not None else None

    async def list_reports(
        self, *, manager_bot_id: Optional[str] = None, limit: int = 50
    ) -> list[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 200))
        manager_id = _short_text(manager_bot_id, limit=160)
        query = "SELECT * FROM cp_supervision_reports"
        values: list[Any] = []
        if manager_id:
            query += " WHERE manager_bot_id = ?"
            values.append(manager_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        values.append(safe_limit)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, values) as cursor:
                rows = await cursor.fetchall()
        return [self._report_from_row(row) for row in rows]

    async def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        normalized_id = _short_text(action_id, limit=80)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM cp_supervision_actions WHERE id = ? LIMIT 1", (normalized_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return self._action_from_row(row) if row is not None else None

    async def list_actions(
        self, *, status: Optional[str] = None, limit: int = 100
    ) -> list[Dict[str, Any]]:
        await self._ensure_db()
        safe_limit = max(1, min(int(limit), 500))
        normalized_status = _short_text(status, limit=32).lower()
        query = "SELECT * FROM cp_supervision_actions"
        values: list[Any] = []
        if normalized_status:
            query += " WHERE status = ?"
            values.append(normalized_status)
        query += " ORDER BY created_at DESC LIMIT ?"
        values.append(safe_limit)
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, values) as cursor:
                rows = await cursor.fetchall()
        return [self._action_from_row(row) for row in rows]

    async def decide_action(
        self,
        action_id: str,
        *,
        status: str,
        decided_by: str,
        decision_note: str = "",
    ) -> Optional[Dict[str, Any]]:
        await self._ensure_db()
        normalized_status = _short_text(status, limit=32).lower()
        if normalized_status not in _ACTION_STATUSES - {"pending"}:
            raise ValueError("invalid supervision action decision status")
        now = _utc_now()
        async with open_sqlite(self._db_path) as db:
            cursor = await db.execute(
                """
                UPDATE cp_supervision_actions
                SET status = ?, decision_note = ?, decided_by = ?, decided_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (
                    normalized_status,
                    _short_text(decision_note, limit=2_000) or None,
                    _short_text(decided_by, limit=120) or "operator",
                    now,
                    now,
                    _short_text(action_id, limit=80),
                ),
            )
            await db.commit()
        if not cursor.rowcount:
            return None
        return await self.get_action(action_id)

    async def record_manager_result(
        self,
        *,
        bot: Any,
        task_id: str,
        result: Any,
    ) -> Optional[Dict[str, Any]]:
        """Persist bounded report data from a configured manager bot.

        Proposals never execute here.  The API must receive an explicit operator
        approval before a schedule is paused or a bot is placed on hold.
        """
        config = supervision_manager_config(bot)
        if config is None:
            return None
        await self._ensure_db()
        manager_bot_id = _short_text(getattr(bot, "id", ""), limit=160)
        manager_task_id = _short_text(task_id, limit=120)
        if not manager_bot_id or not manager_task_id:
            return None

        parsed = _parse_result(result)
        overall_status = _short_text(parsed.get("overall_status"), limit=32).lower()
        if overall_status not in _OVERALL_STATUSES:
            overall_status = "attention"
        report_payload = {
            "executive_summary": _short_text(parsed.get("executive_summary"), limit=5_000)
            or "Manager report did not include an executive summary.",
            "overall_status": overall_status,
            "accomplishments": _short_list(parsed.get("accomplishments"), limit=16, item_limit=1_000),
            "risks": _short_list(parsed.get("risks"), limit=16, item_limit=1_000),
            "decisions_needed": _short_list(parsed.get("decisions_needed"), limit=16, item_limit=1_000),
            "portfolio": _normalized_portfolio(parsed.get("portfolio")),
            "portfolio_metrics": _normalized_portfolio_metrics(parsed.get("portfolio")),
        }
        report_id = str(uuid.uuid4())
        created_at = _utc_now()
        async with open_sqlite(self._db_path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO cp_supervision_reports
                        (id, manager_bot_id, manager_task_id, project_id, overall_status, report_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_id,
                        manager_bot_id,
                        manager_task_id,
                        config.get("project_id"),
                        overall_status,
                        json.dumps(report_payload, ensure_ascii=False, sort_keys=True),
                        created_at,
                    ),
                )
                for proposal in self._normalized_action_proposals(
                    parsed.get("action_proposals"), config=config
                ):
                    await db.execute(
                        """
                        INSERT INTO cp_supervision_actions
                            (id, report_id, manager_bot_id, action_type, target_type, target_id,
                             rationale, evidence_json, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            report_id,
                            manager_bot_id,
                            proposal["action_type"],
                            proposal["target_type"],
                            proposal["target_id"],
                            proposal["rationale"],
                            json.dumps(proposal["evidence"], ensure_ascii=False),
                            created_at,
                            created_at,
                        ),
                    )
                await db.commit()
            except aiosqlite.IntegrityError:
                # Replaying a completion event must not duplicate executive reports.
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM cp_supervision_reports WHERE manager_task_id = ? LIMIT 1",
                    (manager_task_id,),
                ) as cursor:
                    row = await cursor.fetchone()
                if row is not None:
                    return self._report_from_row(row)
                raise
        report = await self.get_report(report_id)
        if report is None:
            raise RuntimeError("supervision report was not persisted")
        return report

    @staticmethod
    def _normalized_action_proposals(
        value: Any,
        *,
        config: Dict[str, Any],
    ) -> Iterable[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        allowed_actions = set(config.get("allowed_actions") or [])
        allowed_bots = set(config.get("bot_ids") or [])
        allowed_schedules = set(config.get("schedule_ids") or [])
        proposals: list[Dict[str, Any]] = []
        for raw in value[:20]:
            if not isinstance(raw, dict):
                continue
            # Some model providers naturally use the shorter proposal/target
            # names.  Accept those aliases only after the exact same declared
            # portfolio and action-type allowlists below have been enforced.
            action_type = _short_text(
                raw.get("action_type") or raw.get("proposal"), limit=64
            ).lower()
            target_id = _short_text(raw.get("target_id") or raw.get("target"), limit=160)
            expected_target_type = _ACTION_TARGET_TYPES.get(action_type)
            if (
                action_type not in _ALLOWED_ACTION_TYPES
                or action_type not in allowed_actions
                or not target_id
                or expected_target_type is None
            ):
                continue
            if expected_target_type == "bot" and target_id not in allowed_bots:
                continue
            if expected_target_type == "schedule" and target_id not in allowed_schedules:
                continue
            rationale = _short_text(raw.get("rationale"), limit=2_000)
            if not rationale:
                continue
            proposals.append(
                {
                    "action_type": action_type,
                    "target_type": expected_target_type,
                    "target_id": target_id,
                    "rationale": rationale,
                    "evidence": _short_list(raw.get("evidence"), limit=8, item_limit=400),
                }
            )
        return proposals
