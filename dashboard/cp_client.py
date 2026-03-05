"""Thin synchronous HTTP client for the NexusAI Control Plane API."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_CP_BASE = os.environ.get("CONTROL_PLANE_URL", "http://control_plane:8000")
_TIMEOUT = float(os.environ.get("CP_TIMEOUT", "2"))


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

    def _put(self, path: str, json: Any) -> Optional[Any]:
        try:
            resp = requests.put(f"{self.base_url}{path}", json=json, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("CP PUT %s failed: %s", path, exc)
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

    def update_worker(self, worker_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._put(f"/v1/workers/{worker_id}", body)

    def heartbeat_worker(self, worker_id: str, metrics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {}
        if metrics:
            payload["metrics"] = metrics
        return self._post(f"/v1/workers/{worker_id}/heartbeat", payload)

    def delete_worker(self, worker_id: str) -> bool:
        return self._delete(f"/v1/workers/{worker_id}")

    # Bots
    def list_bots(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/bots")

    def get_bot(self, bot_id: str) -> Optional[Dict]:
        return self._get(f"/v1/bots/{bot_id}")

    def create_bot(self, bot: Dict) -> Optional[Dict]:
        return self._post("/v1/bots", bot)

    def update_bot(self, bot_id: str, bot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._put(f"/v1/bots/{bot_id}", bot)

    def delete_bot(self, bot_id: str) -> bool:
        return self._delete(f"/v1/bots/{bot_id}")

    # Tasks
    def list_tasks(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/tasks")

    def get_task(self, task_id: str) -> Optional[Dict]:
        return self._get(f"/v1/tasks/{task_id}")

    def create_task(self, bot_id: str, payload: Any) -> Optional[Dict]:
        return self._post("/v1/tasks", {"bot_id": bot_id, "payload": payload})

    def create_task_full(
        self,
        bot_id: str,
        payload: Any,
        metadata: Optional[Dict[str, Any]] = None,
        depends_on: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"bot_id": bot_id, "payload": payload}
        if metadata is not None:
            body["metadata"] = metadata
        if depends_on is not None:
            body["depends_on"] = depends_on
        return self._post("/v1/tasks", body)

    # Projects
    def list_projects(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/projects")

    def create_project(self, project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/projects", project)

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}")

    def delete_project(self, project_id: str) -> bool:
        return self._delete(f"/v1/projects/{project_id}")

    def add_project_bridge(self, project_id: str, target_project_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/projects/{project_id}/bridges/{target_project_id}", {})

    def remove_project_bridge(self, project_id: str, target_project_id: str) -> bool:
        return self._delete(f"/v1/projects/{project_id}/bridges/{target_project_id}")

    # Models
    def list_models(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/models")

    def create_model(self, model: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/models", model)

    def delete_model(self, model_id: str) -> bool:
        return self._delete(f"/v1/models/{model_id}")

    # Keys
    def list_keys(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/keys")

    def upsert_key(self, name: str, provider: str, value: str) -> Optional[Dict[str, Any]]:
        return self._post("/v1/keys", {"name": name, "provider": provider, "value": value})

    def delete_key(self, name: str) -> bool:
        return self._delete(f"/v1/keys/{name}")

    # Chat
    def list_conversations(self, project_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        path = "/v1/chat/conversations"
        if project_id:
            path = f"{path}?project_id={project_id}"
        return self._get(path)

    def create_conversation(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/chat/conversations", body)

    def list_messages(self, conversation_id: str) -> Optional[List[Dict[str, Any]]]:
        return self._get(f"/v1/chat/conversations/{conversation_id}/messages")

    def post_message(self, conversation_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/chat/conversations/{conversation_id}/messages", body)

    # Vault
    def list_vault_items(
        self,
        namespace: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
    ) -> Optional[List[Dict[str, Any]]]:
        parts = [f"limit={limit}"]
        if namespace:
            parts.append(f"namespace={namespace}")
        if project_id:
            parts.append(f"project_id={project_id}")
        qs = "&".join(parts)
        return self._get(f"/v1/vault/items?{qs}")

    def ingest_vault_item(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/vault/items", body)

    def search_vault(self, body: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        return self._post("/v1/vault/search", body)

    def get_vault_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/vault/items/{item_id}")

    def list_vault_chunks(self, item_id: str) -> Optional[List[Dict[str, Any]]]:
        return self._get(f"/v1/vault/items/{item_id}/chunks")

    def delete_vault_item(self, item_id: str) -> bool:
        return self._delete(f"/v1/vault/items/{item_id}")

    def list_vault_namespaces(self) -> Optional[List[str]]:
        return self._get("/v1/vault/namespaces")


_client: Optional[CPClient] = None


def get_cp_client() -> CPClient:
    global _client
    if _client is None:
        _client = CPClient()
    return _client
