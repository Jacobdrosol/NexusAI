from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
from control_plane.connection_action_approvals import connection_action_key
from shared.bot_policy import bot_execution_policy
from shared.connection_runtime import resolve_declared_http_action
from shared.exceptions import BotNotFoundError

router = APIRouter(prefix="/v1/connection-action-approvals", tags=["connection-action-approvals"])


class CreateConnectionActionApprovalRequest(BaseModel):
    bot_id: str = Field(min_length=1, max_length=200)
    payload: Dict[str, Any]
    expires_in_seconds: int = Field(default=300, ge=30, le=900)


@router.post("", status_code=201)
async def create_connection_action_approval(
    request: Request,
    body: CreateConnectionActionApprovalRequest,
) -> Dict[str, Any]:
    try:
        bot = await request.app.state.bot_registry.get(body.bot_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    connection_ref = body.payload.get("connection") if isinstance(body.payload.get("connection"), dict) else {}
    connection = request.app.state.connection_resolver.find_bot_connection(
        bot.id,
        requested_name=str(connection_ref.get("name") or body.payload.get("connection_name") or "").strip() or None,
        requested_id=str(connection_ref.get("id") or body.payload.get("connection_id") or "").strip() or None,
    )
    if connection is None or str(connection.get("kind") or "").strip().lower() != "http":
        raise HTTPException(status_code=409, detail="The requested bot HTTP connection is unavailable")

    action = body.payload.get("connection_action")
    if not isinstance(action, dict) or isinstance(body.payload.get("connection_actions"), (dict, list)):
        raise HTTPException(status_code=400, detail="Approval requests require exactly one connection_action object")
    declared_action = resolve_declared_http_action(str(connection.get("schema_text") or ""), action)
    if declared_action is None:
        raise HTTPException(status_code=400, detail="The connection action is not declared in the attached OpenAPI schema")
    if declared_action["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(status_code=409, detail="Owner approvals are only issued for connection mutations")

    action_key = connection_action_key(connection.get("name"), {"operation_id": declared_action["operation_id"]})
    policy = bot_execution_policy(bot)
    if action_key not in policy.connection_action_allowlist:
        raise HTTPException(status_code=409, detail=f"Bot {bot.id} is not authorized for {action_key}")
    if action_key not in policy.connection_action_owner_approval_required:
        raise HTTPException(
            status_code=409,
            detail=f"Bot {bot.id} does not require an owner approval for {action_key}",
        )

    store = getattr(request.app.state, "connection_action_approval_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Connection action approval service is unavailable")
    try:
        approval = await store.create(
            bot_id=bot.id,
            action_key=action_key,
            payload=body.payload,
            expires_in_seconds=body.expires_in_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await record_audit_event(
        request,
        action="connection_action_approvals.create",
        resource=f"bot:{bot.id}",
        details={
            "approval_id": approval["id"],
            "action_key": action_key,
            "expires_at": approval["expires_at"],
        },
    )
    return {
        "approval_id": approval["id"],
        "bot_id": approval["bot_id"],
        "action_key": approval["action_key"],
        "expires_at": approval["expires_at"],
    }
