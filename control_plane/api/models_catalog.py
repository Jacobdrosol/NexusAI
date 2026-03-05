from typing import List

from fastapi import APIRouter, HTTPException, Request

from control_plane.audit.utils import record_audit_event
from shared.exceptions import CatalogModelNotFoundError
from shared.models import CatalogModel

router = APIRouter(prefix="/v1/models", tags=["models"])


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
