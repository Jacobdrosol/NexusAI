import hmac
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from control_plane.audit.utils import record_audit_event
from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from shared.exceptions import BotNotFoundError
from shared.models import Bot, BotRun, BotRunArtifact, Task, TaskMetadata
from shared.settings_manager import SettingsManager

router = APIRouter(prefix="/v1/bots", tags=["bots"])
logger = logging.getLogger(__name__)


def _settings_int(name: str, default: int) -> int:
    try:
        return int(SettingsManager.instance().get(name, default))
    except Exception:
        return default


def _settings_str(name: str, default: str) -> str:
    try:
        value = str(SettingsManager.instance().get(name, default) or "").strip()
        return value or default
    except Exception:
        return default


def _lookup_nested_path(payload: Any, path: str) -> Any:
    current: Any = payload
    for part in str(path or "").split("."):
        key = part.strip()
        if not key:
            continue
        if isinstance(current, dict):
            if key not in current:
                return None
            current = current[key]
            continue
        if isinstance(current, list):
            if not key.isdigit():
                return None
            idx = int(key)
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
            continue
        return None
    return current


def _parse_external_trigger_config(bot: Bot) -> Dict[str, Any]:
    routing = bot.routing_rules if isinstance(bot.routing_rules, dict) else {}
    raw = routing.get("external_trigger") if isinstance(routing, dict) else None
    cfg = raw if isinstance(raw, dict) else {}
    default_header = _settings_str("external_trigger_default_auth_header", "X-Nexus-Trigger-Token")
    default_source = _settings_str("external_trigger_default_source", "external_trigger")
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "require_auth": bool(cfg.get("require_auth", True)),
        "auth_header": str(cfg.get("auth_header") or default_header).strip() or default_header,
        "auth_token": str(cfg.get("auth_token") or "").strip(),
        "source": str(cfg.get("source") or default_source).strip() or default_source,
        "payload_field": str(cfg.get("payload_field") or "").strip(),
        "allow_metadata": bool(cfg.get("allow_metadata", False)),
    }


def _build_external_trigger_metadata(config: Dict[str, Any], body: Any) -> TaskMetadata:
    source = str(config.get("source") or "external_trigger").strip() or "external_trigger"
    metadata_defaults: Dict[str, Any] = {"source": source}
    if not bool(config.get("allow_metadata")) or not isinstance(body, dict):
        return TaskMetadata(**metadata_defaults)

    raw_meta = body.get("metadata")
    if not isinstance(raw_meta, dict):
        return TaskMetadata(**metadata_defaults)

    allowed_fields = {
        "user_id",
        "project_id",
        "priority",
        "conversation_id",
        "orchestration_id",
        "pipeline_name",
        "pipeline_entry_bot_id",
    }
    for key in allowed_fields:
        value = raw_meta.get(key)
        if value in (None, ""):
            continue
        metadata_defaults[key] = value
    return TaskMetadata(**metadata_defaults)


def _resolve_external_payload(config: Dict[str, Any], body: Any) -> Any:
    payload = body
    if isinstance(body, dict) and "payload" in body:
        payload = body.get("payload")
    payload_field = str(config.get("payload_field") or "").strip()
    if payload_field:
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="payload_field requires a JSON object body")
        resolved = _lookup_nested_path(body, payload_field)
        if resolved is None:
            raise HTTPException(status_code=400, detail=f"payload_field not found: {payload_field}")
        payload = resolved
    return payload


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


@router.post("/{bot_id}/trigger", response_model=Task)
async def trigger_bot_external(bot_id: str, request: Request) -> Task:
    await enforce_body_size(
        request,
        route_name="external_bot_trigger",
        default_max_bytes=max(1, _settings_int("external_trigger_max_body_bytes", 1_000_000)),
    )
    await enforce_rate_limit(
        request,
        route_name="external_bot_trigger",
        default_limit=max(1, _settings_int("external_trigger_rate_limit_count", 120)),
        default_window_seconds=max(1, _settings_int("external_trigger_rate_limit_window_seconds", 60)),
    )

    bot_registry = request.app.state.bot_registry
    task_manager = request.app.state.task_manager

    try:
        bot = await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    config = _parse_external_trigger_config(bot)
    if not config["enabled"]:
        raise HTTPException(status_code=403, detail="external trigger is disabled for this bot")

    if config["require_auth"]:
        expected = str(config.get("auth_token") or "").strip()
        if not expected:
            logger.warning("External trigger for bot %s requires auth but no auth_token is configured", bot_id)
            raise HTTPException(status_code=500, detail="external trigger is misconfigured")
        header_name = str(config.get("auth_header") or "X-Nexus-Trigger-Token").strip()
        provided = str(request.headers.get(header_name, "") or "").strip()
        if not provided:
            raise HTTPException(status_code=401, detail=f"missing auth header: {header_name}")
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid trigger auth token")

    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON body: {e}")

    payload = _resolve_external_payload(config, body)
    metadata = _build_external_trigger_metadata(config, body)
    try:
        task = await task_manager.create_task(bot_id=bot_id, payload=payload, metadata=metadata)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    await record_audit_event(
        request,
        action="bots.external_trigger",
        resource=f"bot:{bot_id}",
        details={
            "task_id": task.id,
            "source": metadata.source,
        },
    )
    return task


@router.get("/{bot_id}/runs", response_model=List[BotRun])
async def list_bot_runs(
    bot_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> List[BotRun]:
    bot_registry = request.app.state.bot_registry
    task_manager = request.app.state.task_manager
    try:
        await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await task_manager.list_bot_runs(bot_id=bot_id, limit=limit)


@router.get("/{bot_id}/artifacts", response_model=List[BotRunArtifact])
async def list_bot_artifacts(
    bot_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=300),
    task_id: str | None = Query(default=None),
    include_content: bool = Query(default=False),
) -> List[BotRunArtifact]:
    bot_registry = request.app.state.bot_registry
    task_manager = request.app.state.task_manager
    try:
        await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return await task_manager.list_bot_run_artifacts(
        bot_id=bot_id,
        limit=limit,
        task_id=task_id,
        include_content=include_content,
    )


@router.get("/{bot_id}/artifacts/{artifact_id}", response_model=BotRunArtifact)
async def get_bot_artifact(
    bot_id: str,
    artifact_id: str,
    request: Request,
) -> BotRunArtifact:
    bot_registry = request.app.state.bot_registry
    task_manager = request.app.state.task_manager
    try:
        await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        return await task_manager.get_bot_run_artifact(bot_id=bot_id, artifact_id=artifact_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
