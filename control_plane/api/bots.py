import hmac
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import ValidationError

from control_plane.audit.utils import record_audit_event
from control_plane.bot_readiness import assess_bot_instance_readiness
from control_plane.security.guards import enforce_body_size, enforce_rate_limit
from shared.exceptions import APIKeyNotFoundError, BotNotFoundError, ProjectNotFoundError
from shared.bot_policy import bot_autonomous_dispatch_blockers, validate_bot_configuration
from shared.models import Bot, BotRun, BotRunArtifact, Task, TaskMetadata
from shared.settings_manager import SettingsManager

router = APIRouter(prefix="/v1/bots", tags=["bots"])
logger = logging.getLogger(__name__)


def _bot_validation_detail(
    *,
    reason_code: str,
    message: str,
    validation_errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "reason_code": reason_code,
        "message": message,
        "validation_errors": validation_errors,
    }


def _raise_bot_validation_error(
    *,
    reason_code: str,
    message: str,
    validation_errors: List[Dict[str, Any]],
    status_code: int = 400,
) -> None:
    detail = _bot_validation_detail(
        reason_code=reason_code,
        message=message,
        validation_errors=validation_errors,
    )
    logger.warning(
        "Bot validation failed: reason_code=%s status=%s validation_errors=%s",
        reason_code,
        status_code,
        validation_errors,
    )
    raise HTTPException(status_code=status_code, detail=detail)


def _schema_validation_errors(payload: Any, exc: ValidationError) -> List[Dict[str, Any]]:
    validation_errors: List[Dict[str, Any]] = []
    for item in exc.errors():
        loc = [str(part) for part in item.get("loc") or [] if str(part)]
        field_path = ".".join(loc)
        invalid_value = _lookup_nested_path(payload, field_path) if field_path else payload
        validation_errors.append(
            {
                "field_path": field_path,
                "message": str(item.get("msg") or "Invalid value"),
                "invalid_value": invalid_value,
                "error_type": str(item.get("type") or "").strip() or None,
            }
        )
    return validation_errors


def _policy_validation_errors(errors: List[str]) -> List[Dict[str, Any]]:
    validation_errors: List[Dict[str, Any]] = []
    for error in errors:
        field_path = _infer_policy_field_path(error)
        validation_errors.append(
            {
                "field_path": field_path,
                "message": error,
                "invalid_value": None,
            }
        )
    return validation_errors


def _infer_policy_field_path(error: str) -> str:
    error_lower = error.lower()
    if "workflow.reference_graph.graph_id" in error_lower:
        return "workflow.reference_graph.graph_id"
    if "workflow.reference_graph.current_bot_id" in error_lower:
        return "workflow.reference_graph.current_bot_id"
    if "workflow.reference_graph.entry_bot_id" in error_lower:
        return "workflow.reference_graph.entry_bot_id"
    if "reference graph" in error_lower:
        if "node" in error_lower:
            return "workflow.reference_graph.nodes"
        if "edge" in error_lower:
            return "workflow.reference_graph.edges"
        return "workflow.reference_graph"
    if "project manager" in error_lower and "workflow triggers" in error_lower:
        return "workflow.triggers"
    return ""


def _parse_bot_payload_or_400(payload: Any) -> Bot:
    if not isinstance(payload, dict):
        _raise_bot_validation_error(
            reason_code="bot_validation_failed",
            message="Bot payload must be a JSON object.",
            validation_errors=[{"field_path": "", "message": "Expected an object body.", "invalid_value": payload}],
        )
    try:
        return Bot.model_validate(payload)
    except ValidationError as exc:
        _raise_bot_validation_error(
            reason_code="bot_validation_failed",
            message="Bot payload failed schema validation.",
            validation_errors=_schema_validation_errors(payload, exc),
        )


def _validate_bot_or_400(bot: Bot) -> None:
    errors = validate_bot_configuration(bot)
    if errors:
        _raise_bot_validation_error(
            reason_code="bot_validation_failed",
            message="Bot payload failed workflow validation.",
            validation_errors=_policy_validation_errors(errors),
        )


def _bot_dependency_detail(
    *,
    bot_id: str,
    schedule_references: List[Dict[str, Any]],
    workflow_references: List[Dict[str, Any]],
) -> Dict[str, Any]:
    active_schedules = [
        reference
        for reference in schedule_references
        if str(reference.get("status") or "").strip().lower() == "active"
    ]
    enabled_trigger_references = [
        reference
        for reference in workflow_references
        if reference.get("relation") == "workflow_trigger"
        and bool(reference.get("source_bot_enabled"))
        and bool(reference.get("trigger_enabled"))
    ]
    return {
        "bot_id": bot_id,
        "schedule_references": schedule_references,
        "workflow_references": workflow_references,
        "active_schedule_ids": [str(reference["id"]) for reference in active_schedules],
        "enabled_trigger_source_ids": [
            str(reference["source_bot_id"])
            for reference in enabled_trigger_references
        ],
        "can_disable": not active_schedules and not enabled_trigger_references,
        "can_delete": not schedule_references and not workflow_references,
    }


async def _bot_dependencies(request: Request, bot_id: str) -> Dict[str, Any]:
    """Return schedule and workflow references that would outlive a bot mutation."""
    safe_bot_id = str(bot_id or "").strip()
    schedules = await request.app.state.agent_schedule_engine.list_schedules(limit=500)
    schedule_references: List[Dict[str, Any]] = []
    for schedule in schedules:
        for relation, field_name in (
            ("target_bot", "target_bot_id"),
            ("assignment_pm", "assignment_pm_bot_id"),
        ):
            if str(schedule.get(field_name) or "").strip() != safe_bot_id:
                continue
            schedule_references.append(
                {
                    "id": str(schedule.get("id") or ""),
                    "name": str(schedule.get("name") or ""),
                    "status": str(schedule.get("status") or ""),
                    "relation": relation,
                    "project_id": str(schedule.get("project_id") or "") or None,
                }
            )

    workflow_references: List[Dict[str, Any]] = []
    for source_bot in await request.app.state.bot_registry.list():
        if str(source_bot.id or "").strip() == safe_bot_id:
            continue
        workflow = source_bot.workflow
        if workflow is None:
            continue
        source = {
            "source_bot_id": str(source_bot.id or ""),
            "source_bot_name": str(source_bot.name or ""),
            "source_bot_enabled": bool(source_bot.enabled),
        }
        for trigger in workflow.triggers or []:
            if str(trigger.target_bot_id or "").strip() != safe_bot_id:
                continue
            workflow_references.append(
                {
                    **source,
                    "relation": "workflow_trigger",
                    "trigger_id": str(trigger.id or ""),
                    "trigger_enabled": bool(trigger.enabled),
                }
            )
        graph = workflow.reference_graph
        if graph is None:
            continue
        for node in graph.nodes or []:
            if str(node.bot_id or "").strip() != safe_bot_id:
                continue
            workflow_references.append(
                {
                    **source,
                    "relation": "reference_graph_node",
                    "trigger_id": None,
                    "trigger_enabled": None,
                }
            )
        for edge in graph.edges or []:
            source_id = str(edge.source_bot_id or "").strip()
            target_id = str(edge.target_bot_id or "").strip()
            if safe_bot_id not in {source_id, target_id}:
                continue
            workflow_references.append(
                {
                    **source,
                    "relation": "reference_graph_edge",
                    "trigger_id": None,
                    "trigger_enabled": None,
                    "edge": {"source_bot_id": source_id, "target_bot_id": target_id},
                }
            )
    return _bot_dependency_detail(
        bot_id=safe_bot_id,
        schedule_references=schedule_references,
        workflow_references=workflow_references,
    )


def _raise_bot_dependency_error(*, reason_code: str, message: str, dependencies: Dict[str, Any]) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "reason_code": reason_code,
            "message": message,
            "dependencies": dependencies,
        },
    )


async def _require_known_enabled_bot_project(bot: Bot, request: Request) -> None:
    """Reject bindings to missing or disabled projects before persisting a bot."""
    project_id = str(bot.project_id or "").strip()
    if not project_id:
        return
    try:
        project = await request.app.state.project_registry.get(project_id)
    except ProjectNotFoundError:
        _raise_bot_validation_error(
            reason_code="bot_project_not_found",
            message=f"Bot project '{project_id}' does not exist.",
            validation_errors=[
                {
                    "field_path": "project_id",
                    "message": "Project binding must reference an existing project.",
                    "invalid_value": project_id,
                }
            ],
            status_code=409,
        )
    if not bool(project.enabled):
        _raise_bot_validation_error(
            reason_code="bot_project_disabled",
            message=f"Bot project '{project_id}' is disabled.",
            validation_errors=[
                {
                    "field_path": "project_id",
                    "message": "Bots cannot be bound to a disabled project.",
                    "invalid_value": project_id,
                }
            ],
            status_code=409,
        )


async def _require_bot_ready_to_enable(bot: Bot, request: Request) -> None:
    """Block activation when the staged bot cannot dispatch its declared backends."""
    await _require_known_enabled_bot_project(bot, request)
    candidate = bot.model_copy(update={"enabled": True})
    readiness = await assess_bot_instance_readiness(
        candidate,
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


async def _preflight_bot_payload(payload: Any, request: Request) -> Dict[str, Any]:
    """Validate a staged bot without registering, enabling, or dispatching it."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Bot payload must be a JSON object.")
    try:
        bot = Bot.model_validate(payload)
    except ValidationError as exc:
        validation_errors = [
            {
                "field_path": ".".join(str(part) for part in item.get("loc") or []),
                "message": str(item.get("msg") or "Invalid value"),
                "error_type": str(item.get("type") or "").strip() or None,
            }
            for item in exc.errors()
        ]
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "bot_validation_failed",
                "message": "Bot payload failed schema validation.",
                "validation_errors": validation_errors,
            },
        ) from exc

    policy_errors = validate_bot_configuration(bot)
    if policy_errors:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": "bot_validation_failed",
                "message": "Bot payload failed workflow validation.",
                "validation_errors": _policy_validation_errors(policy_errors),
            },
        )
    readiness = await assess_bot_instance_readiness(
        bot.model_copy(update={"enabled": True}),
        worker_registry=request.app.state.worker_registry,
        connection_resolver=request.app.state.connection_resolver,
        worker_probe_store=request.app.state.worker_probe_store,
        key_vault=request.app.state.key_vault,
        model_registry=request.app.state.model_registry,
    )
    return {
        "bot_id": bot.id,
        "candidate_enabled": bool(bot.enabled),
        "ready_to_enable": bool(readiness.get("ready")),
        "readiness": readiness,
    }


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
        "autonomy_safe": bool(cfg.get("autonomy_safe", False)),
        "require_auth": bool(cfg.get("require_auth", True)),
        "auth_header": str(cfg.get("auth_header") or default_header).strip() or default_header,
        "auth_token_ref": str(cfg.get("auth_token_ref") or "").strip(),
        "source": str(cfg.get("source") or default_source).strip() or default_source,
        "payload_field": str(cfg.get("payload_field") or "").strip(),
        "allow_metadata": bool(cfg.get("allow_metadata", False)),
    }


async def _require_external_trigger_target_safe(
    bot: Bot,
    config: Dict[str, Any],
    request: Request,
) -> None:
    """Permit external events only for explicit, ready, non-mutating bot targets."""

    if not bool(config.get("autonomy_safe", False)):
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_autonomy_not_attested",
                "message": (
                    "External triggers require external_trigger.autonomy_safe=true and "
                    "a read-only or draft-only target bot."
                ),
            },
        )
    blockers = bot_autonomous_dispatch_blockers(bot)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_target_not_autonomy_safe",
                "message": (
                    f"External trigger target '{bot.id}' cannot run autonomously because "
                    + "; ".join(blockers)
                    + "."
                ),
                "blockers": blockers,
            },
        )
    await _require_bot_ready_to_enable(bot, request)


async def _resolve_external_trigger_secret(
    config: Dict[str, Any],
    request: Request,
) -> str:
    """Resolve a webhook secret without retaining it in the bot configuration."""

    key_ref = str(config.get("auth_token_ref") or "").strip()
    if not key_ref:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_secret_ref_required",
                "message": "External triggers require an encrypted auth_token_ref from the key vault.",
            },
        )
    try:
        secret = str(await request.app.state.key_vault.get_secret(key_ref)).strip()
    except APIKeyNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_secret_unavailable",
                "message": f"External trigger credential reference '{key_ref}' is not available.",
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_secret_unavailable",
                "message": "External trigger credential could not be resolved.",
            },
        ) from exc
    if not secret:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_secret_unavailable",
                "message": f"External trigger credential reference '{key_ref}' is empty.",
            },
        )
    return secret


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
async def create_bot(request: Request, payload: Any = Body(...)) -> Bot:
    bot = _parse_bot_payload_or_400(payload)
    _validate_bot_or_400(bot)
    if bot.enabled and bot.backends:
        await _require_bot_ready_to_enable(bot, request)
    else:
        await _require_known_enabled_bot_project(bot, request)
    bot_registry = request.app.state.bot_registry
    await bot_registry.register(bot)
    await record_audit_event(request, action="bots.create", resource=f"bot:{bot.id}")
    return bot


@router.get("", response_model=List[Bot])
async def list_bots(request: Request) -> List[Bot]:
    bot_registry = request.app.state.bot_registry
    return await bot_registry.list()


@router.post("/preflight")
async def preflight_bot(request: Request, payload: Any = Body(...)) -> Dict[str, Any]:
    """Return non-secret schema, policy, and dispatch readiness for a staged bot."""
    return await _preflight_bot_payload(payload, request)


def _with_operational_state(
    bot: Bot,
    readiness: Dict[str, Any],
    supervision_hold: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Add a dashboard-safe operational state without changing dispatch readiness."""
    result = dict(readiness)
    if supervision_hold is not None:
        checks = list(result.get("checks") or [])
        checks.append(
            {
                "component": "supervision",
                "status": "failed",
                "message": "Bot is on an active supervision hold.",
            }
        )
        result["checks"] = checks
        summary = dict(result.get("summary") or {})
        summary["checks"] = int(summary.get("checks") or 0) + 1
        summary["failed"] = int(summary.get("failed") or 0) + 1
        summary["blocking"] = int(summary.get("blocking") or 0) + 1
        result["summary"] = summary
        result["ready"] = False
    enabled = bool(bot.enabled)
    if not enabled:
        state = "disabled"
    elif bool(result.get("ready")):
        state = "ready"
    else:
        state = "blocked"
    result.update(
        {
            "enabled": enabled,
            "state": state,
            "supervision_hold": supervision_hold,
        }
    )
    return result


@router.get("/readiness")
async def list_bot_readiness(request: Request) -> Dict[str, Any]:
    """Return non-mutating dispatch readiness for every registered bot."""
    bot_registry = request.app.state.bot_registry
    bots = await bot_registry.list()
    supervision_store = getattr(request.app.state, "supervision_store", None)
    holds = await supervision_store.list_holds(limit=500) if supervision_store is not None else []
    holds_by_bot = {
        str(item.get("bot_id") or "").strip(): item
        for item in holds
        if isinstance(item, dict) and str(item.get("bot_id") or "").strip()
    }
    readiness = []
    for bot in bots:
        assessed = await assess_bot_instance_readiness(
            bot,
            worker_registry=request.app.state.worker_registry,
            connection_resolver=request.app.state.connection_resolver,
            worker_probe_store=request.app.state.worker_probe_store,
            key_vault=request.app.state.key_vault,
            model_registry=request.app.state.model_registry,
        )
        readiness.append(_with_operational_state(bot, assessed, holds_by_bot.get(str(bot.id))))
    summary = {
        state: sum(1 for item in readiness if item["state"] == state)
        for state in ("ready", "blocked", "disabled")
    }
    return {"readiness": readiness, "count": len(readiness), "summary": summary}


@router.get("/{bot_id}/readiness")
async def get_bot_readiness(bot_id: str, request: Request) -> Dict[str, Any]:
    bot_registry = request.app.state.bot_registry
    try:
        bot = await bot_registry.get(bot_id)
        readiness = await assess_bot_instance_readiness(
            bot,
            worker_registry=request.app.state.worker_registry,
            connection_resolver=request.app.state.connection_resolver,
            worker_probe_store=request.app.state.worker_probe_store,
            key_vault=request.app.state.key_vault,
            model_registry=request.app.state.model_registry,
        )
        supervision_store = getattr(request.app.state, "supervision_store", None)
        hold = await supervision_store.get_hold(bot.id) if supervision_store is not None else None
        return _with_operational_state(bot, readiness, hold)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{bot_id}/dependencies")
async def get_bot_dependencies(bot_id: str, request: Request) -> Dict[str, Any]:
    try:
        await request.app.state.bot_registry.get(bot_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await _bot_dependencies(request, bot_id)


@router.get("/{bot_id}", response_model=Bot)
async def get_bot(bot_id: str, request: Request) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        return await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{bot_id}", response_model=Bot)
async def update_bot(bot_id: str, request: Request, payload: Any = Body(...)) -> Bot:
    bot = _parse_bot_payload_or_400(payload)
    if bot.id != bot_id:
        _raise_bot_validation_error(
            reason_code="bot_id_mismatch",
            message="bot.id must match the path bot_id",
            validation_errors=[
                {
                    "field_path": "id",
                    "message": "bot.id must match the path bot_id",
                    "invalid_value": bot.id,
                }
            ],
        )
    _validate_bot_or_400(bot)
    bot_registry = request.app.state.bot_registry
    try:
        current = await bot_registry.get(bot_id)
        await _require_known_enabled_bot_project(bot, request)
        if bool(current.enabled) and not bool(bot.enabled):
            dependencies = await _bot_dependencies(request, bot_id)
            if not bool(dependencies["can_disable"]):
                _raise_bot_dependency_error(
                    reason_code="bot_disable_blocked",
                    message="Pause dependent schedules and disable upstream workflow triggers before disabling this bot.",
                    dependencies=dependencies,
                )
        requires_readiness_check = bot.enabled and (
            not current.enabled
            or current.backends != bot.backends
            or current.execution_policy != bot.execution_policy
        )
        if requires_readiness_check:
            await _require_bot_ready_to_enable(bot, request)
        await bot_registry.update(bot_id, bot)
        await record_audit_event(request, action="bots.update", resource=f"bot:{bot_id}")
        return bot
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{bot_id}")
async def delete_bot(bot_id: str, request: Request) -> dict:
    bot_registry = request.app.state.bot_registry
    try:
        await bot_registry.get(bot_id)
        dependencies = await _bot_dependencies(request, bot_id)
        if not bool(dependencies["can_delete"]):
            _raise_bot_dependency_error(
                reason_code="bot_delete_blocked",
                message="Remove or repoint every dependent schedule and workflow reference before deleting this bot.",
                dependencies=dependencies,
            )
        await bot_registry.remove(bot_id)
        await record_audit_event(request, action="bots.delete", resource=f"bot:{bot_id}")
        return {"message": f"Bot {bot_id} removed"}
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{bot_id}/enable", response_model=Bot)
async def enable_bot(bot_id: str, request: Request) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        bot = await bot_registry.get(bot_id)
        await _require_bot_ready_to_enable(bot, request)
        await bot_registry.enable(bot_id)
        await record_audit_event(request, action="bots.enable", resource=f"bot:{bot_id}")
        return await bot_registry.get(bot_id)
    except BotNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{bot_id}/disable", response_model=Bot)
async def disable_bot(bot_id: str, request: Request) -> Bot:
    bot_registry = request.app.state.bot_registry
    try:
        bot = await bot_registry.get(bot_id)
        if bot.enabled:
            dependencies = await _bot_dependencies(request, bot_id)
            if not bool(dependencies["can_disable"]):
                _raise_bot_dependency_error(
                    reason_code="bot_disable_blocked",
                    message="Pause dependent schedules and disable upstream workflow triggers before disabling this bot.",
                    dependencies=dependencies,
                )
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

    if not config["require_auth"]:
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": "external_trigger_auth_required",
                "message": "External triggers must require a dedicated trigger authentication token.",
            },
        )
    expected = await _resolve_external_trigger_secret(config, request)
    header_name = str(config.get("auth_header") or "X-Nexus-Trigger-Token").strip()
    provided = str(request.headers.get(header_name, "") or "").strip()
    if not provided:
        raise HTTPException(status_code=401, detail=f"missing auth header: {header_name}")
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid trigger auth token")

    await _require_external_trigger_target_safe(bot, config, request)

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
