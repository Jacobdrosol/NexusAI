from __future__ import annotations

import asyncio
import json
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
    return {
        "status": "passed" if suite_passed else "failed",
        "score": round(suite_score, 4),
        "suite_pass_threshold": suite_threshold,
        "tests": evaluated,
        "task_count": len(tasks),
        "graph_node_count": len(_node_ids(graph)),
        "evaluated_at": _now(),
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
        phase = "observe"
        active_action = "monitor_pipeline"
        if not context.get("orchestration_id"):
            active_action = "await_orchestration_attachment"
            phase = "observe"
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
                try:
                    launch_orchestration_id = str(uuid.uuid4())
                    payload = await self._pipeline_launch_payload(pipeline_bot_id=pipeline_bot_id, goal=goal)
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
                    await self._store.update_session(
                        session_id,
                        orchestration_id=launch_orchestration_id,
                        metadata={
                            "autonomous_launch_state": "launched",
                            "autonomous_launched_orchestration_id": launch_orchestration_id,
                            "autonomous_launched_task_id": str(getattr(created, "id", "") or ""),
                        },
                    )
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {
                            "action": "autonomous_orchestration_launched",
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
                            f"Autonomous tuner launched orchestration `{launch_orchestration_id}` for pipeline "
                            f"`{pipeline_name or pipeline_bot_id}`. I will monitor it and run quality suite evaluation when terminal."
                        ),
                        metadata={"source": "autonomous_tuner"},
                    )
                except Exception as exc:
                    await self._store.update_session(
                        session_id,
                        metadata={"autonomous_launch_state": "failed", "autonomous_launch_error": str(exc)},
                    )
                    await self._store.append_event(
                        session_id,
                        "action_trace",
                        {"action": "autonomous_orchestration_launch_failed", "detail": str(exc)},
                    )
            return

        tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
        graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
        if not orchestration_id or not tasks:
            return

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
        await self._store.update_session(
            session_id,
            metadata={
                "autonomous_last_eval_signature": eval_signature,
                "autonomous_last_eval_status": str(evaluation.get("status") or "failed"),
                "autonomous_last_eval_score": float(evaluation.get("score") or 0.0),
                "autonomous_last_eval_run_id": str((final_run or {}).get("id") or ""),
                "autonomous_last_eval_at": _now(),
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
