"""API for composing and registering safe, specialized bot configurations."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from control_plane.audit.utils import record_audit_event
from control_plane.bot_blueprints import (
    SpecialistBlueprintRequest,
    build_specialist_bot,
    list_specialist_blueprints,
)
from shared.exceptions import BotNotFoundError


router = APIRouter(prefix="/v1/bot-blueprints", tags=["bot-blueprints"])


@router.get("")
async def list_blueprints() -> dict:
    return {"blueprints": list_specialist_blueprints()}


@router.post("/preview")
async def preview_blueprint(payload: SpecialistBlueprintRequest) -> dict:
    try:
        bot = build_specialist_bot(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bot": bot.model_dump()}


@router.post("/create")
async def create_blueprint_bot(payload: SpecialistBlueprintRequest, request: Request) -> dict:
    try:
        bot = build_specialist_bot(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    bot_registry = request.app.state.bot_registry
    try:
        await bot_registry.get(bot.id)
    except BotNotFoundError:
        pass
    else:
        raise HTTPException(status_code=409, detail=f"Bot '{bot.id}' already exists")

    await bot_registry.register(bot)
    await record_audit_event(
        request,
        action="bot_blueprints.create",
        resource=f"bot:{bot.id}",
        details={
            "kind": payload.kind,
            "enabled": bot.enabled,
            "repo_write_granted": bot.execution_policy.repo_output_mode == "allow",
        },
    )
    return {"bot": bot.model_dump()}
