from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from control_plane.bot_readiness import assess_bot_instance_readiness
from control_plane.platform_ai.session_store import PlatformAISessionStore
from shared.bot_policy import validate_bot_configuration
from shared.exceptions import BotNotFoundError
from shared.models import BackendConfig, BackendParams, Bot, CatalogModel, TaskMetadata


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "retried"}
_QUALITY_FIELDS = {"summary", "quality_gates", "acceptance_criteria", "tests", "artifacts", "warnings", "errors"}
_SESSION_STATUSES = {"ready", "running", "stopped"}
_CONFIGURATION_MUTATION_ACTIONS = {"upsert_bot", "upsert_bots", "delete_bot", "remove_bot", "configure_pipeline_entry"}
_AUTONOMOUS_PIPELINE_ACTIONS = {"set_pipeline_target", "launch_pipeline"}
_APPROVABLE_PROPOSAL_BACKEND_TYPES = {"local_llm", "remote_llm", "cloud_api"}
_DIRECT_CREDENTIAL_PREFIXES = ("sk-", "ghp_", "github_pat_", "xoxb-", "xoxp-", "akia", "aiza")
_PROPOSAL_PREFLIGHT_TTL_SECONDS = 300


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _configuration_mutations_enabled() -> bool:
    return _env_enabled("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED")


def _autonomous_pipeline_runs_enabled() -> bool:
    return _env_enabled("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED")


def _owner_allowlist() -> set[str]:
    raw = str(os.environ.get("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "") or "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _csv_env_values(*names: str) -> set[str]:
    values: set[str] = set()
    for name in names:
        raw = str(os.environ.get(name, "") or "")
        for item in raw.split(","):
            safe = str(item or "").strip()
            if safe:
                values.add(safe)
    return values


def _platform_project_allowlist() -> set[str]:
    values = _csv_env_values("NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ALLOWLIST")
    single = str(os.environ.get("NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ID", "") or "").strip()
    if single:
        values.add(single)
    return values


def _project_edit_project_allowlist() -> set[str]:
    return _csv_env_values("NEXUS_PLATFORM_AI_PROJECT_EDIT_PROJECT_ALLOWLIST")


def _session_allows_auto_bot_activation(metadata: Dict[str, Any]) -> bool:
    """Require a deliberate session grant as well as a deployment-level opt-in."""
    return _env_enabled("NEXUS_PLATFORM_AI_AUTO_ACTIVATE_BOTS") and metadata.get("allow_bot_activation") is True


def _proposal_preflight_is_fresh(preflight: Dict[str, Any]) -> bool:
    checked_at = str(preflight.get("checked_at") or "").strip()
    if not checked_at:
        return False
    try:
        timestamp = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    return 0 <= age_seconds <= _PROPOSAL_PREFLIGHT_TTL_SECONDS


def _safe_timeout_seconds(env_name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = str(os.environ.get(env_name, "") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except Exception:
        return default
    return max(min_value, min(max_value, value))


def _extract_json_chunks(text: str) -> List[Any]:
    chunks: List[Any] = []
    raw = str(text or "")
    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
    for match in fence_pattern.finditer(raw):
        candidate = str(match.group(1) or "").strip()
        if not candidate:
            continue
        try:
            chunks.append(json.loads(candidate))
        except Exception:
            continue
    if not chunks:
        direct = raw.strip()
        if direct and (direct.startswith("{") or direct.startswith("[")):
            try:
                chunks.append(json.loads(direct))
            except Exception:
                pass
    return chunks


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


def _build_default_suite(
    *,
    suite_name: str,
    graph: Dict[str, Any],
    brief_expected_deliverables: Optional[List[str]] = None,
    brief_forbidden: Optional[List[str]] = None,
) -> Dict[str, Any]:
    tests = [
        {
            "id": "pipeline-completion",
            "name": "Pipeline completes without failed nodes",
            "type": "pipeline",
            "weight": 0.30,
            "pass_threshold": 0.95,
            "assertions": [{"kind": "no_failed_tasks"}, {"kind": "min_completed_ratio", "value": 1.0}],
        },
        {
            "id": "graph-coverage",
            "name": "Graph nodes are represented in run execution",
            "type": "coverage",
            "weight": 0.20,
            "pass_threshold": 0.9,
            "assertions": [{"kind": "node_coverage_ratio", "value": 1.0}],
        },
        {
            "id": "critical-quality",
            "name": "Critical stages meet quality signals",
            "type": "quality",
            "weight": 0.25,
            "pass_threshold": 0.8,
            "assertions": [{"kind": "min_avg_quality", "value": 0.7, "target_nodes": _critical_nodes(graph)}],
        },
        {
            "id": "terminal-stage",
            "name": "Terminal delivery stage was reached and completed",
            "type": "topology",
            "weight": 0.15,
            "pass_threshold": 1.0,
            "assertions": [{"kind": "terminal_stage_reached"}],
        },
        {
            "id": "no-stall-loop",
            "name": "No stage spun in a stalled loop",
            "type": "topology",
            "weight": 0.10,
            "pass_threshold": 1.0,
            # Tighter threshold when operator explicitly forbids spinning behavior.
            "assertions": [{"kind": "no_stalled_loop", "value": 3 if brief_forbidden else 5}],
        },
    ]
    # If the session brief declares expected deliverables, add materialization
    # assertions so their stages must be reached and completed.  These are
    # zero-weight extras — they inform the report without skewing the weighted
    # score because they're already covered by terminal-stage/graph-coverage.
    if brief_expected_deliverables:
        for deliverable in brief_expected_deliverables:
            stage_hint = deliverable.lower().replace(" ", "_")
            tests.append({
                "id": f"deliverable-{stage_hint}",
                "name": f"Expected deliverable stage '{deliverable}' materialized",
                "type": "topology",
                "weight": 0.0,
                "pass_threshold": 1.0,
                "assertions": [{"kind": "required_stage_materialization", "target_node": stage_hint}],
            })
    return {
        "name": suite_name,
        "version": "v1",
        "generated_at": _now(),
        "suite_pass_threshold": 0.8,
        "graph_nodes": _node_ids(graph),
        "tests": tests,
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
        scheduler: Any = None,
        worker_registry: Any = None,
        connection_resolver: Any = None,
        worker_probe_store: Any = None,
        key_vault: Any = None,
    ) -> None:
        self._store = store
        self._assignment_service = assignment_service
        self._run_store = run_store
        self._task_manager = task_manager
        self._bot_registry = bot_registry
        self._scheduler = scheduler
        self._worker_registry = worker_registry
        self._connection_resolver = connection_resolver
        self._worker_probe_store = worker_probe_store
        self._key_vault = key_vault
        self._session_tasks: Dict[str, asyncio.Task[None]] = {}
        self._deploy_tasks: Dict[str, asyncio.Task[None]] = {}
        self._repo_edit_tasks: Dict[str, asyncio.Task[None]] = {}
        self._project_edit_tasks: Dict[str, asyncio.Task[None]] = {}
        self._processed_operator_messages: Dict[str, set[str]] = {}
        self._last_progress_signature: Dict[str, str] = {}
        self._last_heartbeat_ts: Dict[str, float] = {}
        self._bot_name_cache: Dict[str, str] = {}
        self._session_wake_events: Dict[str, asyncio.Event] = {}
        self._session_task_lock = asyncio.Lock()

    async def ensure_session_loop(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if not sid:
            return
        async with self._session_task_lock:
            task = self._session_tasks.get(sid)
            if task is not None and not task.done():
                wake = self._session_wake_events.get(sid)
                if wake is not None:
                    wake.set()
                return
            wake = self._session_wake_events.get(sid)
            if wake is None:
                wake = asyncio.Event()
                self._session_wake_events[sid] = wake
            wake.clear()
            self._session_tasks[sid] = asyncio.create_task(self._session_loop(sid))

    async def stop_session_loop(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        task = self._session_tasks.get(sid)
        if task is None:
            return
        wake = self._session_wake_events.get(sid)
        if wake is not None:
            wake.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _sleep_until_poked(self, session_id: str, delay_seconds: float) -> None:
        sid = str(session_id or "").strip()
        if delay_seconds <= 0:
            await asyncio.sleep(0)
            return
        wake = self._session_wake_events.get(sid)
        if wake is None:
            await asyncio.sleep(delay_seconds)
            return
        try:
            await asyncio.wait_for(wake.wait(), timeout=delay_seconds)
        except asyncio.TimeoutError:
            pass
        finally:
            wake.clear()

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

    def _platform_backend_from_session(self, session: Dict[str, Any]) -> Optional[BackendConfig]:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        backend = metadata.get("backend") if isinstance(metadata.get("backend"), dict) else {}
        provider = str(backend.get("provider") or "").strip().lower()
        model = str(backend.get("model") or "").strip()
        backend_type = str(backend.get("backend_type") or "").strip() or "cloud_api"
        if not provider or not model:
            return None
        params_raw = backend.get("params") if isinstance(backend.get("params"), dict) else {}
        allowed_param_keys = set(getattr(BackendParams, "model_fields", {}).keys())
        params_filtered = {key: params_raw.get(key) for key in params_raw.keys() if key in allowed_param_keys}
        payload: Dict[str, Any] = {
            "type": backend_type,
            "provider": provider,
            "model": model,
            "api_key_ref": str(backend.get("credential_ref") or "").strip() or None,
            "worker_id": str(backend.get("worker_id") or "").strip() or None,
        }
        command = str(backend.get("command") or "").strip()
        if command:
            payload["command"] = command
        if params_filtered:
            payload["params"] = params_filtered
        try:
            return BackendConfig.model_validate(payload)
        except Exception:
            return None

    def _build_platform_brain_messages(
        self,
        *,
        session: Dict[str, Any],
        operator_message: str,
        recent_messages: List[Dict[str, Any]],
    ) -> List[Dict[str, str]]:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        runtime_state = metadata.get("runtime_state") if isinstance(metadata.get("runtime_state"), dict) else {}
        status_counts = runtime_state.get("status_counts") if isinstance(runtime_state.get("status_counts"), dict) else {}
        transcript_lines: List[str] = []
        for row in recent_messages[-14:]:
            role = str(row.get("role") or "").strip().lower() or "operator"
            content = str(row.get("content") or "").strip()
            if not content:
                continue
            transcript_lines.append(f"{role}: {content[:1200]}")
        transcript = "\n".join(transcript_lines[-12:])
        mode = str(session.get("mode") or "").strip().lower()
        status = str(session.get("status") or "").strip().lower()
        session_scope = {
            "mode": mode,
            "status": status,
            "pipeline_bot_id": str(metadata.get("pipeline_bot_id") or "").strip() or None,
            "target_bot_id": str(metadata.get("target_bot_id") or "").strip() or None,
            "project_id": str(metadata.get("project_id") or "").strip() or None,
            "conversation_id": str(metadata.get("conversation_id") or "").strip() or None,
            "orchestration_id": str(session.get("orchestration_id") or "").strip() or None,
            "runtime_status_counts": status_counts,
        }
        runtime_policy = (
            "Configuration changes and autonomous pipeline launches are disabled. "
            "Return those actions as proposals only; they will not execute until an operator enables the matching runtime policy."
            if not _configuration_mutations_enabled() or not _autonomous_pipeline_runs_enabled()
            else "Configuration changes and autonomous pipeline launches are enabled for this deployment."
        )
        system_prompt = (
            "You are Platform AI, an autonomous operator for NexusAI sessions. "
            "Session mode decides scope: bot_tuner edits only target_bot_id, pipeline_tuner edits only pipeline graph bots. "
            "Keep responses concise and actionable. "
            "If a concrete tool action is needed, return JSON with actions using `platform_ai_action` values. "
            "Allowed actions include upsert_bot, upsert_bots, delete_bot, set_pipeline_target, launch_pipeline, "
            "project_code_edit, repo_edit, external_repo_edit, deploy. "
            "Never produce actions outside scope. "
            f"{runtime_policy}"
        )
        user_prompt = (
            f"Session scope:\n{json.dumps(session_scope, ensure_ascii=False)}\n\n"
            f"Conversation excerpt:\n{transcript}\n\n"
            f"Latest operator message:\n{operator_message[:6000]}\n\n"
            "Respond with JSON only. Schema:\n"
            "{\"assistant_reply\":\"...\", \"actions\":[...]}.\n"
            "Use an empty actions array when no immediate action is required."
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    def _parse_platform_brain_output(self, raw_output: str) -> Dict[str, Any]:
        text = str(raw_output or "").strip()
        reply = text
        actions: List[Dict[str, Any]] = []
        chunks = _extract_json_chunks(text)
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            parsed_reply = str(
                chunk.get("assistant_reply")
                or chunk.get("reply")
                or chunk.get("message")
                or ""
            ).strip()
            parsed_actions = chunk.get("actions") if isinstance(chunk.get("actions"), list) else []
            clean_actions = [item for item in parsed_actions if isinstance(item, dict)]
            if parsed_reply:
                reply = parsed_reply
            if clean_actions:
                actions = clean_actions
            if parsed_reply or clean_actions:
                break
        return {"assistant_reply": reply[:12000], "actions": actions}

    @staticmethod
    def _is_model_catalog_block_error(exc: Exception) -> bool:
        detail = str(exc or "").strip().lower()
        return "not present/enabled in the model catalog" in detail

    @staticmethod
    def _require_platform_brain_for_autonomy() -> bool:
        raw = str(os.environ.get("NEXUS_PLATFORM_AI_REQUIRE_BRAIN_FOR_AUTONOMY", "1") or "").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    @staticmethod
    def _platform_brain_error_hint(*, provider: str, model: str, error: str, session_backend: Dict[str, Any]) -> str:
        safe_provider = str(provider or "").strip().lower()
        detail = str(error or "").strip()
        if safe_provider != "vertex":
            return (
                f"Platform brain backend `{provider}:{model}` failed. "
                "Verify provider credentials and model identifier."
            )
        project_id = str(session_backend.get("vertex_project_id") or "").strip()
        location = str(session_backend.get("vertex_location") or "").strip() or "us-central1"
        notes: List[str] = []
        if project_id and project_id.lower() != project_id:
            notes.append(
                "vertex_project_id appears to be a display name or invalid casing; use the real lowercase GCP project ID"
            )
        if "404" in detail or "rawpredict" in detail.lower():
            notes.append("model endpoint returned 404 from Vertex")
            notes.append("ensure the Anthropic model is enabled for this project/location in Vertex Model Garden")
            notes.append("try a regional location such as us-east5 or us-central1 if global is unavailable")
        if not notes:
            notes.append("verify service-account access, project ID, location, and model availability")
        return (
            f"Platform brain backend `vertex:{model}` failed for project `{project_id or 'unset'}` "
            f"location `{location}`. " + "; ".join(notes) + "."
        )

    async def _auto_register_platform_brain_model(
        self,
        *,
        session_id: str,
        backend: BackendConfig,
    ) -> bool:
        scheduler = self._scheduler
        registry = getattr(scheduler, "model_registry", None) if scheduler is not None else None
        if registry is None:
            return False
        provider = str(getattr(backend, "provider", "") or "").strip().lower()
        model = str(getattr(backend, "model", "") or "").strip()
        if not provider or not model:
            return False
        try:
            exists = await registry.exists(provider, model)
            if exists:
                return False
            digest = hashlib.sha1(f"{provider}:{model}".encode("utf-8")).hexdigest()[:16]
            catalog_model = CatalogModel(
                id=f"platform-ai-auto:{provider}:{digest}",
                provider=provider,
                name=model,
                capabilities=["chat"],
                enabled=True,
                notes="Auto-registered from Platform AI runtime backend config.",
            )
            await registry.register(catalog_model)
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "platform_brain_catalog_autoregistered",
                    "provider": provider,
                    "model": model,
                },
            )
            return True
        except Exception:
            return False

    async def _dispatch_platform_brain_with_fallback(
        self,
        *,
        session_id: str,
        backend: BackendConfig,
        payload: Any,
    ) -> Dict[str, Any]:
        assert self._scheduler is not None
        try:
            raw = await self._scheduler._dispatch_backend(backend, payload, task=None)
            return {"raw": raw, "catalog_fallback_used": False}
        except Exception as exc:
            if not self._is_model_catalog_block_error(exc):
                raise
            auto_registered = await self._auto_register_platform_brain_model(
                session_id=session_id,
                backend=backend,
            )
            if auto_registered:
                try:
                    raw = await self._scheduler._dispatch_backend(backend, payload, task=None)
                    return {"raw": raw, "catalog_fallback_used": False}
                except Exception as retry_exc:
                    if not self._is_model_catalog_block_error(retry_exc):
                        raise
                    exc = retry_exc
            provider = str(backend.provider or "").strip().lower()
            method_map = {
                "vertex": "_call_vertex",
                "openai": "_call_openai",
                "claude": "_call_claude",
                "gemini": "_call_gemini",
                "ollama_cloud": "_call_ollama_cloud",
            }
            method_name = method_map.get(provider, "")
            method = getattr(self._scheduler, method_name, None) if method_name else None
            if method is None:
                raise
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "platform_brain_catalog_fallback",
                    "provider": provider,
                    "model": str(backend.model or ""),
                    "detail": str(exc),
                    "fallback_method": method_name,
                    "catalog_auto_registered": bool(auto_registered),
                },
            )
            raw = await method(backend, payload)
            return {"raw": raw, "catalog_fallback_used": True}

    async def _invoke_platform_brain(
        self,
        session_id: str,
        *,
        session: Dict[str, Any],
        operator_message: str,
        recent_messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if self._scheduler is None:
            return {"ok": False, "skipped": "scheduler_unavailable"}
        backend = self._platform_backend_from_session(session)
        if backend is None:
            return {"ok": False, "skipped": "session_backend_unconfigured"}
        messages = self._build_platform_brain_messages(
            session=session,
            operator_message=operator_message,
            recent_messages=recent_messages,
        )
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        payload: Any = messages
        if str(backend.provider or "").strip().lower() == "vertex":
            payload = {
                "messages": messages,
                "vertex_project_id": str(
                    (metadata.get("backend") or {}).get("vertex_project_id")
                    if isinstance(metadata.get("backend"), dict)
                    else ""
                ).strip()
                or None,
                "vertex_location": str(
                    (metadata.get("backend") or {}).get("vertex_location")
                    if isinstance(metadata.get("backend"), dict)
                    else ""
                ).strip()
                or None,
            }
        try:
            dispatch = await self._dispatch_platform_brain_with_fallback(
                session_id=session_id,
                backend=backend,
                payload=payload,
            )
            raw = dispatch.get("raw")
            catalog_fallback_used = bool(dispatch.get("catalog_fallback_used"))
            output = ""
            usage: Dict[str, Any] = {}
            if isinstance(raw, dict):
                output = str(raw.get("output") or raw.get("content") or "").strip()
                usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
            else:
                output = str(raw or "").strip()
            parsed = self._parse_platform_brain_output(output)
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "platform_brain_invoked",
                    "provider": str(backend.provider or ""),
                    "model": str(backend.model or ""),
                    "backend_type": str(backend.type or ""),
                    "catalog_fallback_used": catalog_fallback_used,
                    "usage": usage,
                    "output_preview": output[:400],
                },
            )
            await self._store.update_session(
                session_id,
                metadata={"autonomous_last_brain_error": None},
            )
            return {"ok": True, "reply": parsed.get("assistant_reply"), "actions": parsed.get("actions") or []}
        except Exception as exc:
            backend_meta = metadata.get("backend") if isinstance(metadata.get("backend"), dict) else {}
            hint = self._platform_brain_error_hint(
                provider=str(backend.provider or ""),
                model=str(backend.model or ""),
                error=str(exc),
                session_backend=backend_meta,
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "platform_brain_error",
                    "detail": str(exc),
                    "hint": hint,
                    "provider": str(backend.provider or ""),
                    "model": str(backend.model or ""),
                },
            )
            await self._store.update_session(
                session_id,
                metadata={"autonomous_last_brain_error": str(exc)},
            )
            return {"ok": False, "error": str(exc), "hint": hint}

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

    def _session_has_inflight_runtime(
        self,
        *,
        session: Dict[str, Any],
        session_meta: Dict[str, Any],
    ) -> bool:
        runtime_state = session_meta.get("runtime_state") if isinstance(session_meta.get("runtime_state"), dict) else {}
        active_tasks = runtime_state.get("active_tasks") if isinstance(runtime_state.get("active_tasks"), list) else []
        if active_tasks:
            return True
        status_counts = runtime_state.get("status_counts") if isinstance(runtime_state.get("status_counts"), dict) else {}
        for key in ("running", "queued", "blocked"):
            try:
                if int(status_counts.get(key) or 0) > 0:
                    return True
            except Exception:
                continue
        orchestration_id = str(runtime_state.get("orchestration_id") or session.get("orchestration_id") or "").strip()
        if not orchestration_id:
            return False
        try:
            task_total = int(runtime_state.get("task_total") or 0)
        except Exception:
            task_total = 0
        if task_total <= 0:
            return False
        completed_like = 0
        for key in ("completed", "failed", "cancelled", "retried"):
            try:
                completed_like += int(status_counts.get(key) or 0)
            except Exception:
                continue
        return completed_like < task_total

    async def _halt_session(
        self,
        session_id: str,
        *,
        reason: str,
        message: str,
    ) -> None:
        await self._store.update_session(session_id, status="ready")
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "session_checkpoint_ready", "reason": reason, "checkpoint_at": _now()},
        )
        await self._store.append_message(
            session_id,
            role="assistant",
            content=message,
            metadata={"source": "checkpoint_guard", "checkpoint_reason": reason},
        )

    async def _handle_stall_without_halt(
        self,
        session_id: str,
        *,
        session: Dict[str, Any],
        snapshot: Dict[str, Any],
        reason: str,
    ) -> None:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        mode = str(session.get("mode") or "").strip().lower()
        if (
            mode == "pipeline_tuner"
            and self._require_platform_brain_for_autonomy()
            and str(metadata.get("autonomous_last_brain_error") or "").strip()
        ):
            await self._halt_session(
                session_id,
                reason="platform_brain_unavailable",
                message=(
                    "No-progress safeguard detected while Platform brain backend is unavailable. "
                    "Session moved to ready to avoid blind rerun loops. Fix backend configuration and resume."
                ),
            )
            return
        replan_count = int(metadata.get("autonomous_replan_count") or 0) + 1
        await self._store.update_session(
            session_id,
            metadata={
                "autonomous_state": "replanning",
                "autonomous_replan_count": replan_count,
                "autonomous_launch_state": None,
                "autonomous_last_eval_signature": None,
                "autonomous_last_refine_signature": None,
            },
        )
        action = await self._create_action_record(
            session_id,
            action_type="autonomous_replan",
            snapshot={
                "reason": reason,
                "orchestration_id": snapshot.get("orchestration_id"),
                "mode": mode,
            },
            rationale="Adaptive replan triggered after repeated no-op cycle",
        )
        await self._complete_action_record(
            action["id"],
            output_snapshot={"reason": reason, "replan_count": replan_count},
            had_effect=True,
            summary=f"Adaptive replan #{replan_count} triggered ({reason}).",
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_replan",
                "reason": reason,
                "replan_count": replan_count,
                "orchestration_id": snapshot.get("orchestration_id"),
            },
        )
        should_announce = replan_count <= 2 or (replan_count % 3 == 0)
        if should_announce:
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"No-progress safeguard triggered (`{reason}`). Platform AI is applying an adaptive replan "
                    f"(count {replan_count}) and continuing autonomously."
                ),
                metadata={"source": "autonomous_tuner", "state": "replanning", "replan_count": replan_count},
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
                "status": "running",
                "reason": "autonomous_converged",
                "message": (
                    f"Autonomous tuner converged for `{pipeline_name}` on orchestration `{orchestration_id}` "
                    f"with score {score:.3f}. Awaiting quality-gate pass streak confirmation."
                ),
            }
        if state == "max_iterations_reached":
            return {
                "status": "running",
                "reason": "autonomous_max_iterations_reached",
                "message": (
                    f"Autonomous tuner reached iteration cap for `{pipeline_name}` at {current_iteration} iteration(s) "
                    f"(target {target_score:.3f}, last score {score:.3f}). Applying adaptive strategy shift."
                ),
            }
        if state in {"launch_failed", "refinement_launch_failed"}:
            return {
                "status": "running",
                "reason": state,
                "message": (
                    f"Autonomous tuner launch step failed for `{pipeline_name}` after orchestration "
                    f"`{orchestration_id}`. Retrying with alternate strategy."
                ),
            }
        if (
            failed_tasks > 0
            and last_eval_signature
            and state in {"needs_refinement", "tune", "inspect_failures", ""}
            and last_refine_signature == last_eval_signature
        ):
            return {
                "status": "running",
                "reason": "autonomous_stalled_after_evaluation",
                "message": (
                    f"Autonomous tuner detected a stalled refinement path for `{pipeline_name}` on orchestration "
                    f"`{orchestration_id}` ({failed_tasks} failed task(s)); triggering replanning."
                ),
            }
        if failed_tasks > 0 and current_iteration >= max_iterations:
            return {
                "status": "running",
                "reason": "autonomous_max_iterations_reached",
                "message": (
                    f"Autonomous tuner hit iteration cap with {failed_tasks} failed task(s) for `{pipeline_name}` "
                    f"on orchestration `{orchestration_id}`; continuing with strategy shift."
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
        message = str(resolution.get("message") or "").strip()
        updated = await self._store.update_session(
            session_id,
            metadata={
                "autonomous_terminalized_at": _now(),
                "autonomous_terminal_reason": reason,
                "autonomous_state": str(reason or "").strip() or "observe",
            },
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "autonomous_session_terminalized",
                "reason": reason,
                "status": str((updated or {}).get("status") or "running"),
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
        role_text = str(role or "").strip().lower()
        if session is not None and status == "running":
            await self.ensure_session_loop(session_id)
        elif session is not None and role_text == "operator" and status == "ready":
            await self.ensure_session_loop(session_id)
        elif role_text == "operator":
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    "Message received and queued. Session is not running right now. "
                    "Resume/start the session to process queued instructions."
                ),
                metadata={"source": "session_state_ack", "session_status": status or "unknown"},
            )
        return message

    async def start_deploy_run(self, session_id: str, *, requested_by: str) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "error", "detail": "session_id is required"}
        gate = await self._authorize_privileged_action(
            sid,
            requested_by=requested_by,
            feature_flag="NEXUS_PLATFORM_AI_DEPLOY_ENABLED",
            action="deploy",
        )
        if not bool(gate.get("ok")):
            return {"status": str(gate.get("status") or "denied"), "detail": str(gate.get("detail") or "deploy denied")}
        existing = self._deploy_tasks.get(sid)
        if existing is not None and not existing.done():
            return {"status": "running", "detail": "deploy runner already active"}
        await self._store.update_session(
            sid,
            metadata={
                "deploy_runner_state": "starting",
                "deploy_runner_requested_by": str(requested_by or "").strip() or None,
                "deploy_runner_requested_at": _now(),
            },
        )
        self._deploy_tasks[sid] = asyncio.create_task(self._deploy_loop(sid, requested_by=requested_by))
        return {"status": "started"}

    async def start_project_edit_run(
        self,
        session_id: str,
        *,
        requested_by: str,
        instruction: str = "",
    ) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "error", "detail": "session_id is required"}
        if not _env_enabled("NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED"):
            return {"status": "disabled", "detail": "project_code_edit is disabled (NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED not enabled)"}
        project_allowlist = _project_edit_project_allowlist()
        require_project_id = _env_enabled("NEXUS_PLATFORM_AI_PROJECT_EDIT_REQUIRE_PROJECT_ID") or bool(project_allowlist)
        scope_gate = await self._enforce_project_scope_policy(
            session_id=sid,
            action="project_code_edit",
            require_project_id=require_project_id,
            allowed_project_ids=project_allowlist,
        )
        if not bool(scope_gate.get("ok")):
            return {"status": str(scope_gate.get("status") or "denied"), "detail": str(scope_gate.get("detail") or "project scope denied")}
        existing = self._project_edit_tasks.get(sid)
        if existing is not None and not existing.done():
            return {"status": "running", "detail": "project edit runner already active"}
        await self._store.update_session(
            sid,
            metadata={
                "project_edit_runner_state": "starting",
                "project_edit_runner_requested_by": str(requested_by or "").strip() or None,
                "project_edit_runner_requested_at": _now(),
                "project_edit_runner_project_id": str(scope_gate.get("project_id") or "").strip() or None,
            },
        )
        self._project_edit_tasks[sid] = asyncio.create_task(
            self._project_edit_loop(
                sid,
                requested_by=requested_by,
                instruction=instruction,
            )
        )
        return {"status": "started"}

    async def start_repo_edit_run(
        self,
        session_id: str,
        *,
        requested_by: str,
        instruction: str = "",
        external: bool = False,
    ) -> Dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            return {"status": "error", "detail": "session_id is required"}
        feature_flag = (
            "NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_ENABLED"
            if external
            else "NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED"
        )
        gate = await self._authorize_privileged_action(
            sid,
            requested_by=requested_by,
            feature_flag=feature_flag,
            action="external_repo_edit" if external else "repo_edit",
        )
        if not bool(gate.get("ok")):
            return {
                "status": str(gate.get("status") or "denied"),
                "detail": str(gate.get("detail") or "repo edit denied"),
            }
        if not external:
            enforce_project_scope = _env_enabled("NEXUS_PLATFORM_AI_ENFORCE_PROJECT_ID")
            platform_allowlist = _platform_project_allowlist()
            if enforce_project_scope and not platform_allowlist:
                return {
                    "status": "denied",
                    "detail": (
                        "NEXUS_PLATFORM_AI_ENFORCE_PROJECT_ID is enabled but platform project allowlist is empty; "
                        "set NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ID or NEXUS_PLATFORM_AI_PLATFORM_PROJECT_ALLOWLIST"
                    ),
                }
            require_project_id = enforce_project_scope or bool(platform_allowlist)
            scope_gate = await self._enforce_project_scope_policy(
                session_id=sid,
                action="repo_edit",
                require_project_id=require_project_id,
                allowed_project_ids=platform_allowlist,
            )
            if not bool(scope_gate.get("ok")):
                return {
                    "status": str(scope_gate.get("status") or "denied"),
                    "detail": str(scope_gate.get("detail") or "repo edit project scope denied"),
                }
        else:
            scope_gate = {"project_id": None}
        existing = self._repo_edit_tasks.get(sid)
        if existing is not None and not existing.done():
            return {"status": "running", "detail": "repo edit runner already active"}
        await self._store.update_session(
            sid,
            metadata={
                "repo_edit_runner_state": "starting",
                "repo_edit_runner_kind": "external_repo_edit" if external else "repo_edit",
                "repo_edit_runner_requested_by": str(requested_by or "").strip() or None,
                "repo_edit_runner_requested_at": _now(),
                "repo_edit_runner_project_id": str(scope_gate.get("project_id") or "").strip() or None,
            },
        )
        self._repo_edit_tasks[sid] = asyncio.create_task(
            self._repo_edit_loop(
                sid,
                requested_by=requested_by,
                instruction=instruction,
                external=external,
            )
        )
        return {"status": "started"}

    async def _authorize_privileged_action(
        self,
        session_id: str,
        *,
        requested_by: str,
        feature_flag: str,
        action: str,
    ) -> Dict[str, Any]:
        if not _env_enabled(feature_flag):
            return {"ok": False, "status": "disabled", "detail": f"{action} is disabled ({feature_flag} not enabled)"}
        if not _env_enabled("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED"):
            return {"ok": False, "status": "denied", "detail": "privileged platform ai actions are disabled"}
        allowlist = _owner_allowlist()
        if not allowlist:
            return {"ok": False, "status": "denied", "detail": "owner allowlist is empty"}
        session = await self._store.get_session(session_id)
        operator_id = str((session or {}).get("operator_id") or "").strip().lower()
        requested = str(requested_by or "").strip().lower()
        candidate = requested or operator_id
        if candidate not in allowlist:
            return {"ok": False, "status": "denied", "detail": f"operator '{candidate or 'unknown'}' is not allowlisted"}
        return {"ok": True, "status": "ok"}

    async def _record_project_scope_denied(
        self,
        *,
        session_id: str,
        action: str,
        project_id: str,
        allowed_project_ids: List[str],
        detail: str,
    ) -> None:
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "project_scope_denied",
                "attempted_action": action,
                "project_id": project_id or None,
                "allowed_project_ids": allowed_project_ids,
                "detail": detail,
            },
        )
        await self._store.append_message(
            session_id,
            role="assistant",
            content=(
                f"Project scope guard denied `{action}`. "
                f"session project_id=`{project_id or 'unset'}`; detail: {detail}"
            ),
            metadata={
                "source": "project_scope_guard",
                "attempted_action": action,
                "project_id": project_id or None,
                "allowed_project_ids": allowed_project_ids,
            },
        )

    async def _enforce_project_scope_policy(
        self,
        *,
        session_id: str,
        action: str,
        require_project_id: bool,
        allowed_project_ids: set[str],
    ) -> Dict[str, Any]:
        session = await self._store.get_session(session_id)
        if session is None:
            return {"ok": False, "status": "error", "detail": "session_not_found"}
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        project_id = str(metadata.get("project_id") or "").strip()
        allowed = sorted({str(item or "").strip() for item in allowed_project_ids if str(item or "").strip()})

        if require_project_id and not project_id:
            detail = "session metadata.project_id is required"
            await self._record_project_scope_denied(
                session_id=session_id,
                action=action,
                project_id=project_id,
                allowed_project_ids=allowed,
                detail=detail,
            )
            return {"ok": False, "status": "denied", "detail": detail}

        if allowed:
            if not project_id:
                detail = "session metadata.project_id is required because an allowlist is configured"
                await self._record_project_scope_denied(
                    session_id=session_id,
                    action=action,
                    project_id=project_id,
                    allowed_project_ids=allowed,
                    detail=detail,
                )
                return {"ok": False, "status": "denied", "detail": detail}
            if project_id not in allowed:
                detail = f"session project_id '{project_id}' is not in allowlist"
                await self._record_project_scope_denied(
                    session_id=session_id,
                    action=action,
                    project_id=project_id,
                    allowed_project_ids=allowed,
                    detail=detail,
                )
                return {"ok": False, "status": "denied", "detail": detail}
        return {"ok": True, "status": "ok", "project_id": project_id, "allowed_project_ids": allowed}

    def _looks_like_bot_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        bot_id = str(payload.get("id") or "").strip()
        role = str(payload.get("role") or "").strip()
        backends = payload.get("backends")
        return bool(bot_id and role and isinstance(backends, list))

    @staticmethod
    def _proposal_bot_safety_error(bot: Bot) -> Optional[str]:
        """Keep approval-time bot changes bounded to declarative model backends.

        CLI, browser, and custom backends can carry host tooling or arbitrary commands.
        Those require a direct operator configuration path, not an AI-generated proposal.
        """
        for backend in list(getattr(bot, "backends", None) or []):
            backend_type = str(getattr(backend, "type", "") or "").strip().lower()
            if backend_type not in _APPROVABLE_PROPOSAL_BACKEND_TYPES:
                return f"proposal_backend_type_not_approvable:{backend_type or 'missing'}"
            if str(getattr(backend, "command", "") or "").strip():
                return "proposal_backend_command_not_allowed"
            credential_ref = str(getattr(backend, "api_key_ref", "") or "").strip()
            normalized_ref = credential_ref.lower()
            if normalized_ref.startswith(_DIRECT_CREDENTIAL_PREFIXES) or "=" in credential_ref or any(
                char.isspace() for char in credential_ref
            ):
                return "proposal_direct_credential_not_allowed"
        return None

    async def _create_bot_configuration_proposal(
        self,
        *,
        session_id: str,
        session: Dict[str, Any],
        payload: Dict[str, Any],
        rationale: str = "",
    ) -> Dict[str, Any]:
        """Persist a bounded bot configuration for individual operator approval."""
        if self._bot_registry is None:
            return {"ok": False, "detail": "bot_registry_unavailable"}
        try:
            proposed_bot = Bot.model_validate(payload)
        except Exception as exc:
            return {"ok": False, "detail": f"invalid_bot_payload:{exc}"}
        safety_error = self._proposal_bot_safety_error(proposed_bot)
        if safety_error:
            return {"ok": False, "detail": safety_error}

        safe_id = str(proposed_bot.id or "").strip()
        if not safe_id:
            return {"ok": False, "detail": "bot_id_missing"}

        # Proposals never carry activation authority. New bots remain disabled and an
        # existing enabled bot must be changed through the direct operator path.
        proposed_bot = proposed_bot.model_copy(update={"enabled": False})
        existing_bot: Optional[Bot] = None
        try:
            existing_bot = await self._bot_registry.get(safe_id)
        except BotNotFoundError:
            existing_bot = None
        except Exception:
            existing_bot = None

        before_state: Dict[str, Any] = {
            "exists": existing_bot is not None,
            "enabled": bool(getattr(existing_bot, "enabled", False)) if existing_bot is not None else False,
        }
        if existing_bot is not None:
            before_state["bot"] = existing_bot.model_dump(mode="json", exclude_none=True)

        action_record = await self._create_action_record(
            session_id,
            action_type="bot_configuration_proposal",
            target_type="bot",
            target_id=safe_id,
            rationale=str(rationale or "").strip() or "Platform AI proposed a bot configuration.",
            snapshot={"bot_id": safe_id, "proposed_bot": proposed_bot.model_dump(mode="json", exclude_none=True)},
        )
        proposal = await self._store.create_patch_proposal(
            session_id,
            action_id=action_record["id"],
            target_config=f"bot:{safe_id}:configuration",
            before_state=before_state,
            after_state={
                "proposal_kind": "bot_configuration",
                "action": "upsert_bot",
                "bot": proposed_bot.model_dump(mode="json", exclude_none=True),
            },
            rationale=str(rationale or "").strip() or "Platform AI proposed a bounded bot configuration.",
            expected_effect="Create or update a disabled bot after individual operator approval.",
            validation_steps=[
                "validate_bot_schema",
                "validate_backend_type",
                "validate_session_scope",
                "preserve_manual_activation",
            ],
            rollback_note="Disable or remove the bot through the direct operator controls after review.",
        )
        await self._store.update_action(
            action_record["id"],
            status="proposed",
            state_delta_summary=f"Awaiting operator review for bot {safe_id}.",
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "bot_configuration_proposed",
                "proposal_id": proposal["id"],
                "bot_id": safe_id,
                "existing_bot_enabled": bool(getattr(existing_bot, "enabled", False)) if existing_bot is not None else False,
            },
        )
        return {
            "ok": False,
            "detail": "configuration_mutations_disabled",
            "proposal_only": True,
            "proposal_id": proposal["id"],
            "bot_id": safe_id,
        }

    def _mode_mutation_policy(self, *, mode: str, metadata: Dict[str, Any]) -> Dict[str, bool]:
        defaults = {
            "bot_tuner": {"create": False, "update": True, "delete": False},
            "bot_creator": {"create": True, "update": True, "delete": False},
            "pipeline_tuner": {"create": True, "update": True, "delete": True},
            "pipeline_creator": {"create": True, "update": True, "delete": True},
        }
        merged = dict(defaults.get(mode, {"create": False, "update": True, "delete": False}))
        override = metadata.get("mutation_policy") if isinstance(metadata.get("mutation_policy"), dict) else {}
        for key in ("create", "update", "delete"):
            if key in override:
                merged[key] = bool(override.get(key))
        return merged

    async def _editable_bot_scope(self, *, session: Dict[str, Any]) -> set[str]:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        mode = str(session.get("mode") or "").strip().lower()
        scope: set[str] = set()
        for key in ("editable_bot_ids", "mutable_bot_ids", "pipeline_mutable_bot_ids"):
            values = metadata.get(key) if isinstance(metadata.get(key), list) else []
            for item in values:
                safe = str(item or "").strip()
                if safe:
                    scope.add(safe)
        target_bot_id = str(metadata.get("target_bot_id") or "").strip()
        pipeline_bot_id = str(metadata.get("pipeline_bot_id") or "").strip()
        if mode == "bot_tuner" and target_bot_id:
            scope.add(target_bot_id)
        if mode in {"pipeline_tuner", "pipeline_creator"} and pipeline_bot_id:
            scope.add(pipeline_bot_id)
            context = await self._resolve_context(session)
            graph = context.get("graph") if isinstance(context.get("graph"), dict) else {}
            nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                bot_id = str(node.get("bot_id") or node.get("id") or "").strip()
                if bot_id:
                    scope.add(bot_id)
        return scope

    async def _reference_bot_scope(self, *, session: Dict[str, Any]) -> set[str]:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        read_only: set[str] = set()
        for key in ("reference_bot_ids", "reference_scope_bot_ids"):
            values = metadata.get(key) if isinstance(metadata.get(key), list) else []
            for item in values:
                safe = str(item or "").strip()
                if safe:
                    read_only.add(safe)
        referenced_pipelines = metadata.get("reference_pipeline_ids") if isinstance(metadata.get("reference_pipeline_ids"), list) else []
        for item in referenced_pipelines:
            pipeline_bot_id = str(item or "").strip()
            if not pipeline_bot_id:
                continue
            read_only.add(pipeline_bot_id)
            if self._bot_registry is None:
                continue
            try:
                bot = await self._bot_registry.get(pipeline_bot_id)
            except Exception:
                continue
            workflow = getattr(bot, "workflow", None)
            reference_graph = getattr(workflow, "reference_graph", None) if workflow is not None else None
            nodes = getattr(reference_graph, "nodes", None) if reference_graph is not None else None
            for node in nodes or []:
                bot_id = str(getattr(node, "bot_id", "") or "").strip()
                if bot_id:
                    read_only.add(bot_id)
        return read_only

    async def _record_scope_denied(
        self,
        *,
        session_id: str,
        action: str,
        bot_id: str,
        allowed_scope: List[str],
        reason: str = "out_of_scope",
    ) -> None:
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "scope_denied",
                "attempted_action": action,
                "bot_id": bot_id,
                "allowed_scope": allowed_scope,
                "reason": reason,
            },
        )
        if reason == "reference_scope_read_only":
            warning = (
                f"Scope warning: denied `{action}` for bot `{bot_id}` because it is inside read-only reference scope."
            )
        else:
            warning = (
                f"Scope warning: denied `{action}` for bot `{bot_id}` because it is outside the editable scope for this session."
            )
        await self._store.append_message(
            session_id,
            role="assistant",
            content=warning,
            metadata={"source": "scope_guard", "bot_id": bot_id, "attempted_action": action, "reason": reason},
        )

    async def _upsert_bot_payload(
        self,
        payload: Dict[str, Any],
        *,
        session_id: str,
        session: Dict[str, Any],
        allow_scope_expansion: bool,
    ) -> Dict[str, Any]:
        if self._bot_registry is None:
            return {"ok": False, "detail": "bot_registry_unavailable"}
        try:
            bot = Bot.model_validate(payload)
        except Exception as exc:
            return {"ok": False, "detail": f"invalid_bot_payload:{exc}"}
        safe_id = str(bot.id or "").strip()
        if not safe_id:
            return {"ok": False, "detail": "bot_id_missing"}
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        mode = str(session.get("mode") or "").strip().lower()
        policy = self._mode_mutation_policy(mode=mode, metadata=metadata)
        scope = await self._editable_bot_scope(session=session)
        reference_scope = await self._reference_bot_scope(session=session)
        existed = True
        existing_bot: Optional[Bot] = None
        try:
            existing_bot = await self._bot_registry.get(safe_id)
        except BotNotFoundError:
            existed = False
        except Exception:
            existed = False
        if existed and not bool(policy.get("update")):
            return {"ok": False, "detail": "update_denied_by_policy", "bot_id": safe_id}
        if not existed and not bool(policy.get("create")):
            return {"ok": False, "detail": "create_denied_by_policy", "bot_id": safe_id}
        if safe_id in reference_scope:
            await self._record_scope_denied(
                session_id=session_id,
                action="update_bot" if existed else "create_bot",
                bot_id=safe_id,
                allowed_scope=sorted(scope),
                reason="reference_scope_read_only",
            )
            return {"ok": False, "detail": "reference_scope_read_only", "bot_id": safe_id, "allowed_scope": sorted(scope)}
        if scope and safe_id not in scope and not (allow_scope_expansion and not existed and bool(policy.get("create"))):
            await self._record_scope_denied(
                session_id=session_id,
                action="update_bot" if existed else "create_bot",
                bot_id=safe_id,
                allowed_scope=sorted(scope),
            )
            return {"ok": False, "detail": "out_of_scope", "bot_id": safe_id, "allowed_scope": sorted(scope)}

        activation_change = "unchanged"
        if not _session_allows_auto_bot_activation(metadata):
            if existed and existing_bot is not None:
                current_enabled = bool(getattr(existing_bot, "enabled", False))
                if bool(bot.enabled) != current_enabled:
                    bot = bot.model_copy(update={"enabled": current_enabled})
                    activation_change = "preserved_existing_state"
            elif bool(bot.enabled):
                bot = bot.model_copy(update={"enabled": False})
                activation_change = "created_disabled"
        elif bool(bot.enabled):
            activation_change = "auto_activation_allowed"
        try:
            if existed:
                await self._bot_registry.update(safe_id, bot)
            else:
                await self._bot_registry.register(bot)
        except Exception as exc:
            return {"ok": False, "detail": f"bot_upsert_failed:{safe_id}:{exc}"}
        if not existed and allow_scope_expansion:
            mutable = metadata.get("mutable_bot_ids") if isinstance(metadata.get("mutable_bot_ids"), list) else []
            if safe_id not in mutable:
                mutable = list(mutable) + [safe_id]
                await self._store.update_session(session_id, metadata={"mutable_bot_ids": mutable})
        return {
            "ok": True,
            "bot_id": safe_id,
            "operation": "updated" if existed else "created",
            "activation_change": activation_change,
        }

    async def _configure_linear_pipeline_entry(
        self,
        *,
        entry_bot_id: str,
        stage_bot_ids: List[str],
        pipeline_name: str,
        launch_instruction: str,
    ) -> Dict[str, Any]:
        if self._bot_registry is None:
            return {"ok": False, "detail": "bot_registry_unavailable"}
        safe_entry = str(entry_bot_id or "").strip()
        if not safe_entry:
            return {"ok": False, "detail": "entry_bot_id_required"}
        cleaned_stages: List[str] = []
        for item in stage_bot_ids:
            sid = str(item or "").strip()
            if sid and sid not in cleaned_stages:
                cleaned_stages.append(sid)
        if not cleaned_stages:
            cleaned_stages = [safe_entry]
        if cleaned_stages[0] != safe_entry:
            cleaned_stages.insert(0, safe_entry)

        try:
            entry_bot = await self._bot_registry.get(safe_entry)
        except Exception as exc:
            return {"ok": False, "detail": f"entry_bot_not_found:{exc}"}

        triggers: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        for idx in range(len(cleaned_stages) - 1):
            source = cleaned_stages[idx]
            target = cleaned_stages[idx + 1]
            trigger_id = f"auto-{source}-to-{target}"
            triggers.append(
                {
                    "id": trigger_id,
                    "event": "task_completed",
                    "target_bot_id": target,
                    "enabled": True,
                    "condition": "always",
                }
            )
            edges.append({"source_bot_id": source, "target_bot_id": target, "route_kind": "forward"})

        nodes = [{"bot_id": bot_id, "title": bot_id, "stage_kind": "stage"} for bot_id in cleaned_stages]
        workflow = {
            "triggers": triggers,
            "reference_graph": {
                "graph_id": f"{safe_entry}-pipeline",
                "entry_bot_id": safe_entry,
                "current_bot_id": safe_entry,
                "nodes": nodes,
                "edges": edges,
            },
        }
        routing_rules = dict(getattr(entry_bot, "routing_rules", None) or {})
        launch_profile = dict(routing_rules.get("launch_profile") or {})
        launch_profile.update(
            {
                "enabled": True,
                "is_pipeline": True,
                "label": pipeline_name or safe_entry,
                "pipeline_name": pipeline_name or safe_entry,
                "payload": {"instruction": launch_instruction or f"Execute pipeline {pipeline_name or safe_entry}"},
            }
        )
        routing_rules["launch_profile"] = launch_profile
        assignment_capabilities = dict(getattr(entry_bot, "assignment_capabilities", None) or {})
        assignment_capabilities.update(
            {
                "is_pipeline_entry": True,
                "pipeline": True,
                "pipeline_name": pipeline_name or safe_entry,
            }
        )
        try:
            updated = entry_bot.model_copy(
                update={
                    "workflow": workflow,
                    "routing_rules": routing_rules,
                    "assignment_capabilities": assignment_capabilities,
                }
            )
            await self._bot_registry.update(safe_entry, updated)
        except Exception as exc:
            return {"ok": False, "detail": f"pipeline_entry_update_failed:{exc}"}
        return {"ok": True, "entry_bot_id": safe_entry, "stage_count": len(cleaned_stages)}

    def _extract_tuning_overrides(self, content: str) -> Dict[str, Any]:
        text = str(content or "")
        lower = text.lower()
        updates: Dict[str, Any] = {}
        score_match = re.search(r"(?:target\s+score|score\s+target)\s*(?:to|=|:)?\s*(0(?:\.\d+)?|1(?:\.0+)?)", lower)
        if score_match:
            try:
                score = float(score_match.group(1))
                updates["autonomous_target_score"] = max(0.6, min(0.99, score))
            except Exception:
                pass
        iter_match = re.search(r"(?:max(?:imum)?\s+iterations?)\s*(?:to|=|:)?\s*(\d+)", lower)
        if iter_match:
            try:
                max_iterations = int(iter_match.group(1))
                updates["autonomous_max_iterations"] = max(1, min(25, max_iterations))
            except Exception:
                pass
        pipeline_match = re.search(r"(?:pipeline\s+bot(?:\s+id)?)\s*(?:to|=|:)?\s*([a-z0-9._:-]+)", lower)
        if pipeline_match:
            updates["pipeline_bot_id"] = str(pipeline_match.group(1) or "").strip()
        return updates

    async def _apply_operator_directives(
        self,
        session_id: str,
        *,
        session: Dict[str, Any],
        content: str,
    ) -> Dict[str, Any]:
        actions_taken: List[Dict[str, Any]] = []
        mode = str(session.get("mode") or "").strip().lower()
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        allow_scope_expansion = mode in {"bot_creator", "pipeline_creator", "pipeline_tuner"}
        payloads = _extract_json_chunks(content)
        directives: List[Any] = []
        for payload in payloads:
            if isinstance(payload, dict) and isinstance(payload.get("actions"), list):
                directives.extend(payload.get("actions") or [])
            else:
                directives.append(payload)

        for directive in directives:
            if isinstance(directive, list):
                if all(self._looks_like_bot_payload(item) for item in directive if isinstance(item, dict)):
                    directive = {"platform_ai_action": "upsert_bots", "bots": directive}
                else:
                    continue
            if not isinstance(directive, dict):
                continue
            action = str(directive.get("platform_ai_action") or directive.get("action") or "").strip().lower()
            if not action and self._looks_like_bot_payload(directive):
                action = "upsert_bot"
            if action in _CONFIGURATION_MUTATION_ACTIONS and not _configuration_mutations_enabled():
                if action == "upsert_bot":
                    bot_payload = directive.get("bot") if isinstance(directive.get("bot"), dict) else directive
                    result = await self._create_bot_configuration_proposal(
                        session_id=session_id,
                        session=session,
                        payload=bot_payload if isinstance(bot_payload, dict) else {},
                        rationale=str(directive.get("rationale") or directive.get("reason") or "").strip(),
                    )
                    actions_taken.append({"action": "upsert_bot", "result": result})
                    continue
                if action == "upsert_bots":
                    bots = directive.get("bots") if isinstance(directive.get("bots"), list) else []
                    for item in bots:
                        if not isinstance(item, dict):
                            continue
                        result = await self._create_bot_configuration_proposal(
                            session_id=session_id,
                            session=session,
                            payload=item,
                            rationale=str(directive.get("rationale") or directive.get("reason") or "").strip(),
                        )
                        actions_taken.append({"action": "upsert_bot", "result": result})
                    continue
                actions_taken.append(
                    {
                        "action": action,
                        "result": {"ok": False, "detail": "configuration_mutations_disabled", "proposal_only": True},
                    }
                )
                continue
            if action in _AUTONOMOUS_PIPELINE_ACTIONS and not _autonomous_pipeline_runs_enabled():
                actions_taken.append(
                    {
                        "action": action,
                        "result": {"ok": False, "detail": "autonomous_pipeline_runs_disabled", "proposal_only": True},
                    }
                )
                continue
            if action == "upsert_bot":
                bot_payload = directive.get("bot") if isinstance(directive.get("bot"), dict) else directive
                result = await self._upsert_bot_payload(
                    bot_payload if isinstance(bot_payload, dict) else {},
                    session_id=session_id,
                    session=session,
                    allow_scope_expansion=allow_scope_expansion,
                )
                actions_taken.append({"action": "upsert_bot", "result": result})
            elif action == "upsert_bots":
                bots = directive.get("bots") if isinstance(directive.get("bots"), list) else []
                for item in bots:
                    if not isinstance(item, dict):
                        continue
                    result = await self._upsert_bot_payload(
                        item,
                        session_id=session_id,
                        session=session,
                        allow_scope_expansion=allow_scope_expansion,
                    )
                    actions_taken.append({"action": "upsert_bot", "result": result})
            elif action in {"delete_bot", "remove_bot"}:
                bot_id = str(directive.get("bot_id") or directive.get("id") or "").strip()
                if not bot_id:
                    actions_taken.append({"action": action, "result": {"ok": False, "detail": "bot_id_required"}})
                elif self._bot_registry is None:
                    actions_taken.append({"action": action, "result": {"ok": False, "detail": "bot_registry_unavailable"}})
                else:
                    policy = self._mode_mutation_policy(mode=mode, metadata=metadata)
                    scope = await self._editable_bot_scope(session=session)
                    reference_scope = await self._reference_bot_scope(session=session)
                    if not bool(policy.get("delete")):
                        actions_taken.append({"action": action, "result": {"ok": False, "detail": "delete_denied_by_policy", "bot_id": bot_id}})
                    elif bot_id in reference_scope:
                        await self._record_scope_denied(
                            session_id=session_id,
                            action="delete_bot",
                            bot_id=bot_id,
                            allowed_scope=sorted(scope),
                            reason="reference_scope_read_only",
                        )
                        actions_taken.append({"action": action, "result": {"ok": False, "detail": "reference_scope_read_only", "bot_id": bot_id}})
                    elif scope and bot_id not in scope:
                        await self._record_scope_denied(
                            session_id=session_id,
                            action="delete_bot",
                            bot_id=bot_id,
                            allowed_scope=sorted(scope),
                        )
                        actions_taken.append({"action": action, "result": {"ok": False, "detail": "out_of_scope", "bot_id": bot_id}})
                    else:
                        try:
                            await self._bot_registry.remove(bot_id)
                            actions_taken.append({"action": action, "result": {"ok": True, "bot_id": bot_id, "operation": "deleted"}})
                        except Exception as exc:
                            actions_taken.append({"action": action, "result": {"ok": False, "detail": f"bot_delete_failed:{exc}", "bot_id": bot_id}})
            elif action == "configure_pipeline_entry":
                stage_ids = [str(item) for item in (directive.get("stage_bot_ids") or [])]
                result = await self._configure_linear_pipeline_entry(
                    entry_bot_id=str(directive.get("entry_bot_id") or "").strip(),
                    stage_bot_ids=stage_ids,
                    pipeline_name=str(directive.get("pipeline_name") or "").strip(),
                    launch_instruction=str(directive.get("launch_instruction") or "").strip(),
                )
                actions_taken.append({"action": "configure_pipeline_entry", "result": result})
            elif action == "set_pipeline_target":
                pipeline_bot_id = str(directive.get("pipeline_bot_id") or "").strip()
                pipeline_name = str(directive.get("pipeline_name") or "").strip()
                updates: Dict[str, Any] = {}
                if pipeline_bot_id:
                    updates["pipeline_bot_id"] = pipeline_bot_id
                if pipeline_name:
                    updates["pipeline_name"] = pipeline_name
                if mode == "pipeline_tuner":
                    updates["autonomous_enabled"] = True
                    updates["autonomous_goal"] = str(directive.get("goal") or content)[:4000]
                if updates:
                    await self._store.update_session(session_id, metadata=updates)
                    actions_taken.append({"action": "set_pipeline_target", "result": {"ok": True, **updates}})
            elif action == "launch_pipeline":
                pipeline_bot_id = str(
                    directive.get("pipeline_bot_id")
                    or metadata.get("pipeline_bot_id")
                    or ""
                ).strip()
                if pipeline_bot_id:
                    pipeline_name = str(directive.get("pipeline_name") or metadata.get("pipeline_name") or "").strip()
                    launched = await self._launch_autonomous_orchestration(
                        session_id=session_id,
                        pipeline_bot_id=pipeline_bot_id,
                        pipeline_name=pipeline_name or pipeline_bot_id,
                        goal=str(directive.get("goal") or content)[:2000],
                        reason="operator_directive",
                        iteration=int(metadata.get("autonomous_iteration") or 0) + 1,
                    )
                    actions_taken.append({"action": "launch_pipeline", "result": {"ok": bool(launched), "orchestration_id": launched}})
            elif action in {"project_code_edit", "public_project_edit"}:
                instruction = str(directive.get("instruction") or content).strip()
                project_result = await self.start_project_edit_run(
                    session_id,
                    requested_by=str(session.get("operator_id") or "platform-ai"),
                    instruction=instruction,
                )
                actions_taken.append({"action": "project_code_edit", "result": project_result})
            elif action == "deploy":
                deploy_result = await self.start_deploy_run(session_id, requested_by=str(session.get("operator_id") or "platform-ai"))
                actions_taken.append({"action": "deploy", "result": deploy_result})
            elif action in {"repo_edit", "code_edit", "hotfix", "external_repo_edit"}:
                instruction = str(directive.get("instruction") or content).strip()
                repo_result = await self.start_repo_edit_run(
                    session_id,
                    requested_by=str(session.get("operator_id") or "platform-ai"),
                    instruction=instruction,
                    external=(action == "external_repo_edit"),
                )
                actions_taken.append({"action": action, "result": repo_result})

        if not actions_taken:
            overrides = self._extract_tuning_overrides(content)
            if mode == "pipeline_tuner" and overrides:
                if _autonomous_pipeline_runs_enabled():
                    overrides["autonomous_enabled"] = True
                    await self._store.update_session(session_id, metadata=overrides)
                    actions_taken.append({"action": "tuning_override", "result": {"ok": True, **overrides}})
                else:
                    actions_taken.append(
                        {
                            "action": "tuning_override",
                            "result": {"ok": False, "detail": "autonomous_pipeline_runs_disabled", "proposal_only": True},
                        }
                    )
            lowered = str(content or "").lower()
            if any(token in lowered for token in ("deploy now", "run deployment", "build deployment", "deploy latest")):
                deploy_result = await self.start_deploy_run(session_id, requested_by=str(session.get("operator_id") or "platform-ai"))
                actions_taken.append({"action": "deploy", "result": deploy_result})
            if any(token in lowered for token in ("project code edit", "edit project code", "patch project", "apply project patch")):
                project_result = await self.start_project_edit_run(
                    session_id,
                    requested_by=str(session.get("operator_id") or "platform-ai"),
                    instruction=str(content or ""),
                )
                actions_taken.append({"action": "project_code_edit", "result": project_result})
            if any(token in lowered for token in ("code edit", "hotfix", "commit and push", "edit platform code")):
                repo_result = await self.start_repo_edit_run(
                    session_id,
                    requested_by=str(session.get("operator_id") or "platform-ai"),
                    instruction=str(content or ""),
                    external=False,
                )
                actions_taken.append({"action": "repo_edit", "result": repo_result})
        return {"actions": actions_taken}

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
                if bool(session.get("archived")):
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "runtime_loop_stopped", "status": "archived", "stopped_at": _now()},
                    )
                    break
                if status == "stopped":
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "runtime_loop_stopped", "status": status, "stopped_at": _now()},
                    )
                    break
                await self._process_operator_messages(session_id)
                session = await self._store.get_session(session_id) or session
                status = str(session.get("status") or "").strip().lower()
                if status in {"stopped"}:
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "runtime_loop_stopped", "status": status, "stopped_at": _now()},
                    )
                    break
                if status != "running":
                    await self._sleep_until_poked(session_id, 1.0)
                    continue

                session_meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
                mode = str(session.get("mode") or "").strip().lower()
                if mode == "pipeline_tuner":
                    _has_target = bool(str(session_meta.get("pipeline_bot_id") or "").strip()) or bool(str(session.get("orchestration_id") or "").strip())
                elif mode == "bot_tuner":
                    _has_target = bool(str(session_meta.get("target_bot_id") or "").strip())
                elif mode == "bot_creator":
                    _has_target = bool(str(session_meta.get("bot_name_seed") or "").strip())
                elif mode == "pipeline_creator":
                    _has_target = bool(str(session_meta.get("pipeline_name_seed") or session_meta.get("pipeline_name") or "").strip())
                else:
                    _has_target = True
                if not _has_target:
                    _waiting_emitted = bool(session_meta.get("_waiting_for_target_emitted"))
                    if not _waiting_emitted:
                        await self._store.append_event(
                            session_id,
                            "action_trace",
                            {"action": "waiting_for_target", "detail": f"Missing required target/config seed for mode `{mode}`. Waiting for operator input."},
                        )
                    await self._store.update_session(
                        session_id,
                        status="ready",
                        metadata={"_waiting_for_target_emitted": True, "checkpoint_reason": "missing_target"},
                    )
                    await self._store.append_message(
                        session_id,
                        role="assistant",
                        content=f"Session moved to ready state: provide required context for mode `{mode}` and resume to continue autonomous execution.",
                        metadata={"source": "checkpoint_guard", "checkpoint_reason": "missing_target"},
                    )
                    break

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
                if str(session.get("status") or "").strip().lower() != "running":
                    # Runtime transitioned to ready/stopped during autonomous step;
                    # skip further evaluation/replan logic in this tick.
                    await self._sleep_until_poked(session_id, 0.2)
                    continue
                if await self._finalize_autonomous_session_if_terminal(session_id, session=session, snapshot=snapshot):
                    continue

                # Check for stall condition after the latest snapshot/tuner pass.
                # This avoids false halts when an orchestration has just become terminal
                # and the loop is about to evaluate/finalize that transition.
                snapshot_runtime = snapshot.get("runtime_state") if isinstance(snapshot.get("runtime_state"), dict) else {}
                if not self._session_has_inflight_runtime(session=session, session_meta={"runtime_state": snapshot_runtime}):
                    halt_reason = await self._check_should_halt_as_stalled(session_id)
                    if halt_reason:
                        await self._handle_stall_without_halt(
                            session_id,
                            session=session,
                            snapshot=snapshot,
                            reason=halt_reason,
                        )
                        await self._sleep_until_poked(session_id, 1.5)
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
                    await self._sleep_until_poked(session_id, 4.0)
                elif bool(status_counts.get("running")) or bool(active_tasks):
                    await self._sleep_until_poked(session_id, 1.5)
                else:
                    await self._sleep_until_poked(session_id, 3.0)
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
            self._session_wake_events.pop(session_id, None)

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
            preview = content.replace("\n", " ").strip()[:280]
            summary = f"Acknowledged. Applying operator direction ({len(content)} chars)."
            if preview:
                summary = f"{summary} Preview: {preview}"
            await self._store.append_message(
                session_id,
                role="assistant",
                content=summary,
                metadata={"source": "runtime_ack", "operator_message_id": mid},
            )
            session = await self._store.get_session(session_id)
            mode = str((session or {}).get("mode") or "").strip().lower()
            if mode == "pipeline_tuner" and _autonomous_pipeline_runs_enabled():
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
            elif mode == "pipeline_tuner":
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "autonomous_goal_proposed",
                        "goal_preview": content[:240],
                        "message_id": mid,
                        "detail": "Autonomous pipeline runs are disabled by runtime policy.",
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
            applied_actions: List[Dict[str, Any]] = []
            directive_result = await self._apply_operator_directives(
                session_id,
                session=session or {},
                content=content,
            )
            actions = directive_result.get("actions") if isinstance(directive_result, dict) else []
            if isinstance(actions, list):
                applied_actions.extend([item for item in actions if isinstance(item, dict)])

            brain_result = await self._invoke_platform_brain(
                session_id,
                session=session or {},
                operator_message=content,
                recent_messages=messages,
            )
            if bool(brain_result.get("ok")):
                brain_reply = str(brain_result.get("reply") or "").strip()
                if brain_reply:
                    await self._store.append_message(
                        session_id,
                        role="assistant",
                        content=brain_reply,
                        metadata={
                            "source": "platform_brain",
                            "operator_message_id": mid,
                        },
                    )
                brain_actions = brain_result.get("actions") if isinstance(brain_result.get("actions"), list) else []
                clean_brain_actions = [item for item in brain_actions if isinstance(item, dict)]
                if clean_brain_actions:
                    brain_directive_result = await self._apply_operator_directives(
                        session_id,
                        session=session or {},
                        content=json.dumps({"actions": clean_brain_actions}, ensure_ascii=False),
                    )
                    model_actions = brain_directive_result.get("actions") if isinstance(brain_directive_result, dict) else []
                    if isinstance(model_actions, list):
                        applied_actions.extend([item for item in model_actions if isinstance(item, dict)])
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "platform_brain_actions_applied",
                            "message_id": mid,
                            "requested_count": len(clean_brain_actions),
                            "applied_count": len(model_actions or []),
                            "applied_actions": [item.get("action") for item in model_actions or [] if isinstance(item, dict)],
                        },
                    )
            else:
                await self._store.append_message(
                    session_id,
                    role="assistant",
                    content=(
                        str(brain_result.get("hint") or "").strip()
                        or (
                            "Platform brain backend is unavailable right now. "
                            "Fix backend configuration to enable AI-driven investigation."
                        )
                    ),
                    metadata={
                        "source": "platform_brain_error",
                        "operator_message_id": mid,
                        "error": str(brain_result.get("error") or "").strip() or None,
                    },
                )

            if applied_actions:
                executed_actions = [
                    item
                    for item in applied_actions
                    if isinstance(item, dict)
                    and isinstance(item.get("result"), dict)
                    and bool(item["result"].get("ok"))
                ]
                proposal_actions = [
                    item
                    for item in applied_actions
                    if isinstance(item, dict)
                    and isinstance(item.get("result"), dict)
                    and bool(item["result"].get("proposal_only"))
                ]
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "operator_directives_applied",
                        "message_id": mid,
                        "executed_count": len(executed_actions),
                        "proposal_count": len(proposal_actions),
                        "applied_actions": [item.get("action") for item in applied_actions if isinstance(item, dict)],
                    },
                )
                if executed_actions:
                    action_names = ", ".join(
                        str(item.get("action") or "action")
                        for item in executed_actions[:6]
                    )
                    await self._store.append_message(
                        session_id,
                        role="assistant",
                        content=f"Executed operator directives: {action_names}.",
                        metadata={"source": "operator_directive_executor", "operator_message_id": mid},
                    )
                if proposal_actions:
                    proposal_details = ", ".join(
                        f"{str(item.get('action') or 'action')} ({str((item.get('result') or {}).get('detail') or 'approval_required')})"
                        for item in proposal_actions[:6]
                    )
                    await self._store.append_message(
                        session_id,
                        role="assistant",
                        content=f"Recorded proposal-only directives: {proposal_details}.",
                        metadata={"source": "operator_directive_proposal", "operator_message_id": mid},
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
        explicit_orchestration_id = str(session.get("orchestration_id") or "").strip() or None
        resolved_orchestration_id = explicit_orchestration_id
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}

        run: Optional[Dict[str, Any]] = None
        if self._run_store is not None:
            try:
                # Prefer an explicitly attached orchestration over stale run_id pointers.
                # This keeps autonomous tuner context aligned with the active orchestration.
                if explicit_orchestration_id:
                    run = await self._run_store.get_run_by_orchestration(explicit_orchestration_id)
                elif resolved_run_id:
                    run = await self._run_store.get_run(resolved_run_id)
                elif resolved_orchestration_id:
                    run = await self._run_store.get_run_by_orchestration(resolved_orchestration_id)
                elif resolved_assignment_id:
                    run = await self._run_store.get_latest_run_for_assignment(resolved_assignment_id)
            except Exception:
                run = None
        if isinstance(run, dict):
            run_assignment_id = str(run.get("assignment_id") or "").strip() or None
            run_id = str(run.get("id") or "").strip() or None
            run_orchestration_id = str(run.get("orchestration_id") or "").strip() or None
            if explicit_orchestration_id and run_orchestration_id and run_orchestration_id != explicit_orchestration_id:
                # Defensive fallback: ignore stale run bindings when they disagree
                # with the session's explicit orchestration attachment.
                run = None
            else:
                resolved_assignment_id = run_assignment_id or resolved_assignment_id
                resolved_run_id = run_id or resolved_run_id
                resolved_orchestration_id = run_orchestration_id or resolved_orchestration_id
        if explicit_orchestration_id:
            resolved_orchestration_id = explicit_orchestration_id
            if run is None:
                # Prevent stale run pointers from overriding explicit orchestration scope.
                resolved_run_id = None

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

    def _derive_seed_binding_from_context(
        self,
        *,
        context: Dict[str, Any],
        session_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
        task_rows = [row for row in tasks if isinstance(row, dict)]
        if not task_rows and not isinstance(context, dict):
            return {}

        def _task_key(task: Dict[str, Any]) -> tuple[str, str]:
            return (str(task.get("created_at") or ""), str(task.get("id") or ""))

        ordered_tasks = sorted(task_rows, key=_task_key)
        root_task: Optional[Dict[str, Any]] = None
        for task in ordered_tasks:
            metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
            task_id = str(task.get("id") or "").strip()
            workflow_root_task_id = str(metadata.get("workflow_root_task_id") or "").strip()
            if task_id and workflow_root_task_id and workflow_root_task_id == task_id:
                root_task = task
                break
        if root_task is None:
            for task in ordered_tasks:
                metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
                parent_task_id = str(metadata.get("parent_task_id") or "").strip()
                if not parent_task_id:
                    root_task = task
                    break

        def _metadata_value(key: str) -> str:
            for source in [root_task, *ordered_tasks]:
                if not isinstance(source, dict):
                    continue
                metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
                value = str(metadata.get(key) or "").strip()
                if value:
                    return value
            return ""

        candidate_tasks: List[Dict[str, Any]] = []
        if isinstance(root_task, dict):
            candidate_tasks.append(root_task)
        candidate_tasks.extend(task for task in ordered_tasks if task is not root_task)

        instruction = ""
        node_overrides: Dict[str, Any] = {}
        for task in candidate_tasks:
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            if not instruction:
                instruction = (
                    str(payload.get("instruction") or "").strip()
                    or str(payload.get("prompt") or "").strip()
                )
            if not node_overrides and isinstance(payload.get("node_overrides"), dict):
                node_overrides = copy.deepcopy(payload.get("node_overrides") or {})
            if instruction and node_overrides:
                break

        project_id = (
            str(session_metadata.get("project_id") or "").strip()
            or _metadata_value("project_id")
        )
        conversation_id = (
            str(session_metadata.get("conversation_id") or "").strip()
            or _metadata_value("conversation_id")
        )
        trigger_source = _metadata_value("source")

        seed_binding: Dict[str, Any] = {}
        assignment_id = str(context.get("assignment_id") or "").strip()
        run_id = str(context.get("run_id") or "").strip()
        orchestration_id = str(context.get("orchestration_id") or "").strip()
        if assignment_id:
            seed_binding["seed_assignment_id"] = assignment_id
        if run_id:
            seed_binding["seed_run_id"] = run_id
        if orchestration_id:
            seed_binding["seed_orchestration_id"] = orchestration_id
        if project_id:
            seed_binding["seed_project_id"] = project_id
        if conversation_id:
            seed_binding["seed_conversation_id"] = conversation_id
        if instruction:
            seed_binding["instruction"] = instruction[:4000]
        if node_overrides:
            seed_binding["node_overrides"] = node_overrides
        if trigger_source:
            seed_binding["trigger_source"] = trigger_source
        return seed_binding

    async def _backfill_seed_binding_from_context(
        self,
        session_id: str,
        *,
        context: Dict[str, Any],
        session_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        metadata = dict(session_metadata or {})
        derived = self._derive_seed_binding_from_context(context=context, session_metadata=metadata)
        if not derived:
            return metadata
        existing = metadata.get("seed_binding") if isinstance(metadata.get("seed_binding"), dict) else {}
        merged = dict(existing)
        changed = False
        for key, value in derived.items():
            if key == "node_overrides":
                if not isinstance(merged.get("node_overrides"), dict) or not merged.get("node_overrides"):
                    if isinstance(value, dict) and value:
                        merged["node_overrides"] = copy.deepcopy(value)
                        changed = True
                continue
            current_value = str(merged.get(key) or "").strip()
            next_value = str(value or "").strip()
            if not current_value and next_value:
                merged[key] = value
                changed = True
        updates: Dict[str, Any] = {}
        if changed:
            updates["seed_binding"] = merged
        if not str(metadata.get("project_id") or "").strip():
            project_id = str(derived.get("seed_project_id") or "").strip()
            if project_id:
                updates["project_id"] = project_id
        if not str(metadata.get("conversation_id") or "").strip():
            conversation_id = str(derived.get("seed_conversation_id") or "").strip()
            if conversation_id:
                updates["conversation_id"] = conversation_id
        if not updates:
            return metadata
        await self._store.update_session(session_id, metadata=updates)
        metadata.update(updates)
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "seed_binding_backfilled",
                "keys": sorted(updates.keys()),
                "has_instruction": bool(str((updates.get("seed_binding") or {}).get("instruction") or "").strip()),
            },
        )
        return metadata

    async def _pipeline_launch_payload(
        self,
        *,
        pipeline_bot_id: str,
        goal: str,
        seed_binding: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        safe_bot_id = str(pipeline_bot_id or "").strip()
        seed_data = seed_binding if isinstance(seed_binding, dict) else {}
        seed_instruction = str(seed_data.get("instruction") or "").strip()
        seed_overrides = seed_data.get("node_overrides") if isinstance(seed_data.get("node_overrides"), dict) else {}
        instruction = seed_instruction or (goal[:2000] if goal else f"Run pipeline test for {safe_bot_id}")
        fallback: Dict[str, Any] = {"instruction": instruction}
        if goal and seed_instruction:
            fallback["platform_ai_goal"] = goal[:2000]
        if seed_overrides:
            fallback["node_overrides"] = copy.deepcopy(seed_overrides)
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
        if seed_instruction:
            merged["instruction"] = seed_instruction
            if goal:
                merged["platform_ai_goal"] = goal[:2000]
        elif goal and not str(merged.get("instruction") or "").strip():
            merged["instruction"] = goal[:2000]
        if seed_overrides and not isinstance(merged.get("node_overrides"), dict):
            merged["node_overrides"] = copy.deepcopy(seed_overrides)
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
        # The autonomous evaluator may run in proposal-only mode. Keep this
        # guard at the write boundary so no caller can bypass that policy.
        if not _configuration_mutations_enabled():
            return {
                "updated": False,
                "reason": "configuration_mutations_disabled",
                "proposal_only": True,
            }
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
        if not _autonomous_pipeline_runs_enabled():
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_orchestration_blocked",
                    "reason": reason,
                    "detail": "Autonomous pipeline runs are disabled by runtime policy.",
                },
            )
            return None
        if self._task_manager is None:
            return None
        launch_orchestration_id = str(uuid.uuid4())
        live_session = await self._store.get_session(session_id)
        live_meta = live_session.get("metadata") if isinstance((live_session or {}).get("metadata"), dict) else {}
        seed_binding = live_meta.get("seed_binding") if isinstance(live_meta.get("seed_binding"), dict) else {}
        session_project_id = str(
            live_meta.get("project_id")
            or seed_binding.get("seed_project_id")
            or ""
        ).strip()
        session_conversation_id = str(
            live_meta.get("conversation_id")
            or seed_binding.get("seed_conversation_id")
            or ""
        ).strip()
        payload = await self._pipeline_launch_payload(
            pipeline_bot_id=pipeline_bot_id,
            goal=goal,
            seed_binding=seed_binding,
        )
        trigger_source = str(seed_binding.get("trigger_source") or "").strip() or "platform_ai_autonomous_tuner"
        try:
            created = await self._task_manager.create_task(
                bot_id=pipeline_bot_id,
                payload=payload,
                metadata=TaskMetadata(
                    source=trigger_source,
                    project_id=session_project_id or None,
                    conversation_id=session_conversation_id or None,
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
                "autonomous_terminalized_at": None,
                "autonomous_terminal_reason": None,
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
                "seed_binding": {
                    "seed_run_id": str(seed_binding.get("seed_run_id") or "").strip() or None,
                    "seed_orchestration_id": str(seed_binding.get("seed_orchestration_id") or "").strip() or None,
                    "trigger_source": trigger_source,
                },
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
        if not _autonomous_pipeline_runs_enabled():
            return
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        if not bool(metadata.get("autonomous_enabled")):
            return
        context = await self._resolve_context(session)
        metadata = await self._backfill_seed_binding_from_context(
            session_id,
            context=context,
            session_metadata=metadata,
        )
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

        # Read session brief — it constrains what the tuner should focus on and
        # provides expected_deliverables / forbidden_behaviors for richer assertions.
        brief_data: Dict[str, Any] = {}
        try:
            brief_row = await self._store.get_session_brief(session_id)
            if isinstance(brief_row, dict):
                brief_data = brief_row.get("brief") if isinstance(brief_row.get("brief"), dict) else {}
        except Exception:
            pass
        brief_expected_deliverables: List[str] = [
            str(d) for d in (brief_data.get("expected_deliverables") or []) if str(d).strip()
        ]
        brief_tuning_scope = str(brief_data.get("tuning_scope") or "").strip()
        brief_forbidden = [
            str(b) for b in (brief_data.get("forbidden_behaviors") or []) if str(b).strip()
        ]

        # Dedup using a richer signature that includes task statuses so that
        # status transitions (e.g. running→completed) are not suppressed.
        _status_key = ":".join(
            f"{str(t.get('id', ''))[:8]}={str(t.get('status', ''))}"
            for t in sorted(tasks, key=lambda t: str(t.get("id") or ""))[:20]
        )
        eval_signature_preview = f"{orchestration_id}:{len(tasks)}:{_status_key}"
        action_snapshot = {
            "orchestration_id": orchestration_id,
            "eval_signature": eval_signature_preview,
            "brief_tuning_scope": brief_tuning_scope or None,
        }
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
            suite_def = _build_default_suite(
                suite_name=suite_name,
                graph=graph,
                brief_expected_deliverables=brief_expected_deliverables,
                brief_forbidden=brief_forbidden,
            )
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
            await self._complete_action_record(
                action["id"],
                output_snapshot={"reason": "tasks_not_terminal", "task_count": len(tasks)},
                had_effect=False,
                summary="Tasks not yet terminal; deferring evaluation.",
            )
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
            await self._complete_action_record(
                action["id"],
                output_snapshot={"reason": "eval_signature_unchanged"},
                had_effect=False,
                summary="Eval signature unchanged since last run; no new evaluation needed.",
            )
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
            # Session brief constraints — makes the evaluation result actionable
            # against operator-declared expectations, not just graph heuristics.
            "brief_expected_deliverables": brief_expected_deliverables or None,
            "brief_tuning_scope": brief_tuning_scope or None,
            "brief_forbidden_behaviors": brief_forbidden or None,
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
        previous_consecutive = int(metadata.get("autonomous_consecutive_passes") or 0)
        consecutive_passes = (previous_consecutive + 1) if passed_target else 0
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
                "autonomous_consecutive_passes": consecutive_passes,
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
        recent_messages = await self._store.list_messages(session_id, limit=120)
        brain_prompt = (
            "Autonomous tuning iteration decision point.\n"
            f"Pipeline: {pipeline_name or pipeline_bot_id}\n"
            f"Orchestration: {orchestration_id}\n"
            f"Evaluation status: {eval_status}\n"
            f"Evaluation score: {eval_score:.3f} (target {target_score:.3f})\n"
            f"Consecutive passes: {consecutive_passes}/3\n"
            f"Failed checks: {', '.join(str(item.get('id') or item.get('name') or 'test') for item in failed_tests[:6]) or 'none'}\n"
            "Decide whether to emit actionable directives to improve the pipeline."
        )
        brain_result = await self._invoke_platform_brain(
            session_id,
            session=session,
            operator_message=brain_prompt,
            recent_messages=recent_messages,
        )
        if bool(brain_result.get("ok")):
            brain_reply = str(brain_result.get("reply") or "").strip()
            if brain_reply:
                await self._store.append_message(
                    session_id,
                    role="assistant",
                    content=brain_reply[:4000],
                    metadata={
                        "source": "platform_brain_autonomous",
                        "suite_run_id": (final_run or {}).get("id"),
                    },
                )
            model_actions = brain_result.get("actions") if isinstance(brain_result.get("actions"), list) else []
            clean_model_actions = [item for item in model_actions if isinstance(item, dict)]
            if clean_model_actions:
                model_directive_result = await self._apply_operator_directives(
                    session_id,
                    session=session,
                    content=json.dumps({"actions": clean_model_actions}, ensure_ascii=False),
                )
                applied = model_directive_result.get("actions") if isinstance(model_directive_result, dict) else []
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "platform_brain_autonomous_actions_applied",
                        "suite_run_id": (final_run or {}).get("id"),
                        "requested_count": len(clean_model_actions),
                        "applied_count": len(applied or []),
                        "applied_actions": [item.get("action") for item in applied or [] if isinstance(item, dict)],
                    },
                )
        elif self._require_platform_brain_for_autonomy():
            hint = str(brain_result.get("hint") or "").strip() or (
                "Platform brain backend is unavailable; pausing autonomy to avoid blind reruns."
            )
            await self._store.update_session(
                session_id,
                status="ready",
                metadata={
                    "checkpoint_reason": "platform_brain_unavailable",
                    "autonomous_state": "ready_checkpoint",
                    "autonomous_last_brain_error": str(brain_result.get("error") or "").strip() or None,
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"{hint} Session moved to ready. Fix backend config and resume to continue AI-driven tuning."
                ),
                metadata={
                    "source": "platform_brain_error",
                    "state": "ready_checkpoint",
                    "suite_run_id": (final_run or {}).get("id"),
                },
            )
            await self._complete_action_record(
                action["id"],
                output_snapshot={
                    "eval_status": eval_status,
                    "eval_score": eval_score,
                    "launched": False,
                    "platform_brain_available": False,
                },
                had_effect=True,
                summary="Paused autonomous refinement because platform brain backend is unavailable.",
            )
            return

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
            _proposal_directives = [
                f"Platform AI tuning iteration: {_next_iter_for_pp}",
                f"Goal summary: {goal[:1200] if goal else 'Improve end-to-end execution and output quality.'}",
                f"Failed tests: {', '.join(str(item.get('id') or item.get('name') or 'test') for item in _failed_tests_for_pp[:5]) or 'none'}",
                f"Failed assertion kinds: {', '.join(_failed_assertions_for_pp) or 'none'}",
                "Requirements:",
                "- Produce deterministic, structured outputs with explicit quality sections and acceptance checks.",
                "- Prioritize passing no_failed_tasks, completed_ratio, node_coverage_ratio, and min_avg_quality checks.",
                "- Avoid partial/incomplete outputs; prefer complete artifacts with validation notes.",
            ]
            _proposal_keywords = self._goal_keywords(goal)
            if _proposal_keywords:
                _proposal_directives.append(f"- Ensure outputs explicitly cover: {', '.join(_proposal_keywords)}.")
            _patch_proposal = await self._store.create_patch_proposal(
                session_id,
                action_id=action["id"],
                target_config=f"bot:{pipeline_bot_id}:system_prompt",
                before_state={"system_prompt_preview": _existing_prompt_preview},
                after_state={
                    "proposal_kind": "bot_system_prompt_refinement",
                    "pipeline_bot_id": pipeline_bot_id,
                    "iteration": _next_iter_for_pp,
                    "directives_applied": f"iteration_{_next_iter_for_pp}_refinement",
                    "failed_tests": [
                        str(item.get("id") or item.get("name") or "test")
                        for item in _failed_tests_for_pp[:5]
                    ],
                    "failed_assertions": _failed_assertions_for_pp[:8],
                    "suggested_autotune_block": self._merge_autotune_directives("", "\n".join(_proposal_directives)),
                    "requires_direct_operator_edit": True,
                },
                rationale=f"Bot refinement for iteration {_next_iter_for_pp} based on failed tests: {_failed_assertions_for_pp[:5]}",
                expected_effect=f"Pipeline quality score improvement from {eval_score:.3f} toward target {target_score:.3f}",
                validation_steps=[
                    "review_prompt_refinement",
                    "apply_direct_operator_edit",
                    "launch_new_orchestration",
                    "evaluate_suite",
                    "compare_score",
                ],
                rollback_note="Remove [[NEXUS_PLATFORM_AI_AUTOTUNE_START]]...[[NEXUS_PLATFORM_AI_AUTOTUNE_END]] block from system_prompt",
            )
            await self._store.append_event(session_id, "action_trace", {
                "action": "patch_proposal_created",
                "proposal_id": _patch_proposal["id"],
                "target_config": _patch_proposal["target_config"],
            })
            if not _configuration_mutations_enabled():
                await self._store.update_session(
                    session_id,
                    status="ready",
                    metadata={
                        "checkpoint_reason": "configuration_proposal_pending",
                        "autonomous_state": "ready_checkpoint",
                        "autonomous_pending_proposal_id": _patch_proposal["id"],
                        "autonomous_last_refine_signature": eval_signature,
                    },
                )
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "autonomous_bot_refinement_proposed",
                        "proposal_id": _patch_proposal["id"],
                        "pipeline_bot_id": pipeline_bot_id,
                        "iteration": _next_iter_for_pp,
                        "configuration_mutations_enabled": False,
                    },
                )
                await self._store.append_message(
                    session_id,
                    role="assistant",
                    content=(
                        f"Created a prompt-refinement proposal for `{pipeline_name or pipeline_bot_id}` after "
                        f"quality score {eval_score:.3f}. Configuration mutations are disabled, so the live bot, "
                        "quality suite, and orchestration remain unchanged. Review the proposal, apply any approved "
                        "prompt change through the Bot editor, then resume this session to run a fresh iteration."
                    ),
                    metadata={
                        "source": "autonomous_tuner",
                        "state": "configuration_proposal_pending",
                        "proposal_id": _patch_proposal["id"],
                    },
                )
                await self._complete_action_record(
                    action["id"],
                    output_snapshot={
                        "eval_status": eval_status,
                        "eval_score": eval_score,
                        "launched": False,
                        "proposal_id": _patch_proposal["id"],
                        "configuration_mutations_enabled": False,
                    },
                    had_effect=True,
                    summary="Paused after recording a prompt-refinement proposal; live configuration remains unchanged.",
                )
                return

        if passed_target and consecutive_passes >= 3:
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_converged",
                    "target_score": target_score,
                    "score": eval_score,
                    "iteration": current_iteration,
                    "consecutive_passes": consecutive_passes,
                },
            )
            completion_report = {
                "completed_at": _now(),
                "pipeline_bot_id": pipeline_bot_id,
                "pipeline_name": pipeline_name or pipeline_bot_id,
                "target_score": target_score,
                "latest_score": eval_score,
                "required_consecutive_passes": 3,
                "achieved_consecutive_passes": consecutive_passes,
                "suite_id": str(suite.get("id") or ""),
                "latest_suite_run_id": str((final_run or {}).get("id") or ""),
                "orchestration_id": orchestration_id,
            }
            await self._store.update_session(
                session_id,
                status="ready",
                metadata={"autonomous_completion_report": completion_report, "checkpoint_reason": "quality_gate_passed"},
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"Autonomous tuner reached target quality for `{pipeline_name or pipeline_bot_id}`: "
                    f"score {eval_score:.3f} (target {target_score:.3f}) with {consecutive_passes}/3 consecutive passes. "
                    "Session moved to ready."
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
        if passed_target and consecutive_passes < 3:
            validation_iteration = current_iteration + 1
            launched_validation = await self._launch_autonomous_orchestration(
                session_id=session_id,
                pipeline_bot_id=pipeline_bot_id or "",
                pipeline_name=pipeline_name or (pipeline_bot_id or ""),
                goal=goal,
                reason="quality_gate_validation",
                iteration=validation_iteration,
            )
            await self._store.update_session(
                session_id,
                metadata={
                    "autonomous_iteration": validation_iteration,
                    "autonomous_state": "quality_gate_validation",
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"Quality gate pass {consecutive_passes}/3 achieved for `{pipeline_name or pipeline_bot_id}`. "
                    f"Launching validation run `{launched_validation}`."
                ),
                metadata={"source": "autonomous_tuner", "state": "quality_gate_validation"},
            )
            await self._complete_action_record(
                action["id"],
                output_snapshot={"eval_status": eval_status, "eval_score": eval_score, "launched": bool(launched_validation)},
                had_effect=True,
                summary=f"Quality gate streak {consecutive_passes}/3; launched validation={bool(launched_validation)}",
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
        strategy_shift = False
        if current_iteration >= max_iterations:
            strategy_shift = True
            strategy_shift_count = int(metadata.get("autonomous_strategy_shift_count") or 0) + 1
            await self._store.update_session(
                session_id,
                metadata={
                    "autonomous_state": "strategy_shift",
                    "autonomous_strategy_shift_count": strategy_shift_count,
                    "autonomous_last_refine_signature": None,
                },
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "autonomous_strategy_shift",
                    "iteration": current_iteration,
                    "max_iterations": max_iterations,
                    "score": eval_score,
                    "target_score": target_score,
                    "strategy_shift_count": strategy_shift_count,
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    f"Autonomous tuner hit the configured max iterations ({max_iterations}) with score {eval_score:.3f}. "
                    "Applying strategy shift and continuing refinement."
                ),
                metadata={"source": "autonomous_tuner", "state": "strategy_shift"},
            )
            current_iteration = 0

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
            reason="strategy_shift_iteration" if strategy_shift else "refinement_iteration",
            iteration=next_iteration,
        )
        await self._store.update_session(
            session_id,
            metadata={
                "autonomous_iteration": next_iteration,
                "autonomous_suite_id": str(refined_suite.get("id") or ""),
                "autonomous_last_refine_signature": eval_signature,
                "autonomous_last_bot_refine_result": bot_refine,
                "autonomous_state": "running_iteration" if launched else "needs_replan",
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
        else:
            await self._store.update_session(
                session_id,
                status="ready",
                metadata={"checkpoint_reason": "launch_failed_after_refinement"},
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    "Autonomous refinement updated the suite/config but could not launch the next orchestration. "
                    "Session moved to ready; verify runtime dependencies and resume."
                ),
                metadata={"source": "autonomous_tuner", "state": "ready_checkpoint"},
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
        await self._store.update_session(
            session_id,
            metadata={
                "deploy_runner_state": "running",
                "deploy_runner_started_at": _now(),
                "deploy_runner_last_error": None,
            },
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
                await self._store.update_session(
                    session_id,
                    metadata={
                        "deploy_runner_state": "failed",
                        "deploy_runner_last_error": f"deploy manager unavailable: {exc}",
                        "deploy_runner_finished_at": _now(),
                    },
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
                await self._store.update_session(
                    session_id,
                    metadata={
                        "deploy_runner_state": "failed",
                        "deploy_runner_last_error": str(message or "deploy start rejected"),
                        "deploy_runner_finished_at": _now(),
                    },
                )
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
                    await self._store.update_session(
                        session_id,
                        metadata={
                            "deploy_runner_state": "succeeded" if state == "succeeded" else "failed",
                            "deploy_runner_last_error": status.get("last_error"),
                            "deploy_runner_finished_at": _now(),
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
                    else:
                        await self._store.append_message(
                            session_id,
                            role="assistant",
                            content=(
                                "Deployment completed successfully. The deploy runner has finished and session automation can continue."
                            ),
                            metadata={"source": "deploy_runner", "state": "succeeded"},
                        )
                    break
                await asyncio.sleep(2.0)
        finally:
            self._deploy_tasks.pop(session_id, None)

    async def _project_edit_loop(
        self,
        session_id: str,
        *,
        requested_by: str,
        instruction: str,
    ) -> None:
        cmd_env = "NEXUS_PLATFORM_AI_PROJECT_EDIT_RUN_CMD"
        run_cmd = str(os.environ.get(cmd_env, "") or "").strip()
        kind = "project_code_edit"
        await self._store.append_event(
            session_id,
            "action_trace",
            {"action": "project_edit_runner_started", "requested_by": requested_by, "started_at": _now()},
        )
        await self._store.update_session(
            session_id,
            metadata={
                "project_edit_runner_state": "running",
                "project_edit_runner_started_at": _now(),
                "project_edit_runner_last_error": None,
            },
        )
        if not run_cmd:
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "project_edit_runner_error", "detail": f"{cmd_env} is not configured"},
            )
            await self._store.update_session(
                session_id,
                status="ready",
                metadata={
                    "project_edit_runner_state": "failed",
                    "project_edit_runner_last_error": f"{cmd_env} is not configured",
                    "project_edit_runner_finished_at": _now(),
                    "checkpoint_reason": "project_edit_runner_unavailable",
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Project code edit runner is unavailable: `{cmd_env}` is not configured.",
                metadata={"source": "project_edit_runner", "state": "failed"},
            )
            return

        default_cwd = Path(__file__).resolve().parents[2]
        configured_cwd = str(os.environ.get("NEXUS_PLATFORM_AI_PROJECT_EDIT_CWD", "") or "").strip()
        cwd_path = Path(configured_cwd).resolve() if configured_cwd else default_cwd
        if not cwd_path.exists() or not cwd_path.is_dir():
            cwd_path = default_cwd

        live_session = await self._store.get_session(session_id)
        live_metadata = live_session.get("metadata") if isinstance((live_session or {}).get("metadata"), dict) else {}
        session_project_id = str(live_metadata.get("project_id") or "").strip()
        session_mode = str((live_session or {}).get("mode") or "").strip()
        env = os.environ.copy()
        env["NEXUS_PLATFORM_AI_SESSION_ID"] = str(session_id)
        env["NEXUS_PLATFORM_AI_REQUESTED_BY"] = str(requested_by or "")
        env["NEXUS_PLATFORM_AI_OPERATOR_INSTRUCTION"] = str(instruction or "")
        env["NEXUS_PLATFORM_AI_REPO_EDIT_KIND"] = kind
        env["NEXUS_PLATFORM_AI_SESSION_PROJECT_ID"] = session_project_id
        env["NEXUS_PLATFORM_AI_SESSION_MODE"] = session_mode
        timeout_seconds = _safe_timeout_seconds(
            "NEXUS_PLATFORM_AI_PROJECT_EDIT_TIMEOUT_SECONDS",
            1800.0,
            min_value=30.0,
            max_value=14400.0,
        )
        try:
            proc = await asyncio.create_subprocess_shell(
                run_cmd,
                cwd=str(cwd_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            assert proc.stdout is not None
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "project_edit_requested", "cwd": str(cwd_path), "command_env": cmd_env},
            )
            lines: List[str] = []
            async def _stream_lines() -> None:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").rstrip()
                    if not text:
                        continue
                    if len(lines) < 200:
                        lines.append(text)
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "project_edit_log", "line": text},
                    )

            stream_task = asyncio.create_task(_stream_lines())
            timed_out = False
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                rc = await proc.wait()
            await stream_task
            succeeded = rc == 0
            suggested_commit = f"project-edit: apply platform ai patch set ({session_id[:8]})"
            alternatives = [
                "Split change into infra vs product commits",
                "Squash into one patch-only commit after human review",
                "Discard and rerun with tighter scope if quality gates fail",
            ]
            await self._store.update_session(
                session_id,
                status="ready",
                metadata={
                    "project_edit_runner_state": "succeeded" if succeeded else "failed",
                    "project_edit_runner_exit_code": rc,
                    "project_edit_runner_timed_out": timed_out,
                    "project_edit_runner_finished_at": _now(),
                    "project_edit_runner_last_error": None if succeeded else ("runner timed out" if timed_out else f"exit_code={rc}"),
                    "checkpoint_reason": "project_edit_complete",
                    "project_edit_report": {
                        "exit_code": rc,
                        "timed_out": timed_out,
                        "suggested_commit_message": suggested_commit,
                        "alternatives": alternatives,
                        "log_preview": lines[-20:],
                    },
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    (
                        "Public project edit run completed (patch+tests). Session moved to ready for operator review. "
                        f"Suggested commit message: {suggested_commit}. "
                        f"Alternatives: {', '.join(alternatives)}. No commit/push was performed by Platform AI."
                    )
                    if succeeded
                    else (
                        "Public project edit run failed and session moved to ready for review. "
                        f"exit_code={rc} timed_out={timed_out}. No commit/push was performed by Platform AI."
                    )
                ),
                metadata={"source": "project_edit_runner", "state": "succeeded" if succeeded else "failed"},
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "project_edit_finished",
                    "state": "succeeded" if succeeded else "failed",
                    "exit_code": rc,
                    "timed_out": timed_out,
                },
            )
        except Exception as exc:
            await self._store.update_session(
                session_id,
                status="ready",
                metadata={
                    "project_edit_runner_state": "failed",
                    "project_edit_runner_last_error": str(exc),
                    "project_edit_runner_finished_at": _now(),
                    "checkpoint_reason": "project_edit_error",
                },
            )
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "project_edit_runner_error", "detail": str(exc)},
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Project code edit runner crashed: {exc}",
                metadata={"source": "project_edit_runner", "state": "failed"},
            )
        finally:
            self._project_edit_tasks.pop(session_id, None)

    async def _repo_edit_loop(
        self,
        session_id: str,
        *,
        requested_by: str,
        instruction: str,
        external: bool,
    ) -> None:
        cmd_env = "NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_RUN_CMD" if external else "NEXUS_PLATFORM_AI_REPO_EDIT_RUN_CMD"
        run_cmd = str(os.environ.get(cmd_env, "") or "").strip()
        kind = "external_repo_edit" if external else "repo_edit"
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "repo_edit_runner_started",
                "requested_by": requested_by,
                "kind": kind,
                "started_at": _now(),
            },
        )
        await self._store.update_session(
            session_id,
            metadata={
                "repo_edit_runner_state": "running",
                "repo_edit_runner_kind": kind,
                "repo_edit_runner_started_at": _now(),
                "repo_edit_runner_last_error": None,
            },
        )
        if not run_cmd:
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "repo_edit_runner_error",
                    "kind": kind,
                    "detail": f"{cmd_env} is not configured",
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Repo edit runner is unavailable: `{cmd_env}` is not configured.",
                metadata={"source": "repo_edit_runner", "state": "failed", "kind": kind},
            )
            await self._store.update_session(
                session_id,
                metadata={
                    "repo_edit_runner_state": "failed",
                    "repo_edit_runner_last_error": f"{cmd_env} is not configured",
                    "repo_edit_runner_finished_at": _now(),
                },
            )
            return

        default_cwd = Path(__file__).resolve().parents[2]
        configured_cwd = str(os.environ.get("NEXUS_PLATFORM_AI_REPO_EDIT_CWD", "") or "").strip()
        cwd_path = Path(configured_cwd).resolve() if configured_cwd else default_cwd
        if not cwd_path.exists() or not cwd_path.is_dir():
            cwd_path = default_cwd

        live_session = await self._store.get_session(session_id)
        live_metadata = live_session.get("metadata") if isinstance((live_session or {}).get("metadata"), dict) else {}
        session_project_id = str(live_metadata.get("project_id") or "").strip()
        session_mode = str((live_session or {}).get("mode") or "").strip()
        env = os.environ.copy()
        env["NEXUS_PLATFORM_AI_SESSION_ID"] = str(session_id)
        env["NEXUS_PLATFORM_AI_REQUESTED_BY"] = str(requested_by or "")
        env["NEXUS_PLATFORM_AI_OPERATOR_INSTRUCTION"] = str(instruction or "")
        env["NEXUS_PLATFORM_AI_REPO_EDIT_KIND"] = kind
        env["NEXUS_PLATFORM_AI_SESSION_PROJECT_ID"] = session_project_id
        env["NEXUS_PLATFORM_AI_SESSION_MODE"] = session_mode
        timeout_seconds = _safe_timeout_seconds(
            "NEXUS_PLATFORM_AI_REPO_EDIT_TIMEOUT_SECONDS",
            1800.0,
            min_value=30.0,
            max_value=14400.0,
        )

        try:
            proc = await asyncio.create_subprocess_shell(
                run_cmd,
                cwd=str(cwd_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            assert proc.stdout is not None
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "repo_edit_requested",
                    "kind": kind,
                    "cwd": str(cwd_path),
                    "command_env": cmd_env,
                },
            )
            async def _stream_lines() -> None:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").rstrip()
                    if not text:
                        continue
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "repo_edit_log", "kind": kind, "line": text},
                    )

            stream_task = asyncio.create_task(_stream_lines())
            timed_out = False
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                rc = await proc.wait()
            await stream_task
            succeeded = rc == 0
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "repo_edit_finished",
                    "kind": kind,
                    "state": "succeeded" if succeeded else "failed",
                    "exit_code": rc,
                    "timed_out": timed_out,
                },
            )
            await self._store.update_session(
                session_id,
                metadata={
                    "repo_edit_runner_state": "succeeded" if succeeded else "failed",
                    "repo_edit_runner_exit_code": rc,
                    "repo_edit_runner_timed_out": timed_out,
                    "repo_edit_runner_finished_at": _now(),
                    "repo_edit_runner_last_error": (
                        f"runner timed out after {int(timeout_seconds)}s" if timed_out else None
                    ),
                },
            )
            if succeeded:
                await self._store.append_message(
                    session_id,
                    role="assistant",
                    content=(
                        "Repo edit runner completed successfully. Code update/commit/push automation finished and control returned to Platform AI."
                    ),
                    metadata={"source": "repo_edit_runner", "state": "succeeded", "kind": kind},
                )
                if not external and _env_enabled("NEXUS_PLATFORM_AI_REPO_EDIT_AUTO_DEPLOY"):
                    deploy = await self.start_deploy_run(session_id, requested_by=requested_by or "platform-ai")
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "repo_edit_auto_deploy", "result": deploy},
                    )
            else:
                await self._store.append_message(
                    session_id,
                    role="assistant",
                    content=(
                        (
                            f"Repo edit runner failed with exit code {rc}. Review the action trace logs, then retry after fixing the runner command."
                            if not timed_out
                            else f"Repo edit runner timed out after {int(timeout_seconds)}s and was terminated."
                        )
                    ),
                    metadata={"source": "repo_edit_runner", "state": "failed", "kind": kind, "exit_code": rc},
                )
        except Exception as exc:
            await self._store.append_event(
                session_id,
                "action_trace",
                {"action": "repo_edit_runner_error", "kind": kind, "detail": str(exc)},
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=f"Repo edit runner crashed: {exc}",
                metadata={"source": "repo_edit_runner", "state": "failed", "kind": kind},
            )
            await self._store.update_session(
                session_id,
                metadata={
                    "repo_edit_runner_state": "failed",
                    "repo_edit_runner_last_error": str(exc),
                    "repo_edit_runner_finished_at": _now(),
                },
            )
        finally:
            self._repo_edit_tasks.pop(session_id, None)

    async def get_session_brief(self, session_id: str) -> Optional[Dict[str, Any]]:
        return await self._store.get_session_brief(session_id)

    async def list_session_actions(
        self,
        session_id: str,
        *,
        limit: int = 100,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return await self._store.list_actions(session_id, limit=limit, status=status)

    async def get_patch_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        return await self._store.get_patch_proposal(proposal_id)

    async def list_patch_proposals(self, session_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        return await self._store.list_patch_proposals(session_id, limit=limit)

    async def preflight_patch_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Read-only policy and runtime validation for one pending bot proposal.

        This intentionally validates an enabled in-memory copy. Proposals are kept
        disabled until a separate operator-controlled approval flow applies them.
        """
        proposal = await self._store.get_patch_proposal(proposal_id)
        if proposal is None:
            return {"status": "error", "detail": "proposal_not_found"}
        if str(proposal.get("session_id") or "").strip() != str(session_id or "").strip():
            return {"status": "error", "detail": "proposal_session_mismatch"}
        if str(proposal.get("status") or "").strip().lower() != "proposed":
            return {"status": "error", "detail": "proposal_not_pending", "proposal": proposal}

        after_state = proposal.get("after_state") if isinstance(proposal.get("after_state"), dict) else {}
        if str(after_state.get("proposal_kind") or "").strip() != "bot_configuration":
            return {"status": "blocked", "detail": "proposal_preflight_not_supported", "proposal": proposal}

        preflight: Dict[str, Any] = {
            "version": 1,
            "checked_at": _now(),
            "proposal_kind": "bot_configuration",
            "schema_valid": False,
            "policy_errors": [],
            "safety_error": None,
            "readiness": None,
            "ready_for_operator_review": False,
            "manual_activation_required": True,
            "valid_for_seconds": _PROPOSAL_PREFLIGHT_TTL_SECONDS,
        }
        bot_payload = after_state.get("bot") if isinstance(after_state.get("bot"), dict) else {}
        try:
            bot = Bot.model_validate(bot_payload)
        except Exception:
            preflight["policy_errors"] = ["invalid_proposed_bot"]
            return await self._record_proposal_preflight(
                session_id,
                proposal,
                after_state,
                preflight,
                operator_id=operator_id,
            )

        preflight["schema_valid"] = True
        preflight["bot_id"] = str(bot.id or "")
        policy_errors = validate_bot_configuration(bot)
        preflight["policy_errors"] = policy_errors
        safety_error = self._proposal_bot_safety_error(bot)
        preflight["safety_error"] = safety_error or None
        if self._worker_registry is None or self._connection_resolver is None:
            preflight["policy_errors"] = [*policy_errors, "runtime_readiness_unavailable"]
            return await self._record_proposal_preflight(
                session_id,
                proposal,
                after_state,
                preflight,
                operator_id=operator_id,
            )

        staged_bot = bot.model_copy(update={"enabled": True})
        try:
            readiness = await assess_bot_instance_readiness(
                staged_bot,
                worker_registry=self._worker_registry,
                connection_resolver=self._connection_resolver,
                worker_probe_store=self._worker_probe_store,
                key_vault=self._key_vault,
            )
        except Exception:
            readiness = {
                "bot_id": str(bot.id or ""),
                "ready": False,
                "summary": {"checks": 0, "failed": 1, "blocking": 1, "warnings": 0, "viable_backends": 0},
                "checks": [{"component": "runtime", "status": "failed", "message": "Runtime readiness check unavailable."}],
            }
        preflight["readiness"] = readiness
        preflight["ready_for_operator_review"] = bool(
            not policy_errors and not safety_error and readiness.get("ready")
        )
        return await self._record_proposal_preflight(
            session_id,
            proposal,
            after_state,
            preflight,
            operator_id=operator_id,
        )

    async def _record_proposal_preflight(
        self,
        session_id: str,
        proposal: Dict[str, Any],
        after_state: Dict[str, Any],
        preflight: Dict[str, Any],
        *,
        operator_id: Optional[str],
    ) -> Dict[str, Any]:
        updated_after_state = dict(after_state)
        updated_after_state["preflight"] = preflight
        updated = await self._store.update_patch_proposal_after_state(
            str(proposal.get("id") or ""),
            updated_after_state,
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "bot_configuration_proposal_preflight",
                "proposal_id": str(proposal.get("id") or ""),
                "bot_id": str(preflight.get("bot_id") or ""),
                "ready_for_operator_review": bool(preflight.get("ready_for_operator_review")),
                "operator_id": str(operator_id or "").strip() or None,
            },
        )
        return {
            "status": "ready" if bool(preflight.get("ready_for_operator_review")) else "blocked",
            "proposal": updated or proposal,
            "preflight": preflight,
        }

    async def approve_patch_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        operator_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        proposal = await self._store.get_patch_proposal(proposal_id)
        if proposal is None:
            return {"status": "error", "detail": "proposal_not_found"}
        if str(proposal.get("session_id") or "").strip() != str(session_id or "").strip():
            return {"status": "error", "detail": "proposal_session_mismatch"}
        if str(proposal.get("status") or "").strip().lower() != "proposed":
            return {"status": "error", "detail": "proposal_not_pending", "proposal": proposal}

        after_state = proposal.get("after_state") if isinstance(proposal.get("after_state"), dict) else {}
        if str(after_state.get("proposal_kind") or "").strip() == "bot_system_prompt_refinement":
            updated = await self._store.update_patch_proposal_status(proposal_id, "approved")
            await self._store.append_event(
                session_id,
                "action_trace",
                {
                    "action": "bot_system_prompt_refinement_approved",
                    "proposal_id": proposal_id,
                    "target_config": proposal.get("target_config"),
                    "operator_id": str(operator_id or "").strip() or None,
                    "notes": str(notes or "").strip() or None,
                },
            )
            await self._store.append_message(
                session_id,
                role="assistant",
                content=(
                    "Prompt-refinement proposal approved as a review checkpoint. The live bot remains unchanged; "
                    "apply the reviewed prompt change through the Bot editor before resuming an autonomous iteration."
                ),
                metadata={
                    "source": "proposal_approval",
                    "proposal_id": proposal_id,
                    "requires_direct_operator_edit": True,
                },
            )
            return {
                "status": "approved",
                "detail": "approved_direct_operator_edit_required",
                "proposal": updated,
            }
        if str(after_state.get("proposal_kind") or "").strip() == "bot_configuration":
            session = await self._store.get_session(session_id)
            if session is None:
                return {"status": "error", "detail": "session_not_found"}
            preflight = after_state.get("preflight") if isinstance(after_state.get("preflight"), dict) else {}
            if not bool(preflight.get("ready_for_operator_review")):
                return {"status": "blocked", "detail": "proposal_preflight_required", "proposal": proposal}
            if not _proposal_preflight_is_fresh(preflight):
                return {"status": "blocked", "detail": "proposal_preflight_stale", "proposal": proposal}
            bot_payload = after_state.get("bot") if isinstance(after_state.get("bot"), dict) else {}
            try:
                bot = Bot.model_validate(bot_payload)
            except Exception as exc:
                return {"status": "blocked", "detail": f"invalid_proposed_bot:{exc}", "proposal": proposal}
            safety_error = self._proposal_bot_safety_error(bot)
            if safety_error:
                return {"status": "blocked", "detail": safety_error, "proposal": proposal}
            if self._bot_registry is None:
                return {"status": "blocked", "detail": "bot_registry_unavailable", "proposal": proposal}
            try:
                existing_bot = await self._bot_registry.get(str(bot.id or "").strip())
            except BotNotFoundError:
                existing_bot = None
            except Exception:
                existing_bot = None
            if existing_bot is not None and bool(getattr(existing_bot, "enabled", False)):
                return {
                    "status": "blocked",
                    "detail": "active_bot_update_requires_direct_operator_edit",
                    "proposal": proposal,
                }

            mode = str(session.get("mode") or "").strip().lower()
            result = await self._upsert_bot_payload(
                bot.model_dump(mode="json", exclude_none=True),
                session_id=session_id,
                session=session,
                allow_scope_expansion=mode in {"bot_creator", "pipeline_creator", "pipeline_tuner"},
            )
            if not bool(result.get("ok")):
                await self._store.append_event(
                    session_id,
                    "action_trace",
                    {
                        "action": "bot_configuration_proposal_approval_blocked",
                        "proposal_id": proposal_id,
                        "detail": result.get("detail"),
                        "operator_id": str(operator_id or "").strip() or None,
                    },
                )
                return {"status": "blocked", "detail": result.get("detail"), "proposal": proposal, "result": result}

            updated = await self._store.update_patch_proposal_status(proposal_id, "applied")
            action_id = str(proposal.get("action_id") or "").strip()
            if action_id:
                await self._store.update_action(
                    action_id,
                    status="completed",
                    output_snapshot_hash=self._compute_state_hash(result),
                    state_delta_summary=f"Approved bot configuration applied to {result.get('bot_id') or bot.id}.",
                )
            await self._store.append_event(session_id, "action_trace", {
                "action": "bot_configuration_proposal_applied",
                "proposal_id": proposal_id,
                "bot_id": result.get("bot_id") or bot.id,
                "operator_id": str(operator_id or "").strip() or None,
                "notes": str(notes or "").strip() or None,
            })
            return {"status": "applied", "proposal": updated, "result": result}

        updated = await self._store.update_patch_proposal_status(proposal_id, "approved")
        await self._store.append_event(session_id, "action_trace", {
            "action": "patch_proposal_approved",
            "proposal_id": proposal_id,
            "target_config": updated.get("target_config"),
            "operator_id": str(operator_id or "").strip() or None,
            "notes": str(notes or "").strip() or None,
        })
        return {"status": "approved", "proposal": updated}

    async def reject_patch_proposal(
        self,
        session_id: str,
        proposal_id: str,
        *,
        operator_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        proposal = await self._store.get_patch_proposal(proposal_id)
        if proposal is None:
            return {"status": "error", "detail": "proposal_not_found"}
        if str(proposal.get("session_id") or "").strip() != str(session_id or "").strip():
            return {"status": "error", "detail": "proposal_session_mismatch"}
        if str(proposal.get("status") or "").strip().lower() != "proposed":
            return {"status": "error", "detail": "proposal_not_pending", "proposal": proposal}
        updated = await self._store.update_patch_proposal_status(proposal_id, "rejected")
        action_id = str(proposal.get("action_id") or "").strip()
        if action_id:
            await self._store.update_action(
                action_id,
                status="rejected",
                state_delta_summary="Operator rejected the proposal.",
            )
        await self._store.append_event(session_id, "action_trace", {
            "action": "patch_proposal_rejected",
            "proposal_id": proposal_id,
            "target_config": proposal.get("target_config"),
            "operator_id": str(operator_id or "").strip() or None,
            "notes": str(notes or "").strip() or None,
        })
        return {"status": "rejected", "proposal": updated}

    async def halt_session(
        self,
        session_id: str,
        *,
        reason: str = "operator_halt",
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        updated = await self._store.update_session(
            session_id,
            status="stopped",
            metadata={
                "stopped_reason": reason,
                "stopped_at": _now(),
                "stopped_by": str(operator_id or "").strip() or None,
            },
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "session_stopped_by_operator",
                "reason": reason,
                "stopped_at": _now(),
                "operator_id": str(operator_id or "").strip() or None,
            },
        )
        await self._store.append_message(
            session_id,
            role="assistant",
            content=f"Session stopped by operator (reason: {reason}).",
            metadata={"source": "operator_stop", "reason": reason, "operator_id": str(operator_id or "").strip() or None},
        )
        return updated or {"status": "stopped", "reason": reason}

    async def refresh_session_brief(self, session_id: str, *, operator_id: Optional[str] = None) -> Dict[str, Any]:
        session = await self._store.get_session(session_id)
        if session is None:
            return {"status": "error", "detail": "session_not_found"}
        messages = await self._store.list_messages(session_id, limit=400)
        latest_operator_content = ""
        for row in reversed(messages):
            if str(row.get("role") or "").strip().lower() != "operator":
                continue
            latest_operator_content = str(row.get("content") or "").strip()
            if latest_operator_content:
                break
        brief = await self._synthesize_session_brief(
            session_id,
            session=session,
            message_content=latest_operator_content,
        )
        await self._store.append_event(
            session_id,
            "action_trace",
            {
                "action": "session_brief_refreshed",
                "operator_id": str(operator_id or "").strip() or None,
                "used_operator_message_preview": latest_operator_content[:240],
            },
        )
        return {"status": "ok", "brief": brief}
