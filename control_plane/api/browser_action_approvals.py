from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from control_plane.audit.utils import record_audit_event
from control_plane.browser_action_approvals import browser_action_key
from shared.bot_policy import bot_execution_policy
from shared.exceptions import BotNotFoundError

router = APIRouter(prefix="/v1/browser-action-approvals", tags=["browser-action-approvals"])


class CreateBrowserActionApprovalRequest(BaseModel):
    bot_id: str = Field(min_length=1, max_length=200)
    payload: Dict[str, Any]
    expires_in_seconds: int = Field(default=300, ge=30, le=900)


@router.post("", status_code=201)
async def create_browser_action_approval(
    request: Request,
    body: CreateBrowserActionApprovalRequest,
) -> Dict[str, Any]:
    try:
        bot = await request.app.state.bot_registry.get(body.bot_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        action_key = browser_action_key(body.payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    policy = bot_execution_policy(bot)
    if action_key not in policy.browser_action_allowlist:
        raise HTTPException(status_code=409, detail=f"Bot {bot.id} is not authorized for {action_key}")
    if action_key not in policy.browser_action_owner_approval_required:
        raise HTTPException(
            status_code=409,
            detail=f"Bot {bot.id} does not require an owner approval for {action_key}",
        )

    store = getattr(request.app.state, "browser_action_approval_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Browser action approval service is unavailable")
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
        action="browser_action_approvals.create",
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
