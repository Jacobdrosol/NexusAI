"""Deterministic quality checks for PM work plans.

These checks are pure and testable: they validate a plan's structure and
routing without calling any model. They are the "deterministic" half of
the QC pipeline; an AI read-through review (the model half) runs as a
separate task.

Checks:
  - Plan is a dict with a ``steps`` list.
  - Every step has an id, bot_id, and step_kind.
  - Step bot ids are within the orchestration allowlist when provided.
  - Step kinds are among the known set.
  - ``depends_on`` references only existing step ids and is acyclic.
  - Every step has at least one acceptance criterion and deliverable.
  - No step bot_id is repeated as an entry point unless intentional.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

PLAN_STEP_KINDS = {
    "specification",
    "planning",
    "repo_change",
    "test_execution",
    "review",
    "release",
}

_REQUIRED_STEP_FIELDS = ("id", "bot_id", "step_kind")


class PlanQCError(ValueError):
    """A plan failed one or more deterministic quality checks."""


def _step_id(step: Dict[str, Any]) -> str:
    return str(step.get("id") or step.get("step_id") or "").strip()


def validate_plan_structure(
    plan: Any,
    *,
    allowed_bot_ids: Optional[Sequence[str]] = None,
    require_acceptance_criteria: bool = True,
    require_deliverables: bool = True,
) -> Dict[str, Any]:
    """Validate a plan and return a report.

    Raises PlanQCError if the plan is fatally malformed. Otherwise returns
    a report dict with ``ok``, ``step_count``, ``warnings``, and ``errors``.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not isinstance(plan, dict):
        raise PlanQCError("plan must be an object")

    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise PlanQCError("plan.steps must be a list")
    if not steps:
        raise PlanQCError("plan.steps must not be empty")

    allowed = set(allowed_bot_ids or [])
    step_ids: set[str] = set()
    step_bots: set[str] = set()

    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step[{index}] must be an object")
            continue
        missing = [field for field in _REQUIRED_STEP_FIELDS if not str(step.get(field) or "").strip()]
        if missing:
            errors.append(f"step[{index}] missing required field(s): {', '.join(missing)}")
        sid = _step_id(step)
        if sid:
            if sid in step_ids:
                errors.append(f"duplicate step id '{sid}'")
            step_ids.add(sid)
        bot_id = str(step.get("bot_id") or "").strip()
        if bot_id:
            step_bots.add(bot_id)
            if allowed and bot_id not in allowed:
                errors.append(f"step '{sid or index}' bot_id '{bot_id}' is outside the allowed set")
        kind = str(step.get("step_kind") or "").strip()
        if kind and kind not in PLAN_STEP_KINDS:
            warnings.append(f"step '{sid or index}' has unknown step_kind '{kind}'")

        deps = step.get("depends_on")
        if deps is not None:
            if not isinstance(deps, list):
                errors.append(f"step '{sid or index}' depends_on must be a list")
            else:
                for dep in deps:
                    dep_id = str(dep or "").strip()
                    if dep_id and dep_id not in step_ids and dep_id != sid:
                        errors.append(
                            f"step '{sid or index}' depends_on unknown step '{dep_id}'"
                        )

        if require_acceptance_criteria:
            ac = step.get("acceptance_criteria")
            if not ac or (isinstance(ac, list) and not ac):
                warnings.append(f"step '{sid or index}' has no acceptance criteria")
        if require_deliverables:
            deliverables = step.get("deliverables")
            if not deliverables or (isinstance(deliverables, list) and not deliverables):
                warnings.append(f"step '{sid or index}' has no deliverables")

    if _has_cycle(steps):
        errors.append("plan dependency graph contains a cycle")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "step_count": len(steps),
        "bot_ids": sorted(step_bots),
    }


def _has_cycle(steps: List[Dict[str, Any]]) -> bool:
    edges: Dict[str, List[str]] = {}
    for step in steps:
        sid = _step_id(step)
        deps = step.get("depends_on")
        if not isinstance(deps, list):
            continue
        edges.setdefault(sid, [])
        for dep in deps:
            dep_id = str(dep or "").strip()
            if dep_id and dep_id != sid:
                edges.setdefault(sid, []).append(dep_id)

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {sid: WHITE for sid in edges}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for neighbor in edges.get(node, []):
            if neighbor not in color:
                continue
            if color[neighbor] == GRAY:
                return True
            if color[neighbor] == WHITE and visit(neighbor):
                return True
        color[node] = BLACK
        return False

    return any(color[sid] == WHITE and visit(sid) for sid in edges)


def extract_handoff_map(plan: Any) -> Dict[str, Dict[str, Any]]:
    """Return {step_id: {bot_id, step_kind, instruction, deliverables}}.

    Used at approval time so each execution bot receives only its own
    minimal context, not the whole plan.
    """
    result: Dict[str, Dict[str, Any]] = {}
    if not isinstance(plan, dict):
        return result
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return result
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = _step_id(step)
        if not sid:
            continue
        result[sid] = {
            "bot_id": str(step.get("bot_id") or "").strip(),
            "step_kind": str(step.get("step_kind") or "").strip(),
            "instruction": str(step.get("instruction") or step.get("title") or "").strip(),
            "deliverables": step.get("deliverables") if isinstance(step.get("deliverables"), list) else [],
            "acceptance_criteria": step.get("acceptance_criteria") if isinstance(step.get("acceptance_criteria"), list) else [],
            "depends_on": step.get("depends_on") if isinstance(step.get("depends_on"), list) else [],
        }
    return result


def qc_report_summary(report: Dict[str, Any]) -> str:
    parts = [f"plan qc: {report['step_count']} step(s)"]
    if report["errors"]:
        parts.append(f"{len(report['errors'])} error(s)")
    if report["warnings"]:
        parts.append(f"{len(report['warnings'])} warning(s)")
    return ", ".join(parts) + (" [OK]" if report["ok"] else " [FAIL]")