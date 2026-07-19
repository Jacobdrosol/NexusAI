"""API for composing and registering safe, specialized bot configurations."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from control_plane.audit.utils import record_audit_event
from control_plane.bot_readiness import assess_bot_instance_readiness
from control_plane.bot_blueprints import (
    SpecialistBlueprintRequest,
    build_specialist_bot,
    list_specialist_blueprints,
)
from shared.exceptions import BotNotFoundError, ProjectNotFoundError
from shared.models import Bot


router = APIRouter(prefix="/v1/bot-blueprints", tags=["bot-blueprints"])


async def _require_specialist_ready_to_enable(bot: Bot, request: Request) -> None:
    """Apply the same dispatch gate used by the normal bot creation route."""
    readiness = await assess_bot_instance_readiness(
        bot,
        worker_registry=request.app.state.worker_registry,
        connection_resolver=request.app.state.connection_resolver,
        worker_probe_store=request.app.state.worker_probe_store,
        key_vault=request.app.state.key_vault,
        model_registry=request.app.state.model_registry,
    )
    if not readiness["ready"]:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "bot_not_ready",
                "message": f"Bot '{bot.id}' cannot be enabled until its dispatch checks pass.",
                "readiness": readiness,
            },
        )


async def _require_specialist_project_available(bot: Bot, request: Request) -> None:
    """Keep blueprint-created specialists bound only to an enabled, known project."""
    project_id = str(bot.project_id or "").strip()
    if not project_id:
        return
    try:
        project = await request.app.state.project_registry.get(project_id)
    except ProjectNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "bot_project_not_found",
                "message": f"Bot project '{project_id}' does not exist.",
            },
        ) from exc
    if not bool(project.enabled):
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "bot_project_disabled",
                "message": f"Bot project '{project_id}' is disabled.",
            },
        )


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

    await _require_specialist_project_available(bot, request)
    if bot.enabled:
        await _require_specialist_ready_to_enable(bot, request)

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
