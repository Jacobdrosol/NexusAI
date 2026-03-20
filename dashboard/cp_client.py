"""Thin synchronous HTTP client for the NexusAI Control Plane API."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_CP_BASE = os.environ.get("CONTROL_PLANE_URL", "http://control_plane:8000")
_TIMEOUT = float(os.environ.get("CP_TIMEOUT", "2"))
_CHAT_TIMEOUT = float(os.environ.get("CP_CHAT_TIMEOUT", "900"))
_INGEST_TIMEOUT = float(os.environ.get("CP_INGEST_TIMEOUT", "1800"))
_CP_API_TOKEN = os.environ.get("CONTROL_PLANE_API_TOKEN", "").strip()


class CPClient:
    """Synchronous HTTP client for the control plane REST API."""

    def __init__(self, base_url: str = _CP_BASE, timeout: float = _TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_token = _CP_API_TOKEN
        self._last_error: Dict[str, Any] = {}

    def _headers(self) -> Dict[str, str]:
        if not self.api_token:
            return {}
        return {"X-Nexus-API-Key": self.api_token}

    def _record_error(self, *, method: str, path: str, status_code: Optional[int], detail: str) -> None:
        self._last_error = {
            "method": method,
            "path": path,
            "status_code": status_code,
            "detail": detail,
        }

    def _clear_error(self) -> None:
        self._last_error = {}

    def last_error(self) -> Dict[str, Any]:
        return dict(self._last_error)

    def unavailable_reason(self) -> str:
        err = self.last_error()
        if not err:
            return "Control plane request failed."
        code = err.get("status_code")
        path = err.get("path") or "unknown path"
        if code == 401:
            return (
                f"Control plane auth failed on {path} (401). "
                "Verify CONTROL_PLANE_API_TOKEN matches control plane."
            )
        if code == 403:
            return (
                f"Control plane rejected request on {path} (403). "
                "Verify control-plane auth policy and token permissions."
            )
        if code == 404:
            return (
                f"Control plane route not found on {path} (404). "
                "Verify CONTROL_PLANE_URL points to the correct service."
            )
        if code:
            return f"Control plane request failed on {path} (HTTP {code})."
        return (
            f"Control plane request failed on {path}. "
            "Verify CONTROL_PLANE_URL reachability from dashboard container."
        )

    def probe_paths(self, paths: List[str]) -> List[Dict[str, Any]]:
        """Probe control-plane paths and return per-endpoint status details."""
        results: List[Dict[str, Any]] = []
        for path in paths:
            url = f"{self.base_url}{path}"
            try:
                resp = requests.get(url, timeout=self.timeout, headers=self._headers())
                detail = ""
                try:
                    detail = (resp.text or "")[:160].strip()
                except Exception:
                    detail = ""
                results.append(
                    {
                        "path": path,
                        "ok": 200 <= resp.status_code < 300,
                        "status_code": resp.status_code,
                        "detail": detail,
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "path": path,
                        "ok": False,
                        "status_code": None,
                        "detail": str(exc),
                    }
                )
        return results

    def _request(self, method: str, path: str, *, json: Any = None, timeout: Optional[float] = None) -> Optional[Any]:
        url = f"{self.base_url}{path}"
        req_timeout = self.timeout if timeout is None else timeout
        try:
            if method == "GET":
                resp = requests.get(url, timeout=req_timeout, headers=self._headers())
            elif method == "POST":
                resp = requests.post(url, json=json, timeout=req_timeout, headers=self._headers())
            elif method == "PUT":
                resp = requests.put(url, json=json, timeout=req_timeout, headers=self._headers())
            elif method == "DELETE":
                resp = requests.delete(url, timeout=req_timeout, headers=self._headers())
            else:
                raise ValueError(f"unsupported method {method}")
            resp.raise_for_status()
            self._clear_error()
            if not resp.text:
                return {}
            return resp.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            detail = ""
            try:
                if exc.response is not None:
                    detail = (exc.response.text or "")[:500]
            except Exception:
                detail = str(exc)
            self._record_error(method=method, path=path, status_code=status, detail=detail or str(exc))
            logger.warning("CP %s %s failed: %s", method, path, exc)
            return None
        except Exception as exc:
            self._record_error(method=method, path=path, status_code=None, detail=str(exc))
            logger.warning("CP %s %s failed: %s", method, path, exc)
            return None

    def _get(self, path: str) -> Optional[Any]:
        return self._request("GET", path)

    def _post(self, path: str, json: Any, *, timeout: Optional[float] = None) -> Optional[Any]:
        return self._request("POST", path, json=json, timeout=timeout)

    def _put(self, path: str, json: Any) -> Optional[Any]:
        return self._request("PUT", path, json=json)

    def _delete(self, path: str) -> bool:
        result = self._request("DELETE", path)
        return result is not None

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

    def list_bot_runs(self, bot_id: str, limit: int = 50) -> Optional[List[Dict[str, Any]]]:
        return self._get(f"/v1/bots/{bot_id}/runs?limit={int(limit)}")

    def list_bot_artifacts(
        self,
        bot_id: str,
        limit: int = 100,
        task_id: Optional[str] = None,
        include_content: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        path = f"/v1/bots/{bot_id}/artifacts?limit={int(limit)}&include_content={'true' if include_content else 'false'}"
        if task_id:
            path += f"&task_id={task_id}"
        return self._get(path)

    def get_bot_artifact(self, bot_id: str, artifact_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/bots/{bot_id}/artifacts/{artifact_id}")

    # Tasks
    def list_tasks(
        self,
        orchestration_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        bot_id: Optional[str] = None,
        limit: int = 200,
        include_content: bool = True,
    ) -> Optional[List[Dict[str, Any]]]:
        params: List[str] = [
            f"limit={max(1, int(limit))}",
            f"include_content={'true' if include_content else 'false'}",
        ]
        if orchestration_id:
            params.append(f"orchestration_id={orchestration_id}")
        if statuses:
            encoded = ",".join(str(status).strip() for status in statuses if str(status).strip())
            if encoded:
                params.append(f"status={encoded}")
        if bot_id:
            params.append(f"bot_id={bot_id}")
        return self._get(f"/v1/tasks?{'&'.join(params)}")

    def get_task(self, task_id: str) -> Optional[Dict]:
        return self._get(f"/v1/tasks/{task_id}")

    def retry_task(self, task_id: str, payload: Any = None) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if payload is not None:
            body["payload"] = payload
        return self._post(f"/v1/tasks/{task_id}/retry", body)

    def cancel_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/tasks/{task_id}/cancel", {})

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

    def connect_project_github_pat(
        self,
        project_id: str,
        token: str,
        repo_full_name: Optional[str] = None,
        validate: bool = True,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "token": token,
            "validate": validate,
        }
        if repo_full_name:
            body["repo_full_name"] = repo_full_name
        return self._post(f"/v1/projects/{project_id}/github/pat", body)

    def get_project_github_status(
        self, project_id: str, validate: bool = False
    ) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/github/status?validate={'true' if validate else 'false'}")

    def disconnect_project_github_pat(self, project_id: str) -> bool:
        return self._delete(f"/v1/projects/{project_id}/github/pat")

    def set_project_github_webhook_secret(self, project_id: str, secret: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/projects/{project_id}/github/webhook/secret", {"secret": secret})

    def delete_project_github_webhook_secret(self, project_id: str) -> bool:
        return self._delete(f"/v1/projects/{project_id}/github/webhook/secret")

    def list_project_github_webhook_events(
        self, project_id: str, limit: int = 30
    ) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/github/webhook/events?limit={limit}")

    def sync_project_github_context(
        self,
        project_id: str,
        sync_mode: str = "full",
        branch: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "sync_mode": sync_mode,
        }
        if branch:
            body["branch"] = branch
        if namespace:
            body["namespace"] = namespace
        return self._post(
            f"/v1/projects/{project_id}/github/context/sync",
            body,
            timeout=_INGEST_TIMEOUT,
        )

    def get_project_github_context_sync_status(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/github/context/sync")

    def configure_project_github_pr_review(
        self,
        project_id: str,
        enabled: bool,
        bot_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"enabled": enabled, "bot_id": bot_id}
        return self._post(f"/v1/projects/{project_id}/github/pr-review/config", body)

    def get_project_cloud_context_policy(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/cloud-context-policy")

    def update_project_cloud_context_policy(
        self,
        project_id: str,
        provider_policies: Dict[str, str],
        bot_overrides: Dict[str, Dict[str, str]],
    ) -> Optional[Dict[str, Any]]:
        body = {
            "provider_policies": provider_policies,
            "bot_overrides": bot_overrides,
        }
        return self._put(f"/v1/projects/{project_id}/cloud-context-policy", body)

    def get_project_chat_tool_access(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/chat-tool-access")

    def update_project_chat_tool_access(
        self,
        project_id: str,
        enabled: bool,
        filesystem: bool,
        repo_search: bool,
        workspace_root: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "enabled": bool(enabled),
            "filesystem": bool(filesystem),
            "repo_search": bool(repo_search),
            "workspace_root": workspace_root,
        }
        return self._put(f"/v1/projects/{project_id}/chat-tool-access", body)

    def get_project_repo_workspace(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/repo/workspace")

    def update_project_repo_workspace(
        self,
        project_id: str,
        *,
        enabled: bool,
        managed_path_mode: bool = True,
        root_path: Optional[str] = None,
        clone_url: Optional[str],
        default_branch: Optional[str],
        allow_push: bool,
        allow_command_execution: bool,
        include_clone_url: bool = True,
        include_default_branch: bool = True,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "enabled": bool(enabled),
            "managed_path_mode": bool(managed_path_mode),
            "root_path": root_path,
            "allow_push": bool(allow_push),
            "allow_command_execution": bool(allow_command_execution),
        }
        if include_clone_url:
            body["clone_url"] = clone_url
        if include_default_branch:
            body["default_branch"] = default_branch
        return self._put(f"/v1/projects/{project_id}/repo/workspace", body)

    def get_project_repo_workspace_status(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/repo/workspace/status")

    def discard_project_repo_workspace_untracked(
        self,
        project_id: str,
        *,
        paths: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"paths": list(paths or [])}
        return self._post(f"/v1/projects/{project_id}/repo/workspace/discard-untracked", body)

    def clone_project_repo_workspace(
        self,
        project_id: str,
        *,
        clone_url: Optional[str] = None,
        branch: Optional[str] = None,
        depth: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if clone_url:
            body["clone_url"] = clone_url
        if branch:
            body["branch"] = branch
        if depth is not None:
            body["depth"] = int(depth)
        return self._post(f"/v1/projects/{project_id}/repo/workspace/clone", body, timeout=_INGEST_TIMEOUT)

    def pull_project_repo_workspace(
        self,
        project_id: str,
        *,
        remote: str = "origin",
        branch: Optional[str] = None,
        rebase: bool = False,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"remote": remote, "branch": branch, "rebase": bool(rebase)}
        return self._post(f"/v1/projects/{project_id}/repo/workspace/pull", body, timeout=_INGEST_TIMEOUT)

    def commit_project_repo_workspace(
        self,
        project_id: str,
        *,
        message: str,
        add_all: bool = True,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"message": message, "add_all": bool(add_all)}
        return self._post(f"/v1/projects/{project_id}/repo/workspace/commit", body)

    def push_project_repo_workspace(
        self,
        project_id: str,
        *,
        remote: str = "origin",
        branch: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {"remote": remote, "branch": branch}
        return self._post(f"/v1/projects/{project_id}/repo/workspace/push", body, timeout=_INGEST_TIMEOUT)

    def run_project_repo_workspace_command(
        self,
        project_id: str,
        *,
        command: List[str],
        timeout_seconds: Optional[int] = None,
        use_temp_workspace: bool = False,
        temp_ref: Optional[str] = None,
        bootstrap: bool = False,
        bootstrap_languages: Optional[List[str]] = None,
        keep_temp_workspace: bool = False,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "command": command,
            "use_temp_workspace": bool(use_temp_workspace),
            "temp_ref": temp_ref,
            "bootstrap": bool(bootstrap),
            "bootstrap_languages": list(bootstrap_languages or []),
            "keep_temp_workspace": bool(keep_temp_workspace),
        }
        if timeout_seconds is not None:
            body["timeout_seconds"] = int(timeout_seconds)
        return self._post(f"/v1/projects/{project_id}/repo/workspace/run", body, timeout=_INGEST_TIMEOUT)

    def apply_project_assignment_to_repo_workspace(
        self,
        project_id: str,
        *,
        orchestration_id: str,
        overwrite: bool = True,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "orchestration_id": orchestration_id,
            "overwrite": bool(overwrite),
        }
        return self._post(
            f"/v1/projects/{project_id}/repo/workspace/apply-assignment",
            body,
            timeout=_INGEST_TIMEOUT,
        )

    def list_project_repo_workspace_runs(
        self,
        project_id: str,
        *,
        limit: int = 100,
    ) -> Optional[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        return self._get(f"/v1/projects/{project_id}/repo/workspace/runs?limit={safe_limit}")

    def summarize_project_repo_workspace_runs(
        self,
        project_id: str,
        *,
        since_hours: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        path = f"/v1/projects/{project_id}/repo/workspace/runs/summary"
        if since_hours is not None:
            safe_hours = max(1, min(int(since_hours), 24 * 365))
            path += f"?since_hours={safe_hours}"
        return self._get(path)

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
    def list_conversations(
        self,
        project_id: Optional[str] = None,
        archived: str = "active",
    ) -> Optional[List[Dict[str, Any]]]:
        path = "/v1/chat/conversations"
        parts = []
        if project_id:
            parts.append(f"project_id={project_id}")
        if archived:
            parts.append(f"archived={archived}")
        if parts:
            path = f"{path}?{'&'.join(parts)}"
        return self._get(path)

    def create_conversation(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/chat/conversations", body, timeout=_CHAT_TIMEOUT)

    def delete_conversation(self, conversation_id: str) -> bool:
        return self._delete(f"/v1/chat/conversations/{conversation_id}")

    def archive_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/chat/conversations/{conversation_id}/archive", {})

    def restore_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/chat/conversations/{conversation_id}/restore", {})

    def list_messages(self, conversation_id: str, limit: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
        path = f"/v1/chat/conversations/{conversation_id}/messages"
        if isinstance(limit, int) and limit > 0:
            path += f"?limit={int(limit)}"
        return self._get(path)

    def post_message(self, conversation_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/chat/conversations/{conversation_id}/messages", body, timeout=_CHAT_TIMEOUT)

    def mark_pm_run_failed(self, conversation_id: str, orchestration_id: str) -> Optional[Dict[str, Any]]:
        return self._post(
            f"/v1/chat/conversations/{conversation_id}/orchestrations/{orchestration_id}/mark-failed",
            {},
            timeout=_CHAT_TIMEOUT,
        )

    def update_conversation_tool_access(
        self,
        conversation_id: str,
        enabled: bool,
        filesystem: bool,
        repo_search: bool,
    ) -> Optional[Dict[str, Any]]:
        body = {
            "enabled": bool(enabled),
            "filesystem": bool(filesystem),
            "repo_search": bool(repo_search),
        }
        return self._put(f"/v1/chat/conversations/{conversation_id}/tool-access", body)

    # Vault
    def list_vault_items(
        self,
        namespace: Optional[str] = None,
        project_id: Optional[str] = None,
        limit: int = 100,
        include_content: bool = True,
    ) -> Optional[List[Dict[str, Any]]]:
        parts = [f"limit={limit}", f"include_content={'true' if include_content else 'false'}"]
        if namespace:
            parts.append(f"namespace={namespace}")
        if project_id:
            parts.append(f"project_id={project_id}")
        qs = "&".join(parts)
        return self._get(f"/v1/vault/items?{qs}")

    def ingest_vault_item(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/vault/items", body)

    def upsert_vault_item(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/vault/items/upsert", body)

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
