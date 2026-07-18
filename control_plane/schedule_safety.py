"""Safety checks shared by schedule API handlers and background dispatch."""
from __future__ import annotations

from typing import Any, Dict, Iterable

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
    _require_schedule_input_contract(schedule, bot)
