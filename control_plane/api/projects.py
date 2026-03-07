from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import base64
import hashlib
import hmac
import os
from typing import Any, Dict, List, Optional, Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.audit.utils import record_audit_event
from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from shared.exceptions import APIKeyNotFoundError, ProjectNotFoundError
from shared.models import Project, TaskMetadata

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
    sync_scope: Literal["sample", "full"] = "sample"
    max_files: int = 25
    include_repo_files: bool = True
    include_commits: bool = False
    include_pull_requests: bool = False
    include_issues: bool = False
    include_conversations: bool = False
    max_commits: int = 25
    max_pull_requests: int = 15
    max_issues: int = 15
    max_conversation_comments: int = 50
    namespace: Optional[str] = None


class ConfigurePRReviewRequest(BaseModel):
    enabled: bool = True
    bot_id: Optional[str] = None


class UpdateCloudContextPolicyRequest(BaseModel):
    provider_policies: Dict[str, str] = Field(default_factory=dict)
    bot_overrides: Dict[str, Dict[str, str]] = Field(default_factory=dict)


_CLOUD_POLICY_VALUES = {"allow", "redact", "block"}
_SUPPORTED_CLOUD_PROVIDERS = {"openai", "claude", "gemini"}


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
    max_files: int,
    sync_scope: str = "sample",
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
        if sync_scope == "full":
            file_limit = max(1, min(max_files, 5000))
        else:
            file_limit = max(1, min(max_files, 200))
        for item in candidates[:file_limit]:
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


async def _fetch_repo_commits(
    token: str,
    repo_full_name: str,
    branch: Optional[str],
    max_commits: int,
) -> Dict[str, Any]:
    headers = _github_headers(token)
    commit_limit = max(1, min(max_commits, 250))
    async with httpx.AsyncClient(timeout=30.0) as client:
        repo_resp = await client.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers)
        repo_resp.raise_for_status()
        repo_data = repo_resp.json()
        ref = branch or str(repo_data.get("default_branch") or "main")
        commits_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/commits",
            headers=headers,
            params={"sha": ref, "per_page": commit_limit},
        )
        commits_resp.raise_for_status()
        rows = commits_resp.json()
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
    comments_resp = await client.get(
        f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments",
        headers=headers,
        params={"per_page": max(1, min(max_comments, 100))},
    )
    comments_resp.raise_for_status()
    rows = comments_resp.json()
    comments: List[Dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
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
    comments_resp = await client.get(
        f"https://api.github.com/repos/{repo_full_name}/pulls/{pull_number}/comments",
        headers=headers,
        params={"per_page": max(1, min(max_comments, 100))},
    )
    comments_resp.raise_for_status()
    rows = comments_resp.json()
    comments: List[Dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
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
    max_pull_requests: int,
    include_conversations: bool,
    max_comments: int,
) -> List[Dict[str, Any]]:
    headers = _github_headers(token)
    pr_limit = max(1, min(max_pull_requests, 100))
    async with httpx.AsyncClient(timeout=30.0) as client:
        prs_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls",
            headers=headers,
            params={"state": "all", "sort": "updated", "direction": "desc", "per_page": pr_limit},
        )
        prs_resp.raise_for_status()
        rows = prs_resp.json()
        pulls: List[Dict[str, Any]] = []
        if not isinstance(rows, list):
            return pulls
        for row in rows:
            if not isinstance(row, dict):
                continue
            pr_number = int(row.get("number") or 0)
            issue_comments: List[Dict[str, Any]] = []
            review_comments: List[Dict[str, Any]] = []
            if include_conversations and pr_number > 0:
                issue_comments = await _fetch_issue_comments(
                    client, headers, repo_full_name, pr_number, max_comments
                )
                review_comments = await _fetch_pr_review_comments(
                    client, headers, repo_full_name, pr_number, max_comments
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
    max_issues: int,
    include_conversations: bool,
    max_comments: int,
) -> List[Dict[str, Any]]:
    headers = _github_headers(token)
    issue_limit = max(1, min(max_issues, 100))
    async with httpx.AsyncClient(timeout=30.0) as client:
        issues_resp = await client.get(
            f"https://api.github.com/repos/{repo_full_name}/issues",
            headers=headers,
            params={"state": "all", "sort": "updated", "direction": "desc", "per_page": issue_limit},
        )
        issues_resp.raise_for_status()
        rows = issues_resp.json()
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
                    client, headers, repo_full_name, issue_number, max_comments
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


def _verify_github_signature(secret: str, raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    provided = signature_header.split("=", 1)[1].strip()
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


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


@router.post("/{project_id}/github/context/sync")
async def sync_github_repo_context(
    project_id: str,
    request: Request,
    body: SyncGitHubContextRequest,
) -> dict:
    project_registry = request.app.state.project_registry
    key_vault = request.app.state.key_vault
    vault_manager = request.app.state.vault_manager
    try:
        project = await project_registry.get(project_id)
    except ProjectNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

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

    sync_scope = (body.sync_scope or "sample").strip().lower()
    if sync_scope not in {"sample", "full"}:
        raise HTTPException(status_code=400, detail="sync_scope must be sample or full")

    namespace = (body.namespace or f"project:{project_id}:repo").strip() or f"project:{project_id}:repo"
    ingested = []
    counts = {
        "files": 0,
        "commits": 0,
        "pull_requests": 0,
        "issues": 0,
        "conversations": 0,
    }
    warnings: List[str] = []

    repo_full_name_str = str(repo_full_name)
    branch_name = body.branch
    repo_file_result = None
    if body.include_repo_files:
        try:
            repo_file_result = await _fetch_repo_context_files(
                token=token,
                repo_full_name=repo_full_name_str,
                branch=branch_name,
                max_files=body.max_files,
                sync_scope=sync_scope,
            )
            branch_name = repo_file_result["branch"]
            for file in repo_file_result["files"]:
                item = await vault_manager.ingest_text(
                    title=f"{repo_file_result['repo_full_name']}:{file['path']}",
                    content=file["content"],
                    namespace=namespace,
                    project_id=project_id,
                    source_type="file",
                    source_ref=f"github://{repo_file_result['repo_full_name']}/{file['path']}",
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
        except Exception as e:
            if not any(
                [
                    body.include_commits,
                    body.include_pull_requests,
                    body.include_issues,
                ]
            ):
                raise HTTPException(status_code=400, detail=f"GitHub context sync failed: {e}")
            warnings.append(f"repo files failed: {e}")

    if body.include_commits:
        try:
            commit_result = await _fetch_repo_commits(
                token=token,
                repo_full_name=repo_full_name_str,
                branch=branch_name,
                max_commits=body.max_commits,
            )
            branch_name = commit_result["branch"]
            for commit in commit_result["commits"]:
                item = await vault_manager.ingest_text(
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

    if body.include_pull_requests:
        try:
            pulls = await _fetch_repo_pull_requests(
                token=token,
                repo_full_name=repo_full_name_str,
                max_pull_requests=body.max_pull_requests,
                include_conversations=body.include_conversations,
                max_comments=body.max_conversation_comments,
            )
            for pr in pulls:
                item = await vault_manager.ingest_text(
                    title=f"{repo_full_name_str}:pr:{pr.get('number')}",
                    content=_build_pull_request_text(
                        repo_full_name_str,
                        pr,
                        include_conversations=body.include_conversations,
                    ),
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
                        "ingest_kind": "pull_request",
                    },
                )
                ingested.append({"item_id": item.id, "type": "pull_request", "number": pr.get("number")})
                counts["pull_requests"] += 1
                counts["conversations"] += len(pr.get("issue_comments") or []) + len(pr.get("review_comments") or [])
        except Exception as e:
            warnings.append(f"pull requests failed: {e}")

    if body.include_issues:
        try:
            issues = await _fetch_repo_issues(
                token=token,
                repo_full_name=repo_full_name_str,
                max_issues=body.max_issues,
                include_conversations=body.include_conversations,
                max_comments=body.max_conversation_comments,
            )
            for issue in issues:
                item = await vault_manager.ingest_text(
                    title=f"{repo_full_name_str}:issue:{issue.get('number')}",
                    content=_build_issue_text(
                        repo_full_name_str,
                        issue,
                        include_conversations=body.include_conversations,
                    ),
                    namespace=namespace,
                    project_id=project_id,
                    source_type="custom",
                    source_ref=f"github://{repo_full_name_str}/issues/{issue.get('number')}",
                    metadata={
                        "provider": "github",
                        "repo_full_name": repo_full_name_str,
                        "number": issue.get("number"),
                        "state": issue.get("state"),
                        "ingest_kind": "issue",
                    },
                )
                ingested.append({"item_id": item.id, "type": "issue", "number": issue.get("number")})
                counts["issues"] += 1
                counts["conversations"] += len(issue.get("comments") or [])
        except Exception as e:
            warnings.append(f"issues failed: {e}")

    await record_audit_event(
        request,
        action="projects.github.context.sync",
        resource=f"project:{project_id}",
        details={
            "repo_full_name": repo_full_name_str,
            "branch": branch_name,
            "ingested_count": len(ingested),
            "namespace": namespace,
            "sync_scope": sync_scope,
            "counts": counts,
            "warnings": warnings,
        },
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "repo_full_name": repo_full_name_str,
        "branch": branch_name,
        "namespace": namespace,
        "ingested_count": len(ingested),
        "ingested": ingested,
        "sync_scope": sync_scope,
        "counts": counts,
        "warnings": warnings,
    }


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
