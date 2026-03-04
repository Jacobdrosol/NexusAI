import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Literal, Optional

from shared.exceptions import WorkerNotFoundError
from shared.models import Worker, WorkerMetrics

logger = logging.getLogger(__name__)


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: Dict[str, Worker] = {}
        self._last_heartbeat: Dict[str, datetime] = {}
        self._lock = asyncio.Lock()

    async def register(self, worker: Worker) -> None:
        async with self._lock:
            self._workers[worker.id] = worker
            self._last_heartbeat[worker.id] = datetime.now(timezone.utc)
            logger.info("Registered worker %s", worker.id)

    async def get(self, worker_id: str) -> Worker:
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            return self._workers[worker_id]

    async def list(self) -> List[Worker]:
        async with self._lock:
            return list(self._workers.values())

    async def update_status(
        self, worker_id: str, status: Literal["online", "offline", "degraded"]
    ) -> None:
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            self._workers[worker_id] = self._workers[worker_id].model_copy(
                update={"status": status}
            )

    async def update_heartbeat(self, worker_id: str) -> None:
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            self._last_heartbeat[worker_id] = datetime.now(timezone.utc)
            self._workers[worker_id] = self._workers[worker_id].model_copy(
                update={"status": "online"}
            )

    async def update_metrics(self, worker_id: str, metrics: WorkerMetrics) -> None:
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            self._workers[worker_id] = self._workers[worker_id].model_copy(
                update={"metrics": metrics}
            )

    async def remove(self, worker_id: str) -> None:
        async with self._lock:
            if worker_id not in self._workers:
                raise WorkerNotFoundError(f"Worker not found: {worker_id}")
            del self._workers[worker_id]
            self._last_heartbeat.pop(worker_id, None)

    async def get_worker_ids(self) -> List[str]:
        async with self._lock:
            return list(self._workers.keys())

    async def get_last_heartbeat(self, worker_id: str) -> Optional[datetime]:
        async with self._lock:
            return self._last_heartbeat.get(worker_id)

    def load_from_configs(self, configs: list) -> None:
        for cfg in configs:
            try:
                worker = Worker.model_validate(cfg)
                self._workers[worker.id] = worker
                self._last_heartbeat[worker.id] = datetime.now(timezone.utc)
                logger.info("Loaded worker from config: %s", worker.id)
            except Exception as e:
                logger.warning("Failed to load worker config: %s", e)
