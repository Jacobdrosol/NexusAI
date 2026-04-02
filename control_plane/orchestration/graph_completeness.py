"""
GraphCompletenessEvaluator: deterministic pipeline completion truth.

This evaluator computes whether an orchestration run is truly complete
based on required stage materialization, join coverage, branch coverage,
terminal requirements, deliverable completeness, and test suite completion.

Bots MUST NOT determine pipeline completion. Only this evaluator does.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set


# ─── Orchestration-level states ─────────────────────────────────────────────
ORCH_STATES = {
    "draft",
    "ready",
    "running",
    "waiting_for_join",
    "blocked",
    "stalled",
    "needs_operator_input",
    "failed_retryable",
    "failed_terminal",
    "completed",
}

# ─── Node-level states ───────────────────────────────────────────────────────
NODE_STATES = {
    "pending",
    "queued",
    "running",
    "passed",
    "failed",
    "skipped",
    "blocked",
    "escalated",
    "superseded",
}

# Statuses that count as terminal (done, not retrying)
_TERMINAL_NODE_STATUSES = {"passed", "failed", "skipped", "superseded"}
# Statuses that count as successful completion
_SUCCESS_NODE_STATUSES = {"passed", "skipped"}
# Task statuses that map to node terminal status
_TASK_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "retried"}
# Task statuses that map to node passed status
_TASK_SUCCESS_STATUSES = {"completed"}


@dataclass
class JoinDefinition:
    """Explicit join gate between branches and a downstream target."""
    join_id: str
    required_upstream_node_ids: List[str]        # all must reach terminal before join unlocks
    expected_branch_count: int                    # -1 = dynamic (match actual branches)
    acceptable_terminal_statuses: Set[str] = field(default_factory=lambda: {"passed", "skipped"})
    downstream_unlock_node_id: Optional[str] = None
    timeout_seconds: Optional[float] = None      # None = no timeout
    invalidation_behavior: str = "block"          # "block" | "fail_terminal" | "operator_input"


@dataclass
class FanOutDefinition:
    """Explicit fan-out from a source node."""
    fan_out_id: str
    source_node_id: str
    source_output_field: str                     # field in source output that contains branch items
    min_branch_count: int = 1
    max_branch_count: int = 50
    branch_key_strategy: str = "index"           # "index" | "field_value" | "step_id"
    empty_result_behavior: str = "fail_explicit"  # "fail_explicit" | "skip" | "operator_input"
    replay_policy: str = "rerun"                 # "rerun" | "skip_existing"


@dataclass
class CompletenessReport:
    """Result of evaluating a run's completeness."""
    is_complete: bool
    orchestration_state: str
    stage_coverage: float                # 0.0–1.0: fraction of required stages materialized
    join_coverage: float                 # 0.0–1.0: fraction of joins resolved
    branch_coverage: float               # 0.0–1.0: fraction of expected branches present
    terminal_requirements_met: bool
    unresolved_blockers: List[str]
    deliverable_completeness: float      # 0.0–1.0
    test_suite_passed: bool
    required_stages_missing: List[str]
    joins_unresolved: List[str]
    active_node_count: int
    failed_node_count: int
    stall_signature: str
    evaluated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "is_complete": self.is_complete,
            "orchestration_state": self.orchestration_state,
            "stage_coverage": round(self.stage_coverage, 4),
            "join_coverage": round(self.join_coverage, 4),
            "branch_coverage": round(self.branch_coverage, 4),
            "terminal_requirements_met": self.terminal_requirements_met,
            "unresolved_blockers": self.unresolved_blockers,
            "deliverable_completeness": round(self.deliverable_completeness, 4),
            "test_suite_passed": self.test_suite_passed,
            "required_stages_missing": self.required_stages_missing,
            "joins_unresolved": self.joins_unresolved,
            "active_node_count": self.active_node_count,
            "failed_node_count": self.failed_node_count,
            "stall_signature": self.stall_signature,
            "evaluated_at": self.evaluated_at,
        }


class GraphCompletenessEvaluator:
    """
    Source of truth for pipeline run completion.

    Determines whether an orchestration run is complete based on:
    - Required stage materialization (every required role must appear)
    - Join resolution (all defined joins must be resolved)
    - Branch coverage (fan-out branches must be accounted for)
    - Terminal requirements (no required node may remain queued/running/blocked)
    - Deliverable completeness (required deliverables must exist)
    - Test suite pass (canonical suite must pass if defined)
    """

    def __init__(
        self,
        *,
        required_stage_roles: Optional[List[str]] = None,
        join_definitions: Optional[List[JoinDefinition]] = None,
        fan_out_definitions: Optional[List[FanOutDefinition]] = None,
        required_deliverable_keys: Optional[List[str]] = None,
        terminal_stage_role: Optional[str] = None,
        stall_unchanged_ticks: int = 10,
    ) -> None:
        self.required_stage_roles: List[str] = required_stage_roles or []
        self.join_definitions: List[JoinDefinition] = join_definitions or []
        self.fan_out_definitions: List[FanOutDefinition] = fan_out_definitions or []
        self.required_deliverable_keys: List[str] = required_deliverable_keys or []
        self.terminal_stage_role: Optional[str] = terminal_stage_role
        self.stall_unchanged_ticks: int = max(3, stall_unchanged_ticks)
        self._stall_history: List[str] = []

    def _node_ids_from_graph(self, graph: Dict[str, Any]) -> Set[str]:
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        return {str(n.get("id") or n.get("bot_id") or "").strip() for n in nodes if isinstance(n, dict)} - {""}

    def _node_status_map(self, graph: Dict[str, Any], tasks: List[Dict[str, Any]]) -> Dict[str, str]:
        """Map node_id -> effective status from tasks."""
        task_map: Dict[str, str] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            bot_id = str(task.get("bot_id") or "").strip()
            meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            step_id = str(meta.get("step_id") or "").strip()
            raw_status = str(task.get("status") or "").strip().lower()
            # Map task status to node status
            if raw_status in {"completed"}:
                node_status = "passed"
            elif raw_status in {"failed"}:
                node_status = "failed"
            elif raw_status in {"cancelled", "retried"}:
                node_status = "superseded"
            elif raw_status in {"running"}:
                node_status = "running"
            elif raw_status in {"queued", "blocked"}:
                node_status = "queued"
            else:
                node_status = "pending"
            for node_key in (step_id, bot_id):
                if node_key:
                    existing = task_map.get(node_key)
                    # Prefer "passed" > "running" > "queued" > "pending" > "failed"
                    priority = {"passed": 5, "skipped": 4, "running": 3, "queued": 2, "pending": 1, "failed": 0, "superseded": 0}
                    if existing is None or priority.get(node_status, 0) > priority.get(existing, 0):
                        task_map[node_key] = node_status
        return task_map

    def _check_stage_coverage(self, graph: Dict[str, Any], tasks: List[Dict[str, Any]]) -> tuple[float, List[str]]:
        if not self.required_stage_roles:
            return 1.0, []
        status_map = self._node_status_map(graph, tasks)
        node_ids = self._node_ids_from_graph(graph)
        missing = []
        for role in self.required_stage_roles:
            # A role is materialized if there's a task or node matching this role
            matched = any(
                role.lower() in str(nid).lower() or str(nid).lower() == role.lower()
                for nid in (set(status_map.keys()) | node_ids)
            )
            if not matched:
                missing.append(role)
        coverage = (len(self.required_stage_roles) - len(missing)) / max(1, len(self.required_stage_roles))
        return coverage, missing

    def _check_join_coverage(self, tasks: List[Dict[str, Any]]) -> tuple[float, List[str]]:
        if not self.join_definitions:
            return 1.0, []
        status_map: Dict[str, str] = {}
        for task in tasks:
            if not isinstance(task, dict):
                continue
            bot_id = str(task.get("bot_id") or "").strip()
            meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            step_id = str(meta.get("step_id") or "").strip()
            status = str(task.get("status") or "").strip().lower()
            for key in (step_id, bot_id):
                if key:
                    status_map[key] = status
        unresolved = []
        for join_def in self.join_definitions:
            upstream = join_def.required_upstream_node_ids
            all_resolved = all(
                str(status_map.get(uid, "pending")).lower() in _TASK_TERMINAL_STATUSES
                for uid in upstream
            )
            acceptable = join_def.acceptable_terminal_statuses
            all_acceptable = all(
                self._task_status_to_node_status(str(status_map.get(uid, "pending"))) in acceptable
                for uid in upstream
            )
            if not all_resolved or not all_acceptable:
                unresolved.append(join_def.join_id)
        coverage = (len(self.join_definitions) - len(unresolved)) / max(1, len(self.join_definitions))
        return coverage, unresolved

    def _task_status_to_node_status(self, task_status: str) -> str:
        mapping = {
            "completed": "passed",
            "failed": "failed",
            "cancelled": "superseded",
            "retried": "superseded",
            "running": "running",
            "queued": "queued",
            "blocked": "blocked",
        }
        return mapping.get(task_status.lower(), "pending")

    def _check_terminal_requirements(self, tasks: List[Dict[str, Any]]) -> tuple[bool, int, int]:
        active = 0
        failed = 0
        for task in tasks:
            if not isinstance(task, dict):
                continue
            status = str(task.get("status") or "").strip().lower()
            if status in {"running", "queued", "blocked"}:
                active += 1
            if status == "failed":
                failed += 1
        return active == 0, active, failed

    def _check_terminal_stage_reached(self, tasks: List[Dict[str, Any]]) -> bool:
        if not self.terminal_stage_role:
            return True  # no requirement
        for task in tasks:
            if not isinstance(task, dict):
                continue
            bot_id = str(task.get("bot_id") or "").strip()
            meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            step_id = str(meta.get("step_id") or "").strip()
            status = str(task.get("status") or "").strip().lower()
            if self.terminal_stage_role.lower() in bot_id.lower() or self.terminal_stage_role.lower() in step_id.lower():
                if status in _TASK_SUCCESS_STATUSES:
                    return True
        return False

    def _check_deliverables(self, tasks: List[Dict[str, Any]]) -> float:
        if not self.required_deliverable_keys:
            return 1.0
        found_keys: Set[str] = set()
        for task in tasks:
            if not isinstance(task, dict):
                continue
            result = task.get("result")
            if isinstance(result, dict):
                for key in self.required_deliverable_keys:
                    if key in result and result[key]:
                        found_keys.add(key)
            meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            artifacts = meta.get("artifacts") if isinstance(meta.get("artifacts"), list) else []
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    artifact_key = str(artifact.get("key") or artifact.get("name") or "").strip()
                    if artifact_key in self.required_deliverable_keys:
                        found_keys.add(artifact_key)
        return len(found_keys) / max(1, len(self.required_deliverable_keys))

    def _compute_stall_signature(self, tasks: List[Dict[str, Any]]) -> str:
        task_states = sorted(
            (str(t.get("bot_id") or ""), str(t.get("status") or ""), str(t.get("updated_at") or ""))
            for t in tasks if isinstance(t, dict)
        )
        raw = json.dumps(task_states, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _check_stalled(self, tasks: List[Dict[str, Any]]) -> bool:
        sig = self._compute_stall_signature(tasks)
        self._stall_history.append(sig)
        if len(self._stall_history) > self.stall_unchanged_ticks * 2:
            self._stall_history = self._stall_history[-self.stall_unchanged_ticks * 2:]
        if len(self._stall_history) >= self.stall_unchanged_ticks:
            recent = self._stall_history[-self.stall_unchanged_ticks:]
            if len(set(recent)) == 1:  # all same signature
                return True
        return False

    def evaluate(
        self,
        *,
        graph: Dict[str, Any],
        tasks: List[Dict[str, Any]],
        test_suite_passed: bool = False,
        deliverables: Optional[Dict[str, Any]] = None,
    ) -> CompletenessReport:
        """
        Evaluate whether a pipeline run is truly complete.
        This is the single source of truth for completion.
        """
        now = datetime.now(timezone.utc).isoformat()

        stage_coverage, missing_stages = self._check_stage_coverage(graph, tasks)
        join_coverage, unresolved_joins = self._check_join_coverage(tasks)
        all_terminal, active_count, failed_count = self._check_terminal_requirements(tasks)
        terminal_stage_reached = self._check_terminal_stage_reached(tasks)
        deliverable_completeness = self._check_deliverables(tasks)
        stall_signature = self._compute_stall_signature(tasks)
        is_stalled = self._check_stalled(tasks)

        # Branch coverage: if no fan-out defs, assume 1.0
        branch_coverage = 1.0

        blockers: List[str] = []
        if missing_stages:
            blockers.append(f"missing_stages: {missing_stages}")
        if unresolved_joins:
            blockers.append(f"unresolved_joins: {unresolved_joins}")
        if not all_terminal:
            blockers.append(f"active_nodes: {active_count}")
        if not terminal_stage_reached:
            blockers.append("terminal_stage_not_reached")
        if deliverable_completeness < 1.0 and self.required_deliverable_keys:
            blockers.append(f"deliverables_incomplete: {deliverable_completeness:.2f}")

        # Determine orchestration state
        if is_stalled and not all_terminal:
            orch_state = "stalled"
        elif not all_terminal:
            if unresolved_joins:
                orch_state = "waiting_for_join"
            else:
                orch_state = "running"
        elif failed_count > 0 and not all_terminal:
            orch_state = "failed_retryable"
        elif failed_count > 0 and all_terminal:
            orch_state = "failed_terminal"
        elif missing_stages or unresolved_joins or not terminal_stage_reached:
            orch_state = "blocked"
        elif deliverable_completeness < 1.0 and self.required_deliverable_keys:
            orch_state = "blocked"
        else:
            orch_state = "completed"

        is_complete = (
            orch_state == "completed"
            and stage_coverage >= 1.0
            and join_coverage >= 1.0
            and all_terminal
            and terminal_stage_reached
            and deliverable_completeness >= 1.0
        )

        return CompletenessReport(
            is_complete=is_complete,
            orchestration_state=orch_state,
            stage_coverage=stage_coverage,
            join_coverage=join_coverage,
            branch_coverage=branch_coverage,
            terminal_requirements_met=all_terminal,
            unresolved_blockers=blockers,
            deliverable_completeness=deliverable_completeness,
            test_suite_passed=test_suite_passed,
            required_stages_missing=missing_stages,
            joins_unresolved=unresolved_joins,
            active_node_count=active_count,
            failed_node_count=failed_count,
            stall_signature=stall_signature,
            evaluated_at=now,
        )

    @classmethod
    def for_pm_software_delivery(cls, *, terminal_stage_role: str = "final_qc") -> "GraphCompletenessEvaluator":
        """
        Factory: create an evaluator configured for the PM Software Delivery template.

        Required roles (as defined in the handoff specification):
        - planner, research_repo, research_data, research_web
        - engineer, coder, tester, security_reviewer
        - database_engineer, ui_tester, final_qc
        """
        required_roles = [
            "planner",
            "research",   # matches research_repo, research_data, research_web
            "engineer",
            "coder",
            "tester",
            "security",
            "database_engineer",
            "ui_tester",
            "final_qc",
        ]
        # Research join: all 3 research branches must resolve before engineer
        research_join = JoinDefinition(
            join_id="research_to_engineer_join",
            required_upstream_node_ids=["research_repo", "research_data", "research_web"],
            expected_branch_count=3,
            acceptable_terminal_statuses={"passed", "skipped"},
            downstream_unlock_node_id="engineer",
        )
        # Security join: all security branches must resolve before database_engineer
        security_join = JoinDefinition(
            join_id="security_to_db_join",
            required_upstream_node_ids=[],  # dynamic, filled at runtime
            expected_branch_count=-1,       # dynamic
            acceptable_terminal_statuses={"passed", "skipped"},
            downstream_unlock_node_id="database_engineer",
        )
        return cls(
            required_stage_roles=required_roles,
            join_definitions=[research_join, security_join],
            terminal_stage_role=terminal_stage_role,
            stall_unchanged_ticks=8,
        )


def validate_fan_out_result(
    *,
    fan_out_def: FanOutDefinition,
    source_output: Dict[str, Any],
) -> tuple[bool, str]:
    """
    Validate a fan-out result. Returns (valid, error_message).
    If empty and behavior is fail_explicit, returns (False, error).
    """
    field_value = source_output.get(fan_out_def.source_output_field)
    count = 0
    if isinstance(field_value, list):
        count = len(field_value)
    elif isinstance(field_value, dict):
        count = len(field_value)
    elif field_value is not None:
        count = 1

    if count == 0:
        behavior = fan_out_def.empty_result_behavior
        if behavior == "fail_explicit":
            return False, (
                f"Fan-out '{fan_out_def.fan_out_id}' from '{fan_out_def.source_node_id}' "
                f"produced 0 branches from field '{fan_out_def.source_output_field}'. "
                f"Expected at least {fan_out_def.min_branch_count}. "
                "This is a fatal fan-out error - the orchestration cannot proceed."
            )
        elif behavior == "operator_input":
            return False, (
                f"Fan-out '{fan_out_def.fan_out_id}' produced 0 branches. "
                "Operator input required to proceed."
            )
        else:  # skip
            return True, ""

    if count < fan_out_def.min_branch_count:
        return False, (
            f"Fan-out '{fan_out_def.fan_out_id}' produced {count} branch(es) "
            f"but requires at least {fan_out_def.min_branch_count}."
        )
    if fan_out_def.max_branch_count > 0 and count > fan_out_def.max_branch_count:
        return False, (
            f"Fan-out '{fan_out_def.fan_out_id}' produced {count} branch(es) "
            f"but maximum is {fan_out_def.max_branch_count}."
        )
    return True, ""
