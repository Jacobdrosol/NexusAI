"""Thin synchronous HTTP client for the NexusAI Control Plane API."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_CP_BASE = os.environ.get("CONTROL_PLANE_URL", "http://control_plane:8000")
_TIMEOUT = float(os.environ.get("CP_TIMEOUT", "5"))


class CPClient:
    """Synchronous HTTP client for the control plane REST API."""

    def __init__(self, base_url: str = _CP_BASE, timeout: float = _TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str) -> Optional[Any]:
        try:
            resp = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("CP GET %s failed: %s", path, exc)
            return None

    def _post(self, path: str, json: Any) -> Optional[Any]:
        try:
            resp = requests.post(f"{self.base_url}{path}", json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("CP POST %s failed: %s", path, exc)
            return None

    def _delete(self, path: str) -> bool:
        try:
            resp = requests.delete(f"{self.base_url}{path}", timeout=self.timeout)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("CP DELETE %s failed: %s", path, exc)
            return False

    def health(self) -> bool:
        result = self._get("/health")
        return isinstance(result, dict) and result.get("status") == "ok"

    # Workers
    def list_workers(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/workers")

    def get_worker(self, worker_id: str) -> Optional[Dict]:
        return self._get(f"/v1/workers/{worker_id}")

    def register_worker(self, worker: Dict) -> Optional[Dict]:
        return self._post("/v1/workers", worker)

    def delete_worker(self, worker_id: str) -> bool:
        return self._delete(f"/v1/workers/{worker_id}")

    # Bots
    def list_bots(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/bots")

    def get_bot(self, bot_id: str) -> Optional[Dict]:
        return self._get(f"/v1/bots/{bot_id}")

    def create_bot(self, bot: Dict) -> Optional[Dict]:
        return self._post("/v1/bots", bot)

    def delete_bot(self, bot_id: str) -> bool:
        return self._delete(f"/v1/bots/{bot_id}")

    # Tasks
    def list_tasks(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/tasks")

    def get_task(self, task_id: str) -> Optional[Dict]:
        return self._get(f"/v1/tasks/{task_id}")

    def create_task(self, bot_id: str, payload: Any) -> Optional[Dict]:
        return self._post("/v1/tasks", {"bot_id": bot_id, "payload": payload})


_client: Optional[CPClient] = None


def get_cp_client() -> CPClient:
    global _client
    if _client is None:
        _client = CPClient()
    return _client
