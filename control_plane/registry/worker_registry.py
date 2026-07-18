import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Literal, Optional

import aiosqlite

from control_plane.sqlite_helpers import open_sqlite
from shared.exceptions import WorkerNotFoundError
from shared.models import Worker, WorkerMetrics

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = str(Path(__file__).parent.parent.parent / "data" / "nexusai.db")
_CREATE_WORKERS = """
CREATE TABLE IF NOT EXISTS cp_workers (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    last_heartbeat_at TEXT
)
"""


class WorkerRegistry:
    """Persistent worker inventory with registry-owned heartbeat freshness."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._workers: Dict[str, Worker] = {}
        self._last_heartbeat: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._db_ready = False
        if db_path is not None:
            self._db_path = db_path
        else:
            db_url = os.environ.get("DATABASE_URL", "")
            self._db_path = db_url[len("sqlite:///"):] if db_url.startswith("sqlite:///") else _DEFAULT_DB_PATH

    async def _ensure_db(self) -> None:
        if self._db_ready:
            return
        async with self._init_lock:
            if self._db_ready:
                return
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            async with open_sqlite(self._db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute(_CREATE_WORKERS)
                await db.commit()
                async with db.execute("SELECT id, data, last_heartbeat_at FROM cp_workers") as cursor:
                    rows = await cursor.fetchall()
            for row in rows:
                try:
                    worker = Worker.model_validate(json.loads(row["data"]))
                    last_heartbeat_at = self._parse_datetime(row["last_heartbeat_at"]) or worker.last_heartbeat_at
                    if last_heartbeat_at is not None:
                        self._last_heartbeat[worker.id] = last_heartbeat_at
                    self._workers[worker.id] = worker.model_copy(
                        update={"last_heartbeat_at": last_heartbeat_at}
                    )
                except Exception as exc:
                    logger.warning("Failed to restore persisted worker %s: %s", row["id"], exc)
            self._db_ready = True

    async def _persist_worker(self, worker: Worker) -> None:
        snapshot = self._with_last_heartbeat(worker)
        async with open_sqlite(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cp_workers (id, data, last_heartbeat_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    data = excluded.data,
                    last_heartbeat_at = excluded.last_heartbeat_at
                """,
                (
                    snapshot.id,
                    json.dumps(snapshot.model_dump(mode="json")),
                    snapshot.last_heartbeat_at.isoformat() if snapshot.last_heartbeat_at else None,
                ),
            )
            await db.commit()

    async def _delete_worker(self, worker_id: str) -> None:
        async with open_sqlite(self._db_path) as db:
            await db.execute("DELETE FROM cp_workers WHERE id = ?", (worker_id,))
            await db.commit()

    async def register(self, worker: Worker) -> None:
        await self._ensure_db()
        async with self._lock:
            last_heartbeat_at = datetime.now(timezone.utc)
            self._workers[worker.id] = worker.model_copy(
                update={"last_heartbeat_at": last_heartbeat_at}
            )
            self._last_heartbeat[worker.id] = last_heartbeat_at
            snapshot = self._workers[worker.id]
        await self._persist_worker(snapshot)
        logger.info("Registered worker %s", worker.id)

    async def provision(self, worker: Worker) -> None:
        """Create an offline inventory record before a worker runtime first connects."""
        await self._ensure_db()
        async with self._lock:
            if worker.id in self._workers:
                raise ValueError(f"Worker '{worker.id}' already exists")
            self._workers[worker.id] = worker.model_copy(
                update={"status": "offline", "last_heartbeat_at": None}
            )
            snapshot = self._workers[worker.id]
        await self._persist_worker(snapshot)
        logger.info("Provisioned worker %s", worker.id)

    async def get(self, worker_id: str) -> Worker:
        await self._ensure_db()
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            return self._with_last_heartbeat(self._workers[worker_id])

    async def list(self) -> List[Worker]:
        await self._ensure_db()
        async with self._lock:
            return [self._with_last_heartbeat(worker) for worker in self._workers.values()]

    async def update_status(
        self, worker_id: str, status: Literal["online", "offline", "degraded"]
    ) -> None:
        await self._ensure_db()
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            self._workers[worker_id] = self._workers[worker_id].model_copy(
                update={"status": status}
            )
            snapshot = self._workers[worker_id]
        await self._persist_worker(snapshot)

    async def update_heartbeat(self, worker_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            last_heartbeat_at = datetime.now(timezone.utc)
            self._last_heartbeat[worker_id] = last_heartbeat_at
            self._workers[worker_id] = self._workers[worker_id].model_copy(
                update={"status": "online", "last_heartbeat_at": last_heartbeat_at}
            )
            snapshot = self._workers[worker_id]
        await self._persist_worker(snapshot)

    async def update_metrics(self, worker_id: str, metrics: WorkerMetrics) -> None:
        await self._ensure_db()
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            self._workers[worker_id] = self._workers[worker_id].model_copy(
                update={"metrics": metrics}
            )
            snapshot = self._workers[worker_id]
        await self._persist_worker(snapshot)

    async def remove(self, worker_id: str) -> None:
        await self._ensure_db()
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            del self._workers[worker_id]
            self._last_heartbeat.pop(worker_id, None)
        await self._delete_worker(worker_id)

    async def update(self, worker_id: str, worker: Worker) -> None:
        await self._ensure_db()
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            self._workers[worker_id] = self._with_last_heartbeat(worker)
            snapshot = self._workers[worker_id]
        await self._persist_worker(snapshot)

    async def get_worker_ids(self) -> List[str]:
        await self._ensure_db()
        async with self._lock:
            return list(self._workers.keys())

    async def get_last_heartbeat(self, worker_id: str) -> Optional[datetime]:
        await self._ensure_db()
        async with self._lock:
            return self._last_heartbeat.get(worker_id)

    async def seed_from_configs(self, configs: list, *, force: bool = False) -> None:
        """Persist config-defined workers without replacing live registrations by default."""
        await self._ensure_db()
        for cfg in configs:
            try:
                worker = Worker.model_validate(cfg)
                async with self._lock:
                    exists = worker.id in self._workers
                if exists and not force:
                    continue
                await self.register(worker)
                logger.info("Seeded worker from config: %s", worker.id)
            except Exception as exc:
                logger.warning("Failed to seed worker config: %s", exc)

    def _with_last_heartbeat(self, worker: Worker) -> Worker:
        return worker.model_copy(
            update={"last_heartbeat_at": self._last_heartbeat.get(worker.id)}
        )

    @staticmethod
    def _parse_datetime(value: object) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
