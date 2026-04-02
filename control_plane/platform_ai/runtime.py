from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from control_plane.platform_ai.session_store import PlatformAISessionStore
from shared.models import TaskMetadata


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "retried"}
_QUALITY_FIELDS = {"summary", "quality_gates", "acceptance_criteria", "tests", "artifacts", "warnings", "errors"}


def _task_text(task: Dict[str, Any]) -> str:
    value = task.get("result")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return str(value or "")


def _task_fields(task: Dict[str, Any]) -> set[str]:
    result = task.get("result")
    if isinstance(result, dict):
        return {str(key) for key in result.keys()}
    return set()


def _task_quality(task: Dict[str, Any]) -> float:
    score = 0.0
    status = str(task.get("status") or "").strip().lower()
    text = _task_text(task).strip()
    fields = _task_fields(task)
    if status == "completed":
        score += 0.3
    if len(text) >= 100:
        score += 0.2
    elif len(text) >= 40:
        score += 0.1
    if fields:
        score += 0.2
    hits = sum(1 for field in _QUALITY_FIELDS if field in fields)
    if hits >= 2:
        score += 0.3
    elif hits == 1:
        score += 0.15
    if "errors" in fields and isinstance(task.get("result"), dict) and task["result"].get("errors"):
        score -= 0.15
    return max(0.0, min(1.0, score))


def _task_identities(task: Dict[str, Any]) -> set[str]:
    identities = set()
    bot_id = str(task.get("bot_id") or "").strip()
    if bot_id:
        identities.add(bot_id)
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    step_id = str(metadata.get("step_id") or "").strip()
    if step_id:
        identities.add(step_id)
    return identities


def _task_stage_role(task: Dict[str, Any]) -> str:
    """Return the canonical lowercase stage role for a task.

    Checks metadata.stage_role → metadata.step_id → bot_id in priority order.
    Used by topology assertions to match tasks to graph stage roles.
    """
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    role = (
        str(metadata.get("stage_role") or "").strip()
        or str(metadata.get("step_id") or "").strip()
        or str(task.get("bot_id") or "").strip()
    )
    return role.lower()


def _node_ids(graph: Dict[str, Any]) -> List[str]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    ids: List[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("bot_id") or "").strip()
        if node_id and node_id not in ids:
            ids.append(node_id)
    return ids


def _critical_nodes(graph: Dict[str, Any]) -> List[str]:
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    picked: List[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("bot_id") or "").strip()
        desc = f"{node_id} {str(node.get('title') or '')}".lower()
        if any(token in desc for token in ("tester", "security", "final", "qc", "database", "coder")):
            if node_id and node_id not in picked:
                picked.append(node_id)
    return picked or _node_ids(graph)[:3]


def _counts_are_terminal(status_counts: Dict[str, Any]) -> bool:
    running = int(status_counts.get("running") or 0)
    queued = int(status_counts.get("queued") or 0)
    blocked = int(status_counts.get("blocked") or 0)
    return running == 0 and queued == 0 and blocked == 0


def _assertion(kind: str, passed: bool, score: float, detail: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "passed": bool(passed),
        "score": max(0.0, min(1.0, float(score))),
        "detail": str(detail or ""),
    }


def _select_tasks(tasks: List[Dict[str, Any]], targets: List[str]) -> List[Dict[str, Any]]:
    if not targets:
        return list(tasks)
    wanted = {str(item).strip() for item in targets if str(item).strip()}
    selected: List[Dict[str, Any]] = []
    for task in tasks:
        if _task_identities(task).intersection(wanted):
            selected.append(task)
    return selected


def _evaluate_assertion(assertion: Dict[str, Any], tasks: List[Dict[str, Any]], graph: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(assertion.get("kind") or "").strip().lower()
    targets = [str(item) for item in (assertion.get("target_nodes") or []) if str(item).strip()]
    selected = _select_tasks(tasks, targets)
    if kind == "no_failed_tasks":
        failed = sum(1 for task in tasks if str(task.get("status") or "").strip().lower() == "failed")
        return _assertion(kind, failed == 0, 1.0 if failed == 0 else 0.0, f"failed_tasks={failed}")
    if kind == "min_completed_ratio":
        total = max(1, len(tasks))
        completed = sum(1 for task in tasks if str(task.get("status") or "").strip().lower() == "completed")
        ratio = completed / total
        target = float(assertion.get("value") or 1.0)
        return _assertion(kind, ratio >= target, min(1.0, ratio / max(0.01, target)), f"ratio={ratio:.3f}")
    if kind == "node_coverage_ratio":
        nodes = _node_ids(graph)
        if not nodes:
            return _assertion(kind, True, 1.0, "no graph nodes")
        seen = set()
        for task in tasks:
            seen.update(_task_identities(task))
        coverage = sum(1 for node in nodes if node in seen) / max(1, len(nodes))
        target = float(assertion.get("value") or 1.0)
        return _assertion(kind, coverage >= target, min(1.0, coverage / max(0.01, target)), f"coverage={coverage:.3f}")
    if kind == "min_avg_quality":
        if not selected:
            return _assertion(kind, False, 0.0, "no target tasks")
        avg = sum(_task_quality(task) for task in selected) / max(1, len(selected))
        target = float(assertion.get("value") or 0.7)
        return _assertion(kind, avg >= target, min(1.0, avg / max(0.01, target)), f"avg_quality={avg:.3f}")
    if kind == "required_keywords":
        keywords = [str(item).strip().lower() for item in (assertion.get("keywords") or []) if str(item).strip()]
        if not keywords:
            return _assertion(kind, True, 1.0, "no keywords")
        text = "\n".join(_task_text(task) for task in selected).lower()
        hit = sum(1 for word in keywords if word in text)
        ratio = hit / max(1, len(keywords))
        return _assertion(kind, ratio >= 1.0, ratio, f"keywords={hit}/{len(keywords)}")
    if kind == "required_fields":
        required = [str(item).strip() for item in (assertion.get("fields") or []) if str(item).strip()]
        if not required:
            return _assertion(kind, True, 1.0, "no fields")
        available = set()
        for task in selected:
            available.update(_task_fields(task))
        hit = sum(1 for field in required if field in available)
        ratio = hit / max(1, len(required))
        return _assertion(kind, ratio >= 1.0, ratio, f"fields={hit}/{len(required)}")
    if kind == "required_stage_materialization":
        # Each target_node (stage role / step_id / bot_id) must have ≥1 completed task.
        if not targets:
            return _assertion(kind, False, 0.0, "target_nodes required")
        hit = 0
        for target in targets:
            tl = target.lower()
            if any(
                str(task.get("status") or "").strip().lower() == "completed"
                and tl in _task_stage_role(task)
                for task in tasks
            ):
                hit += 1
        ratio = hit / max(1, len(targets))
        return _assertion(kind, ratio >= 1.0, ratio, f"materialized={hit}/{len(targets)}")
    if kind == "exact_branch_count":
        # Fan-out node spawned exactly `value` branches.
        if not targets:
            return _assertion(kind, False, 0.0, "target_nodes required")
        expected = int(assertion.get("value") or 0)
        if expected <= 0:
            return _assertion(kind, False, 0.0, "value (expected branch count) must be > 0")
        target_role = targets[0].lower()
        metadata_matches = sum(
            1 for task in tasks
            if isinstance(task.get("metadata"), dict)
            and target_role in str(
                task["metadata"].get("fan_out_source") or task["metadata"].get("parent_step_id") or ""
            ).lower()
        )
        actual = metadata_matches if metadata_matches > 0 else sum(
            1 for task in tasks if target_role in _task_stage_role(task)
        )
        passed = actual == expected
        score = 1.0 if passed else max(0.0, 1.0 - abs(actual - expected) / max(1, expected))
        return _assertion(kind, passed, score, f"branches={actual} expected={expected}")
    if kind == "join_resolution":
        # Join gate branches are all in terminal states (no active/queued/blocked tasks).
        if not targets:
            return _assertion(kind, False, 0.0, "target_nodes required")
        _TERM = {"completed", "failed", "cancelled", "retried"}
        target_role = targets[0].lower()
        branch_tasks = [
            task for task in tasks
            if isinstance(task.get("metadata"), dict)
            and target_role in str(
                task["metadata"].get("join_gate_id") or task["metadata"].get("join_node_id") or ""
            ).lower()
        ]
        if not branch_tasks:
            branch_tasks = [task for task in tasks if target_role in _task_stage_role(task)]
        if not branch_tasks:
            return _assertion(kind, False, 0.0, f"no tasks for join target={target_role}")
        unresolved = sum(
            1 for task in branch_tasks
            if str(task.get("status") or "").strip().lower() not in _TERM
        )
        score = 1.0 - (unresolved / max(1, len(branch_tasks)))
        return _assertion(kind, unresolved == 0, score, f"unresolved={unresolved}/{len(branch_tasks)}")
    if kind == "downstream_unlock":
        # Nodes immediately downstream of target_nodes in the graph have no blocked tasks.
        if not targets:
            return _assertion(kind, True, 1.0, "no targets — skip")
        target_set = {t.lower() for t in targets}
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
        downstream: set[str] = set()
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("source") or edge.get("from") or "").strip().lower()
            dst = str(edge.get("target") or edge.get("to") or "").strip().lower()
            if src in target_set and dst:
                downstream.add(dst)
        if not downstream:
            return _assertion(kind, True, 1.0, "no downstream edges found")
        blocked = sum(
            1 for task in tasks
            if str(task.get("status") or "").strip().lower() == "blocked"
            and any(ds in _task_stage_role(task) for ds in downstream)
        )
        score = 1.0 if blocked == 0 else max(0.0, 1.0 - blocked / max(1, len(tasks)))
        return _assertion(kind, blocked == 0, score, f"blocked_downstream={blocked}")
    if kind == "terminal_stage_reached":
        # A terminal stage (default: nodes with is_terminal=True, or "final_qc") has ≥1 completed task.
        if targets:
            stage_roles = [t.lower() for t in targets]
        else:
            nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
            stage_roles = [
                str(n.get("id") or n.get("bot_id") or "").strip().lower()
                for n in nodes
                if isinstance(n, dict) and bool(n.get("is_terminal"))
            ] or ["final_qc"]
        hit = any(
            str(task.get("status") or "").strip().lower() == "completed"
            and any(role in _task_stage_role(task) for role in stage_roles)
            for task in tasks
        )
        return _assertion(kind, hit, 1.0 if hit else 0.0, f"terminal_roles={stage_roles} reached={hit}")
    if kind == "no_stalled_loop":
        # No single stage role repeats more than `value` consecutive times without change.
        max_repeats = max(1, int(assertion.get("value") or 5))
        if len(tasks) < 2:
            return _assertion(kind, True, 1.0, "too few tasks to detect loop")
        max_run = current_run = 1
        prev_role = _task_stage_role(tasks[0])
        for task in tasks[1:]:
            role = _task_stage_role(task)
            if role and role == prev_role:
                current_run += 1
                max_run = max(max_run, current_run)
            else:
                prev_role = role
                current_run = 1
        passed = max_run <= max_repeats
        score = min(1.0, max_repeats / max(1, max_run))
        return _assertion(kind, passed, score, f"max_consecutive_same_role={max_run} limit={max_repeats}")
    return _assertion(kind or "unknown", False, 0.0, "unsupported assertion")


def _evaluate_suite(suite: Dict[str, Any], tasks: List[Dict[str, Any]], graph: Dict[str, Any]) -> Dict[str, Any]:
    tests = suite.get("tests") if isinstance(suite.get("tests"), list) else []
    evaluated: List[Dict[str, Any]] = []
    weighted = 0.0
    total_weight = 0.0
    for test in tests:
        if not isinstance(test, dict):
            continue
        assertions = test.get("assertions") if isinstance(test.get("assertions"), list) else []
        checks = [_evaluate_assertion(item, tasks, graph) for item in assertions if isinstance(item, dict)]
        if not checks:
            checks = [_assertion("none", False, 0.0, "no assertions")]
        score = sum(float(item.get("score") or 0.0) for item in checks) / max(1, len(checks))
        threshold = float(test.get("pass_threshold") or 0.8)
        passed = all(bool(item.get("passed")) for item in checks) and score >= threshold
        weight = float(test.get("weight") or 1.0)
        weighted += score * max(0.0, weight)
        total_weight += max(0.0, weight)
        evaluated.append(
            {
                "id": str(test.get("id") or ""),
                "name": str(test.get("name") or ""),
                "type": str(test.get("type") or "quality"),
                "score": score,
                "pass_threshold": threshold,
                "weight": weight,
                "passed": passed,
                "assertions": checks,
            }
        )
    suite_score = weighted / max(0.0001, total_weight)
    suite_threshold = float(suite.get("suite_pass_threshold") or 0.8)
    suite_passed = bool(evaluated) and all(bool(item.get("passed")) for item in evaluated) and suite_score >= suite_threshold
    completeness_report: Optional[Dict[str, Any]] = None
    try:
        from control_plane.orchestration.graph_completeness import GraphCompletenessEvaluator
        _ev = GraphCompletenessEvaluator.for_pm_software_delivery()
        completeness_report = _ev.evaluate(graph=graph, tasks=tasks).to_dict()
    except Exception:
        pass
    return {
        "status": "passed" if suite_passed else "failed",
        "score": round(suite_score, 4),
        "suite_pass_threshold": suite_threshold,
        "tests": evaluated,
        "task_count": len(tasks),
        "graph_node_count": len(_node_ids(graph)),
        "evaluated_at": _now(),
        "completeness_report": completeness_report,
    }


def _build_default_suite(*, suite_name: str, graph: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": suite_name,
        "version": "v1",
        "generated_at": _now(),
        "suite_pass_threshold": 0.8,
        "graph_nodes": _node_ids(graph),
        "tests": [
            {
                "id": "pipeline-completion",
                "name": "Pipeline completes without failed nodes",
                "type": "pipeline",
                "weight": 0.35,
                "pass_threshold": 0.95,
                "assertions": [{"kind": "no_failed_tasks"}, {"kind": "min_completed_ratio", "value": 1.0}],
            },
            {
                "id": "graph-coverage",
                "name": "Graph nodes are represented in run execution",
                "type": "coverage",
                "weight": 0.25,
                "pass_threshold": 0.9,
                "assertions": [{"kind": "node_coverage_ratio", "value": 1.0}],
            },
            {
                "id": "critical-quality",
                "name": "Critical stages meet quality signals",
                "type": "quality",
                "weight": 0.40,
                "pass_threshold": 0.8,
                "assertions": [{"kind": "min_avg_quality", "value": 0.7, "target_nodes": _critical_nodes(graph)}],
            },
        ],
    }


class PlatformAISessionRuntime:
    """In-process runtime loop for Platform AI sessions.

    The runtime is intentionally deterministic and transparent:
    - operator messages are persisted and acknowledged
    - loop heartbeats emit action_trace events
    - deploy actions are executed via dashboard DeployManager and streamed back as traces
    """

    def __init__(
        self,
        store: PlatformAISessionStore,
        *,
        assignment_service: Any = None,
        run_store: Any = None,
        task_manager: Any = None,
        bot_registry: Any = None,
    ) -> None:
        self._store = store
        self._assignment_service = assignment_service
        self._run_store = run_store
        self._task_manager = task_manager
        self._bot_registry = bot_registry
        self._session_tasks: Dict[str, asyncio.Task[None]] = {}
        self._deploy_tasks: Dict[str, asyncio.Task[None]] = {}
        self._processed_operator_messages: Dict[str, set[str]] = {}
        self._last_progress_signature: Dict[str, str] = {}
        self._last_heartbeat_ts: Dict[str, float] = {}
        self._bot_name_cache: Dict[str, str] = {}

    async def ensure_session_loop(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        task = self._session_tasks.get(sid)
        if task is not None and not task.done():
            return
        self._session_tasks[sid] = asyncio.create_task(self._session_loop(sid))

    async def stop_session_loop(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        task = self._session_tasks.get(sid)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _compute_state_hash(self, data: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    async def _synthesize_session_brief(
        self,
        session_id: str,
        *,
        session: Dict[str, Any],
        message_content: str,
    ) -> Dict[str, Any]:
        text = str(message_content or "").strip()
        tuning_goal = text[:4000]
        success_definition = ""
        for phrase in ("i want it to", "success means", "done when", "expected result"):
            idx = text.lower().find(phrase)
            if idx >= 0:
                success_definition = text[idx : idx + 500].strip()
                break
        expected_deliverables: List[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and (stripped[0] in "-*\u2022" or (len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)")):
                item = stripped.lstrip("-*\u20220123456789.) ").strip()
                if item and len(item) >= 5:
                    expected_deliverables.append(item[:200])
                if len(expected_deliverables) >= 20:
                    break
        forbidden_behaviors: List[str] = []
        lower_text = text.lower()
        for phrase in ("do not", "avoid", "never", "don't"):
            start = 0
            while True:
                idx = lower_text.find(phrase, start)
                if idx < 0:
                    break
                snippet = text[idx : idx + 200].strip()
                if snippet:
                    forbidden_behaviors.append(snippet)
                start = idx + 1
                if len(forbidden_behaviors) >= 10:
                    break
            if len(forbidden_behaviors) >= 10:
                break
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        brief = await self._store.upsert_session_brief(
            session_id,
            tuning_goal=tuning_goal,
            success_definition=success_definition,
            expected_deliverables=expected_deliverables,
            forbidden_behaviors=forbidden_behaviors,
            target_pipeline_binding_id=str(metadata.get("pipeline_bot_id") or "").strip() or None,
        )
        await self._store.update_session(
            session_id,
            metadata={"brief_synthesized_at": _now()},
        )
        return brief

    async def _create_action_record(
        self,
        session_id: str,
        *,
        action_type: str,
        snapshot: Dict[str, Any],
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        rationale: str = "",
    ) -> Dict[str, Any]:
        input_hash = self._compute_state_hash(snapshot)
        return await self._store.create_action(
            session_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            rationale=rationale,
            input_snapshot_hash=input_hash,
        )

    async def _complete_action_record(
        self,
        action_id: str,
        *,
        output_snapshot: Dict[str, Any],
        had_effect: bool,
        summary: str = "",
        error: Optional[str] = None,
    ) -> None:
        output_hash = self._compute_state_hash(output_snapshot)
        status = "completed" if had_effect else "no_op"
        await self._store.update_action(
            action_id,
            status=status,
            output_snapshot_hash=output_hash,
            state_delta_summary=summary,
            error=error,
        )

    async def _check_should_halt_as_stalled(
        self,
        session_id: str,
        *,
        no_op_threshold: int = 5,
    ) -> Optional[str]:
        count = await self._store.count_consecutive_no_progress_actions(session_id)
        if count >= no_op_threshold:
            return "stalled_duplicate_actions"
        return None

    async def _halt_session(
        self,
        session_id: str,
        *,
        reason: str,
        message: str,
    ) -> None:
        await self._store.update_session(session_id, status="stopped")
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "session_halted", "reason": reason, "halted_at": _now()},
        )
        await self._store.append_message(
            session_id,
            role="assistant",
            content=message,
            metadata={"source": "halt_guard", "halt_reason": reason},
        )

    def _snapshot_is_terminal(self, snapshot: Dict[str, Any]) -> bool:
        if not str(snapshot.get("orchestration_id") or "").strip():
            return False
        runtime_state = snapshot.get("runtime_state") if isinstance(snapshot.get("runtime_state"), dict) else {}
        task_total = int(runtime_state.get("task_total") or 0)
        status_counts = snapshot.get("status_counts") if isinstance(snapshot.get("status_counts"), dict) else {}
        active_tasks = snapshot.get("active_tasks") if isinstance(snapshot.get("active_tasks"), list) else []
        if task_total <= 0 or active_tasks:
            return False
        completed_like = (
            int(status_counts.get("completed") or 0)
            + int(status_counts.get("failed") or 0)
            + int(status_counts.get("cancelled") or 0)
            + int(status_counts.get("retried") or 0)
        )
        return completed_like >= task_total and _counts_are_terminal(status_counts)

    def _autonomous_terminal_resolution(
        self,
        *,
        session: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        if str(session.get("mode") or "").strip().lower() != "pipeline_tuner":
            return None
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        if not bool(metadata.get("autonomous_enabled")):
            return None
        if not self._snapshot_is_terminal(snapshot):
            return None
        if str(metadata.get("autonomous_terminalized_at") or "").strip():
            return None

        status_counts = snapshot.get("status_counts") if isinstance(snapshot.get("status_counts"), dict) else {}
        failed_tasks = int(status_counts.get("failed") or 0)
        orchestration_id = str(snapshot.get("orchestration_id") or "").strip()
        pipeline_name = str(metadata.get("pipeline_name") or metadata.get("pipeline_bot_id") or "pipeline").strip()
        state = str(metadata.get("autonomous_state") or "").strip().lower()
        current_iteration = int(metadata.get("autonomous_iteration") or 0)
        max_iterations = max(1, min(25, int(metadata.get("autonomous_max_iterations") or 6)))
        score = float(metadata.get("autonomous_last_eval_score") or 0.0)
        target_score = max(0.6, min(0.99, float(metadata.get("autonomous_target_score") or 0.9)))
        last_eval_signature = str(metadata.get("autonomous_last_eval_signature") or "").strip()
        last_refine_signature = str(metadata.get("autonomous_last_refine_signature") or "").strip()

        if state == "converged":
            return {
                "status": "completed",
                "reason": "autonomous_converged",
                "message": (
                    f"Autonomous tuner finished cleanly for `{pipeline_name}` after orchestration "
                    f"`{orchestration_id}` reached the target quality score {score:.3f}."
                ),
            }
        if state == "max_iterations_reached":
            return {
                "status": "failed",
                "reason": "autonomous_max_iterations_reached",
                "message": (
                    f"Autonomous tuner stopped for `{pipeline_name}` after {current_iteration} iteration(s) "
                    f"without reaching target score {target_score:.3f}. Last score: {score:.3f}."
                ),
            }
        if state in {"launch_failed", "refinement_launch_failed"}:
            return {
                "status": "failed",
                "reason": state,
                "message": (
                    f"Autonomous tuner stopped for `{pipeline_name}` because it could not launch the next "
                    f"remediation iteration after orchestration `{orchestration_id}` finished."
                ),
            }
        if (
            failed_tasks > 0
            and last_eval_signature
            and state in {"needs_refinement", "tune", "inspect_failures", ""}
            and last_refine_signature == last_eval_signature
        ):
            return {
                "status": "failed",
                "reason": "autonomous_stalled_after_evaluation",
                "message": (
                    f"Autonomous tuner stopped for `{pipeline_name}` because orchestration `{orchestration_id}` "
                    f"is terminal with {failed_tasks} failed task(s), but no new remediation iteration was launched."
                ),
            }
        if failed_tasks > 0 and current_iteration >= max_iterations:
            return {
                "status": "failed",
                "reason": "autonomous_max_iterations_reached",
                "message": (
                    f"Autonomous tuner stopped for `{pipeline_name}` after hitting the iteration cap with "
                    f"{failed_tasks} failed task(s) remaining in orchestration `{orchestration_id}`."
                ),
            }
        return None

    async def _finalize_autonomous_session_if_terminal(
        self,
        session_id: str,
        *,
        session: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        resolution = self._autonomous_terminal_resolution(session=session, snapshot=snapshot)
        if resolution is None:
            return None
        reason = str(resolution.get("reason") or "autonomous_terminalized")
        next_status = str(resolution.get("status") or "failed")
        message = str(resolution.get("message") or "").strip()
        updated = await self._store.update_session(
            session_id,
            status=next_status,
            metadata={
                "autonomous_terminalized_at": _now(),
                "autonomous_terminal_reason": reason,
                "autonomous_state": "converged" if next_status == "completed" else "stopped",
            },
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_session_terminalized",
                "reason": reason,
                "status": next_status,
                "orchestration_id": snapshot.get("orchestration_id"),
                "runtime_state": snapshot.get("runtime_state"),
            },
        )
        if message:
            await self._store.append_message(
                session_id,
                role="assistant",
                content=message,
                metadata={"source": "autonomous_tuner", "state": reason},
            )
        return updated

    async def post_message(
        self,
        session_id: str,
        *,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message = await self._store.append_message(
            session_id,
            role=role,
            content=content,
            metadata=metadata,
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "session_message",
                "role": role,
                "message_id": message.get("id"),
                "content_preview": str(content or "")[:280],
            },
        )
        session = await self._store.get_session(session_id)
        status = str((session or {}).get("status") or "").strip().lower()
        if session is not None and status == "active":
            await self.ensure_session_loop(session_id)
        elif str(role or "").strip().lower() == "operator":
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    "Message received. Session is not active right now. "
                    "Resume/start the session to execute actions from this instruction."
                ),
                metadata={"source": "session_state_ack", "session_status": status or "unknown"},
            )
        return message

    async def start_deploy_run(self, session_id: str, *, requested_by: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "error", "detail": "session_id is required"}
        existing = self._deploy_tasks.get(sid)
        if existing is not None and not existing.done():
            return {"status": "running", "detail": "deploy runner already active"}
        self._deploy_tasks[sid] = asyncio.create_task(self._deploy_loop(sid, requested_by=requested_by))
        return {"status": "started"}

    async def _session_loop(self, session_id: str) -> None:
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "runtime_loop_started", "started_at": _now()},
        )
        self._last_heartbeat_ts[session_id] = time.monotonic()
        try:
            while True:
                session = await self._store.get_session(session_id)
                if session is None:
                    break
                status = str(session.get("status") or "").strip().lower()
                if status in {"stopped", "completed", "failed", "archived"}:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "runtime_loop_stopped", "status": status, "stopped_at": _now()},
                    )
                    break
                if status == "paused":
                    await asyncio.sleep(1.0)
                    continue

                await self._process_operator_messages(session_id)

                # Check for stall condition
                halt_reason = await self._check_should_halt_as_stalled(session_id)
                if halt_reason:
                    await self._halt_session(
                        session_id,
                        reason=halt_reason,
                        message=(
                            "Platform AI has halted this session: no measurable state change has occurred "
                            "after repeated action attempts. Possible causes: no pipeline selected, ambiguous "
                            "session brief, no writable config, graph state unchanged, or waiting on an active run. "
                            "Review session brief and restart to resume."
                        ),
                    )
                    break

                session_meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
                _has_target = bool(str(session_meta.get("pipeline_bot_id") or "").strip()) or bool(str(session.get("orchestration_id") or "").strip())
                if not _has_target:
                    _waiting_emitted = bool(session_meta.get("_waiting_for_target_emitted"))
                    if not _waiting_emitted:
                        await self._store.append_event(
                            session_id,
                            "action_trace",
                            {"action": "waiting_for_target", "detail": "No pipeline_bot_id or orchestration_id set. Waiting for operator to provide a target."},
                        )
                        await self._store.update_session(session_id, metadata={"_waiting_for_target_emitted": True})

                snapshot = await self._build_progress_snapshot(session)
                signature = str(snapshot.get("signature") or "")
                previous_signature = self._last_progress_signature.get(session_id)
                status_counts = snapshot.get("status_counts") if isinstance(snapshot.get("status_counts"), dict) else {}
                active_tasks = snapshot.get("active_tasks") if isinstance(snapshot.get("active_tasks"), list) else []
                phase = str(snapshot.get("phase") or "observe")
                active_action = str(snapshot.get("active_action") or "monitor_orchestration")
                last_tool = "task_graph_inspector" if snapshot.get("orchestration_id") else None

                await self._store.update_session(
                    session_id,
                    metadata={
                        "runtime_tick": int(snapshot.get("tick") or 0),
                        "current_phase": phase,
                        "active_action": active_action,
                        "last_tool_call": last_tool,
                        "runtime_state": snapshot.get("runtime_state"),
                        "last_heartbeat_at": _now(),
                    },
                )
                await self._run_autonomous_pipeline_tuner(session_id, session=session, snapshot=snapshot)
                session = await self._store.get_session(session_id) or session
                if await self._finalize_autonomous_session_if_terminal(session_id, session=session, snapshot=snapshot):
                    continue
                now_mono = time.monotonic()
                changed = bool(signature) and signature != previous_signature
                heartbeat_due = (now_mono - float(self._last_heartbeat_ts.get(session_id) or 0.0)) >= 30.0

                if changed:
                    self._last_progress_signature[session_id] = signature
                    self._last_heartbeat_ts[session_id] = now_mono
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "runtime_progress",
                            "kind": "decision",
                            "phase": phase,
                            "tick": int(snapshot.get("tick") or 0),
                            "active_action": active_action,
                            "tool": last_tool,
                            "detail": str(snapshot.get("detail") or ""),
                            "runtime_state": snapshot.get("runtime_state"),
                        },
                    )
                elif heartbeat_due:
                    self._last_heartbeat_ts[session_id] = now_mono
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "runtime_heartbeat",
                            "kind": "outcome",
                            "phase": phase,
                            "active_action": active_action,
                            "detail": str(snapshot.get("heartbeat_detail") or "Monitoring session target."),
                            "runtime_state": snapshot.get("runtime_state"),
                        },
                    )

                if not snapshot.get("orchestration_id"):
                    await asyncio.sleep(4.0)
                elif bool(status_counts.get("running")) or bool(active_tasks):
                    await asyncio.sleep(1.5)
                else:
                    await asyncio.sleep(3.0)
        except asyncio.CancelledError:
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "runtime_loop_cancelled", "cancelled_at": _now()},
            )
            raise
        finally:
            current = self._session_tasks.get(session_id)
            if current is asyncio.current_task():
                self._session_tasks.pop(session_id, None)
            self._last_progress_signature.pop(session_id, None)
            self._last_heartbeat_ts.pop(session_id, None)

    async def _process_operator_messages(self, session_id: str) -> None:
        seen = self._processed_operator_messages.setdefault(session_id, set())
        messages = await self._store.list_messages(session_id, limit=300)
        acknowledged_ids: set[str] = set()
        for message in messages:
            role = str(message.get("role") or "").strip().lower()
            if role != "assistant":
                continue
            metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
            source = str(metadata.get("source") or "").strip().lower()
            message_id = str(metadata.get("operator_message_id") or "").strip()
            if source == "runtime_ack" and message_id:
                acknowledged_ids.add(message_id)
        for message in messages:
            if str(message.get("role") or "").strip().lower() != "operator":
                continue
            mid = str(message.get("id") or "").strip()
            if not mid or mid in seen or mid in acknowledged_ids:
                continue
            seen.add(mid)
            content = str(message.get("content") or "").strip()
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "operator_message_received",
                    "message_id": mid,
                    "content_preview": content[:280],
                    "kind": "decision",
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Acknowledged. Applying operator direction: {content[:500]}",
                metadata={"source": "runtime_ack", "operator_message_id": mid},
            )
            session = await self._store.get_session(session_id)
            mode = str((session or {}).get("mode") or "").strip().lower()
            if mode == "pipeline_tuner":
                await self._store.update_session(
                    session_id,
                    metadata={
                        "autonomous_enabled": True,
                        "autonomous_goal": content[:4000],
                        "autonomous_goal_updated_at": _now(),
                        "autonomous_goal_message_id": mid,
                    },
                )
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "autonomous_goal_updated",
                        "goal_preview": content[:240],
                        "message_id": mid,
                    },
                )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "operator_message_acknowledged",
                    "message_id": mid,
                    "kind": "outcome",
                    "detail": "Operator instruction has been accepted into session workflow.",
                },
            )
            brief = await self._synthesize_session_brief(session_id, session=session, message_content=content)
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "session_brief_synthesized",
                    "message_id": mid,
                    "tuning_goal_preview": brief.get("tuning_goal", "")[:240],
                },
            )
            await self._store.update_session(session_id, metadata={"no_progress_count": 0})

    async def _bot_label(self, bot_id: str) -> str:
        raw = str(bot_id or "").strip()
        if not raw:
            return ""
        cached = self._bot_name_cache.get(raw)
        if cached:
            return cached
        label = raw
        if self._bot_registry is not None:
            try:
                bot = await self._bot_registry.get(raw)
                name = str(getattr(bot, "name", "") or "").strip()
                if name:
                    label = f"{name} ({raw})"
            except Exception:
                label = raw
        self._bot_name_cache[raw] = label
        return label

    async def _resolve_context(self, session: Dict[str, Any]) -> Dict[str, Any]:
        resolved_assignment_id = str(session.get("assignment_id") or "").strip() or None
        resolved_run_id = str(session.get("run_id") or "").strip() or None
        resolved_orchestration_id = str(session.get("orchestration_id") or "").strip() or None
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}

        run: Optional[Dict[str, Any]] = None
        if self._run_store is not None:
            try:
                if resolved_run_id:
                    run = await self._run_store.get_run(resolved_run_id)
                elif resolved_orchestration_id:
                    run = await self._run_store.get_run_by_orchestration(resolved_orchestration_id)
                elif resolved_assignment_id:
                    run = await self._run_store.get_latest_run_for_assignment(resolved_assignment_id)
            except Exception:
                run = None
        if isinstance(run, dict):
            resolved_assignment_id = str(run.get("assignment_id") or "").strip() or resolved_assignment_id
            resolved_run_id = str(run.get("id") or "").strip() or resolved_run_id
            resolved_orchestration_id = str(run.get("orchestration_id") or "").strip() or resolved_orchestration_id

        graph: Dict[str, Any] = {"nodes": [], "edges": []}
        tasks: List[Dict[str, Any]] = []
        if self._assignment_service is not None and (resolved_run_id or resolved_orchestration_id):
            try:
                graph_resp = await self._assignment_service.get_graph(
                    run_id=resolved_run_id,
                    orchestration_id=resolved_orchestration_id,
                )
            except Exception:
                graph_resp = {}
            if isinstance(graph_resp.get("graph"), dict):
                graph = graph_resp["graph"]
            raw_tasks = graph_resp.get("tasks")
            if isinstance(raw_tasks, list):
                tasks = [task for task in raw_tasks if isinstance(task, dict)]
        if not tasks and self._task_manager is not None and resolved_orchestration_id:
            try:
                listed = await self._task_manager.list_tasks(orchestration_id=resolved_orchestration_id, limit=1000)
                tasks = [task.model_dump() for task in listed]
            except Exception:
                tasks = []
        if not graph.get("nodes") and isinstance(run, dict) and isinstance(run.get("graph_snapshot"), dict):
            graph = run.get("graph_snapshot")  # type: ignore[assignment]

        return {
            "assignment_id": resolved_assignment_id,
            "run_id": resolved_run_id,
            "orchestration_id": resolved_orchestration_id,
            "pipeline_bot_id": str(metadata.get("pipeline_bot_id") or "").strip() or None,
            "pipeline_name": str(metadata.get("pipeline_name") or "").strip() or None,
            "graph": graph if isinstance(graph, dict) else {"nodes": [], "edges": []},
            "tasks": tasks,
        }

    async def _build_progress_snapshot(self, session: Dict[str, Any]) -> Dict[str, Any]:
        context = await self._resolve_context(session)
        tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
        graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        status_counts: Dict[str, int] = {}
        task_rows: List[Dict[str, Any]] = []
        for row in tasks:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip().lower() or "unknown"
            status_counts[status] = int(status_counts.get(status) or 0) + 1
            task_rows.append(row)

        task_rows.sort(key=lambda item: (str(item.get("updated_at") or ""), str(item.get("id") or "")), reverse=True)
        active_tasks: List[Dict[str, Any]] = []
        for row in task_rows:
            status = str(row.get("status") or "").strip().lower()
            if status not in {"running", "queued", "blocked"}:
                continue
            bot_id = str(row.get("bot_id") or "").strip()
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            active_tasks.append(
                {
                    "task_id": str(row.get("id") or "").strip(),
                    "status": status,
                    "bot_id": bot_id or None,
                    "bot": await self._bot_label(bot_id) if bot_id else None,
                    "step_id": str(meta.get("step_id") or "").strip() or None,
                    "updated_at": str(row.get("updated_at") or "").strip() or None,
                }
            )
            if len(active_tasks) >= 8:
                break

        latest = task_rows[0] if task_rows else None
        latest_status = str((latest or {}).get("status") or "").strip().lower() if isinstance(latest, dict) else ""
        latest_bot_id = str((latest or {}).get("bot_id") or "").strip() if isinstance(latest, dict) else ""
        latest_task: Optional[Dict[str, Any]] = None
        if isinstance(latest, dict):
            latest_task = {
                "task_id": str(latest.get("id") or "").strip() or None,
                "status": latest_status or None,
                "bot_id": latest_bot_id or None,
                "bot": await self._bot_label(latest_bot_id) if latest_bot_id else None,
                "updated_at": str(latest.get("updated_at") or "").strip() or None,
            }

        focus_nodes: List[Dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_status = str(node.get("status") or "").strip().lower() or "queued"
            if node_status in {"succeeded", "skipped"}:
                continue
            node_id = str(node.get("id") or node.get("bot_id") or "").strip()
            if not node_id:
                continue
            focus_nodes.append(
                {
                    "node_id": node_id,
                    "title": str(node.get("title") or "").strip() or node_id,
                    "status": node_status,
                    "stage_kind": str(node.get("stage_kind") or "").strip() or None,
                }
            )
            if len(focus_nodes) >= 8:
                break

        total_tasks = len(task_rows)
        completed_like = (
            int(status_counts.get("completed") or 0)
            + int(status_counts.get("failed") or 0)
            + int(status_counts.get("cancelled") or 0)
            + int(status_counts.get("retried") or 0)
        )
        progress_ratio = (completed_like / total_tasks) if total_tasks else 0.0
        terminal_counts = _counts_are_terminal(status_counts)
        terminal_failure = bool(total_tasks) and terminal_counts and completed_like >= total_tasks and int(status_counts.get("failed") or 0) > 0
        phase = "observe"
        active_action = "monitor_pipeline"
        if not context.get("orchestration_id"):
            active_action = "await_orchestration_attachment"
            phase = "observe"
        elif terminal_failure:
            active_action = "terminal_failure_detected"
            phase = "evaluate"
        elif int(status_counts.get("running") or 0) > 0:
            active_action = "monitor_running_bots"
            phase = "diagnose"
        elif int(status_counts.get("failed") or 0) > 0:
            active_action = "inspect_failures"
            phase = "tune"
        elif total_tasks and int(status_counts.get("completed") or 0) == total_tasks:
            active_action = "verify_outputs"
            phase = "verify"
        elif total_tasks:
            active_action = "await_next_stage"
            phase = "observe"

        running_bot_labels = [str(item.get("bot") or item.get("bot_id") or "").strip() for item in active_tasks if str(item.get("status") or "") == "running"]
        running_bot_labels = [item for item in running_bot_labels if item]
        if not context.get("orchestration_id"):
            detail = "No orchestration attached yet. Attach an orchestration ID or launch an isolated pipeline test."
            heartbeat_detail = "Waiting for attached orchestration ID."
        elif not total_tasks:
            detail = f"Attached to orchestration {context.get('orchestration_id')}, waiting for tasks to appear."
            heartbeat_detail = "No tasks available yet for the attached orchestration."
        elif terminal_failure:
            failed_count = int(status_counts.get("failed") or 0)
            detail = (
                f"Orchestration {context.get('orchestration_id')} is terminal with {failed_count} failed task(s) "
                f"out of {total_tasks}. Waiting for autonomous remediation or terminal stop."
            )
            heartbeat_detail = (
                f"Terminal failure detected ({completed_like}/{total_tasks} processed tasks, "
                f"failed={failed_count})."
            )
        else:
            detail = (
                f"Tracking {total_tasks} tasks: running={int(status_counts.get('running') or 0)}, "
                f"queued={int(status_counts.get('queued') or 0)}, blocked={int(status_counts.get('blocked') or 0)}, "
                f"completed={int(status_counts.get('completed') or 0)}, failed={int(status_counts.get('failed') or 0)}."
            )
            if running_bot_labels:
                detail += f" Active bots: {', '.join(running_bot_labels[:4])}."
            heartbeat_detail = f"Monitoring orchestration progress ({completed_like}/{total_tasks} processed tasks)."

        runtime_state = {
            "assignment_id": context.get("assignment_id"),
            "run_id": context.get("run_id"),
            "orchestration_id": context.get("orchestration_id"),
            "pipeline_bot_id": context.get("pipeline_bot_id"),
            "pipeline_name": context.get("pipeline_name"),
            "status_counts": status_counts,
            "task_total": total_tasks,
            "graph_node_total": len(nodes),
            "progress_ratio": round(progress_ratio, 4),
            "active_tasks": active_tasks,
            "focus_nodes": focus_nodes,
            "latest_task": latest_task,
        }
        signature_payload = {
            "assignment_id": runtime_state.get("assignment_id"),
            "run_id": runtime_state.get("run_id"),
            "orchestration_id": runtime_state.get("orchestration_id"),
            "status_counts": runtime_state.get("status_counts"),
            "active_tasks": [
                {
                    "task_id": str(item.get("task_id") or ""),
                    "status": str(item.get("status") or ""),
                    "bot_id": str(item.get("bot_id") or ""),
                    "updated_at": str(item.get("updated_at") or ""),
                }
                for item in active_tasks
            ],
            "latest_task": {
                "task_id": str((latest_task or {}).get("task_id") or ""),
                "status": str((latest_task or {}).get("status") or ""),
                "updated_at": str((latest_task or {}).get("updated_at") or ""),
            },
        }
        signature = json.dumps(signature_payload, sort_keys=True, ensure_ascii=False)
        tick = int((session.get("metadata") or {}).get("runtime_tick") or 0) + 1
        return {
            "signature": signature,
            "tick": tick,
            "phase": phase,
            "active_action": active_action,
            "detail": detail,
            "heartbeat_detail": heartbeat_detail,
            "runtime_state": runtime_state,
            "status_counts": status_counts,
            "active_tasks": active_tasks,
            "orchestration_id": context.get("orchestration_id"),
        }

    async def _pipeline_name_for_bot_id(self, bot_id: str) -> str:
        safe_bot_id = str(bot_id or "").strip()
        if not safe_bot_id:
            return ""
        if self._bot_registry is None:
            return safe_bot_id
        try:
            bot = await self._bot_registry.get(safe_bot_id)
        except Exception:
            return safe_bot_id
        capabilities = getattr(bot, "assignment_capabilities", None)
        if hasattr(capabilities, "model_dump"):
            capabilities = capabilities.model_dump()
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        routing = getattr(bot, "routing_rules", None)
        routing = routing if isinstance(routing, dict) else {}
        launch_profile = routing.get("launch_profile") if isinstance(routing.get("launch_profile"), dict) else {}
        return (
            str(capabilities.get("pipeline_name") or "").strip()
            or str(launch_profile.get("pipeline_name") or "").strip()
            or str(launch_profile.get("label") or "").strip()
            or str(getattr(bot, "name", "") or "").strip()
            or safe_bot_id
        )

    async def _pipeline_launch_payload(self, *, pipeline_bot_id: str, goal: str) -> Dict[str, Any]:
        safe_bot_id = str(pipeline_bot_id or "").strip()
        fallback = {"instruction": (goal[:2000] if goal else f"Run pipeline test for {safe_bot_id}")}
        if self._bot_registry is None:
            return fallback
        try:
            bot = await self._bot_registry.get(safe_bot_id)
        except Exception:
            return fallback
        routing = getattr(bot, "routing_rules", None)
        routing = routing if isinstance(routing, dict) else {}
        launch_profile = routing.get("launch_profile") if isinstance(routing.get("launch_profile"), dict) else {}
        launch_payload = launch_profile.get("payload") if isinstance(launch_profile.get("payload"), dict) else {}
        if not launch_payload:
            return fallback
        merged = dict(launch_payload)
        if goal and not str(merged.get("instruction") or "").strip():
            merged["instruction"] = goal[:2000]
        return merged

    def _derive_pipeline_bot_id(self, *, context: Dict[str, Any], session_metadata: Dict[str, Any]) -> Optional[str]:
        from_meta = str(session_metadata.get("pipeline_bot_id") or "").strip()
        if from_meta:
            return from_meta
        tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
        for row in tasks:
            if not isinstance(row, dict):
                continue
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            pipeline_entry = str(metadata.get("pipeline_entry_bot_id") or "").strip()
            if pipeline_entry:
                return pipeline_entry
        graph = context.get("graph") if isinstance(context.get("graph"), dict) else {}
        nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
        if nodes:
            first = nodes[0]
            if isinstance(first, dict):
                node_id = str(first.get("id") or first.get("bot_id") or "").strip()
                if node_id:
                    return node_id
        return None

    def _goal_keywords(self, goal: str) -> List[str]:
        raw = str(goal or "").lower()
        tokens = re.findall(r"[a-z0-9_]{5,}", raw)
        blocked = {
            "should",
            "could",
            "would",
            "their",
            "there",
            "about",
            "through",
            "while",
            "these",
            "those",
            "pipeline",
            "please",
            "tests",
            "suite",
            "quality",
            "output",
            "correct",
        }
        seen: List[str] = []
        for token in tokens:
            if token in blocked:
                continue
            if token not in seen:
                seen.append(token)
            if len(seen) >= 8:
                break
        return seen

    def _merge_autotune_directives(self, system_prompt: str, directives: str) -> str:
        start_marker = "[[NEXUS_PLATFORM_AI_AUTOTUNE_START]]"
        end_marker = "[[NEXUS_PLATFORM_AI_AUTOTUNE_END]]"
        base = str(system_prompt or "").strip()
        block = f"{start_marker}\n{directives.strip()}\n{end_marker}".strip()
        if start_marker in base and end_marker in base:
            pattern = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
            return pattern.sub(block, base).strip()
        if not base:
            return block
        return f"{base}\n\n{block}".strip()

    async def _apply_bot_refinement(
        self,
        *,
        session_id: str,
        pipeline_bot_id: str,
        iteration: int,
        goal: str,
        evaluation: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self._bot_registry is None:
            return {"updated": False, "reason": "bot_registry_unavailable"}
        safe_bot_id = str(pipeline_bot_id or "").strip()
        if not safe_bot_id:
            return {"updated": False, "reason": "pipeline_bot_id_missing"}
        try:
            bot = await self._bot_registry.get(safe_bot_id)
        except Exception as exc:
            return {"updated": False, "reason": f"bot_lookup_failed:{exc}"}

        failed_tests = [
            item
            for item in (evaluation.get("tests") if isinstance(evaluation.get("tests"), list) else [])
            if isinstance(item, dict) and not bool(item.get("passed"))
        ]
        failed_assertions: List[str] = []
        for test in failed_tests[:5]:
            assertions = test.get("assertions") if isinstance(test.get("assertions"), list) else []
            for check in assertions:
                if not isinstance(check, dict):
                    continue
                if bool(check.get("passed")):
                    continue
                failed_assertions.append(str(check.get("kind") or "assertion"))
                if len(failed_assertions) >= 8:
                    break
            if len(failed_assertions) >= 8:
                break
        keywords = self._goal_keywords(goal)
        directives = [
            f"Platform AI tuning iteration: {iteration}",
            f"Goal summary: {goal[:1200] if goal else 'Improve end-to-end execution and output quality.'}",
            f"Failed tests: {', '.join(str(item.get('id') or item.get('name') or 'test') for item in failed_tests[:5]) or 'none'}",
            f"Failed assertion kinds: {', '.join(failed_assertions) or 'none'}",
            "Requirements:",
            "- Produce deterministic, structured outputs with explicit quality sections and acceptance checks.",
            "- Prioritize passing no_failed_tasks, completed_ratio, node_coverage_ratio, and min_avg_quality checks.",
            "- Avoid partial/incomplete outputs; prefer complete artifacts with validation notes.",
        ]
        if keywords:
            directives.append(f"- Ensure outputs explicitly cover: {', '.join(keywords)}.")
        existing_prompt = str(getattr(bot, "system_prompt", "") or "")
        new_prompt = self._merge_autotune_directives(existing_prompt, "\n".join(directives))
        routing_rules = getattr(bot, "routing_rules", None)
        routing_rules = copy.deepcopy(routing_rules) if isinstance(routing_rules, dict) else {}
        tuner_meta = routing_rules.get("platform_ai_tuner") if isinstance(routing_rules.get("platform_ai_tuner"), dict) else {}
        tuner_meta.update(
            {
                "last_refined_at": _now(),
                "last_iteration": iteration,
                "last_goal": goal[:2000],
                "last_score": float(evaluation.get("score") or 0.0),
                "last_status": str(evaluation.get("status") or ""),
                "failed_tests": [str(item.get("id") or item.get("name") or "") for item in failed_tests[:10]],
                "failed_assertions": failed_assertions,
            }
        )
        routing_rules["platform_ai_tuner"] = tuner_meta
        updated = bot.model_copy(update={"system_prompt": new_prompt, "routing_rules": routing_rules})
        try:
            await self._bot_registry.update(safe_bot_id, updated)
        except Exception as exc:
            return {"updated": False, "reason": f"bot_update_failed:{exc}"}
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_bot_refined",
                "pipeline_bot_id": safe_bot_id,
                "iteration": iteration,
                "failed_tests": [str(item.get("id") or item.get("name") or "") for item in failed_tests[:5]],
                "failed_assertions": failed_assertions[:8],
            },
        )
        return {"updated": True}

    async def _refine_suite_definition(
        self,
        *,
        base_suite: Dict[str, Any],
        graph: Dict[str, Any],
        evaluation: Dict[str, Any],
        goal: str,
        iteration: int,
    ) -> Dict[str, Any]:
        suite = copy.deepcopy(base_suite if isinstance(base_suite, dict) else {})
        tests = suite.get("tests") if isinstance(suite.get("tests"), list) else []
        keywords = self._goal_keywords(goal)
        failed_tests = [
            item
            for item in (evaluation.get("tests") if isinstance(evaluation.get("tests"), list) else [])
            if isinstance(item, dict) and not bool(item.get("passed"))
        ]
        target_nodes = _critical_nodes(graph)
        dynamic_test = {
            "id": f"autonomous-iteration-{iteration}",
            "name": f"Autonomous Iteration {iteration} Regression Gate",
            "type": "expectation",
            "weight": 0.35,
            "pass_threshold": min(0.95, max(0.75, float(suite.get("suite_pass_threshold") or 0.8))),
            "assertions": [
                {"kind": "no_failed_tasks"},
                {"kind": "min_completed_ratio", "value": 1.0},
                {"kind": "min_avg_quality", "value": min(0.92, max(0.75, float(evaluation.get("score") or 0.75))), "target_nodes": target_nodes},
            ],
        }
        if keywords:
            dynamic_test["assertions"].append({"kind": "required_keywords", "keywords": keywords, "target_nodes": target_nodes})
        failed_assertion_kinds: List[str] = []
        for test in failed_tests[:5]:
            assertions = test.get("assertions") if isinstance(test.get("assertions"), list) else []
            for check in assertions:
                if not isinstance(check, dict) or bool(check.get("passed")):
                    continue
                failed_assertion_kinds.append(str(check.get("kind") or "").strip())
        if "required_fields" in failed_assertion_kinds:
            dynamic_test["assertions"].append(
                {
                    "kind": "required_fields",
                    "fields": ["summary", "quality_gates", "acceptance_criteria"],
                    "target_nodes": target_nodes,
                }
            )
        tests = [item for item in tests if not (isinstance(item, dict) and str(item.get("id") or "").strip() == dynamic_test["id"])]
        tests.append(dynamic_test)
        suite["tests"] = tests
        suite["version"] = f"v1-autonomous-{iteration}"
        suite["generated_at"] = _now()
        suite["suite_pass_threshold"] = min(0.98, max(0.8, float(suite.get("suite_pass_threshold") or 0.8)))
        return suite

    async def _launch_autonomous_orchestration(
        self,
        *,
        session_id: str,
        pipeline_bot_id: str,
        pipeline_name: str,
        goal: str,
        reason: str,
        iteration: int,
    ) -> Optional[str]:
        if self._task_manager is None:
            return None
        launch_orchestration_id = str(uuid.uuid4())
        payload = await self._pipeline_launch_payload(pipeline_bot_id=pipeline_bot_id, goal=goal)
        try:
            created = await self._task_manager.create_task(
                bot_id=pipeline_bot_id,
                payload=payload,
                metadata=TaskMetadata(
                    source="platform_ai_autonomous_tuner",
                    orchestration_id=launch_orchestration_id,
                    pipeline_name=pipeline_name or pipeline_bot_id,
                    pipeline_entry_bot_id=pipeline_bot_id,
                ),
            )
        except Exception as exc:
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "autonomous_orchestration_launch_failed", "reason": reason, "detail": str(exc)},
            )
            return None
        await self._store.update_session(
            session_id,
            orchestration_id=launch_orchestration_id,
            metadata={
                "autonomous_launch_state": "launched",
                "autonomous_launched_orchestration_id": launch_orchestration_id,
                "autonomous_launched_task_id": str(getattr(created, "id", "") or ""),
                "autonomous_iteration": int(iteration),
                "autonomous_state": "running_iteration",
                "autonomous_current_reason": reason,
            },
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_orchestration_launched",
                "reason": reason,
                "iteration": int(iteration),
                "pipeline_bot_id": pipeline_bot_id,
                "pipeline_name": pipeline_name or pipeline_bot_id,
                "orchestration_id": launch_orchestration_id,
                "task_id": str(getattr(created, "id", "") or ""),
            },
        )
        await self._store.append_message(
            session_id,
            role="assistant",
            content=(
                f"Autonomous tuner launched orchestration `{launch_orchestration_id}` "
                f"(iteration {iteration}, reason: {reason}) for `{pipeline_name or pipeline_bot_id}`."
            ),
            metadata={"source": "autonomous_tuner", "iteration": int(iteration), "reason": reason},
        )
        return launch_orchestration_id

    async def _run_autonomous_pipeline_tuner(
        self,
        session_id: str,
        *,
        session: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> None:
        mode = str(session.get("mode") or "").strip().lower()
        if mode != "pipeline_tuner":
            return
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        if not bool(metadata.get("autonomous_enabled")):
            return
        context = await self._resolve_context(session)
        pipeline_bot_id = self._derive_pipeline_bot_id(context=context, session_metadata=metadata)
        pipeline_name = str(metadata.get("pipeline_name") or "").strip()
        if not pipeline_name and pipeline_bot_id:
            pipeline_name = await self._pipeline_name_for_bot_id(pipeline_bot_id)
        orchestration_id = str(context.get("orchestration_id") or "").strip()
        goal = str(metadata.get("autonomous_goal") or "").strip()
        current_iteration = int(metadata.get("autonomous_iteration") or 0)
        max_iterations = max(1, min(25, int(metadata.get("autonomous_max_iterations") or 6)))
        target_score = max(0.6, min(0.99, float(metadata.get("autonomous_target_score") or 0.9)))

        if pipeline_bot_id and (
            str(metadata.get("pipeline_bot_id") or "").strip() != pipeline_bot_id
            or str(metadata.get("pipeline_name") or "").strip() != pipeline_name
        ):
            await self._store.update_session(
                session_id,
                metadata={
                    "pipeline_bot_id": pipeline_bot_id,
                    "pipeline_name": pipeline_name or pipeline_bot_id,
                },
            )

        if not orchestration_id and pipeline_bot_id and self._task_manager is not None:
            launch_lock = str(metadata.get("autonomous_launch_state") or "").strip().lower()
            if launch_lock not in {"launched", "launching"}:
                await self._store.update_session(session_id, metadata={"autonomous_launch_state": "launching"})
                launched = await self._launch_autonomous_orchestration(
                    session_id=session_id,
                    pipeline_bot_id=pipeline_bot_id,
                    pipeline_name=pipeline_name or pipeline_bot_id,
                    goal=goal,
                    reason="initial",
                    iteration=max(1, current_iteration),
                )
                if not launched:
                    await self._store.update_session(
                        session_id,
                        metadata={"autonomous_launch_state": "failed", "autonomous_launch_error": "launch_failed"},
                    )
            return

        tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
        graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
        if not orchestration_id or not tasks:
            return

        eval_signature_preview = f"{orchestration_id}:{len(tasks)}"
        action_snapshot = {"orchestration_id": orchestration_id, "eval_signature": eval_signature_preview}
        recent_actions = await self._store.list_actions(session_id, limit=10)
        current_input_hash = self._compute_state_hash(action_snapshot)
        if any(
            str(a.get("input_snapshot_hash") or "") == current_input_hash
            and str(a.get("action_type") or "") == "run_autonomous_pipeline_tuner"
            for a in recent_actions
        ):
            dedup_action = await self._store.create_action(
                session_id,
                action_type="run_autonomous_pipeline_tuner",
                input_snapshot_hash=current_input_hash,
                rationale="Dedup: identical snapshot already processed",
            )
            await self._store.update_action(
                dedup_action["id"],
                status="no_op",
                state_delta_summary="",
            )
            return

        action = await self._create_action_record(
            session_id,
            action_type="run_autonomous_pipeline_tuner",
            snapshot=action_snapshot,
            rationale="Autonomous pipeline quality evaluation cycle",
        )

        suite_id = str(metadata.get("autonomous_suite_id") or "").strip()
        suite = await self._store.get_test_suite(suite_id) if suite_id else None
        if suite is None:
            existing = await self._store.list_test_suites(
                session_id=session_id,
                pipeline_bot_id=pipeline_bot_id,
                limit=20,
            )
            suite = existing[0] if existing else None
        if suite is None:
            suite_name = f"{pipeline_name or pipeline_bot_id or 'pipeline'} Autonomous Quality Suite"
            suite_def = _build_default_suite(suite_name=suite_name, graph=graph)
            suite = await self._store.create_test_suite(
                session_id=session_id,
                name=suite_name,
                suite=suite_def,
                status="active",
                pipeline_bot_id=pipeline_bot_id,
                assignment_id=context.get("assignment_id"),
                run_id=context.get("run_id"),
                orchestration_id=orchestration_id,
                metadata={"generator": "platform_ai_runtime", "source": "autonomous_tuner"},
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_suite_created",
                    "suite_id": suite.get("id"),
                    "suite_name": suite.get("name"),
                    "pipeline_bot_id": pipeline_bot_id,
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Created autonomous quality suite `{suite.get('name')}` for `{pipeline_name or pipeline_bot_id}`.",
                metadata={"source": "autonomous_tuner"},
            )
        await self._store.update_session(
            session_id,
            metadata={"autonomous_suite_id": str(suite.get("id") or "").strip() or None},
        )

        if not all(str(task.get("status") or "").strip().lower() in _TERMINAL_STATUSES for task in tasks):
            return

        eval_signature = json.dumps(
            {
                "suite_id": str(suite.get("id") or ""),
                "orchestration_id": orchestration_id,
                "tasks": [
                    {
                        "id": str(task.get("id") or ""),
                        "status": str(task.get("status") or ""),
                        "updated_at": str(task.get("updated_at") or ""),
                    }
                    for task in tasks
                    if isinstance(task, dict)
                ],
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        if str(metadata.get("autonomous_last_eval_signature") or "") == eval_signature:
            return

        run_record = await self._store.create_test_run(
            suite_id=str(suite.get("id") or ""),
            session_id=session_id,
            pipeline_bot_id=pipeline_bot_id,
            assignment_id=context.get("assignment_id"),
            run_id=context.get("run_id"),
            orchestration_id=orchestration_id,
            status="running",
            score=0.0,
            result={"started_at": _now(), "source": "autonomous_tuner"},
        )
        suite_payload = suite.get("suite") if isinstance(suite.get("suite"), dict) else {}
        evaluation = _evaluate_suite(suite_payload, [task for task in tasks if isinstance(task, dict)], graph)
        evaluation["context"] = {
            "pipeline_bot_id": pipeline_bot_id,
            "pipeline_name": pipeline_name or pipeline_bot_id,
            "orchestration_id": orchestration_id,
            "assignment_id": context.get("assignment_id"),
            "run_id": context.get("run_id"),
        }
        final_run = await self._store.complete_test_run(
            str(run_record.get("id") or ""),
            status=str(evaluation.get("status") or "failed"),
            score=float(evaluation.get("score") or 0.0),
            result=evaluation,
        )
        eval_status = str(evaluation.get("status") or "failed").strip().lower()
        eval_score = float(evaluation.get("score") or 0.0)
        passed_target = eval_status == "passed" and eval_score >= target_score
        last_eval_status = str(metadata.get("autonomous_last_eval_status") or "").strip().lower()
        await self._store.update_session(
            session_id,
            metadata={
                "autonomous_last_eval_signature": eval_signature,
                "autonomous_last_eval_status": str(evaluation.get("status") or "failed"),
                "autonomous_last_eval_score": float(evaluation.get("score") or 0.0),
                "autonomous_last_eval_run_id": str((final_run or {}).get("id") or ""),
                "autonomous_last_eval_at": _now(),
                "autonomous_state": "converged" if passed_target else "needs_refinement",
            },
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_suite_evaluated",
                "suite_id": suite.get("id"),
                "suite_run_id": (final_run or {}).get("id"),
                "status": (final_run or {}).get("status"),
                "score": (final_run or {}).get("score"),
                "orchestration_id": orchestration_id,
            },
        )
        failed_tests = [
            item
            for item in (evaluation.get("tests") if isinstance(evaluation.get("tests"), list) else [])
            if isinstance(item, dict) and not bool(item.get("passed"))
        ]
        summary = (
            f"Autonomous suite run complete for `{pipeline_name or pipeline_bot_id}` "
            f"on orchestration `{orchestration_id}`: status={evaluation.get('status')} "
            f"score={float(evaluation.get('score') or 0.0):.3f}."
        )
        if failed_tests:
            top = ", ".join(str(item.get("id") or item.get("name") or "test") for item in failed_tests[:3])
            summary += f" Failed checks: {top}."
        await self._store.append_message(
            session_id,
            role="assistant",
            content=summary,
            metadata={"source": "autonomous_tuner", "suite_run_id": (final_run or {}).get("id")},
        )

        if not passed_target:
            _existing_prompt_preview = ""
            if self._bot_registry is not None and pipeline_bot_id:
                try:
                    _bot_for_preview = await self._bot_registry.get(pipeline_bot_id)
                    _existing_prompt_preview = str(getattr(_bot_for_preview, "system_prompt", "") or "")[:500]
                except Exception:
                    _existing_prompt_preview = ""
            _failed_tests_for_pp = [
                item
                for item in (evaluation.get("tests") if isinstance(evaluation.get("tests"), list) else [])
                if isinstance(item, dict) and not bool(item.get("passed"))
            ]
            _failed_assertions_for_pp: List[str] = []
            for _ft in _failed_tests_for_pp[:5]:
                for _check in (_ft.get("assertions") if isinstance(_ft.get("assertions"), list) else []):
                    if isinstance(_check, dict) and not bool(_check.get("passed")):
                        _failed_assertions_for_pp.append(str(_check.get("kind") or "assertion"))
            _next_iter_for_pp = current_iteration + 1
            _patch_proposal = await self._store.create_patch_proposal(
                session_id,
                action_id=action["id"],
                target_config=f"bot:{pipeline_bot_id}:system_prompt",
                before_state={"system_prompt_preview": _existing_prompt_preview},
                after_state={"directives_applied": f"iteration_{_next_iter_for_pp}_refinement"},
                rationale=f"Bot refinement for iteration {_next_iter_for_pp} based on failed tests: {_failed_assertions_for_pp[:5]}",
                expected_effect=f"Pipeline quality score improvement from {eval_score:.3f} toward target {target_score:.3f}",
                validation_steps=["launch_new_orchestration", "evaluate_suite", "compare_score"],
                rollback_note="Remove [[NEXUS_PLATFORM_AI_AUTOTUNE_START]]...[[NEXUS_PLATFORM_AI_AUTOTUNE_END]] block from system_prompt",
            )
            await self._store.append_event(session_id, "action_trace", {
                "action": "patch_proposal_created",
                "proposal_id": _patch_proposal["id"],
                "target_config": _patch_proposal["target_config"],
            })

        if passed_target:
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_converged",
                    "target_score": target_score,
                    "score": eval_score,
                    "iteration": current_iteration,
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"Autonomous tuner reached target quality for `{pipeline_name or pipeline_bot_id}`: "
                    f"score {eval_score:.3f} (target {target_score:.3f})."
                ),
                metadata={"source": "autonomous_tuner", "state": "converged"},
            )
            await self._complete_action_record(
                action["id"],
                output_snapshot={"eval_status": eval_status, "eval_score": eval_score, "launched": False},
                had_effect=eval_status != last_eval_status,
                summary=f"Converged: {eval_status} score={eval_score:.3f}",
            )
            return

        refined_signature = str(metadata.get("autonomous_last_refine_signature") or "")
        if refined_signature == eval_signature:
            await self._complete_action_record(
                action["id"],
                output_snapshot={"eval_status": eval_status, "eval_score": eval_score, "launched": False},
                had_effect=False,
                summary="Refinement signature already processed; no new action taken.",
            )
            return
        if current_iteration >= max_iterations:
            await self._store.update_session(
                session_id,
                metadata={
                    "autonomous_state": "max_iterations_reached",
                    "autonomous_last_refine_signature": eval_signature,
                },
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_max_iterations_reached",
                    "iteration": current_iteration,
                    "max_iterations": max_iterations,
                    "score": eval_score,
                    "target_score": target_score,
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"Autonomous tuner stopped after {current_iteration} iteration(s) without hitting target "
                    f"{target_score:.3f}. Last score: {eval_score:.3f}. Review latest suite run and bot refinements."
                ),
                metadata={"source": "autonomous_tuner", "state": "max_iterations_reached"},
            )
            await self._complete_action_record(
                action["id"],
                output_snapshot={"eval_status": eval_status, "eval_score": eval_score, "launched": False},
                had_effect=eval_status != last_eval_status,
                summary=f"Max iterations reached: {eval_status} score={eval_score:.3f}",
            )
            return

        next_iteration = current_iteration + 1
        refined_suite_payload = await self._refine_suite_definition(
            base_suite=suite.get("suite") if isinstance(suite.get("suite"), dict) else {},
            graph=graph,
            evaluation=evaluation,
            goal=goal,
            iteration=next_iteration,
        )
        refined_suite = await self._store.create_test_suite(
            session_id=session_id,
            name=f"{pipeline_name or pipeline_bot_id} Autonomous Suite v{next_iteration}",
            suite=refined_suite_payload,
            status="active",
            pipeline_bot_id=pipeline_bot_id,
            assignment_id=context.get("assignment_id"),
            run_id=context.get("run_id"),
            orchestration_id=orchestration_id,
            metadata={"generator": "platform_ai_runtime_refine", "iteration": next_iteration, "parent_suite_id": suite.get("id")},
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_suite_refined",
                "previous_suite_id": suite.get("id"),
                "suite_id": refined_suite.get("id"),
                "iteration": next_iteration,
            },
        )
        bot_refine = await self._apply_bot_refinement(
            session_id=session_id,
            pipeline_bot_id=pipeline_bot_id or "",
            iteration=next_iteration,
            goal=goal,
            evaluation=evaluation,
        )
        if not bool(bot_refine.get("updated")):
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_bot_refine_skipped",
                    "iteration": next_iteration,
                    "result": bot_refine,
                },
            )
        launched = await self._launch_autonomous_orchestration(
            session_id=session_id,
            pipeline_bot_id=pipeline_bot_id or "",
            pipeline_name=pipeline_name or (pipeline_bot_id or ""),
            goal=goal,
            reason="refinement_iteration",
            iteration=next_iteration,
        )
        await self._store.update_session(
            session_id,
            metadata={
                "autonomous_iteration": next_iteration,
                "autonomous_suite_id": str(refined_suite.get("id") or ""),
                "autonomous_last_refine_signature": eval_signature,
                "autonomous_last_bot_refine_result": bot_refine,
                "autonomous_state": "running_iteration" if launched else "refinement_launch_failed",
            },
        )
        if launched:
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"Autonomous refinement iteration {next_iteration} applied. "
                    f"Suite `{refined_suite.get('id')}` and bot tuning updated; launched orchestration `{launched}`."
                ),
                metadata={"source": "autonomous_tuner", "iteration": next_iteration},
            )
        await self._complete_action_record(
            action["id"],
            output_snapshot={"eval_status": eval_status, "eval_score": eval_score, "launched": bool(launched)},
            had_effect=bool(launched) or eval_status != last_eval_status,
            summary=f"Evaluation: {eval_status} score={eval_score:.3f}, launched={bool(launched)}",
        )

    async def _deploy_loop(self, session_id: str, *, requested_by: str) -> None:
        last_log_len = 0
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "deploy_runner_started", "requested_by": requested_by, "started_at": _now()},
        )
        try:
            try:
                from dashboard.deploy_manager import DeployManager
            except Exception as exc:
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {"action": "deploy_runner_error", "detail": f"deploy manager unavailable: {exc}"},
                )
                return

            manager = DeployManager.instance()
            ok, message = manager.start(requested_by=requested_by or "platform-ai")
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "deploy_requested", "ok": bool(ok), "message": str(message or "")},
            )
            if not ok:
                return
            while True:
                status = manager.status(refresh_remote=False)
                logs = status.get("log_tail") if isinstance(status.get("log_tail"), list) else []
                for line in logs[last_log_len:]:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "deploy_log", "line": str(line)},
                    )
                last_log_len = len(logs)
                state = str(status.get("state") or "").strip().lower()
                if state in {"succeeded", "failed"}:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "deploy_finished",
                            "state": state,
                            "last_error": status.get("last_error"),
                            "finished_at": status.get("finished_at"),
                        },
                    )
                    if state == "failed":
                        await self._store.append_message(
                            session_id,
                            role="assistant",
                            content=(
                                "Deployment failed. Captured logs were added to action trace. "
                                "Apply fixes, commit/push, then trigger deploy again."
                            ),
                            metadata={"source": "deploy_runner", "state": "failed"},
                        )
                    break
                await asyncio.sleep(2.0)
        finally:
            self._deploy_tasks.pop(session_id, None)

    async def get_session_brief(self, session_id: str) -> Optional[Dict[str, Any]]:
        return await self._store.get_session_brief(session_id)

    async def list_session_actions(self, session_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
        return await self._store.list_actions(session_id, limit=limit)

    async def get_patch_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return await self._store.get_patch_proposal(proposal_id)

    async def list_patch_proposals(self, session_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._store.list_patch_proposals(session_id, limit=limit)

    async def approve_patch_proposal(self, session_id: str, proposal_id: str) -> Dict[str, Any]:
        updated = await self._store.update_patch_proposal_status(proposal_id, "approved")
        if updated is None:
            return {"status": "error", "detail": "proposal_not_found"}
        await self._store.append_event(session_id, "action_trace", {
            "action": "patch_proposal_approved",
            "proposal_id": proposal_id,
            "target_config": updated.get("target_config"),
        })
        return {"status": "approved", "proposal": updated}

    async def halt_session(self, session_id: str, *, reason: str = "operator_halt") -> Dict[str, Any]:
        await self._halt_session(session_id, reason=reason, message=f"Session halted by operator (reason: {reason}).")
        return {"status": "stopped", "reason": reason}

    async def refresh_session_brief(self, session_id: str, *, content: str) -> Dict[str, Any]:
        session = await self._store.get_session(session_id)
        if session is None:
            return {"status": "error", "detail": "session_not_found"}
        brief = await self._synthesize_session_brief(session_id, session=session, message_content=content)
        return {"status": "ok", "brief": brief}
