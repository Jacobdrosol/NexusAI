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
