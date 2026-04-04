from typing import List

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from control_plane.audit.utils import record_audit_event
from shared.exceptions import CatalogModelNotFoundError
from shared.models import CatalogModel

router = APIRouter(prefix="/v1/models", tags=["models"])


class OllamaCloudPullRequest(BaseModel):
    model: str


class OllamaCloudPullResponse(BaseModel):
    model: str
    status: str
    message: str


@router.post("/ollama-cloud/pull", response_model=OllamaCloudPullResponse)
async def pull_ollama_cloud_model(request: Request, body: OllamaCloudPullRequest) -> OllamaCloudPullResponse:
    """Trigger an Ollama Cloud pull for the specified model ID.

    Calls POST /api/pull on the configured OLLAMA_CLOUD_BASE_URL endpoint using
    the stored Ollama Cloud API key. Returns immediately once the pull completes.
    Use this from the model catalog UI after adding a new ollama_cloud model.
    """
    import os

    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")

    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        raise HTTPException(status_code=503, detail="Scheduler not available")

    api_key = await scheduler._resolve_api_key("Ollama_Cloud1", "OLLAMA_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="Ollama Cloud API key not configured")

    base_url = os.environ.get("OLLAMA_CLOUD_BASE_URL", "https://ollama.com/api").rstrip("/")

    try:
        await scheduler._pull_ollama_cloud_model(base_url, api_key, model)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    await record_audit_event(request, action="models.ollama_cloud_pull", resource=f"model:{model}")
    return OllamaCloudPullResponse(model=model, status="ok", message=f"Model '{model}' pulled successfully")


@router.post("", response_model=CatalogModel)
async def create_model(request: Request, model: CatalogModel) -> CatalogModel:
    model_registry = request.app.state.model_registry
    await model_registry.register(model)
    await record_audit_event(request, action="models.create", resource=f"model:{model.id}")
    return model


@router.get("", response_model=List[CatalogModel])
async def list_models(request: Request) -> List[CatalogModel]:
    model_registry = request.app.state.model_registry
    return await model_registry.list()


@router.get("/{model_id}", response_model=CatalogModel)
async def get_model(model_id: str, request: Request) -> CatalogModel:
    model_registry = request.app.state.model_registry
    try:
        return await model_registry.get(model_id)
    except CatalogModelNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{model_id}", response_model=CatalogModel)
async def update_model(model_id: str, request: Request, model: CatalogModel) -> CatalogModel:
    model_registry = request.app.state.model_registry
    try:
        await model_registry.update(model_id, model)
        await record_audit_event(request, action="models.update", resource=f"model:{model_id}")
        return model
    except CatalogModelNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{model_id}")
async def delete_model(model_id: str, request: Request) -> dict:
    model_registry = request.app.state.model_registry
    try:
        await model_registry.remove(model_id)
        await record_audit_event(request, action="models.delete", resource=f"model:{model_id}")
        return {"message": f"Catalog model {model_id} removed"}
    except CatalogModelNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
