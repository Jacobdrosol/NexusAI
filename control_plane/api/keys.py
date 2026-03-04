from typing import List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from shared.exceptions import APIKeyNotFoundError

router = APIRouter(prefix="/v1/keys", tags=["keys"])


class UpsertAPIKeyRequest(BaseModel):
    name: str
    provider: str
    value: str


@router.post("")
async def upsert_key(request: Request, body: UpsertAPIKeyRequest) -> dict:
    key_vault = request.app.state.key_vault
    try:
        await key_vault.set_key(body.name, body.provider, body.value)
        return {"status": "ok"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("")
async def list_keys(request: Request) -> List[dict]:
    key_vault = request.app.state.key_vault
    return await key_vault.list_keys()


@router.get("/{name}")
async def get_key(request: Request, name: str) -> dict:
    key_vault = request.app.state.key_vault
    try:
        return await key_vault.get_key(name)
    except APIKeyNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{name}")
async def delete_key(request: Request, name: str) -> dict:
    key_vault = request.app.state.key_vault
    try:
        await key_vault.delete_key(name)
        return {"status": "ok"}
    except APIKeyNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
