"""Safety checks shared by schedule API handlers and background dispatch."""
from __future__ import annotations

from typing import Any, Dict, Iterable

from control_plane.bot_readiness import assess_bot_readiness
from control_plane.schedule_payload_sources import SystemPayloadSourceError, validate_system_payload_source
from shared.bot_policy import bot_allows_repo_output, bot_can_apply_db_actions, bot_is_pipeline_entry


_AUTONOMOUSLY_UNSAFE_BACKEND_TYPES = {"browser", "cli"}


class ScheduleAutonomySafetyError(ValueError):
    """A schedule cannot safely run without a direct operator controlling it."""

    def __init__(self, reason_code: str, message: str, *, blockers: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.blockers = list(blockers)

    def as_detail(self) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"reason_code": self.reason_code, "message": self.message}
        if self.blockers:
            detail["blockers"] = self.blockers
        return detail


def _schedule_requires_autonomy_guard(schedule: Dict[str, Any], *, only_when_active: bool) -> bool:
    if not only_when_active:
        return True
    return str(schedule.get("status") or "").strip().lower() == "active"


def _schedule_task_payload(schedule: Dict[str, Any]) -> Dict[str, Any]:
    direct_payload = schedule.get("task_payload")
    if isinstance(direct_payload, dict):
        return dict(direct_payload)
    metadata = schedule.get("metadata")
    if isinstance(metadata, dict) and isinstance(metadata.get("task_payload"), dict):
        return dict(metadata["task_payload"])
    return {}


def _is_attested_read_only_browser_inspection(bot: Any, schedule: Dict[str, Any]) -> bool:
    """Allow the single browser shape that cannot interact with the target UI."""
    metadata = schedule.get("metadata") if isinstance(schedule.get("metadata"), dict) else {}
    if str(metadata.get("connection_operation") or "").strip().lower() != "inspect":
        return False
    routing_rules = getattr(bot, "routing_rules", None)
    profile = routing_rules.get("worker_profile") if isinstance(routing_rules, dict) else None
    profile = profile if isinstance(profile, dict) else {}
    execution_policy = getattr(bot, "execution_policy", None)
    if hasattr(execution_policy, "model_dump"):
        execution_policy = execution_policy.model_dump()
    execution_policy = execution_policy if isinstance(execution_policy, dict) else {}
    return (
        str(profile.get("role") or "").strip().lower() == "browser-inspector"
        and str(profile.get("task_scope") or "").strip().lower() == "read-only-browser-inspection"
        and profile.get("can_edit") is False
        and str(execution_policy.get("repo_output_mode") or "").strip().lower() == "deny"
        and execution_policy.get("can_apply_db_actions") is False
    )


def _payload_field_value(payload: Dict[str, Any], field_path: str) -> Any:
    value: Any = payload
    for segment in (part.strip() for part in str(field_path or "").split(".") if part.strip()):
        if not isinstance(value, dict) or segment not in value:
            return _MISSING
        value = value[segment]
    return value


def _is_empty_payload_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (
        isinstance(value, (dict, list, tuple, set)) and not value
    )


_MISSING = object()


def _require_schedule_input_contract(schedule: Dict[str, Any], bot: Any) -> None:
    routing_rules = getattr(bot, "routing_rules", None)
    contract = routing_rules.get("input_contract") if isinstance(routing_rules, dict) else None
    if not isinstance(contract, dict) or not bool(contract.get("enabled", True)):
        return
    task_payload = _schedule_task_payload(schedule)
    task_payload.update(
        {
            "instruction": str(schedule.get("prompt") or "").strip(),
            "source": "agent_schedule",
            "project_id": str(schedule.get("project_id") or "").strip() or None,
            "node_overrides": schedule.get("node_overrides") if isinstance(schedule.get("node_overrides"), dict) else {},
        }
    )
    required_fields = [str(field).strip() for field in contract.get("required_fields") or [] if str(field).strip()]
    non_empty_fields = [str(field).strip() for field in contract.get("non_empty_fields") or [] if str(field).strip()]
    missing = [field for field in required_fields if _payload_field_value(task_payload, field) is _MISSING]
    empty = [
        field
        for field in non_empty_fields
        if (value := _payload_field_value(task_payload, field)) is _MISSING or _is_empty_payload_value(value)
    ]
    if missing or empty:
        detail: list[str] = []
        if missing:
            detail.append("missing required fields: " + ", ".join(missing))
        if empty:
            detail.append("empty required fields: " + ", ".join(empty))
        raise ScheduleAutonomySafetyError(
            "schedule_payload_contract_incomplete",
            f"Schedule target '{bot.id}' cannot run until its task payload is complete: " + "; ".join(detail) + ".",
            blockers=detail,
        )


async def require_schedule_autonomy_safety(
    schedule: Dict[str, Any],
    *,
    bot_registry: Any,
    only_when_active: bool,
) -> None:
    """Permit only explicitly attested, non-mutating direct bot dispatches."""
    if not _schedule_requires_autonomy_guard(schedule, only_when_active=only_when_active):
        return

    metadata = schedule.get("metadata") if isinstance(schedule.get("metadata"), dict) else {}
    if metadata.get("mutation_safe") is not True:
        raise ScheduleAutonomySafetyError(
            "schedule_autonomy_not_attested",
            "Recurring or manually triggered schedules require an explicit read-only or draft-only mutation_safe attestation.",
        )

    if str(schedule.get("assignment_pm_bot_id") or "").strip():
        raise ScheduleAutonomySafetyError(
            "schedule_autonomy_pipeline_not_allowed",
            "Autonomous schedules cannot dispatch a project-manager pipeline.",
        )

    bot_id = str(schedule.get("target_bot_id") or "").strip()
    if not bot_id:
        return
    try:
        bot = await bot_registry.get(bot_id)
    except Exception as exc:
        raise ScheduleAutonomySafetyError(
            "schedule_target_not_ready",
            f"Schedule target '{bot_id}' cannot be inspected for autonomous safety.",
        ) from exc

    blockers: list[str] = []
    if bot_allows_repo_output(bot):
        blockers.append("the bot permits repository writes")
    if bot_can_apply_db_actions(bot):
        blockers.append("the bot can apply database actions")
    if bot_is_pipeline_entry(bot):
        blockers.append("the bot can dispatch a pipeline")
    unsafe_backends = sorted(
        {
            str(backend.type or "").strip().lower()
            for backend in bot.backends
            if str(backend.type or "").strip().lower() in _AUTONOMOUSLY_UNSAFE_BACKEND_TYPES
            and not (
                str(backend.type or "").strip().lower() == "browser"
                and _is_attested_read_only_browser_inspection(bot, schedule)
            )
        }
    )
    if unsafe_backends:
        blockers.append(f"the bot uses restricted backend types: {', '.join(unsafe_backends)}")
    if blockers:
        raise ScheduleAutonomySafetyError(
            "schedule_target_not_autonomy_safe",
            f"Schedule target '{bot_id}' cannot run autonomously because " + "; ".join(blockers) + ".",
            blockers=blockers,
        )
    try:
        validate_system_payload_source(schedule, bot)
    except SystemPayloadSourceError as exc:
        raise ScheduleAutonomySafetyError(
            "schedule_system_payload_source_not_allowed",
            f"Schedule target '{bot_id}' cannot use the requested system payload source: {exc}.",
        ) from exc
    _require_schedule_input_contract(schedule, bot)


async def require_schedule_runtime_readiness(
    schedule: Dict[str, Any],
    *,
    bot_registry: Any,
    worker_registry: Any,
    connection_resolver: Any,
    worker_probe_store: Any = None,
    key_vault: Any = None,
    model_registry: Any = None,
) -> None:
    """Verify a schedule target is dispatchable immediately before execution."""
    bot_id = str(
        schedule.get("assignment_pm_bot_id") or schedule.get("target_bot_id") or ""
    ).strip()
    if not bot_id:
        return
    try:
        readiness = await assess_bot_readiness(
            bot_id,
            bot_registry=bot_registry,
            worker_registry=worker_registry,
            connection_resolver=connection_resolver,
            worker_probe_store=worker_probe_store,
            key_vault=key_vault,
            model_registry=model_registry,
        )
    except Exception as exc:
        raise ScheduleAutonomySafetyError(
            "schedule_target_not_ready",
            f"Schedule target '{bot_id}' cannot be validated for dispatch.",
        ) from exc
    if bool(readiness.get("ready")):
        return
    blockers = [
        str(check.get("message") or "").strip()
        for check in readiness.get("checks") or []
        if isinstance(check, dict) and str(check.get("status") or "").strip().lower() == "failed"
    ]
    raise ScheduleAutonomySafetyError(
        "schedule_target_not_ready",
        f"Schedule target '{bot_id}' is not ready for dispatch.",
        blockers=[item for item in blockers if item][:8],
    )
