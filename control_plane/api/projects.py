from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from shared.exceptions import APIKeyNotFoundError, ProjectNotFoundError
from shared.models import Project

router = APIRouter(prefix="/v1/projects", tags=["projects"])


class ConnectGitHubPATRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    token: str
    repo_full_name: Optional[str] = None
    validate_token: bool = Field(default=True, alias="validate")


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
    return {"status": "disconnected", "project_id": project_id}
