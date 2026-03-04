from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from shared.exceptions import VaultItemNotFoundError
from shared.models import VaultChunk, VaultItem

router = APIRouter(prefix="/v1/vault", tags=["vault"])


class IngestVaultItemRequest(BaseModel):
    title: str
    content: str
    namespace: str = "global"
    project_id: Optional[str] = None
    source_type: str = "text"
    source_ref: Optional[str] = None
    metadata: Optional[Any] = None
    chunk_size: int = 1000
    chunk_overlap: int = 150


class VaultSearchRequest(BaseModel):
    query: str
    namespace: Optional[str] = None
    project_id: Optional[str] = None
    limit: int = 5


@router.post("/items", response_model=VaultItem)
async def ingest_item(request: Request, body: IngestVaultItemRequest) -> VaultItem:
    vault_manager = request.app.state.vault_manager
    try:
        return await vault_manager.ingest_text(
            title=body.title,
            content=body.content,
            namespace=body.namespace,
            project_id=body.project_id,
            source_type=body.source_type,
            source_ref=body.source_ref,
            metadata=body.metadata,
            chunk_size=body.chunk_size,
            chunk_overlap=body.chunk_overlap,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/items", response_model=List[VaultItem])
async def list_items(
    request: Request,
    namespace: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> List[VaultItem]:
    vault_manager = request.app.state.vault_manager
    return await vault_manager.list_items(namespace=namespace, project_id=project_id, limit=limit)


@router.get("/items/{item_id}", response_model=VaultItem)
async def get_item(item_id: str, request: Request) -> VaultItem:
    vault_manager = request.app.state.vault_manager
    try:
        return await vault_manager.get_item(item_id)
    except VaultItemNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/items/{item_id}/chunks", response_model=List[VaultChunk])
async def get_item_chunks(item_id: str, request: Request) -> List[VaultChunk]:
    vault_manager = request.app.state.vault_manager
    try:
        return await vault_manager.list_chunks(item_id)
    except VaultItemNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/search")
async def search_vault(request: Request, body: VaultSearchRequest) -> List[Dict[str, Any]]:
    vault_manager = request.app.state.vault_manager
    return await vault_manager.search(
        query=body.query,
        namespace=body.namespace,
        project_id=body.project_id,
        limit=body.limit,
    )


@router.post("/context")
async def pull_context(request: Request, body: VaultSearchRequest) -> Dict[str, Any]:
    mcp_broker = request.app.state.mcp_broker
    return await mcp_broker.pull_context(
        query=body.query,
        namespace=body.namespace,
        project_id=body.project_id,
        limit=body.limit,
    )
