"""Thin synchronous HTTP client for the NexusAI Control Plane API."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

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
            elif method == "PATCH":
                resp = requests.patch(url, json=json, timeout=req_timeout, headers=self._headers())
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

    def _get(self, path: str, *, timeout: Optional[float] = None) -> Optional[Any]:
        return self._request("GET", path, timeout=timeout)

    def _post(self, path: str, json: Any, *, timeout: Optional[float] = None) -> Optional[Any]:
        return self._request("POST", path, json=json, timeout=timeout)

    def _put(self, path: str, json: Any) -> Optional[Any]:
        return self._request("PUT", path, json=json)

    def _delete(self, path: str) -> bool:
        result = self._request("DELETE", path)
        return result is not None

    def _patch(self, path: str, json: Any) -> Optional[Any]:
        return self._request("PATCH", path, json=json)

    def health(self) -> bool:
        result = self._get("/health")
        return isinstance(result, dict) and result.get("status") == "ok"

    # Workers
    def list_workers(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/workers")

    def get_worker(self, worker_id: str) -> Optional[Dict]:
        return self._get(f"/v1/workers/{worker_id}")

    def get_worker_dependencies(self, worker_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/workers/{worker_id}/dependencies")

    def register_worker(self, worker: Dict) -> Optional[Dict]:
        return self._post("/v1/workers", worker)

    def provision_worker(self, worker: Dict) -> Optional[Dict]:
        return self._post("/v1/workers/provision", worker)

    def update_worker(self, worker_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._put(f"/v1/workers/{worker_id}", body)

    def heartbeat_worker(self, worker_id: str, metrics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        payload: Dict[str, Any] = {}
        if metrics:
            payload["metrics"] = metrics
        return self._post(f"/v1/workers/{worker_id}/heartbeat", payload)

    def probe_worker(self, worker_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/workers/{worker_id}/probe", {})

    def get_worker_probe(self, worker_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/workers/{worker_id}/probe")

    def list_worker_probes(self) -> Optional[Dict[str, Any]]:
        return self._get("/v1/workers/probes")

    def get_fleet_summary(self, *, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Return the bounded, non-secret fleet health summary."""
        return self._get("/v1/workers/fleet-summary", timeout=timeout)

    def verify_worker_inference(self, worker_id: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/workers/{worker_id}/verify-inference", body or {})

    def delete_worker(self, worker_id: str) -> bool:
        return self._delete(f"/v1/workers/{worker_id}")

    # Bots
    def list_bots(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/bots")

    def get_bot(self, bot_id: str) -> Optional[Dict]:
        return self._get(f"/v1/bots/{bot_id}")

    def get_bot_dependencies(self, bot_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/bots/{bot_id}/dependencies")

    def get_bot_readiness(self, bot_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/bots/{bot_id}/readiness")

    def list_bot_readiness(self) -> Optional[Dict[str, Any]]:
        return self._get("/v1/bots/readiness")

    def create_bot(self, bot: Dict) -> Optional[Dict]:
        return self._post("/v1/bots", bot)

    def preflight_bot(self, bot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/bots/preflight", bot)

    def list_bot_blueprints(self) -> Optional[Dict[str, Any]]:
        return self._get("/v1/bot-blueprints")

    def preview_bot_blueprint(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/bot-blueprints/preview", body)

    def preflight_bot_blueprint(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate a generated specialist config without registering it."""
        return self.preflight_bot(body)

    def create_bot_blueprint(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/bot-blueprints/create", body)

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
        timeout: Optional[float] = None,
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
        return self._get(f"/v1/tasks?{'&'.join(params)}", timeout=timeout)

    def get_task(self, task_id: str) -> Optional[Dict]:
        return self._get(f"/v1/tasks/{task_id}")

    def task_usage(
        self,
        hours: int = 24,
        limit_bots: int = 25,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._get(
            f"/v1/tasks/usage?hours={max(1, int(hours))}&limit_bots={max(1, int(limit_bots))}",
            timeout=timeout,
        )

    def chat_usage(
        self,
        hours: int = 24,
        limit_conversations: int = 25,
        timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._get(
            f"/v1/chat/usage?hours={max(1, int(hours))}&limit_conversations={max(1, int(limit_conversations))}",
            timeout=timeout,
        )

    def list_work_dispatch_holds(self, *, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        return self._get("/v1/tasks/work-dispatch-holds", timeout=timeout)

    def set_work_dispatch_hold(
        self,
        *,
        project_id: str,
        manager_id: str = "",
        reason: str = "operator_hold",
        operator_id: str = "operator",
    ) -> Optional[Dict[str, Any]]:
        return self._post(
            "/v1/tasks/work-dispatch-holds",
            {
                "project_id": project_id,
                "manager_id": manager_id,
                "reason": reason,
                "operator_id": operator_id,
            },
        )

    def release_work_dispatch_hold(
        self,
        *,
        project_id: str,
        manager_id: str = "",
        operator_id: str = "operator",
    ) -> Optional[Dict[str, Any]]:
        return self._post(
            "/v1/tasks/work-dispatch-holds/release",
            {
                "project_id": project_id,
                "manager_id": manager_id,
                "operator_id": operator_id,
            },
        )

    def retry_task(self, task_id: str, payload: Any = None) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if payload is not None:
            body["payload"] = payload
        return self._post(f"/v1/tasks/{task_id}/retry", body)

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._post(f"/v1/tasks/{task_id}/cancel", body)

    def cancel_orchestration(self, orchestration_id: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if reason:
            body["reason"] = reason
        return self._post(f"/v1/tasks/orchestrations/{orchestration_id}/cancel", body)

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

    def update_project(self, project_id: str, project: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._put(f"/v1/projects/{project_id}", project)

    def delete_project(self, project_id: str) -> bool:
        return self._delete(f"/v1/projects/{project_id}")

    # Memory profiles
    def list_memory_profile_items(
        self,
        *,
        user_id: str,
        profile_id: str = "default",
        limit: int = 200,
        query: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        params = f"user_id={quote(user_id, safe='')}&profile_id={quote(profile_id, safe='')}&limit={int(limit)}"
        if query:
            params += f"&query={quote(query, safe='')}"
        return self._get(f"/v1/chat/memory-profile/items?{params}")

    def create_memory_profile_item(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/chat/memory-profile/items", body)

    def update_memory_profile_item(self, item_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._put(f"/v1/chat/memory-profile/items/{item_id}", body)

    def delete_memory_profile_item(self, item_id: str, *, user_id: str) -> bool:
        return self._delete(f"/v1/chat/memory-profile/items/{item_id}?user_id={quote(user_id, safe='')}")

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

    def review_project_assignment_files(
        self,
        project_id: str,
        *,
        orchestration_id: str,
        include_content: bool = True,
        max_content_chars: int = 20000,
        diff_context_lines: int = 3,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "orchestration_id": orchestration_id,
            "include_content": bool(include_content),
            "max_content_chars": max(1000, min(int(max_content_chars), 200000)),
            "diff_context_lines": max(0, min(int(diff_context_lines), 20)),
        }
        return self._post(
            f"/v1/projects/{project_id}/repo/workspace/review-assignment",
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

    def list_project_orchestration_workspaces(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/repo/workspace/orchestrations")

    # Models
    def list_models(self) -> Optional[List[Dict[str, Any]]]:
        return self._get("/v1/models")

    def create_model(self, model: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/models", model)

    def delete_model(self, model_id: str) -> bool:
        return self._delete(f"/v1/models/{model_id}")

    def fetch_ollama_cloud_available(self) -> Optional[List[str]]:
        return self._get("/v1/models/ollama-cloud/available")

    def check_ollama_cloud_model(self, model: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/models/ollama-cloud/check?model={model}")

    def pull_ollama_cloud_model(self, model: str) -> Optional[Dict[str, Any]]:
        return self._post("/v1/models/ollama-cloud/pull", {"model": model})

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

    def select_response_variant(self, conversation_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        return self._post(
            f"/v1/chat/conversations/{conversation_id}/messages/{message_id}/select-response",
            {},
            timeout=_CHAT_TIMEOUT,
        )

    def delete_message_pair(self, conversation_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        return self._request(
            "DELETE",
            f"/v1/chat/conversations/{conversation_id}/messages/{message_id}",
            timeout=_CHAT_TIMEOUT,
        )

    # Assignments
    def preview_assignment(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/assignments/preview", body, timeout=_CHAT_TIMEOUT)

    def create_assignment(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/assignments", body, timeout=_CHAT_TIMEOUT)

    def get_assignment_graph(self, assignment_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/assignments/{assignment_id}/graph")

    def get_assignment_graph_by_orchestration(self, orchestration_id: str) -> Optional[Dict[str, Any]]:
        safe_id = requests.utils.quote(str(orchestration_id), safe="")
        return self._get(f"/v1/assignments/by-orchestration/{safe_id}/graph")

    def splice_assignment(self, assignment_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/assignments/{assignment_id}/splice", body, timeout=_CHAT_TIMEOUT)

    def rerun_assignment_node(self, assignment_id: str, node_id: str, payload: Any = None) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if payload is not None:
            body["payload"] = payload
        return self._post(
            f"/v1/assignments/{assignment_id}/nodes/{node_id}/rerun",
            body,
            timeout=_CHAT_TIMEOUT,
        )

    def list_assignment_lineage(self, assignment_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/assignments/{assignment_id}/lineage")

    # Platform AI
    def get_platform_ai_capabilities(self) -> Optional[Dict[str, Any]]:
        return self._get("/v1/platform-ai/capabilities")

    def create_platform_ai_session(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/platform-ai/sessions", body, timeout=_CHAT_TIMEOUT)

    def list_platform_ai_sessions(
        self,
        *,
        assignment_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        mode: Optional[str] = None,
        archived: str = "active",
        limit: int = 100,
    ) -> Optional[Dict[str, Any]]:
        parts = [f"limit={max(1, int(limit))}"]
        if assignment_id:
            parts.append(f"assignment_id={requests.utils.quote(str(assignment_id), safe='')}")
        if orchestration_id:
            parts.append(f"orchestration_id={requests.utils.quote(str(orchestration_id), safe='')}")
        if mode:
            parts.append(f"mode={requests.utils.quote(str(mode), safe='')}")
        if archived:
            parts.append(f"archived={requests.utils.quote(str(archived), safe='')}")
        return self._get("/v1/platform-ai/sessions?" + "&".join(parts))

    def control_platform_ai_session(self, session_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/sessions/{session_id}/control", body, timeout=_CHAT_TIMEOUT)

    def get_platform_ai_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/sessions/{session_id}")

    def export_platform_ai_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/sessions/{session_id}/export")

    def patch_platform_ai_session(self, session_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._patch(f"/v1/platform-ai/sessions/{session_id}", body)

    def list_platform_ai_events(self, session_id: str, limit: int = 200) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/sessions/{session_id}/events?limit={max(1, int(limit))}")

    def list_platform_ai_messages(self, session_id: str, limit: int = 200) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/sessions/{session_id}/messages?limit={max(1, int(limit))}")

    def list_platform_ai_proposals(self, session_id: str, limit: int = 100) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/sessions/{session_id}/proposals?limit={max(1, int(limit))}")

    def approve_platform_ai_proposal(self, session_id: str, proposal_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/sessions/{session_id}/proposals/{proposal_id}/approve", body, timeout=_CHAT_TIMEOUT)

    def preflight_platform_ai_proposal(self, session_id: str, proposal_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/sessions/{session_id}/proposals/{proposal_id}/preflight", body, timeout=_CHAT_TIMEOUT)

    def reject_platform_ai_proposal(self, session_id: str, proposal_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/sessions/{session_id}/proposals/{proposal_id}/reject", body, timeout=_CHAT_TIMEOUT)

    def post_platform_ai_message(self, session_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/sessions/{session_id}/messages", body, timeout=_CHAT_TIMEOUT)

    def design_platform_ai_quality_suite(self, session_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/sessions/{session_id}/test-suites/design", body, timeout=_CHAT_TIMEOUT)

    def list_platform_ai_quality_suites(self, session_id: str, limit: int = 100) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/sessions/{session_id}/test-suites?limit={max(1, int(limit))}")

    def list_platform_ai_quality_suites_global(
        self,
        *,
        session_id: Optional[str] = None,
        pipeline_bot_id: Optional[str] = None,
        assignment_id: Optional[str] = None,
        orchestration_id: Optional[str] = None,
        limit: int = 200,
    ) -> Optional[Dict[str, Any]]:
        parts = [f"limit={max(1, int(limit))}"]
        if session_id:
            parts.append(f"session_id={requests.utils.quote(str(session_id), safe='')}")
        if pipeline_bot_id:
            parts.append(f"pipeline_bot_id={requests.utils.quote(str(pipeline_bot_id), safe='')}")
        if assignment_id:
            parts.append(f"assignment_id={requests.utils.quote(str(assignment_id), safe='')}")
        if orchestration_id:
            parts.append(f"orchestration_id={requests.utils.quote(str(orchestration_id), safe='')}")
        return self._get("/v1/platform-ai/test-suites?" + "&".join(parts))

    def get_platform_ai_quality_suite(self, suite_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/test-suites/{suite_id}")

    def run_platform_ai_quality_suite(self, suite_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/platform-ai/test-suites/{suite_id}/run", body, timeout=_CHAT_TIMEOUT)

    def list_platform_ai_quality_suite_runs(self, suite_id: str, limit: int = 100) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/test-suites/{suite_id}/runs?limit={max(1, int(limit))}")

    def get_platform_ai_quality_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/platform-ai/test-runs/{run_id}")

    def list_platform_ai_pipelines(self) -> Optional[Dict[str, Any]]:
        return self._get("/v1/platform-ai/pipelines")

    def list_platform_ai_pipeline_test_suites(self, pipeline_bot_id: str, limit: int = 200) -> Optional[Dict[str, Any]]:
        safe_id = requests.utils.quote(str(pipeline_bot_id), safe="")
        return self._get(f"/v1/platform-ai/pipelines/{safe_id}/test-suites?limit={max(1, int(limit))}")

    def design_platform_ai_pipeline_test_suite(
        self, pipeline_bot_id: str, body: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        safe_id = requests.utils.quote(str(pipeline_bot_id), safe="")
        return self._post(f"/v1/platform-ai/pipelines/{safe_id}/test-suites/design", body, timeout=_CHAT_TIMEOUT)

    def run_platform_ai_pipeline_test_suite(self, pipeline_bot_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        safe_id = requests.utils.quote(str(pipeline_bot_id), safe="")
        return self._post(f"/v1/platform-ai/pipelines/{safe_id}/test-suites/run", body, timeout=_CHAT_TIMEOUT)

    # Agent schedules
    def list_schedules(
        self,
        limit: int = 100,
        status: Optional[str] = None,
        target_bot_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params = [f"limit={max(1, int(limit))}"]
        if status:
            params.append(f"status={requests.utils.quote(str(status), safe='')}")
        if target_bot_id:
            params.append(f"target_bot_id={requests.utils.quote(str(target_bot_id), safe='')}")
        return self._get(f"/v1/schedules?{'&'.join(params)}")

    def list_schedule_queue_sources(self) -> Optional[Dict[str, Any]]:
        """Return non-content metadata for read-only schedule work queues."""
        return self._get("/v1/schedules/queue-sources")

    def create_schedule(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._post("/v1/schedules", body)

    def update_schedule(self, schedule_id: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._patch(f"/v1/schedules/{schedule_id}", body)

    def get_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/schedules/{schedule_id}")

    def trigger_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/schedules/{schedule_id}/trigger", {})

    def preview_schedule(self, schedule_id: str) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/schedules/{schedule_id}/preview", {})

    def list_schedule_runs(self, schedule_id: str, limit: int = 50) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/schedules/{schedule_id}/runs?limit={max(1, int(limit))}")

    # Supervisory operations
    def get_supervision_overview(self) -> Optional[Dict[str, Any]]:
        return self._get("/v1/supervision/overview", timeout=float(os.environ.get("CP_SUPERVISION_OVERVIEW_TIMEOUT", "1.0")))

    def list_supervision_reports(self, limit: int = 50) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/supervision/reports?limit={max(1, min(int(limit), 200))}")

    def list_supervision_actions(self, status: Optional[str] = None, limit: int = 100) -> Optional[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        query = f"limit={safe_limit}"
        if status:
            query += f"&status={status}"
        return self._get(f"/v1/supervision/actions?{query}")

    def list_supervision_holds(self, limit: int = 100) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/supervision/holds?limit={max(1, min(int(limit), 200))}")

    def approve_supervision_action(self, action_id: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/supervision/actions/{action_id}/approve", body or {})

    def reject_supervision_action(self, action_id: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/supervision/actions/{action_id}/reject", body or {})

    def release_supervision_hold(self, bot_id: str, body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return self._post(f"/v1/supervision/holds/{bot_id}/release", body or {})

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

    def update_conversation_memory_profile(
        self,
        conversation_id: str,
        *,
        enabled: bool,
        profile_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        return self._put(
            f"/v1/chat/conversations/{conversation_id}/memory-profile",
            {"enabled": bool(enabled), "profile_id": profile_id or "default"},
        )

    def update_conversation_route_defaults(
        self,
        conversation_id: str,
        *,
        default_bot_id: Optional[str] = None,
        default_model_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._put(
            f"/v1/chat/conversations/{conversation_id}/route-defaults",
            {
                "default_bot_id": str(default_bot_id or "").strip() or None,
                "default_model_id": str(default_model_id or "").strip() or None,
            },
        )

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

    # ------------------------------------------------------------------
    #  Ticket sources
    # ------------------------------------------------------------------

    def list_ticket_sources(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/ticket-sources")

    def create_ticket_source(
        self,
        project_id: str,
        *,
        name: str,
        source_type: str,
        config: Optional[Dict[str, Any]] = None,
        credential_value: Optional[str] = None,
        credential_key_ref: Optional[str] = None,
        enabled: bool = True,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {
            "name": name,
            "source_type": source_type,
            "enabled": enabled,
        }
        if config:
            body["config"] = config
        if credential_value:
            body["credential_value"] = credential_value
        if credential_key_ref:
            body["credential_key_ref"] = credential_key_ref
        return self._post(f"/v1/projects/{project_id}/ticket-sources", body)

    def get_ticket_source(self, project_id: str, source_id: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/v1/projects/{project_id}/ticket-sources/{source_id}")

    def update_ticket_source(
        self,
        project_id: str,
        source_id: str,
        *,
        name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        credential_value: Optional[str] = None,
        credential_key_ref: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if config is not None:
            body["config"] = config
        if credential_value is not None:
            body["credential_value"] = credential_value
        if credential_key_ref is not None:
            body["credential_key_ref"] = credential_key_ref
        if enabled is not None:
            body["enabled"] = enabled
        return self._request("PATCH", f"/v1/projects/{project_id}/ticket-sources/{source_id}", json=body)

    def delete_ticket_source(self, project_id: str, source_id: str) -> Optional[Dict[str, Any]]:
        return self._request("DELETE", f"/v1/projects/{project_id}/ticket-sources/{source_id}")

    def poll_ticket_source(
        self, project_id: str, source_id: str, max_items: Optional[int] = None
    ) -> Optional[Dict[str, Any]]:
        body = {}
        if max_items:
            body["max_items"] = max_items
        return self._post(f"/v1/projects/{project_id}/ticket-sources/{source_id}/poll", body or {})

    def list_ticket_source_items(
        self,
        project_id: str,
        source_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        unlinked_only: bool = False,
    ) -> Optional[Dict[str, Any]]:
        params = f"?limit={limit}&offset={offset}"
        if unlinked_only:
            params += "&unlinked_only=true"
        return self._get(f"/v1/projects/{project_id}/ticket-sources/{source_id}/items{params}")


_client: Optional[CPClient] = None


def get_cp_client() -> CPClient:
    global _client
    if _client is None:
        _client = CPClient()
    return _client
