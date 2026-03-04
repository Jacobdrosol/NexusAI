from typing import List

from fastapi import APIRouter, HTTPException, Request

from shared.exceptions import ProjectNotFoundError
from shared.models import Project

router = APIRouter(prefix="/v1/projects", tags=["projects"])


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
