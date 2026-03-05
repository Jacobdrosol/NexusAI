from typing import List

from fastapi import APIRouter, HTTPException, Request

from control_plane.audit.utils import record_audit_event
from shared.exceptions import BotNotFoundError
from shared.models import Bot

router = APIRouter(prefix="/v1/bots", tags=["bots"])


@router.post("", response_model=Bot)
async def create_bot(request: Request, bot: Bot) -> Bot:
    bot_registry = request.app.state.bot_registry
    await bot_registry.register(bot)
    await record_audit_event(request, action="bots.create", resource=f"bot:{bot.id}")
    return bot


@router.get("", response_model=List[Bot])
async def list_bots(request: Request) -> List[Bot]:
    bot_registry = request.app.state.bot_registry
    return await bot_registry.list()


@router.get("/{bot_id}", response_model=Bot)
async def get_bot(bot_id: str, request: Request) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        return await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{bot_id}", response_model=Bot)
async def update_bot(bot_id: str, request: Request, bot: Bot) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        await bot_registry.update(bot_id, bot)
        await record_audit_event(request, action="bots.update", resource=f"bot:{bot_id}")
        return bot
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str, request: Request) -> dict:
    bot_registry = request.app.state.bot_registry
    try:
        await bot_registry.remove(bot_id)
        await record_audit_event(request, action="bots.delete", resource=f"bot:{bot_id}")
        return {"message": f"Bot {bot_id} removed"}
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{bot_id}/enable", response_model=Bot)
async def enable_bot(bot_id: str, request: Request) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        await bot_registry.enable(bot_id)
        await record_audit_event(request, action="bots.enable", resource=f"bot:{bot_id}")
        return await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{bot_id}/disable", response_model=Bot)
async def disable_bot(bot_id: str, request: Request) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        await bot_registry.disable(bot_id)
        await record_audit_event(request, action="bots.disable", resource=f"bot:{bot_id}")
        return await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
