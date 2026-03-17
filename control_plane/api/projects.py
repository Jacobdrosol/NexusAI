import asyncio
import ast
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional, Literal, Tuple
from urllib.parse import urlsplit, urlunsplit
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.audit.utils import record_audit_event
from control_plane.repo_workspace import (
    build_github_http_auth_header,
    is_within_workspace,
    normalize_workspace_root,
    run_command as run_repo_command,
)
from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from control_plane.task_result_files import extract_file_candidates
from shared.exceptions import APIKeyNotFoundError, ProjectNotFoundError
from shared.models import Project, Task, TaskMetadata

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class ConnectGitHubPATRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    token: str
    repo_full_name: Optional[str] = None
    validate_token: bool = Field(default=True, alias="validate")


class SetGitHubWebhookSecretRequest(BaseModel):
    secret: str


class SyncGitHubContextRequest(BaseModel):
    branch: Optional[str] = None
    sync_mode: Literal["full", "update"] = "full"
    namespace: Optional[str] = None


class ConfigurePRReviewRequest(BaseModel):
    enabled: bool = True
    bot_id: Optional[str] = None


class UpdateCloudContextPolicyRequest(BaseModel):
    provider_policies: Dict[str, str] = Field(default_factory=dict)
    bot_overrides: Dict[str, Dict[str, str]] = Field(default_factory=dict)


class UpdateProjectChatToolAccessRequest(BaseModel):
    enabled: bool = False
    filesystem: bool = False
    repo_search: bool = False
    workspace_root: Optional[str] = None


class UpdateProjectRepoWorkspaceRequest(BaseModel):
    enabled: bool = False
    managed_path_mode: bool = True
    root_path: Optional[str] = None
    clone_url: Optional[str] = None
    default_branch: Optional[str] = None
    allow_push: bool = False
    allow_command_execution: bool = False


class RepoWorkspaceCloneRequest(BaseModel):
    clone_url: Optional[str] = None
    branch: Optional[str] = None
    depth: Optional[int] = None


class RepoWorkspacePullRequest(BaseModel):
    remote: str = "origin"
    branch: Optional[str] = None
    rebase: bool = False


class RepoWorkspaceCommitRequest(BaseModel):
    message: str
    add_all: bool = True


class RepoWorkspacePushRequest(BaseModel):
    remote: str = "origin"
    branch: Optional[str] = None


class RepoWorkspaceRunRequest(BaseModel):
    command: List[str] = Field(default_factory=list)
    timeout_seconds: Optional[int] = None
    use_temp_workspace: bool = False
    temp_ref: Optional[str] = None
    bootstrap: bool = False
    bootstrap_languages: List[str] = Field(default_factory=list)
    keep_temp_workspace: bool = False


class RepoWorkspaceApplyAssignmentRequest(BaseModel):
    orchestration_id: str
    overwrite: bool = True


class RepoWorkspaceDiscardUntrackedRequest(BaseModel):
    paths: List[str] = Field(default_factory=list)


_CLOUD_POLICY_VALUES = {"allow", "redact", "block"}
_SUPPORTED_CLOUD_PROVIDERS = {"openai", "claude", "gemini"}
_URL_WITH_AUTH_RE = re.compile(r"((?:https?|ssh)://)([^@\s/]+)@", re.IGNORECASE)


def _normalize_cloud_policy_value(value: Any, default: str = "allow") -> str:
    val = str(value or "").strip().lower()
    return val if val in _CLOUD_POLICY_VALUES else default


def _provider_policy_limits(policy: str) -> set[str]:
    if policy == "allow":
        return {"allow", "redact", "block"}
    if policy == "redact":
        return {"redact", "block"}
    return {"block"}


def _extract_cloud_context_policy(project: Project) -> Dict[str, Any]:
    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    raw = settings.get("cloud_context_policy") if isinstance(settings.get("cloud_context_policy"), dict) else {}
    providers_in = raw.get("provider_policies") if isinstance(raw.get("provider_policies"), dict) else {}
    bots_in = raw.get("bot_overrides") if isinstance(raw.get("bot_overrides"), dict) else {}

    provider_policies: Dict[str, str] = {}
    for provider in _SUPPORTED_CLOUD_PROVIDERS:
        provider_policies[provider] = _normalize_cloud_policy_value(providers_in.get(provider), default="allow")

    bot_overrides: Dict[str, Dict[str, str]] = {}
    for bot_id, per_provider in bots_in.items():
        if not isinstance(per_provider, dict):
            continue
        bid = str(bot_id or "").strip()
        if not bid:
            continue
        cleaned: Dict[str, str] = {}
        for provider, policy in per_provider.items():
            p = str(provider or "").strip().lower()
            if p not in _SUPPORTED_CLOUD_PROVIDERS:
                continue
            cleaned[p] = _normalize_cloud_policy_value(policy, default="")
        if cleaned:
            bot_overrides[bid] = cleaned

    for bot_id, per_provider in list(bot_overrides.items()):
        validated: Dict[str, str] = {}
        for provider, policy in per_provider.items():
            allowed = _provider_policy_limits(provider_policies.get(provider, "allow"))
            if policy in allowed:
                validated[provider] = policy
            elif provider_policies.get(provider) == "redact" and policy == "allow":
                validated[provider] = "redact"
            else:
                validated[provider] = "block"
        if validated:
            bot_overrides[bot_id] = validated
        else:
            bot_overrides.pop(bot_id, None)

    return {
        "provider_policies": provider_policies,
        "bot_overrides": bot_overrides,
    }


def _validate_requested_cloud_policy(body: UpdateCloudContextPolicyRequest) -> Dict[str, Any]:
    provider_policies: Dict[str, str] = {}
    for provider in _SUPPORTED_CLOUD_PROVIDERS:
        requested = body.provider_policies.get(provider, "allow")
        provider_policies[provider] = _normalize_cloud_policy_value(requested, default="allow")

    bot_overrides: Dict[str, Dict[str, str]] = {}
    for bot_id, per_provider in body.bot_overrides.items():
        bid = str(bot_id or "").strip()
        if not bid:
            continue
        if not isinstance(per_provider, dict):
            raise HTTPException(status_code=400, detail=f"bot_overrides.{bid} must be an object")
        cleaned: Dict[str, str] = {}
        for provider, policy in per_provider.items():
            p = str(provider or "").strip().lower()
            if p not in _SUPPORTED_CLOUD_PROVIDERS:
                raise HTTPException(status_code=400, detail=f"Unsupported provider in bot override: {provider}")
            pol = _normalize_cloud_policy_value(policy, default="")
            if pol not in _CLOUD_POLICY_VALUES:
                raise HTTPException(status_code=400, detail=f"Invalid policy '{policy}' for bot '{bid}' provider '{p}'")
            allowed = _provider_policy_limits(provider_policies[p])
            if pol not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Bot override '{pol}' not allowed for provider '{p}' "
                        f"when provider policy is '{provider_policies[p]}'"
                    ),
                )
            cleaned[p] = pol
        if cleaned:
            bot_overrides[bid] = cleaned

    return {
        "provider_policies": provider_policies,
        "bot_overrides": bot_overrides,
    }


def _extract_project_chat_tool_access(project: Project) -> Dict[str, Any]:
    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    raw = settings.get("chat_tool_access") if isinstance(settings.get("chat_tool_access"), dict) else {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "filesystem": bool(raw.get("filesystem", False)),
        "repo_search": bool(raw.get("repo_search", False)),
        "workspace_root": str(raw.get("workspace_root") or "").strip() or None,
    }


def _validate_requested_project_chat_tool_access(body: UpdateProjectChatToolAccessRequest) -> Dict[str, Any]:
    workspace_root = str(body.workspace_root or "").strip() or None
    if workspace_root is not None and len(workspace_root) > 1024:
        raise HTTPException(status_code=400, detail="workspace_root is too long")
    return {
        "enabled": bool(body.enabled),
        "filesystem": bool(body.filesystem),
        "repo_search": bool(body.repo_search),
        "workspace_root": workspace_root,
    }


def _extract_project_repo_workspace(project: Project) -> Dict[str, Any]:
    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    raw = settings.get("repo_workspace") if isinstance(settings.get("repo_workspace"), dict) else {}
    managed_path_mode = bool(raw.get("managed_path_mode", True))
    root_path = str(raw.get("root_path") or "").strip() or None
    clone_url = str(raw.get("clone_url") or "").strip() or None
    default_branch = str(raw.get("default_branch") or "").strip() or None
    if managed_path_mode:
        root_path = None
    return {
        "enabled": bool(raw.get("enabled", False)),
        "managed_path_mode": managed_path_mode,
        "root_path": root_path,
        "clone_url": clone_url,
        "default_branch": default_branch,
        "allow_push": bool(raw.get("allow_push", False)),
        "allow_command_execution": bool(raw.get("allow_command_execution", False)),
    }


def _project_workspace_slug(project_id: str) -> str:
    token = str(project_id or "").strip()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", token).strip("-")
    return slug or "project"


def _repo_workspace_base_root() -> Path:
    raw = str(os.environ.get("NEXUSAI_REPO_WORKSPACE_ROOT", "") or "").strip()
    candidate = Path(raw).expanduser() if raw else (Path("data") / "repo_workspaces")
    try:
        if candidate.is_absolute():
            return candidate.resolve(strict=False)
        return (Path.cwd() / candidate).resolve(strict=False)
    except Exception:
        return (Path.cwd() / "data" / "repo_workspaces").resolve(strict=False)


def _managed_repo_workspace_root(project_id: str) -> Path:
    return _repo_workspace_base_root() / _project_workspace_slug(project_id) / "repo"


def _repo_workspace_binding(cfg: Dict[str, Any]) -> str:
    return "managed" if bool(cfg.get("managed_path_mode", True)) else "custom"


def _redact_url_credentials_in_text(value: str) -> str:
    text = str(value or "")
    if not text:
        return text

    def _repl(match: re.Match[str]) -> str:
        scheme = match.group(1)
        userinfo = str(match.group(2) or "")
        if ":" in userinfo:
            username = userinfo.split(":", 1)[0].strip()
            if username:
                return f"{scheme}{username}:***@"
        return f"{scheme}***@"

    return _URL_WITH_AUTH_RE.sub(_repl, text)


def _redact_clone_url(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except Exception:
        return _redact_url_credentials_in_text(raw)
    if not parsed.netloc or "@" not in parsed.netloc:
        return raw
    userinfo, host = parsed.netloc.rsplit("@", 1)
    if ":" in userinfo:
        username = userinfo.split(":", 1)[0].strip()
        masked_userinfo = f"{username}:***" if username else "***"
    else:
        masked_userinfo = "***"
    return urlunsplit((parsed.scheme, f"{masked_userinfo}@{host}", parsed.path, parsed.query, parsed.fragment))


def _redact_repo_value(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith("http.extraheader="):
            return "http.extraHeader=<redacted>"
        return _redact_url_credentials_in_text(value)
    if isinstance(value, list):
        return [_redact_repo_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_repo_value(item) for key, item in value.items()}
    return value


def _sanitize_repo_command_for_record(command: Optional[List[str]]) -> List[str]:
    parts = command if isinstance(command, list) else []
    return [_redact_repo_value(str(part or "")) for part in parts]


def _public_repo_workspace_config(project_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "project_id": project_id,
        "enabled": bool(cfg.get("enabled", False)),
        "managed_path_mode": bool(cfg.get("managed_path_mode", True)),
        "workspace_binding": _repo_workspace_binding(cfg),
        "root_path": None,
        "clone_url": _redact_clone_url(str(cfg.get("clone_url") or "").strip() or None),
        "default_branch": str(cfg.get("default_branch") or "").strip() or None,
        "allow_push": bool(cfg.get("allow_push", False)),
        "allow_command_execution": bool(cfg.get("allow_command_execution", False)),
    }


def _sanitize_workspace_value(value: Any, *, root: Path) -> Any:
    root_path = str(root)
    variants = {
        root_path,
        root_path.replace("\\", "/"),
        root_path.replace("/", "\\"),
    }
    variants = {v for v in variants if v}

    if isinstance(value, str):
        out = value
        for token in variants:
            out = out.replace(token, "<workspace>")
        return out
    if isinstance(value, list):
        return [_sanitize_workspace_value(item, root=root) for item in value]
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            # Do not expose host filesystem path fields in API payloads.
            if str(key).strip().lower() in {"root_path", "workspace_path", "path"}:
                cleaned[key] = "<workspace>"
                continue
            cleaned[key] = _sanitize_workspace_value(item, root=root)
        return cleaned
    return value


def _sanitize_repo_run_row(row: Dict[str, Any], *, root: Path) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return row
    data = dict(row)
    command = data.get("command")
    if isinstance(command, list):
        data["command"] = _redact_repo_value(_sanitize_workspace_value(command, root=root))
    details = data.get("details")
    if isinstance(details, dict):
        safe_details = dict(details)
        safe_details.pop("root_path", None)
        safe_details.pop("workspace_path", None)
        data["details"] = _redact_repo_value(_sanitize_workspace_value(safe_details, root=root))
    return data


def _sanitize_repo_command_result(result: Dict[str, Any], *, root: Path) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    safe = dict(result)
    safe.pop("cwd", None)
    return _redact_repo_value(_sanitize_workspace_value(safe, root=root))


def _validate_requested_project_repo_workspace(body: UpdateProjectRepoWorkspaceRequest) -> Dict[str, Any]:
    managed_path_mode = bool(body.managed_path_mode)
    root_path = str(body.root_path or "").strip() or None
    clone_url = str(body.clone_url or "").strip() or None
    default_branch = str(body.default_branch or "").strip() or None

    if root_path is not None and len(root_path) > 1024:
        raise HTTPException(status_code=400, detail="root_path is too long")
    if clone_url is not None and len(clone_url) > 1024:
        raise HTTPException(status_code=400, detail="clone_url is too long")
    if default_branch is not None and len(default_branch) > 256:
        raise HTTPException(status_code=400, detail="default_branch is too long")

    if bool(body.enabled) and not managed_path_mode:
        if not root_path:
            raise HTTPException(status_code=400, detail="root_path is required when managed_path_mode is false")
        normalized = normalize_workspace_root(root_path)
        if normalized is None:
            raise HTTPException(status_code=400, detail="root_path must be an absolute path")
        root_path = str(normalized)
    elif root_path:
        if managed_path_mode:
            root_path = None
        else:
            normalized = normalize_workspace_root(root_path)
            if normalized is None:
                raise HTTPException(status_code=400, detail="root_path must be an absolute path")
            root_path = str(normalized)

    if clone_url and not (
        clone_url.startswith("https://")
        or clone_url.startswith("http://")
        or clone_url.startswith("ssh://")
        or clone_url.startswith("git@")
    ):
        raise HTTPException(status_code=400, detail="clone_url must be an HTTPS/SSH git URL")

    return {
        "enabled": bool(body.enabled),
        "managed_path_mode": managed_path_mode,
        "root_path": root_path,
        "clone_url": clone_url,
        "default_branch": default_branch,
        "allow_push": bool(body.allow_push),
        "allow_command_execution": bool(body.allow_command_execution),
    }


def _resolve_repo_workspace_root(project_id: str, cfg: Dict[str, Any], *, require_enabled: bool = True) -> Path:
    if require_enabled and not bool(cfg.get("enabled", False)):
        raise HTTPException(status_code=400, detail="repo workspace is disabled for this project")
    if bool(cfg.get("managed_path_mode", True)):
        return _managed_repo_workspace_root(project_id)
    root = normalize_workspace_root(str(cfg.get("root_path") or "").strip() or None)
    if root is None:
        raise HTTPException(status_code=400, detail="repo workspace root_path is not configured")
    return root


async def _project_github_pat(project: Project, key_vault) -> Optional[str]:
    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    key_ref = str(github_cfg.get("pat_key_ref") or "").strip()
    if not key_ref:
        return None
    try:
        return await key_vault.get_secret(key_ref)
    except Exception:
        return None


async def _run_repo_command(
    args: List[str],
    *,
    cwd: Path,
    timeout_seconds: Optional[int] = None,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return await run_repo_command(
        args,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        env_overrides=env_overrides,
    )


async def _repo_auth_git_args(
    *,
    cwd: Path,
    remote: str,
    github_pat: Optional[str],
) -> List[str]:
    token = str(github_pat or "").strip()
    if not token:
        return []
    remote_res = await _run_repo_command(
        ["git", "remote", "get-url", remote],
        cwd=cwd,
        timeout_seconds=15,
    )
    if not remote_res.get("ok"):
        return []
    remote_url = str(remote_res.get("stdout") or "").strip()
    if "github.com" not in remote_url.lower():
        return []
    return ["-c", f"http.extraHeader={build_github_http_auth_header(token)}"]


async def _repo_branch_name(cwd: Path) -> Optional[str]:
    branch_res = await _run_repo_command(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        timeout_seconds=15,
    )
    if not branch_res.get("ok"):
        return None
    branch = str(branch_res.get("stdout") or "").strip()
    return branch or None


async def _repo_status_snapshot(
    *,
    root: Path,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    exists = root.exists()
    is_repo = False
    branch: Optional[str] = None
    porcelain_lines: List[str] = []
    remotes: List[str] = []
    last_commit: Dict[str, Any] = {}

    if exists:
        check_repo = await _run_repo_command(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            timeout_seconds=15,
        )
        is_repo = bool(check_repo.get("ok")) and "true" in str(check_repo.get("stdout") or "").lower()

    if exists and is_repo:
        branch = await _repo_branch_name(root)

        status_res = await _run_repo_command(
            ["git", "status", "--porcelain", "-b", "--untracked-files=all"],
            cwd=root,
            timeout_seconds=20,
        )
        if status_res.get("ok"):
            porcelain_lines = [
                str(line).rstrip()
                for line in str(status_res.get("stdout") or "").splitlines()
                if str(line).strip()
            ]

        remote_res = await _run_repo_command(
            ["git", "remote", "-v"],
            cwd=root,
            timeout_seconds=20,
        )
        if remote_res.get("ok"):
            remotes = [
                str(line).rstrip()
                for line in str(remote_res.get("stdout") or "").splitlines()
                if str(line).strip()
            ]

        commit_res = await _run_repo_command(
            ["git", "log", "-1", "--pretty=format:%H%n%an%n%ad%n%s"],
            cwd=root,
            timeout_seconds=20,
        )
        if commit_res.get("ok"):
            parts = str(commit_res.get("stdout") or "").splitlines()
            if parts:
                last_commit = {
                    "sha": parts[0] if len(parts) > 0 else None,
                    "author": parts[1] if len(parts) > 1 else None,
                    "date": parts[2] if len(parts) > 2 else None,
                    "subject": parts[3] if len(parts) > 3 else None,
                }

    dirty_lines = [line for line in porcelain_lines if not line.startswith("##")]
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "managed_path_mode": bool(cfg.get("managed_path_mode", True)),
        "workspace_binding": _repo_workspace_binding(cfg),
        "root_path": None,
        "clone_url": _redact_clone_url(cfg.get("clone_url")),
        "default_branch": cfg.get("default_branch"),
        "allow_push": bool(cfg.get("allow_push", False)),
        "allow_command_execution": bool(cfg.get("allow_command_execution", False)),
        "workspace_exists": exists,
        "is_repo": is_repo,
        "branch": branch,
        "clean": len(dirty_lines) == 0,
        "porcelain": porcelain_lines,
        "remotes": [_redact_url_credentials_in_text(line) for line in remotes],
        "last_commit": last_commit,
    }


def _assignment_task_sort_key(task: Task) -> tuple[int, str, str]:
    payload = task.payload if isinstance(task.payload, dict) else {}
    try:
        step_number = int(payload.get("step_number") or 0)
    except Exception:
        step_number = 0
    return (step_number, str(task.created_at or ""), str(task.updated_at or ""))


def _assignment_file_candidates(tasks: List[Task]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    ordered_tasks = sorted(tasks, key=_assignment_task_sort_key)
    for task in ordered_tasks:
        if task.status != "completed":
            continue
        for candidate in extract_file_candidates(task.result):
            path = str(candidate.get("path") or "").strip()
            content = str(candidate.get("content") or "")
            if not path or not content:
                continue
            payload = task.payload if isinstance(task.payload, dict) else {}
            merged[path] = {
                "path": path,
                "content": content,
                "source": str(candidate.get("source") or "task_result"),
                "language": candidate.get("language"),
                "task_id": task.id,
                "bot_id": task.bot_id,
                "task_title": str(payload.get("title") or ""),
                "step_number": payload.get("step_number"),
            }
    return list(merged.values())


def _write_assignment_files(
    *,
    root: Path,
    candidates: List[Dict[str, Any]],
    overwrite: bool,
) -> List[Dict[str, Any]]:
    applied: List[Dict[str, Any]] = []
    for item in candidates:
        relative_path = str(item.get("path") or "").strip()
        if not relative_path:
            continue
        target = (root / relative_path).resolve(strict=False)
        if not is_within_workspace(root, target):
            raise HTTPException(status_code=400, detail=f"refusing to write outside workspace: {relative_path}")
        if target.exists() and not overwrite:
            raise HTTPException(status_code=409, detail=f"file already exists and overwrite is disabled: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(item.get("content") or "")
        existed = target.exists()
        previous = target.read_text(encoding="utf-8") if existed else None
        target.write_text(content, encoding="utf-8")
        status = "created"
        if existed:
            status = "unchanged" if previous == content else "updated"
        applied.append(
            {
                "path": relative_path,
                "status": status,
                "task_id": item.get("task_id"),
                "bot_id": item.get("bot_id"),
                "task_title": item.get("task_title"),
                "step_number": item.get("step_number"),
                "source": item.get("source"),
                "language": item.get("language"),
            }
        )
    return applied


def _porcelain_untracked_paths(porcelain_lines: List[str]) -> List[str]:
    paths: List[str] = []
    for line in porcelain_lines:
        raw = str(line or "")
        if not raw.startswith("?? "):
            continue
        path = _decode_git_porcelain_path(raw[3:].strip())
        if path:
            paths.append(path)
    return paths


def _decode_git_porcelain_path(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, str):
                return parsed
        except Exception:
            inner = text[1:-1]
            try:
                return bytes(inner, "utf-8").decode("unicode_escape")
            except Exception:
                return inner.replace('\\"', '"').replace("\\\\", "\\")
    return text


def _prune_empty_parent_dirs(*, start: Path, root: Path) -> None:
    current = start
    while True:
        if current == root or current == current.parent:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _discard_untracked_workspace_paths(
    *,
    root: Path,
    untracked_paths: List[str],
    requested_paths: List[str],
) -> List[str]:
    allowed = {str(path).strip().replace("\\", "/") for path in untracked_paths if str(path).strip()}
    if requested_paths:
        selected = [str(path).strip().replace("\\", "/") for path in requested_paths if str(path).strip()]
    else:
        selected = sorted(allowed)
    if not selected:
        return []

    invalid = [path for path in selected if path not in allowed]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"requested discard paths are not currently untracked: {', '.join(invalid[:5])}",
        )

    removed: List[str] = []
    for relative_path in selected:
        target = (root / relative_path).resolve(strict=False)
        if not is_within_workspace(root, target):
            raise HTTPException(status_code=400, detail=f"refusing to delete outside workspace: {relative_path}")
        if target.is_dir():
            shutil.rmtree(target)
            removed.append(relative_path)
            _prune_empty_parent_dirs(start=target.parent, root=root)
            continue
        if target.exists():
            target.unlink()
            removed.append(relative_path)
            _prune_empty_parent_dirs(start=target.parent, root=root)
    return removed


def _allowed_workspace_commands() -> set[str]:
    raw = (
        os.environ.get("NEXUSAI_REPO_WORKSPACE_ALLOWED_COMMANDS", "")
        or (
            "py,python,pytest,uv,pip,pip3,npm,pnpm,yarn,node,npx,dotnet,go,cargo,make,ninja,cmake,"
            "gcc,g++,clang,clang++,cl,msbuild,git"
        )
    )
    values = {part.strip().lower() for part in raw.split(",") if part.strip()}
    python_name = Path(sys.executable).name.lower()
    python_stem = Path(sys.executable).stem.lower()
    if python_name:
        values.add(python_name)
    if python_stem:
        values.add(python_stem)
    return values


def _safe_command_parts(parts: List[str]) -> List[str]:
    if not isinstance(parts, list) or not parts:
        raise HTTPException(status_code=400, detail="command must be a non-empty array")
    cleaned: List[str] = []
    for part in parts:
        token = str(part or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="command contains an empty argument")
        if len(token) > 512:
            raise HTTPException(status_code=400, detail="command argument is too long")
        if "\n" in token or "\r" in token:
            raise HTTPException(status_code=400, detail="command arguments cannot contain newlines")
        cleaned.append(token)
    return cleaned


def _result_usage(result: Dict[str, Any]) -> Dict[str, Any]:
    usage = result.get("resource_usage")
    return usage if isinstance(usage, dict) else {}


def _aggregate_usage(usages: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not usages:
        return {
            "wall_time_ms": 0,
            "cpu_user_seconds": 0.0,
            "cpu_system_seconds": 0.0,
            "peak_rss_bytes": 0,
            "peak_vms_bytes": 0,
            "io_read_bytes": 0,
            "io_write_bytes": 0,
            "sample_count": 0,
        }
    wall_time_ms = 0
    cpu_user_seconds = 0.0
    cpu_system_seconds = 0.0
    peak_rss_bytes = 0
    peak_vms_bytes = 0
    io_read_bytes = 0
    io_write_bytes = 0
    sample_count = 0
    for usage in usages:
        wall_time_ms += int(usage.get("wall_time_ms") or 0)
        cpu_user_seconds += float(usage.get("cpu_user_seconds") or 0.0)
        cpu_system_seconds += float(usage.get("cpu_system_seconds") or 0.0)
        peak_rss_bytes = max(peak_rss_bytes, int(usage.get("peak_rss_bytes") or 0))
        peak_vms_bytes = max(peak_vms_bytes, int(usage.get("peak_vms_bytes") or 0))
        io_read_bytes += int(usage.get("io_read_bytes") or 0)
        io_write_bytes += int(usage.get("io_write_bytes") or 0)
        sample_count += int(usage.get("sample_count") or 0)
    return {
        "wall_time_ms": int(wall_time_ms),
        "cpu_user_seconds": round(cpu_user_seconds, 6),
        "cpu_system_seconds": round(cpu_system_seconds, 6),
        "peak_rss_bytes": int(peak_rss_bytes),
        "peak_vms_bytes": int(peak_vms_bytes),
        "io_read_bytes": int(io_read_bytes),
        "io_write_bytes": int(io_write_bytes),
        "sample_count": int(sample_count),
    }


async def _record_repo_workspace_usage(
    request: Request,
    *,
    project_id: str,
    action: str,
    status: str,
    command: Optional[List[str]] = None,
    result: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    store = getattr(request.app.state, "repo_workspace_usage_store", None)
    if store is None:
        try:
            from control_plane.repo_workspace_usage_store import RepoWorkspaceUsageStore
            store = RepoWorkspaceUsageStore()
            request.app.state.repo_workspace_usage_store = store
        except Exception:
            return
    safe_result = result if isinstance(result, dict) else {}
    usage = metrics if isinstance(metrics, dict) else _result_usage(safe_result)
    source_command = command or (safe_result.get("command") if isinstance(safe_result.get("command"), list) else [])
    safe_command = _sanitize_repo_command_for_record(source_command)
    raw_details = details if isinstance(details, dict) else {}
    safe_details = _redact_repo_value(raw_details)
    if not isinstance(safe_details, dict):
        safe_details = {}
    try:
        await store.record_run(
            project_id=project_id,
            action=action,
            status=status,
            started_at=str(safe_result.get("started_at") or "").strip() or None,
            finished_at=str(safe_result.get("finished_at") or "").strip() or None,
            command=safe_command,
            details=safe_details,
            metrics=usage or {},
        )
    except Exception:
        return


def _detect_bootstrap_languages(workspace: Path) -> List[str]:
    detected: List[str] = []
    if (workspace / "requirements.txt").exists() or (workspace / "pyproject.toml").exists() or (workspace / "setup.py").exists():
        detected.append("python")
    if (workspace / "package.json").exists():
        detected.append("node")
    has_dotnet = False
    try:
        if any(workspace.glob("*.sln")) or any(workspace.rglob("*.csproj")):
            has_dotnet = True
    except Exception:
        has_dotnet = False
    if has_dotnet:
        detected.append("dotnet")
    has_cpp = (workspace / "CMakeLists.txt").exists() or (workspace / "Makefile").exists()
    if not has_cpp:
        try:
            if any(workspace.rglob("*.vcxproj")):
                has_cpp = True
        except Exception:
            has_cpp = has_cpp
    if has_cpp:
        detected.append("cpp")
    return detected


def _python_venv_executable(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _bootstrap_command_specs(workspace: Path, languages: List[str]) -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    wanted = [str(lang or "").strip().lower() for lang in languages if str(lang or "").strip()]
    if not wanted:
        wanted = _detect_bootstrap_languages(workspace)

    if "python" in wanted:
        venv_dir = workspace / ".nexusai_venv"
        py_bin = _python_venv_executable(venv_dir)
        python_launcher = str(Path(sys.executable))
        specs.append({
            "label": "python_venv_create",
            "command": [python_launcher, "-m", "venv", str(venv_dir)],
            "timeout_seconds": 300,
        })
        if (workspace / "requirements.txt").exists():
            specs.append({
                "label": "python_install_requirements",
                "command": [str(py_bin), "-m", "pip", "install", "-r", "requirements.txt"],
                "timeout_seconds": 1200,
            })
        elif (workspace / "pyproject.toml").exists() or (workspace / "setup.py").exists():
            specs.append({
                "label": "python_install_project",
                "command": [str(py_bin), "-m", "pip", "install", "-e", "."],
                "timeout_seconds": 1200,
            })

    if "node" in wanted and (workspace / "package.json").exists():
        if (workspace / "pnpm-lock.yaml").exists():
            specs.append({
                "label": "node_install_pnpm",
                "command": ["pnpm", "install", "--frozen-lockfile"],
                "timeout_seconds": 1200,
            })
        elif (workspace / "yarn.lock").exists():
            specs.append({
                "label": "node_install_yarn",
                "command": ["yarn", "install", "--frozen-lockfile"],
                "timeout_seconds": 1200,
            })
        elif (workspace / "package-lock.json").exists():
            specs.append({
                "label": "node_install_npm_ci",
                "command": ["npm", "ci"],
                "timeout_seconds": 1200,
            })
        else:
            specs.append({
                "label": "node_install_npm",
                "command": ["npm", "install"],
                "timeout_seconds": 1200,
            })

    if "dotnet" in wanted:
        specs.append({
            "label": "dotnet_restore",
            "command": ["dotnet", "restore"],
            "timeout_seconds": 1200,
        })

    if "cpp" in wanted and (workspace / "CMakeLists.txt").exists():
        specs.append({
            "label": "cpp_cmake_configure",
            "command": ["cmake", "-S", ".", "-B", "build"],
            "timeout_seconds": 600,
        })
    return specs


async def _is_git_repository(root: Path) -> bool:
    check_repo = await _run_repo_command(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        timeout_seconds=20,
    )
    return bool(check_repo.get("ok")) and "true" in str(check_repo.get("stdout") or "").lower()


def _repo_temp_root(project_id: str) -> Path:
    base = str(os.environ.get("NEXUSAI_REPO_WORKSPACE_TEMP_ROOT", "") or "").strip()
    if base:
        candidate = Path(base)
    else:
        candidate = Path(tempfile.gettempdir()) / "nexusai_repo_workspace"
    return candidate / str(project_id) / f"run-{uuid.uuid4().hex[:12]}"


async def _prepare_temp_workspace(
    *,
    project_id: str,
    root: Path,
    ref: Optional[str] = None,
) -> Dict[str, Any]:
    temp_root = _repo_temp_root(project_id)
    temp_root.parent.mkdir(parents=True, exist_ok=True)

    if await _is_git_repository(root):
        cmd = ["git", "worktree", "add", "--detach", str(temp_root)]
        wanted_ref = str(ref or "").strip()
        if wanted_ref:
            cmd.append(wanted_ref)
        setup = await _run_repo_command(cmd, cwd=root, timeout_seconds=300)
        if not setup.get("ok"):
            detail = str(setup.get("stderr") or setup.get("error") or "failed to create temporary git worktree").strip()
            raise HTTPException(status_code=400, detail=detail)
        return {"mode": "git_worktree", "path": temp_root, "setup_result": setup}

    try:
        await asyncio.to_thread(shutil.copytree, str(root), str(temp_root), dirs_exist_ok=False)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to create temporary workspace copy: {exc}")
    return {"mode": "copy", "path": temp_root, "setup_result": None}


async def _cleanup_temp_workspace(
    *,
    base_root: Path,
    temp_root: Path,
    mode: str,
) -> Dict[str, Any]:
    output: Dict[str, Any] = {"ok": True, "mode": mode, "path": str(temp_root)}
    if mode == "git_worktree":
        rm_res = await _run_repo_command(
            ["git", "worktree", "remove", "--force", str(temp_root)],
            cwd=base_root,
            timeout_seconds=120,
        )
        output["remove_result"] = rm_res
        if not rm_res.get("ok"):
            output["ok"] = False
    try:
        await asyncio.to_thread(shutil.rmtree, str(temp_root), True)
    except Exception:
        pass
    return output


async def _fetch_github_identity(token: str, repo_full_name: Optional[str] = None) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        user_resp = await client.get("https://api.github.com/user", headers=headers)
        user_resp.raise_for_status()
        user_data = user_resp.json()
        out: Dict[str, Any] = {
            "user_login": user_data.get("login"),
            "user_id": user_data.get("id"),
        }
        if repo_full_name:
            repo_resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}",
                headers=headers,
            )
            repo_resp.raise_for_status()
            repo_data = repo_resp.json()
            out["repo"] = {
                "full_name": repo_data.get("full_name"),
                "private": bool(repo_data.get("private", False)),
                "default_branch": repo_data.get("default_branch"),
            }
        return out


def _is_probably_text_path(path: str) -> bool:
    lowered = path.lower()
    blocked_suffixes = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
        ".pdf", ".zip", ".gz", ".tar", ".tgz", ".7z", ".exe", ".dll", ".bin",
        ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".mp3",
        ".pyc", ".class", ".jar",
    }
    return not any(lowered.endswith(s) for s in blocked_suffixes)


async def _fetch_repo_context_files(
    token: str,
    repo_full_name: str,
    branch: Optional[str],
) -> Dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        repo_resp = await client.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers)
        repo_resp.raise_for_status()
        repo_data = repo_resp.json()
        ref = branch or str(repo_data.get("default_branch") or "main")

        tree_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/git/trees/{ref}?recursive=1",
            headers=headers,
        )
        tree_resp.raise_for_status()
        tree_data = tree_resp.json()
        tree_items = tree_data.get("tree") if isinstance(tree_data, dict) else []
        if not isinstance(tree_items, list):
            tree_items = []

        candidates = []
        for node in tree_items:
            if not isinstance(node, dict):
                continue
            if node.get("type") != "blob":
                continue
            path = str(node.get("path") or "")
            size = int(node.get("size") or 0)
            if not path or size <= 0 or size > 250_000:
                continue
            if not _is_probably_text_path(path):
                continue
            candidates.append({"path": path, "size": size})

        files = []
        for item in candidates:
            path = item["path"]
            content_resp = await client.get(
                f"https://api.github.com/repos/{repo_full_name}/contents/{path}?ref={ref}",
                headers=headers,
            )
            if content_resp.status_code != 200:
                continue
            content_data = content_resp.json()
            if not isinstance(content_data, dict):
                continue
            if content_data.get("encoding") != "base64":
                continue
            raw_b64 = str(content_data.get("content") or "").replace("\n", "")
            if not raw_b64:
                continue
            try:
                decoded = base64.b64decode(raw_b64)
                text = decoded.decode("utf-8")
            except Exception:
                continue
            files.append(
                {
                    "path": path,
                    "content": text,
                    "size": item["size"],
                    "sha": content_data.get("sha"),
                }
            )

        return {
            "repo_full_name": repo_full_name,
            "branch": ref,
            "files": files,
        }


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def _github_get_all_pages(
    client: httpx.AsyncClient,
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    page = 1
    rows: List[Dict[str, Any]] = []
    base_params = dict(params or {})
    while True:
        call_params = dict(base_params)
        call_params["per_page"] = 100
        call_params["page"] = page
        resp = await client.get(url, headers=headers, params=call_params)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list) or not payload:
            break
        rows.extend([row for row in payload if isinstance(row, dict)])
        if len(payload) < 100:
            break
        page += 1


    return rows


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


async def _fetch_repo_commits(
    token: str,
    repo_full_name: str,
    branch: Optional[str],
    since: Optional[str] = None,
) -> Dict[str, Any]:
    headers = _github_headers(token)
    async with httpx.AsyncClient(timeout=30.0) as client:
        repo_resp = await client.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers)
        repo_resp.raise_for_status()
        repo_data = repo_resp.json()
        ref = branch or str(repo_data.get("default_branch") or "main")
        params: Dict[str, Any] = {"sha": ref}
        if since:
            params["since"] = since
        rows = await _github_get_all_pages(
            client,
            f"https://api.github.com/repos/{repo_full_name}/commits",
            headers=headers,
            params=params,
        )
        commits = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                commit = row.get("commit") if isinstance(row.get("commit"), dict) else {}
                author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
                commits.append(
                    {
                        "sha": row.get("sha"),
                        "html_url": row.get("html_url"),
                        "message": commit.get("message"),
                        "author_name": author.get("name"),
                        "authored_at": author.get("date"),
                    }
                )
        return {"repo_full_name": repo_full_name, "branch": ref, "commits": commits}


async def _fetch_issue_comments(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    repo_full_name: str,
    issue_number: int,
    max_comments: int,
) -> List[Dict[str, Any]]:
    if max_comments <= 0:
        return []
    rows = await _github_get_all_pages(
        client,
        f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments",
        headers=headers,
    )
    comments: List[Dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows[:max_comments]:
            if not isinstance(row, dict):
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            comments.append(
                {
                    "user": user.get("login"),
                    "created_at": row.get("created_at"),
                    "body": row.get("body"),
                    "html_url": row.get("html_url"),
                }
            )
    return comments


async def _fetch_pr_review_comments(
    client: httpx.AsyncClient,
    headers: Dict[str, str],
    repo_full_name: str,
    pull_number: int,
    max_comments: int,
) -> List[Dict[str, Any]]:
    if max_comments <= 0:
        return []
    rows = await _github_get_all_pages(
        client,
        f"https://api.github.com/repos/{repo_full_name}/pulls/{pull_number}/comments",
        headers=headers,
    )
    comments: List[Dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows[:max_comments]:
            if not isinstance(row, dict):
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            comments.append(
                {
                    "user": user.get("login"),
                    "created_at": row.get("created_at"),
                    "path": row.get("path"),
                    "body": row.get("body"),
                    "html_url": row.get("html_url"),
                }
            )
    return comments


async def _fetch_repo_pull_requests(
    token: str,
    repo_full_name: str,
    include_conversations: bool,
    updated_after: Optional[str] = None,
) -> List[Dict[str, Any]]:
    headers = _github_headers(token)
    updated_after_dt = _parse_iso8601(updated_after)
    async with httpx.AsyncClient(timeout=30.0) as client:
        rows = await _github_get_all_pages(
            client,
            f"https://api.github.com/repos/{repo_full_name}/pulls",
            headers=headers,
            params={"state": "all", "sort": "updated", "direction": "desc"},
        )
        pulls: List[Dict[str, Any]] = []
        if not isinstance(rows, list):
            return pulls
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_updated_at = _parse_iso8601(row.get("updated_at"))
            if updated_after_dt and row_updated_at and row_updated_at <= updated_after_dt:
                continue
            pr_number = int(row.get("number") or 0)
            issue_comments: List[Dict[str, Any]] = []
            review_comments: List[Dict[str, Any]] = []
            if include_conversations and pr_number > 0:
                issue_comments = await _fetch_issue_comments(
                    client, headers, repo_full_name, pr_number, 10_000
                )
                review_comments = await _fetch_pr_review_comments(
                    client, headers, repo_full_name, pr_number, 10_000
                )
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            base = row.get("base") if isinstance(row.get("base"), dict) else {}
            head = row.get("head") if isinstance(row.get("head"), dict) else {}
            pulls.append(
                {
                    "number": pr_number,
                    "title": row.get("title"),
                    "body": row.get("body"),
                    "state": row.get("state"),
                    "draft": bool(row.get("draft", False)),
                    "html_url": row.get("html_url"),
                    "user": user.get("login"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "merged_at": row.get("merged_at"),
                    "base_ref": base.get("ref"),
                    "head_ref": head.get("ref"),
                    "issue_comments": issue_comments,
                    "review_comments": review_comments,
                }
            )
        return pulls


async def _fetch_repo_issues(
    token: str,
    repo_full_name: str,
    include_conversations: bool,
    updated_after: Optional[str] = None,
) -> List[Dict[str, Any]]:
    headers = _github_headers(token)
    async with httpx.AsyncClient(timeout=30.0) as client:
        params: Dict[str, Any] = {"state": "all", "sort": "updated", "direction": "desc"}
        if updated_after:
            params["since"] = updated_after
        rows = await _github_get_all_pages(
            client,
            f"https://api.github.com/repos/{repo_full_name}/issues",
            headers=headers,
            params=params,
        )
        issues: List[Dict[str, Any]] = []
        if not isinstance(rows, list):
            return issues
        for row in rows:
            if not isinstance(row, dict) or row.get("pull_request"):
                continue
            issue_number = int(row.get("number") or 0)
            comments: List[Dict[str, Any]] = []
            if include_conversations and issue_number > 0:
                comments = await _fetch_issue_comments(
                    client, headers, repo_full_name, issue_number, 10_000
                )
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            issues.append(
                {
                    "number": issue_number,
                    "title": row.get("title"),
                    "body": row.get("body"),
                    "state": row.get("state"),
                    "html_url": row.get("html_url"),
                    "user": user.get("login"),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "comments": comments,
                }
            )
        return issues


def _build_commit_text(repo_full_name: str, branch: str, commit: Dict[str, Any]) -> str:
    return (
        f"Repository: {repo_full_name}\n"
        f"Branch: {branch}\n"
        f"Commit: {commit.get('sha') or ''}\n"
        f"Author: {commit.get('author_name') or ''}\n"
        f"Authored At: {commit.get('authored_at') or ''}\n"
        f"URL: {commit.get('html_url') or ''}\n\n"
        f"{commit.get('message') or ''}"
    ).strip()


def _build_pull_request_text(repo_full_name: str, pr: Dict[str, Any], include_conversations: bool) -> str:
    lines = [
        f"Repository: {repo_full_name}",
        f"Pull Request: #{pr.get('number') or ''}",
        f"Title: {pr.get('title') or ''}",
        f"State: {pr.get('state') or ''}",
        f"Draft: {'yes' if pr.get('draft') else 'no'}",
        f"Author: {pr.get('user') or ''}",
        f"Base: {pr.get('base_ref') or ''}",
        f"Head: {pr.get('head_ref') or ''}",
        f"Created At: {pr.get('created_at') or ''}",
        f"Updated At: {pr.get('updated_at') or ''}",
        f"Merged At: {pr.get('merged_at') or ''}",
        f"URL: {pr.get('html_url') or ''}",
        "",
        "Body:",
        pr.get("body") or "",
    ]
    if include_conversations:
        issue_comments = pr.get("issue_comments") or []
        review_comments = pr.get("review_comments") or []
        lines.extend(["", "Issue Comments:"])
        if issue_comments:
            for comment in issue_comments:
                lines.extend(
                    [
                        f"- {comment.get('user') or 'unknown'} @ {comment.get('created_at') or ''}",
                        comment.get("body") or "",
                    ]
                )
        else:
            lines.append("(none)")
        lines.extend(["", "Review Comments:"])
        if review_comments:
            for comment in review_comments:
                header = f"- {comment.get('user') or 'unknown'} @ {comment.get('created_at') or ''}"
                if comment.get("path"):
                    header += f" on {comment.get('path')}"
                lines.extend([header, comment.get("body") or ""])
        else:
            lines.append("(none)")
    return "\n".join(lines).strip()


def _build_issue_text(repo_full_name: str, issue: Dict[str, Any], include_conversations: bool) -> str:
    lines = [
        f"Repository: {repo_full_name}",
        f"Issue: #{issue.get('number') or ''}",
        f"Title: {issue.get('title') or ''}",
        f"State: {issue.get('state') or ''}",
        f"Author: {issue.get('user') or ''}",
        f"Created At: {issue.get('created_at') or ''}",
        f"Updated At: {issue.get('updated_at') or ''}",
        f"URL: {issue.get('html_url') or ''}",
        "",
        "Body:",
        issue.get("body") or "",
    ]
    if include_conversations:
        lines.extend(["", "Comments:"])
        comments = issue.get("comments") or []
        if comments:
            for comment in comments:
                lines.extend(
                    [
                        f"- {comment.get('user') or 'unknown'} @ {comment.get('created_at') or ''}",
                        comment.get("body") or "",
                    ]
                )
        else:
            lines.append("(none)")
    return "\n".join(lines).strip()


def _merge_settings(project: Project, patch: Dict[str, Any]) -> Dict[str, Any]:
    base = {}
    if isinstance(project.settings_overrides, dict):
        base = dict(project.settings_overrides)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged = dict(base[key])
            merged.update(value)
            base[key] = merged
        else:
            base[key] = value
    return base


def _extract_github_sync_state(project: Project) -> Dict[str, Any]:
    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    sync_state = github_cfg.get("context_sync") if isinstance(github_cfg.get("context_sync"), dict) else {}
    return dict(sync_state)


async def _upsert_github_vault_item(
    vault_manager,
    *,
    title: str,
    content: str,
    namespace: str,
    project_id: str,
    source_type: str,
    source_ref: str,
    metadata: Dict[str, Any],
):
    return await vault_manager.upsert_text(
        title=title,
        content=content,
        namespace=namespace,
        project_id=project_id,
        source_type=source_type,
        source_ref=source_ref,
        metadata=metadata,
    )


def _verify_github_signature(secret: str, raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    provided = signature_header.split("=", 1)[1].strip()
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


def _ensure_github_sync_job_store(app) -> Dict[str, Dict[str, Any]]:
    store = getattr(app.state, "github_context_sync_jobs", None)
    if not isinstance(store, dict):
        store = {}
        app.state.github_context_sync_jobs = store
    return store


def _get_latest_github_sync_job(app, project_id: str) -> Optional[Dict[str, Any]]:
    store = _ensure_github_sync_job_store(app)
    jobs = [job for job in store.values() if str(job.get("project_id")) == str(project_id)]
    if not jobs:
        return None
    jobs.sort(key=lambda job: str(job.get("updated_at") or job.get("created_at") or ""), reverse=True)
    return dict(jobs[0])


def _set_github_sync_job(app, job: Dict[str, Any]) -> Dict[str, Any]:
    store = _ensure_github_sync_job_store(app)
    job["updated_at"] = datetime.now(timezone.utc).isoformat()
    store[str(job["job_id"])] = dict(job)
    return dict(job)


@router.post("", response_model=Project)
async def create_project(request: Request, project: Project) -> Project:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.register(project)
        return project
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[Project])
async def list_projects(request: Request) -> List[Project]:
    project_registry = request.app.state.project_registry
    return await project_registry.list()


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, request: Request) -> Project:
    project_registry = request.app.state.project_registry
    try:
        return await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{project_id}", response_model=Project)
async def update_project(project_id: str, request: Request, project: Project) -> Project:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.update(project_id, project)
        return project
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{project_id}")
async def delete_project(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.remove(project_id)
        return {"message": f"Project {project_id} removed"}
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{project_id}/bridges/{target_project_id}")
async def add_project_bridge(project_id: str, target_project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.add_bridge(project_id, target_project_id)
        return {"status": "ok"}
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{project_id}/bridges/{target_project_id}")
async def remove_project_bridge(project_id: str, target_project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.remove_bridge(project_id, target_project_id)
        return {"status": "ok"}
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{project_id}/github/pat")
async def connect_github_pat(project_id: str, request: Request, body: ConnectGitHubPATRequest) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    token = body.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="token is required")

    identity: Dict[str, Any] = {}
    if body.validate_token:
        try:
            identity = await _fetch_github_identity(token, repo_full_name=body.repo_full_name)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"GitHub validation failed: {e}")

    key_name = f"github_pat::{project_id}"
    await key_vault.set_key(name=key_name, provider="github", value=token)

    github_settings: Dict[str, Any] = {
        "pat_key_ref": key_name,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    if body.repo_full_name:
        github_settings["repo_full_name"] = body.repo_full_name.strip()
    if identity.get("user_login"):
        github_settings["user_login"] = identity.get("user_login")

    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"github": github_settings})}
    )
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.github.pat.connect",
        resource=f"project:{project_id}",
        details={"repo_full_name": github_settings.get("repo_full_name")},
    )
    return {
        "status": "connected",
        "project_id": project_id,
        "repo_full_name": github_settings.get("repo_full_name"),
        "user_login": github_settings.get("user_login"),
    }


@router.get("/{project_id}/github/status")
async def github_status(project_id: str, request: Request, validate: bool = False) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    key_ref = github_cfg.get("pat_key_ref")
    if not key_ref:
        return {"connected": False, "project_id": project_id}

    key_meta: Optional[Dict[str, Any]] = None
    try:
        key_meta = await key_vault.get_key(str(key_ref))
    except APIKeyNotFoundError:
        return {
            "connected": False,
            "project_id": project_id,
            "error": "stored token reference not found",
        }

    result: Dict[str, Any] = {
        "connected": True,
        "project_id": project_id,
        "repo_full_name": github_cfg.get("repo_full_name"),
        "user_login": github_cfg.get("user_login"),
        "connected_at": github_cfg.get("connected_at"),
        "has_webhook_secret": bool(github_cfg.get("webhook_secret_key_ref")),
        "pr_review": (
            github_cfg.get("pr_review")
            if isinstance(github_cfg.get("pr_review"), dict)
            else {"enabled": False, "bot_id": None}
        ),
        "key_name": key_meta.get("name"),
        "key_updated_at": key_meta.get("updated_at"),
        "context_sync": (
            github_cfg.get("context_sync")
            if isinstance(github_cfg.get("context_sync"), dict)
            else {}
        ),
    }
    if validate:
        try:
            secret = await key_vault.get_secret(str(key_ref))
            identity = await _fetch_github_identity(
                secret,
                repo_full_name=github_cfg.get("repo_full_name"),
            )
            result["validated"] = True
            if identity.get("user_login"):
                result["user_login"] = identity.get("user_login")
        except Exception as e:
            result["validated"] = False
            result["validation_error"] = str(e)
    return result


@router.delete("/{project_id}/github/pat")
async def disconnect_github_pat(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    key_ref = github_cfg.get("pat_key_ref")

    if key_ref:
        try:
            await key_vault.delete_key(str(key_ref))
        except APIKeyNotFoundError:
            pass

    updated_settings = dict(settings)
    if "github" in updated_settings:
        del updated_settings["github"]
    updated = project.model_copy(update={"settings_overrides": updated_settings or None})
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.github.pat.disconnect",
        resource=f"project:{project_id}",
    )
    return {"status": "disconnected", "project_id": project_id}


@router.post("/{project_id}/github/webhook/secret")
async def set_github_webhook_secret(
    project_id: str,
    request: Request,
    body: SetGitHubWebhookSecretRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    secret = body.secret.strip()
    if not secret:
        raise HTTPException(status_code=400, detail="secret is required")

    key_name = f"github_webhook_secret::{project_id}"
    await key_vault.set_key(name=key_name, provider="github", value=secret)

    github_settings: Dict[str, Any] = {
        "webhook_secret_key_ref": key_name,
        "webhook_secret_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"github": github_settings})}
    )
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.github.webhook.secret.set",
        resource=f"project:{project_id}",
    )
    return {"status": "ok", "project_id": project_id}


@router.delete("/{project_id}/github/webhook/secret")
async def delete_github_webhook_secret(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    key_ref = github_cfg.get("webhook_secret_key_ref")
    if key_ref:
        try:
            await key_vault.delete_key(str(key_ref))
        except APIKeyNotFoundError:
            pass

    updated_settings = dict(settings)
    if isinstance(updated_settings.get("github"), dict):
        gh = dict(updated_settings["github"])
        gh.pop("webhook_secret_key_ref", None)
        gh.pop("webhook_secret_updated_at", None)
        if gh:
            updated_settings["github"] = gh
        else:
            updated_settings.pop("github", None)
    updated = project.model_copy(update={"settings_overrides": updated_settings or None})
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.github.webhook.secret.delete",
        resource=f"project:{project_id}",
    )
    return {"status": "ok", "project_id": project_id}


@router.post("/{project_id}/github/webhook")
async def ingest_github_webhook(project_id: str, request: Request) -> dict:
    await enforce_body_size(request, route_name="github_webhook", default_max_bytes=1_000_000)
    await enforce_rate_limit(
        request,
        route_name="github_webhook",
        default_limit=240,
        default_window_seconds=60,
    )
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    store = request.app.state.github_webhook_store
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    secret_key_ref = github_cfg.get("webhook_secret_key_ref")
    if not secret_key_ref:
        raise HTTPException(status_code=400, detail="webhook secret is not configured for this project")
    try:
        webhook_secret = await key_vault.get_secret(str(secret_key_ref))
    except APIKeyNotFoundError:
        raise HTTPException(status_code=400, detail="configured webhook secret key not found")

    raw = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_github_signature(webhook_secret, raw, sig):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    event_type = request.headers.get("X-GitHub-Event", "").strip()
    if event_type not in {"push", "pull_request", "issues"}:
        raise HTTPException(status_code=400, detail="unsupported event type")
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
    require_delivery_id = os.environ.get("NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DELIVERY_ID", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    if require_delivery_id and not delivery_id:
        raise HTTPException(status_code=400, detail="missing required X-GitHub-Delivery header")
    if delivery_id and await store.has_delivery_id(project_id=project_id, delivery_id=delivery_id):
        raise HTTPException(status_code=409, detail="duplicate webhook delivery id")

    max_skew_seconds = int(os.environ.get("NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS", "300"))
    require_date = os.environ.get("NEXUSAI_GITHUB_WEBHOOK_REQUIRE_DATE_HEADER", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    date_header = request.headers.get("Date", "").strip()
    if require_date and not date_header:
        raise HTTPException(status_code=400, detail="missing required Date header")
    if date_header:
        try:
            sent_at = parsedate_to_datetime(date_header)
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            sent_at_utc = sent_at.astimezone(timezone.utc)
            now_utc = datetime.now(timezone.utc)
            skew = abs((now_utc - sent_at_utc).total_seconds())
            if skew > max_skew_seconds:
                raise HTTPException(status_code=401, detail="webhook timestamp outside allowed window")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="invalid Date header")

    try:
        payload: Dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON payload")

    action = payload.get("action") if isinstance(payload, dict) else None
    repo = None
    if isinstance(payload, dict):
        repo_obj = payload.get("repository")
        if isinstance(repo_obj, dict):
            repo = repo_obj.get("full_name")

    event = await store.record_event(
        project_id=project_id,
        delivery_id=delivery_id or None,
        event_type=event_type,
        action=str(action) if action else None,
        repository_full_name=str(repo) if repo else None,
        payload=payload if isinstance(payload, dict) else {},
    )
    ttl_seconds = int(os.environ.get("NEXUSAI_GITHUB_WEBHOOK_DEDUP_TTL_SECONDS", "86400"))
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(60, ttl_seconds))
    await store.prune_older_than(cutoff.isoformat())
    review_task_id = None
    pr_cfg = github_cfg.get("pr_review") if isinstance(github_cfg.get("pr_review"), dict) else {}
    if (
        event_type == "pull_request"
        and bool(pr_cfg.get("enabled"))
        and pr_cfg.get("bot_id")
        and isinstance(payload, dict)
    ):
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
        task_manager = request.app.state.task_manager
        review_task = await task_manager.create_task(
            bot_id=str(pr_cfg.get("bot_id")),
            payload={
                "source": "github_pr_review",
                "project_id": project_id,
                "repo_full_name": repo,
                "action": action,
                "pull_request": {
                    "number": pr.get("number"),
                    "title": pr.get("title"),
                    "body": pr.get("body"),
                    "html_url": pr.get("html_url"),
                    "base_ref": (pr.get("base") or {}).get("ref") if isinstance(pr.get("base"), dict) else None,
                    "head_ref": (pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else None,
                },
            },
            metadata=TaskMetadata(source="github_pr_review", project_id=project_id),
        )
        review_task_id = review_task.id
    return {
        "status": "accepted",
        "event_id": event["id"],
        "event_type": event_type,
        "review_task_id": review_task_id,
    }


@router.get("/{project_id}/github/webhook/events")
async def list_github_webhook_events(
    project_id: str,
    request: Request,
    limit: int = 30,
) -> dict:
    project_registry = request.app.state.project_registry
    store = request.app.state.github_webhook_store
    try:
        await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    safe_limit = max(1, min(int(limit), 200))
    rows = await store.list_events(project_id=project_id, limit=safe_limit)
    return {"events": rows}


async def _perform_github_context_sync(
    app,
    project_id: str,
    body: SyncGitHubContextRequest,
) -> dict:
    project_registry = app.state.project_registry
    key_vault = app.state.key_vault
    vault_manager = app.state.vault_manager
    project = await project_registry.get(project_id)

    settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
    github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
    key_ref = github_cfg.get("pat_key_ref")
    repo_full_name = github_cfg.get("repo_full_name")
    if not key_ref:
        raise HTTPException(status_code=400, detail="GitHub PAT is not configured for this project")
    if not repo_full_name:
        raise HTTPException(status_code=400, detail="repo_full_name is not configured for this project")

    try:
        token = await key_vault.get_secret(str(key_ref))
    except APIKeyNotFoundError:
        raise HTTPException(status_code=400, detail="configured PAT key not found")

    sync_mode = (body.sync_mode or "full").strip().lower()
    namespace = (body.namespace or f"project:{project_id}:repo").strip() or f"project:{project_id}:repo"
    sync_state = _extract_github_sync_state(project)
    last_full_sync_at = _parse_iso8601(sync_state.get("last_full_sync_at"))
    last_update_sync_at = _parse_iso8601(sync_state.get("last_update_sync_at"))
    last_sync_at = last_update_sync_at or last_full_sync_at

    ingested = []
    counts = {"files": 0, "commits": 0, "pull_requests": 0, "issues": 0, "conversations": 0}
    warnings: List[str] = []

    repo_full_name_str = str(repo_full_name)
    branch_name = body.branch
    latest_commit_at: Optional[datetime] = _parse_iso8601(sync_state.get("latest_commit_at"))
    latest_pr_updated_at: Optional[datetime] = _parse_iso8601(sync_state.get("latest_pr_updated_at"))
    latest_issue_updated_at: Optional[datetime] = _parse_iso8601(sync_state.get("latest_issue_updated_at"))

    repo_file_result = await _fetch_repo_context_files(
        token=token,
        repo_full_name=repo_full_name_str,
        branch=branch_name,
    )
    branch_name = repo_file_result["branch"]
    existing_file_items = await vault_manager.list_items(namespace=namespace, project_id=project_id, limit=100000)
    file_sha_by_ref: Dict[str, str] = {}
    for item in existing_file_items:
        meta = item.metadata if isinstance(item.metadata, dict) else {}
        if meta.get("ingest_kind") == "repo_file" and item.source_ref:
            file_sha_by_ref[str(item.source_ref)] = str(meta.get("sha") or "")
    for file in repo_file_result["files"]:
        source_ref = f"github://{repo_file_result['repo_full_name']}/{file['path']}"
        if sync_mode == "update" and file_sha_by_ref.get(source_ref) == str(file.get("sha") or ""):
            continue
        item = await _upsert_github_vault_item(
            vault_manager,
            title=f"{repo_file_result['repo_full_name']}:{file['path']}",
            content=file["content"],
            namespace=namespace,
            project_id=project_id,
            source_type="file",
            source_ref=source_ref,
            metadata={
                "provider": "github",
                "repo_full_name": repo_file_result["repo_full_name"],
                "branch": repo_file_result["branch"],
                "path": file["path"],
                "sha": file.get("sha"),
                "size": file.get("size"),
                "ingest_kind": "repo_file",
            },
        )
        ingested.append({"item_id": item.id, "type": "file", "path": file["path"]})
        counts["files"] += 1

    try:
        commit_result = await _fetch_repo_commits(
            token=token,
            repo_full_name=repo_full_name_str,
            branch=branch_name,
            since=_iso_or_none(last_sync_at) if sync_mode == "update" else None,
        )
        branch_name = commit_result["branch"]
        for commit in commit_result["commits"]:
            authored_at = _parse_iso8601(commit.get("authored_at"))
            if authored_at and (latest_commit_at is None or authored_at > latest_commit_at):
                latest_commit_at = authored_at
            item = await _upsert_github_vault_item(
                vault_manager,
                title=f"{repo_full_name_str}:commit:{str(commit.get('sha') or '')[:12]}",
                content=_build_commit_text(repo_full_name_str, branch_name or "", commit),
                namespace=namespace,
                project_id=project_id,
                source_type="custom",
                source_ref=f"github://{repo_full_name_str}/commit/{commit.get('sha') or ''}",
                metadata={
                    "provider": "github",
                    "repo_full_name": repo_full_name_str,
                    "branch": branch_name,
                    "sha": commit.get("sha"),
                    "author_name": commit.get("author_name"),
                    "authored_at": commit.get("authored_at"),
                    "ingest_kind": "commit",
                },
            )
            ingested.append({"item_id": item.id, "type": "commit", "sha": commit.get("sha")})
            counts["commits"] += 1
    except Exception as e:
        warnings.append(f"commits failed: {e}")

    try:
        pulls = await _fetch_repo_pull_requests(
            token=token,
            repo_full_name=repo_full_name_str,
            include_conversations=True,
            updated_after=_iso_or_none(latest_pr_updated_at if sync_mode == "update" else None),
        )
        for pr in pulls:
            pr_updated_at = _parse_iso8601(pr.get("updated_at"))
            if pr_updated_at and (latest_pr_updated_at is None or pr_updated_at > latest_pr_updated_at):
                latest_pr_updated_at = pr_updated_at
            item = await _upsert_github_vault_item(
                vault_manager,
                title=f"{repo_full_name_str}:pr:{pr.get('number')}",
                content=_build_pull_request_text(repo_full_name_str, pr, include_conversations=True),
                namespace=namespace,
                project_id=project_id,
                source_type="custom",
                source_ref=f"github://{repo_full_name_str}/pull/{pr.get('number')}",
                metadata={
                    "provider": "github",
                    "repo_full_name": repo_full_name_str,
                    "number": pr.get("number"),
                    "state": pr.get("state"),
                    "draft": pr.get("draft"),
                    "base_ref": pr.get("base_ref"),
                    "head_ref": pr.get("head_ref"),
                    "updated_at": pr.get("updated_at"),
                    "ingest_kind": "pull_request",
                },
            )
            ingested.append({"item_id": item.id, "type": "pull_request", "number": pr.get("number")})
            counts["pull_requests"] += 1
            counts["conversations"] += len(pr.get("issue_comments") or []) + len(pr.get("review_comments") or [])
    except Exception as e:
        warnings.append(f"pull requests failed: {e}")

    try:
        issues = await _fetch_repo_issues(
            token=token,
            repo_full_name=repo_full_name_str,
            include_conversations=True,
            updated_after=_iso_or_none(latest_issue_updated_at if sync_mode == "update" else None),
        )
        for issue in issues:
            issue_updated_at = _parse_iso8601(issue.get("updated_at"))
            if issue_updated_at and (latest_issue_updated_at is None or issue_updated_at > latest_issue_updated_at):
                latest_issue_updated_at = issue_updated_at
            item = await _upsert_github_vault_item(
                vault_manager,
                title=f"{repo_full_name_str}:issue:{issue.get('number')}",
                content=_build_issue_text(repo_full_name_str, issue, include_conversations=True),
                namespace=namespace,
                project_id=project_id,
                source_type="custom",
                source_ref=f"github://{repo_full_name_str}/issues/{issue.get('number')}",
                metadata={
                    "provider": "github",
                    "repo_full_name": repo_full_name_str,
                    "number": issue.get("number"),
                    "state": issue.get("state"),
                    "updated_at": issue.get("updated_at"),
                    "ingest_kind": "issue",
                },
            )
            ingested.append({"item_id": item.id, "type": "issue", "number": issue.get("number")})
            counts["issues"] += 1
            counts["conversations"] += len(issue.get("comments") or [])
    except Exception as e:
        warnings.append(f"issues failed: {e}")

    now_iso = datetime.now(timezone.utc).isoformat()
    context_sync_state = {
        "repo_full_name": repo_full_name_str,
        "branch": branch_name,
        "namespace": namespace,
        "last_mode": sync_mode,
        "last_sync_at": now_iso,
        "last_full_sync_at": now_iso if sync_mode == "full" else sync_state.get("last_full_sync_at"),
        "last_update_sync_at": now_iso if sync_mode == "update" else sync_state.get("last_update_sync_at"),
        "latest_commit_at": _iso_or_none(latest_commit_at),
        "latest_pr_updated_at": _iso_or_none(latest_pr_updated_at),
        "latest_issue_updated_at": _iso_or_none(latest_issue_updated_at),
        "last_counts": counts,
        "last_ingested_count": len(ingested),
        "last_warnings": warnings,
    }
    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"github": {"context_sync": context_sync_state}})}
    )
    await project_registry.update(project_id, updated)

    return {
        "status": "completed",
        "project_id": project_id,
        "repo_full_name": repo_full_name_str,
        "branch": branch_name,
        "namespace": namespace,
        "ingested_count": len(ingested),
        "ingested": ingested,
        "sync_mode": sync_mode,
        "counts": counts,
        "warnings": warnings,
        "context_sync": context_sync_state,
    }


@router.post("/{project_id}/github/context/sync")
async def sync_github_repo_context(
    project_id: str,
    request: Request,
    body: SyncGitHubContextRequest,
) -> dict:
    try:
        await request.app.state.project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    existing = _get_latest_github_sync_job(request.app, project_id)
    if existing and existing.get("status") in {"queued", "running"}:
        return existing

    now_iso = datetime.now(timezone.utc).isoformat()
    job = _set_github_sync_job(
        request.app,
        {
            "job_id": f"sync-{project_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "project_id": project_id,
            "status": "queued",
            "sync_mode": body.sync_mode,
            "branch": body.branch,
            "namespace": body.namespace,
            "created_at": now_iso,
            "started_at": None,
            "finished_at": None,
            "error": None,
            "counts": {},
            "warnings": [],
            "ingested_count": 0,
        },
    )

    async def _runner():
        _set_github_sync_job(
            request.app,
            {
                **job,
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        try:
            result = await _perform_github_context_sync(request.app, project_id, body)
            _set_github_sync_job(
                request.app,
                {
                    **job,
                    "status": "completed",
                    "started_at": job.get("started_at") or datetime.now(timezone.utc).isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "counts": result.get("counts") or {},
                    "warnings": result.get("warnings") or [],
                    "ingested_count": int(result.get("ingested_count") or 0),
                    "result": result,
                    "error": None,
                },
            )
        except Exception as exc:
            _set_github_sync_job(
                request.app,
                {
                    **job,
                    "status": "failed",
                    "started_at": job.get("started_at") or datetime.now(timezone.utc).isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(exc),
                },
            )

    asyncio.create_task(_runner())
    await record_audit_event(
        request,
        action="projects.github.context.sync.queue",
        resource=f"project:{project_id}",
        details={"sync_mode": body.sync_mode, "branch": body.branch, "namespace": body.namespace},
    )
    return job


@router.get("/{project_id}/github/context/sync")
async def get_github_context_sync_status(project_id: str, request: Request) -> dict:
    try:
        project = await request.app.state.project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    job = _get_latest_github_sync_job(request.app, project_id) or {
        "job_id": None,
        "project_id": project_id,
        "status": "idle",
        "counts": {},
        "warnings": [],
        "ingested_count": 0,
    }
    sync_state = _extract_github_sync_state(project)
    job["context_sync"] = sync_state
    return job


@router.post("/{project_id}/github/pr-review/config")
async def configure_github_pr_review(
    project_id: str,
    request: Request,
    body: ConfigurePRReviewRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if body.enabled and not (body.bot_id or "").strip():
        raise HTTPException(status_code=400, detail="bot_id is required when PR review workflow is enabled")

    review_cfg = {
        "enabled": bool(body.enabled),
        "bot_id": (body.bot_id or "").strip() or None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"github": {"pr_review": review_cfg}})}
    )
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.github.pr_review.configure",
        resource=f"project:{project_id}",
        details={"enabled": review_cfg["enabled"], "bot_id": review_cfg["bot_id"]},
    )
    return {"status": "ok", "project_id": project_id, "pr_review": review_cfg}


@router.get("/{project_id}/cloud-context-policy")
async def get_project_cloud_context_policy(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    cfg = _extract_cloud_context_policy(project)
    return {"project_id": project_id, **cfg}


@router.put("/{project_id}/cloud-context-policy")
async def update_project_cloud_context_policy(
    project_id: str,
    request: Request,
    body: UpdateCloudContextPolicyRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    validated = _validate_requested_cloud_policy(body)
    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"cloud_context_policy": validated})}
    )
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.cloud_context_policy.update",
        resource=f"project:{project_id}",
        details=validated,
    )
    return {"status": "ok", "project_id": project_id, **validated}


@router.get("/{project_id}/chat-tool-access")
async def get_project_chat_tool_access(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    cfg = _extract_project_chat_tool_access(project)
    return {"project_id": project_id, **cfg}


@router.put("/{project_id}/chat-tool-access")
async def update_project_chat_tool_access(
    project_id: str,
    request: Request,
    body: UpdateProjectChatToolAccessRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    validated = _validate_requested_project_chat_tool_access(body)
    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"chat_tool_access": validated})}
    )
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.chat_tool_access.update",
        resource=f"project:{project_id}",
        details=validated,
    )
    return {"status": "ok", "project_id": project_id, **validated}


@router.get("/{project_id}/repo/workspace")
async def get_project_repo_workspace(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    cfg = _extract_project_repo_workspace(project)
    return _public_repo_workspace_config(project_id, cfg)


@router.put("/{project_id}/repo/workspace")
async def update_project_repo_workspace(
    project_id: str,
    request: Request,
    body: UpdateProjectRepoWorkspaceRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    current_cfg = _extract_project_repo_workspace(project)
    validated = _validate_requested_project_repo_workspace(body)
    effective_cfg = dict(validated)
    if "clone_url" not in body.model_fields_set:
        effective_cfg["clone_url"] = current_cfg.get("clone_url")

    updated = project.model_copy(
        update={"settings_overrides": _merge_settings(project, {"repo_workspace": effective_cfg})}
    )
    await project_registry.update(project_id, updated)
    await record_audit_event(
        request,
        action="projects.repo_workspace.update",
        resource=f"project:{project_id}",
        details={
            "enabled": effective_cfg["enabled"],
            "managed_path_mode": effective_cfg["managed_path_mode"],
            "clone_url": _redact_clone_url(effective_cfg["clone_url"]),
            "default_branch": effective_cfg["default_branch"],
            "allow_push": effective_cfg["allow_push"],
            "allow_command_execution": effective_cfg["allow_command_execution"],
        },
    )
    return {"status": "ok", **_public_repo_workspace_config(project_id, effective_cfg)}


@router.get("/{project_id}/repo/workspace/status")
async def get_project_repo_workspace_status(project_id: str, request: Request) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    if not bool(cfg.get("enabled", False)):
        return {
            **_public_repo_workspace_config(project_id, cfg),
            "workspace_exists": False,
            "is_repo": False,
            "branch": None,
            "clean": True,
            "porcelain": [],
            "remotes": [],
            "last_commit": {},
        }

    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=False)
    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    return {"project_id": project_id, **snapshot}


@router.post("/{project_id}/repo/workspace/discard-untracked")
async def discard_project_repo_workspace_untracked(
    project_id: str,
    request: Request,
    body: RepoWorkspaceDiscardUntrackedRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    if not bool(snapshot.get("is_repo")):
        raise HTTPException(status_code=400, detail="repo workspace is not a git repository")

    untracked_paths = _porcelain_untracked_paths(snapshot.get("porcelain") or [])
    removed_paths = _discard_untracked_workspace_paths(
        root=root,
        untracked_paths=untracked_paths,
        requested_paths=list(body.paths or []),
    )
    updated_snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    await record_audit_event(
        request,
        action="projects.repo_workspace.discard_untracked",
        resource=f"project:{project_id}",
        details={"removed_paths": removed_paths, "removed_count": len(removed_paths)},
    )
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="discard_untracked",
        status="ok",
        details={"removed_paths": removed_paths, "removed_count": len(removed_paths)},
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "removed_paths": removed_paths,
        "workspace": updated_snapshot,
    }


@router.post("/{project_id}/repo/workspace/apply-assignment")
async def apply_assignment_to_project_repo_workspace(
    project_id: str,
    request: Request,
    body: RepoWorkspaceApplyAssignmentRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    task_manager = request.app.state.task_manager
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    if not bool(snapshot.get("is_repo")):
        raise HTTPException(status_code=400, detail="repo workspace is not a git repository; clone it before applying files")

    orchestration_id = str(body.orchestration_id or "").strip()
    if not orchestration_id:
        raise HTTPException(status_code=400, detail="orchestration_id is required")

    tasks = await task_manager.list_tasks(orchestration_id=orchestration_id, limit=500)
    scoped_tasks = [
        task
        for task in tasks
        if task.metadata
        and task.metadata.project_id == project_id
        and str(task.metadata.source or "").strip().lower() == "chat_assign"
    ]
    if not scoped_tasks:
        raise HTTPException(status_code=404, detail="no assignment tasks found for this project and orchestration")

    in_progress = [task.id for task in scoped_tasks if task.status in {"queued", "blocked", "running"}]
    if in_progress:
        raise HTTPException(
            status_code=409,
            detail="assignment is still in progress; wait for all tasks to finish before applying files",
        )

    candidates = _assignment_file_candidates(scoped_tasks)
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="no file outputs were detected in the completed assignment results",
        )

    applied = _write_assignment_files(root=root, candidates=candidates, overwrite=bool(body.overwrite))
    updated_snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    await record_audit_event(
        request,
        action="projects.repo_workspace.apply_assignment",
        resource=f"project:{project_id}",
        details={
            "orchestration_id": orchestration_id,
            "file_count": len(applied),
            "overwrite": bool(body.overwrite),
        },
    )
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="apply_assignment",
        status="ok",
        details={
            "orchestration_id": orchestration_id,
            "file_count": len(applied),
            "overwrite": bool(body.overwrite),
            "files": [item.get("path") for item in applied],
        },
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "orchestration_id": orchestration_id,
        "applied_files": applied,
        "workspace": updated_snapshot,
    }


@router.post("/{project_id}/repo/workspace/clone")
async def clone_project_repo_workspace(
    project_id: str,
    request: Request,
    body: RepoWorkspaceCloneRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)

    clone_url = str(body.clone_url or "").strip() or str(cfg.get("clone_url") or "").strip()
    if not clone_url:
        settings = project.settings_overrides if isinstance(project.settings_overrides, dict) else {}
        github_cfg = settings.get("github") if isinstance(settings.get("github"), dict) else {}
        repo_full_name = str(github_cfg.get("repo_full_name") or "").strip()
        if repo_full_name:
            clone_url = f"https://github.com/{repo_full_name}.git"
    if not clone_url:
        raise HTTPException(status_code=400, detail="clone_url is required (or configure project github repo_full_name)")
    safe_clone_url = _redact_clone_url(clone_url) or clone_url

    if root.exists():
        git_dir = root / ".git"
        if git_dir.exists():
            snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
            await _record_repo_workspace_usage(
                request,
                project_id=project_id,
                action="clone",
                status="already_cloned",
                command=["git", "clone", clone_url, "<workspace>"],
                details={"clone_url": safe_clone_url},
            )
            return {
                "status": "already_cloned",
                "project_id": project_id,
                "message": "workspace already contains a git repository",
                "workspace": snapshot,
            }
        try:
            if any(root.iterdir()):
                # Managed workspaces are dedicated per project; if stale non-repo files remain,
                # reset the folder so clone can proceed without manual host-path cleanup.
                if bool(cfg.get("managed_path_mode", True)):
                    shutil.rmtree(root, ignore_errors=False)
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="workspace path exists and is not empty; choose an empty directory",
                    )
        except PermissionError:
            raise HTTPException(status_code=400, detail="workspace path cannot be accessed")
    root.parent.mkdir(parents=True, exist_ok=True)

    branch = str(body.branch or "").strip() or str(cfg.get("default_branch") or "").strip() or None
    depth = body.depth
    if depth is not None and (depth < 1 or depth > 1000):
        raise HTTPException(status_code=400, detail="depth must be between 1 and 1000")

    cmd: List[str] = ["git"]
    token = await _project_github_pat(project, key_vault)
    if token and "github.com" in clone_url.lower():
        cmd.extend(["-c", f"http.extraHeader={build_github_http_auth_header(token)}"])
    cmd.extend(["clone"])
    if depth is not None:
        cmd.extend(["--depth", str(int(depth))])
    if branch:
        cmd.extend(["--branch", branch])
    cmd.extend([clone_url, str(root)])
    usage_cmd = [part for part in cmd[:-1]] + ["<workspace>"]

    res = await _run_repo_command(cmd, cwd=root.parent, timeout_seconds=900)
    safe_res = _sanitize_repo_command_result(res, root=root)
    if not res.get("ok"):
        detail = str(res.get("stderr") or res.get("error") or "clone failed").strip() or "clone failed"
        lowered = detail.lower()
        if branch and "remote branch" in lowered and "not found" in lowered:
            detail = (
                f"{detail} (Tip: git branch names are case-sensitive. "
                "Use the exact branch name, usually 'main', or clear Default Branch.)"
            )
        await _record_repo_workspace_usage(
            request,
            project_id=project_id,
            action="clone",
            status="failed",
            command=usage_cmd,
            result=safe_res,
            details={"clone_url": safe_clone_url, "branch": branch},
        )
        raise HTTPException(status_code=400, detail=detail)

    if clone_url != cfg.get("clone_url"):
        merged_cfg = dict(cfg)
        merged_cfg["clone_url"] = clone_url
        updated = project.model_copy(
            update={"settings_overrides": _merge_settings(project, {"repo_workspace": merged_cfg})}
        )
        await project_registry.update(project_id, updated)
        cfg = merged_cfg

    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    await record_audit_event(
        request,
        action="projects.repo_workspace.clone",
        resource=f"project:{project_id}",
        details={"clone_url": safe_clone_url, "branch": branch},
    )
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="clone",
        status="ok",
        command=usage_cmd,
        result=safe_res,
        details={"clone_url": safe_clone_url, "branch": branch},
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "result": safe_res,
        "workspace": snapshot,
    }


@router.post("/{project_id}/repo/workspace/pull")
async def pull_project_repo_workspace(
    project_id: str,
    request: Request,
    body: RepoWorkspacePullRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    if not snapshot.get("is_repo"):
        raise HTTPException(status_code=400, detail="workspace is not a git repository")

    remote = str(body.remote or "").strip() or "origin"
    branch = str(body.branch or "").strip() or str(cfg.get("default_branch") or "").strip() or None
    token = await _project_github_pat(project, key_vault)
    auth_args = await _repo_auth_git_args(cwd=root, remote=remote, github_pat=token)

    cmd: List[str] = ["git", *auth_args, "pull"]
    if body.rebase:
        cmd.append("--rebase")
    cmd.append(remote)
    if branch:
        cmd.append(branch)

    res = await _run_repo_command(cmd, cwd=root, timeout_seconds=600)
    if not res.get("ok"):
        detail = str(res.get("stderr") or res.get("error") or "pull failed").strip() or "pull failed"
        await _record_repo_workspace_usage(
            request,
            project_id=project_id,
            action="pull",
            status="failed",
            command=cmd,
            result=res,
            details={"remote": remote, "branch": branch},
        )
        raise HTTPException(status_code=400, detail=detail)

    updated_snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    await record_audit_event(
        request,
        action="projects.repo_workspace.pull",
        resource=f"project:{project_id}",
        details={"remote": remote, "branch": branch},
    )
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="pull",
        status="ok",
        command=cmd,
        result=res,
        details={"remote": remote, "branch": branch},
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "result": res,
        "workspace": updated_snapshot,
    }


@router.post("/{project_id}/repo/workspace/commit")
async def commit_project_repo_workspace(
    project_id: str,
    request: Request,
    body: RepoWorkspaceCommitRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    if not snapshot.get("is_repo"):
        raise HTTPException(status_code=400, detail="workspace is not a git repository")

    message = str(body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="commit message is required")
    if len(message) > 512:
        raise HTTPException(status_code=400, detail="commit message is too long")

    usage_parts: List[Dict[str, Any]] = []
    add_res: Optional[Dict[str, Any]] = None
    if body.add_all:
        add_res = await _run_repo_command(["git", "add", "-A"], cwd=root, timeout_seconds=120)
        usage_parts.append(_result_usage(add_res))
        if not add_res.get("ok"):
            detail = str(add_res.get("stderr") or add_res.get("error") or "git add failed").strip() or "git add failed"
            await _record_repo_workspace_usage(
                request,
                project_id=project_id,
                action="commit",
                status="failed",
                command=["git", "add", "-A"],
                result=add_res,
                details={"stage": "add_all"},
                metrics=_aggregate_usage(usage_parts),
            )
            raise HTTPException(status_code=400, detail=detail)

    status_res = await _run_repo_command(["git", "status", "--porcelain"], cwd=root, timeout_seconds=20)
    usage_parts.append(_result_usage(status_res))
    if not status_res.get("ok"):
        detail = str(status_res.get("stderr") or status_res.get("error") or "git status failed").strip() or "git status failed"
        await _record_repo_workspace_usage(
            request,
            project_id=project_id,
            action="commit",
            status="failed",
            command=["git", "status", "--porcelain"],
            result=status_res,
            details={"stage": "status"},
            metrics=_aggregate_usage(usage_parts),
        )
        raise HTTPException(status_code=400, detail=detail)
    pending = [line for line in str(status_res.get("stdout") or "").splitlines() if str(line).strip()]
    if not pending:
        await _record_repo_workspace_usage(
            request,
            project_id=project_id,
            action="commit",
            status="no_changes",
            command=["git", "commit", "-m", message],
            details={"message": message, "add_all": bool(body.add_all)},
            metrics=_aggregate_usage(usage_parts),
        )
        return {
            "status": "no_changes",
            "project_id": project_id,
            "message": "no staged or unstaged changes to commit",
            "workspace": snapshot,
        }

    commit_res = await _run_repo_command(["git", "commit", "-m", message], cwd=root, timeout_seconds=120)
    usage_parts.append(_result_usage(commit_res))
    if not commit_res.get("ok"):
        detail = str(commit_res.get("stderr") or commit_res.get("error") or "commit failed").strip() or "commit failed"
        await _record_repo_workspace_usage(
            request,
            project_id=project_id,
            action="commit",
            status="failed",
            command=["git", "commit", "-m", message],
            result=commit_res,
            details={"message": message},
            metrics=_aggregate_usage(usage_parts),
        )
        raise HTTPException(status_code=400, detail=detail)

    sha_res = await _run_repo_command(["git", "rev-parse", "HEAD"], cwd=root, timeout_seconds=20)
    usage_parts.append(_result_usage(sha_res))
    commit_sha = str(sha_res.get("stdout") or "").strip() if sha_res.get("ok") else None
    updated_snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    aggregate_usage = _aggregate_usage(usage_parts)
    await record_audit_event(
        request,
        action="projects.repo_workspace.commit",
        resource=f"project:{project_id}",
        details={"message": message, "sha": commit_sha},
    )
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="commit",
        status="ok",
        command=["git", "commit", "-m", message],
        result=commit_res,
        details={"message": message, "sha": commit_sha, "add_all": bool(body.add_all)},
        metrics=aggregate_usage,
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "result": commit_res,
        "commit_sha": commit_sha,
        "usage": aggregate_usage,
        "workspace": updated_snapshot,
    }


@router.post("/{project_id}/repo/workspace/push")
async def push_project_repo_workspace(
    project_id: str,
    request: Request,
    body: RepoWorkspacePushRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    if not bool(cfg.get("allow_push", False)):
        raise HTTPException(status_code=403, detail="push is disabled by project repo workspace policy")
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
    snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    if not snapshot.get("is_repo"):
        raise HTTPException(status_code=400, detail="workspace is not a git repository")

    remote = str(body.remote or "").strip() or "origin"
    branch = str(body.branch or "").strip() or str(cfg.get("default_branch") or "").strip() or snapshot.get("branch")
    if not branch:
        raise HTTPException(status_code=400, detail="branch is required for push")

    token = await _project_github_pat(project, key_vault)
    auth_args = await _repo_auth_git_args(cwd=root, remote=remote, github_pat=token)
    cmd = ["git", *auth_args, "push", remote, str(branch)]
    push_res = await _run_repo_command(cmd, cwd=root, timeout_seconds=600)
    if not push_res.get("ok"):
        detail = str(push_res.get("stderr") or push_res.get("error") or "push failed").strip() or "push failed"
        await _record_repo_workspace_usage(
            request,
            project_id=project_id,
            action="push",
            status="failed",
            command=cmd,
            result=push_res,
            details={"remote": remote, "branch": branch},
        )
        raise HTTPException(status_code=400, detail=detail)

    updated_snapshot = await _repo_status_snapshot(root=root, cfg=cfg)
    await record_audit_event(
        request,
        action="projects.repo_workspace.push",
        resource=f"project:{project_id}",
        details={"remote": remote, "branch": branch},
    )
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="push",
        status="ok",
        command=cmd,
        result=push_res,
        details={"remote": remote, "branch": branch},
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "result": push_res,
        "workspace": updated_snapshot,
    }


@router.post("/{project_id}/repo/workspace/run")
async def run_project_repo_workspace_command(
    project_id: str,
    request: Request,
    body: RepoWorkspaceRunRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    cfg = _extract_project_repo_workspace(project)
    if not bool(cfg.get("allow_command_execution", False)):
        raise HTTPException(status_code=403, detail="command execution is disabled by project repo workspace policy")
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=True)
    if not root.exists():
        if bool(cfg.get("managed_path_mode", True)):
            root.mkdir(parents=True, exist_ok=True)
        else:
            raise HTTPException(status_code=400, detail="workspace path does not exist")

    command = _safe_command_parts(body.command)
    executable = Path(command[0]).name.lower()
    allowed = _allowed_workspace_commands()
    if executable not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"command '{executable}' is not allowed; allowed commands: {', '.join(sorted(allowed))}",
        )

    timeout = body.timeout_seconds
    if timeout is not None:
        timeout = max(1, min(int(timeout), 3600))

    execution_root = root
    temp_ctx: Optional[Dict[str, Any]] = None
    cleanup_result: Optional[Dict[str, Any]] = None
    bootstrap_results: List[Dict[str, Any]] = []
    usage_parts: List[Dict[str, Any]] = []
    main_result: Optional[Dict[str, Any]] = None
    failed_stage: Optional[str] = None

    try:
        if bool(body.use_temp_workspace):
            temp_ctx = await _prepare_temp_workspace(
                project_id=project_id,
                root=root,
                ref=(str(body.temp_ref or "").strip() or None),
            )
            execution_root = Path(temp_ctx["path"])

        if bool(body.bootstrap):
            specs = _bootstrap_command_specs(execution_root, list(body.bootstrap_languages or []))
            for spec in specs:
                bootstrap_cmd = _safe_command_parts(spec.get("command") or [])
                bootstrap_exe = Path(bootstrap_cmd[0]).name.lower()
                if bootstrap_exe not in allowed:
                    # Bootstrap steps must obey the same command allowlist.
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            f"bootstrap command '{bootstrap_exe}' is not allowed; "
                            f"allowed commands: {', '.join(sorted(allowed))}"
                        ),
                    )
                step_timeout = spec.get("timeout_seconds")
                if step_timeout is None:
                    step_timeout = timeout
                if step_timeout is not None:
                    step_timeout = max(1, min(int(step_timeout), 3600))
                step_result = await _run_repo_command(
                    bootstrap_cmd,
                    cwd=execution_root,
                    timeout_seconds=step_timeout,
                )
                usage_parts.append(_result_usage(step_result))
                bootstrap_results.append(
                    {
                        "label": str(spec.get("label") or ""),
                        "result": step_result,
                    }
                )
                if not step_result.get("ok"):
                    failed_stage = str(spec.get("label") or "bootstrap")
                    break

        if failed_stage is None:
            main_result = await _run_repo_command(
                command,
                cwd=execution_root,
                timeout_seconds=timeout,
            )
            usage_parts.append(_result_usage(main_result))
            if not main_result.get("ok"):
                failed_stage = "command"

    finally:
        if temp_ctx and not bool(body.keep_temp_workspace):
            cleanup_result = await _cleanup_temp_workspace(
                base_root=root,
                temp_root=Path(temp_ctx["path"]),
                mode=str(temp_ctx.get("mode") or "copy"),
            )

    usage_summary = _aggregate_usage(usage_parts)
    status = "ok" if failed_stage is None else "failed"
    details = {
        "command": command,
        "ok": status == "ok",
        "failed_stage": failed_stage,
        "use_temp_workspace": bool(body.use_temp_workspace),
        "temp_ref": (str(body.temp_ref or "").strip() or None),
        "bootstrap": bool(body.bootstrap),
        "bootstrap_languages": list(body.bootstrap_languages or []),
    }
    if main_result is not None:
        details["returncode"] = main_result.get("returncode")

    await record_audit_event(
        request,
        action="projects.repo_workspace.run",
        resource=f"project:{project_id}",
        details=details,
    )
    safe_main_result = _sanitize_repo_command_result(main_result if isinstance(main_result, dict) else {}, root=root)
    safe_bootstrap_results = _sanitize_workspace_value(bootstrap_results, root=root)
    safe_cleanup_result = _sanitize_workspace_value(cleanup_result, root=root) if cleanup_result is not None else None
    await _record_repo_workspace_usage(
        request,
        project_id=project_id,
        action="run",
        status=status,
        command=command,
        result=safe_main_result,
        metrics=usage_summary,
        details={
            **details,
            "bootstrap_steps": [str(step.get("label") or "") for step in bootstrap_results],
        },
    )
    return {
        "status": status,
        "project_id": project_id,
        "workspace_binding": _repo_workspace_binding(cfg),
        "temporary_workspace": bool(body.use_temp_workspace),
        "kept_temporary_workspace": bool(body.keep_temp_workspace),
        "bootstrap_results": safe_bootstrap_results,
        "result": safe_main_result,
        "usage": usage_summary,
        "cleanup": safe_cleanup_result,
    }


@router.get("/{project_id}/repo/workspace/runs")
async def list_project_repo_workspace_runs(
    project_id: str,
    request: Request,
    limit: int = 100,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    store = getattr(request.app.state, "repo_workspace_usage_store", None)
    if store is None:
        return {"project_id": project_id, "runs": []}
    project = await project_registry.get(project_id)
    cfg = _extract_project_repo_workspace(project)
    root = _resolve_repo_workspace_root(project_id, cfg, require_enabled=False)
    rows = await store.list_runs(project_id=project_id, limit=limit)
    safe_rows = [_sanitize_repo_run_row(dict(row), root=root) for row in rows if isinstance(row, dict)]
    return {"project_id": project_id, "runs": safe_rows}


@router.get("/{project_id}/repo/workspace/runs/summary")
async def summarize_project_repo_workspace_runs(
    project_id: str,
    request: Request,
    since_hours: Optional[int] = None,
) -> dict:
    project_registry = request.app.state.project_registry
    try:
        await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if since_hours is not None:
        since_hours = max(1, min(int(since_hours), 24 * 365))
    store = getattr(request.app.state, "repo_workspace_usage_store", None)
    if store is None:
        return {
            "project_id": project_id,
            "since_hours": since_hours,
            "totals": {},
            "by_action": [],
        }
    return await store.summarize(project_id=project_id, since_hours=since_hours)
