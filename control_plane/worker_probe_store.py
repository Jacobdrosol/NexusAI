"""Persistent, nonsecret evidence from read-only worker runtime probes."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent / "data" / "nexusai.db")
_CREATE_PROBES = """
CREATE TABLE IF NOT EXISTS cp_worker_probes (
    worker_id TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


class WorkerProbeStore:
    """Store the latest bounded probe result for each registered worker."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._init_lock = asyncio.Lock()
        self._db_ready = False
        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            self._db_path = (
                db_url[len("sqlite:///") :]
                if db_url.startswith("sqlite:///")
                else _DEFAULT_DB_PATH
            )

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                await db.execute(_CREATE_PROBES)
                await db.commit()
            self._db_ready = True

    async def record(self, result: dict[str, Any]) -> dict[str, Any]:
        """Persist a worker probe result without accepting arbitrary identifiers."""
        await self._ensure_db()
        worker_id = str(result.get("worker_id") or "").strip()
        if not worker_id:
            raise ValueError("worker probe result is missing worker_id")

        snapshot = dict(result)
        snapshot["worker_id"] = worker_id
        checked_at = str(snapshot.get("checked_at") or "").strip()
        if not checked_at:
            checked_at = datetime.now(timezone.utc).isoformat()
            snapshot["checked_at"] = checked_at

        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_worker_probes (worker_id, checked_at, data)
                VALUES (?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    checked_at = excluded.checked_at,
                    data = excluded.data
                """,
                (worker_id, checked_at, json.dumps(snapshot, ensure_ascii=False)),
            )
            await db.commit()
        return snapshot

    async def get(self, worker_id: str) -> Optional[dict[str, Any]]:
        """Return the latest persisted result, or None when a worker has not been probed."""
        await self._ensure_db()
        normalized_id = str(worker_id or "").strip()
        if not normalized_id:
            return None
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT data FROM cp_worker_probes WHERE worker_id = ? LIMIT 1",
                (normalized_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row["data"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Failed to restore worker probe for %s: %s", normalized_id, exc)
            return None
        if not isinstance(result, dict):
            return None
        result["worker_id"] = normalized_id
        return result

    async def list_for_workers(self, worker_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return the latest stored probe for each requested registered worker.

        The control plane supplies the authoritative worker IDs, so orphaned rows from
        removed workers are never exposed through the fleet view.
        """
        await self._ensure_db()
        normalized_ids: list[str] = []
        seen: set[str] = set()
        for worker_id in worker_ids:
            normalized_id = str(worker_id or "").strip()
            if normalized_id and normalized_id not in seen:
                seen.add(normalized_id)
                normalized_ids.append(normalized_id)
        if not normalized_ids:
            return {}

        placeholders = ", ".join("?" for _ in normalized_ids)
        records: dict[str, dict[str, Any]] = {}
        async with open_sqlite(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT worker_id, data FROM cp_worker_probes WHERE worker_id IN ({placeholders})",
                normalized_ids,
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            worker_id = str(row["worker_id"] or "").strip()
            if worker_id not in seen:
                continue
            try:
                result = json.loads(row["data"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Failed to restore worker probe for %s: %s", worker_id, exc)
                continue
            if not isinstance(result, dict):
                continue
            result["worker_id"] = worker_id
            records[worker_id] = result
        return records
