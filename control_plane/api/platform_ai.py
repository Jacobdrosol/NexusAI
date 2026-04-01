from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/v1/platform-ai", tags=["platform-ai"])

_QUALITY_FIELDS = {"summary", "quality_gates", "acceptance_criteria", "tests", "artifacts", "warnings", "errors"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _env_enabled(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _owner_allowlist() -> set[str]:
    raw = str(os.environ.get("NEXUS_PLATFORM_AI_OWNER_ALLOWLIST", "") or "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def _is_privileged_allowed(operator_id: str) -> bool:
    if not _env_enabled("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED"):
        return False
    allowlist = _owner_allowlist()
    if not allowlist:
        return False
    return str(operator_id or "").strip().lower() in allowlist


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


def _select_tasks(tasks: List[Dict[str, Any]], targets: List[str]) -> List[Dict[str, Any]]:
    if not targets:
        return list(tasks)
    wanted = {str(item).strip() for item in targets if str(item).strip()}
    selected: List[Dict[str, Any]] = []
    for task in tasks:
        if _task_identities(task).intersection(wanted):
            selected.append(task)
    return selected


def _assertion(kind: str, passed: bool, score: float, detail: str) -> Dict[str, Any]:
    return {
        "kind": kind,
        "passed": bool(passed),
        "score": max(0.0, min(1.0, float(score))),
        "detail": str(detail or ""),
    }


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


async def _resolve_context(
    request: Request,
    *,
    assignment_id: Optional[str],
    run_id: Optional[str],
    orchestration_id: Optional[str],
) -> Dict[str, Any]:
    run_store = request.app.state.orchestration_run_store
    assignment_service = request.app.state.assignment_service
    task_manager = request.app.state.task_manager
    resolved_assignment_id = str(assignment_id or "").strip() or None
    resolved_run_id = str(run_id or "").strip() or None
    resolved_orch_id = str(orchestration_id or "").strip() or None

    run: Optional[Dict[str, Any]] = None
    if resolved_run_id:
        run = await run_store.get_run(resolved_run_id)
    elif resolved_orch_id:
        run = await run_store.get_run_by_orchestration(resolved_orch_id)
    elif resolved_assignment_id:
        run = await run_store.get_latest_run_for_assignment(resolved_assignment_id)
    if run is not None:
        resolved_assignment_id = str(run.get("assignment_id") or "") or resolved_assignment_id
        resolved_run_id = str(run.get("id") or "") or resolved_run_id
        resolved_orch_id = str(run.get("orchestration_id") or "") or resolved_orch_id

    graph: Dict[str, Any] = {"nodes": [], "edges": []}
    tasks: List[Dict[str, Any]] = []
    if resolved_run_id or resolved_orch_id:
        try:
            graph_resp = await assignment_service.get_graph(run_id=resolved_run_id, orchestration_id=resolved_orch_id)
        except Exception:
            graph_resp = {}
        if isinstance(graph_resp.get("graph"), dict):
            graph = graph_resp["graph"]
        raw_tasks = graph_resp.get("tasks") if isinstance(graph_resp.get("tasks"), list) else []
        tasks = [task for task in raw_tasks if isinstance(task, dict)]
    if not tasks and resolved_orch_id:
        listed = await task_manager.list_tasks(orchestration_id=resolved_orch_id, limit=1000)
        tasks = [task.model_dump() for task in listed]
    return {
        "assignment_id": resolved_assignment_id,
        "run_id": resolved_run_id,
        "orchestration_id": resolved_orch_id,
        "graph": graph,
        "tasks": tasks,
    }


def _build_suite_definition(
    *,
    suite_name: str,
    graph: Dict[str, Any],
    include_default_tests: bool,
    quality_expectations: List["QualityExpectation"],
    suite_pass_threshold: float,
) -> Dict[str, Any]:
    tests: List[Dict[str, Any]] = []
    if include_default_tests:
        tests.extend(
            [
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
            ]
        )
    for idx, expectation in enumerate(quality_expectations):
        tests.append(
            {
                "id": f"expectation-{idx + 1}",
                "name": expectation.name,
                "type": "expectation",
                "weight": 0.3,
                "pass_threshold": expectation.min_score,
                "assertions": [
                    {"kind": "min_avg_quality", "value": expectation.min_score, "target_nodes": expectation.target_nodes},
                    {"kind": "required_keywords", "keywords": expectation.required_keywords, "target_nodes": expectation.target_nodes},
                    {"kind": "required_fields", "fields": expectation.required_fields, "target_nodes": expectation.target_nodes},
                ],
            }
        )
    return {
        "name": suite_name,
        "version": "v1",
        "generated_at": _now(),
        "suite_pass_threshold": max(0.0, min(1.0, float(suite_pass_threshold))),
        "graph_nodes": _node_ids(graph),
        "tests": tests,
    }


async def _wait_for_orchestration_terminal(
    request: Request,
    *,
    orchestration_id: str,
    poll_interval_seconds: float,
    max_wait_seconds: float,
) -> List[Dict[str, Any]]:
    task_manager = request.app.state.task_manager
    terminal = {"completed", "failed", "cancelled", "retried"}
    deadline = time.monotonic() + max(0.0, float(max_wait_seconds))
    while True:
        listed = await task_manager.list_tasks(orchestration_id=orchestration_id, limit=1000)
        tasks = [task.model_dump() for task in listed]
        if tasks and all(str(task.get("status") or "").strip().lower() in terminal for task in tasks):
            return tasks
        if time.monotonic() >= deadline:
            return tasks
        await asyncio.sleep(max(0.1, float(poll_interval_seconds)))


class CreatePlatformAISessionRequest(BaseModel):
    mode: str
    assignment_id: Optional[str] = None
    run_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    operator_id: Optional[str] = None
    privileged: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ControlPlatformAISessionRequest(BaseModel):
    action: str
    operator_id: Optional[str] = None
    assignment_id: Optional[str] = None
    node_id: Optional[str] = None
    run_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    node_overrides: Dict[str, Any] = Field(default_factory=dict)
    payload: Optional[Any] = None
    context_items: list[str] = Field(default_factory=list)
    privileged_action: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QualityExpectation(BaseModel):
    name: str
    target_nodes: List[str] = Field(default_factory=list)
    required_keywords: List[str] = Field(default_factory=list)
    required_fields: List[str] = Field(default_factory=list)
    min_score: float = 0.7


class DesignQualitySuiteRequest(BaseModel):
    name: Optional[str] = None
    assignment_id: Optional[str] = None
    run_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    include_default_tests: bool = True
    suite_pass_threshold: float = 0.8
    quality_expectations: List[QualityExpectation] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunQualitySuiteRequest(BaseModel):
    assignment_id: Optional[str] = None
    run_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    operator_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    wait_for_terminal: bool = True
    poll_interval_seconds: float = 1.0
    max_wait_seconds: float = 900.0


@router.post("/sessions")
async def create_session(request: Request, body: CreatePlatformAISessionRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    operator_id = str(body.operator_id or request.headers.get("X-Nexus-Operator-ID") or "").strip()
    privileged = bool(body.privileged)
    if privileged and not _is_privileged_allowed(operator_id):
        raise HTTPException(status_code=403, detail="privileged Platform AI mode is disabled or operator is not allowlisted")
    return await store.create_session(
        mode=body.mode,
        assignment_id=body.assignment_id,
        run_id=body.run_id,
        orchestration_id=body.orchestration_id,
        operator_id=operator_id or None,
        privileged=privileged,
        metadata=body.metadata,
    )


@router.get("/sessions")
async def list_sessions(
    request: Request,
    assignment_id: Optional[str] = None,
    orchestration_id: Optional[str] = None,
    mode: Optional[str] = None,
    limit: int = 100,
) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    sessions = await store.list_sessions(
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        mode=mode,
        limit=limit,
    )
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return session


@router.get("/sessions/{session_id}/events")
async def list_session_events(session_id: str, request: Request, limit: int = 200) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session_id, "events": await store.list_events(session_id, limit=limit)}


@router.post("/sessions/{session_id}/control")
async def control_session(session_id: str, request: Request, body: ControlPlatformAISessionRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    assignment_service = request.app.state.assignment_service
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    operator_id = str(body.operator_id or request.headers.get("X-Nexus-Operator-ID") or session.get("operator_id") or "").strip()
    action = str(body.action or "").strip().lower()
    privileged_requested = bool(body.privileged_action) or action in {"code_edit", "deploy", "hotfix"}
    if privileged_requested and not _is_privileged_allowed(operator_id):
        raise HTTPException(status_code=403, detail="privileged control action denied")

    next_status: Optional[str] = None
    result: Dict[str, Any] = {}
    if action in {"pause", "hold"}:
        next_status = "paused"
        result = {"status": "paused"}
    elif action in {"resume", "continue"}:
        next_status = "active"
        result = {"status": "active"}
    elif action in {"stop", "cancel"}:
        next_status = "stopped"
        result = {"status": "stopped"}
    elif action == "follow":
        result = {"status": session.get("status"), "follow": "attached"}
    elif action == "splice":
        run_id = str(body.run_id or session.get("run_id") or "").strip()
        node_id = str(body.node_id or "").strip()
        if not run_id or not node_id:
            raise HTTPException(status_code=400, detail="splice requires run_id and node_id")
        result = await assignment_service.splice_and_rerun(
            run_id=run_id,
            from_node_id=node_id,
            override_patch=body.node_overrides,
            context_items=body.context_items,
        )
    elif action == "rerun_node":
        orch_id = str(body.orchestration_id or session.get("orchestration_id") or "").strip()
        node_id = str(body.node_id or "").strip()
        if not orch_id or not node_id:
            raise HTTPException(status_code=400, detail="rerun_node requires orchestration_id and node_id")
        result = await assignment_service.rerun_node(orchestration_id=orch_id, node_id=node_id, payload_override=body.payload)
    else:
        raise HTTPException(status_code=400, detail=f"unsupported control action: {action}")

    if next_status is not None:
        session = await store.update_session(session_id, status=next_status, metadata=body.metadata) or session
    elif body.metadata:
        session = await store.update_session(session_id, metadata=body.metadata) or session
    event = await store.append_event(
        session_id,
        "action_trace",
        {"action": action, "operator_id": operator_id, "privileged": privileged_requested, "result": result, "metadata": body.metadata},
    )
    return {"session": session, "result": result, "event": event}


@router.post("/sessions/{session_id}/test-suites/design")
async def design_quality_test_suite(session_id: str, request: Request, body: DesignQualitySuiteRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    context = await _resolve_context(
        request,
        assignment_id=body.assignment_id or session.get("assignment_id"),
        run_id=body.run_id or session.get("run_id"),
        orchestration_id=body.orchestration_id or session.get("orchestration_id"),
    )
    graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
    suite_name = str(body.name or "").strip() or "Platform AI Quality Suite"
    suite_def = _build_suite_definition(
        suite_name=suite_name,
        graph=graph,
        include_default_tests=bool(body.include_default_tests),
        quality_expectations=body.quality_expectations,
        suite_pass_threshold=float(body.suite_pass_threshold),
    )
    suite = await store.create_test_suite(
        session_id=session_id,
        name=suite_name,
        suite=suite_def,
        assignment_id=context.get("assignment_id"),
        run_id=context.get("run_id"),
        orchestration_id=context.get("orchestration_id"),
        metadata=body.metadata,
    )
    session = await store.update_session(
        session_id,
        assignment_id=context.get("assignment_id"),
        run_id=context.get("run_id"),
        orchestration_id=context.get("orchestration_id"),
        metadata={"last_quality_suite_id": suite.get("id")},
    ) or session
    event = await store.append_event(
        session_id,
        "action_trace",
        {"action": "design_quality_tests", "suite_id": suite.get("id"), "test_count": len(suite_def.get("tests") or [])},
    )
    return {"session": session, "suite": suite, "event": event}


@router.get("/sessions/{session_id}/test-suites")
async def list_quality_test_suites(session_id: str, request: Request, limit: int = 100) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session_id, "suites": await store.list_test_suites(session_id=session_id, limit=limit)}


@router.get("/test-suites")
async def list_quality_test_suites_global(
    request: Request,
    session_id: Optional[str] = None,
    assignment_id: Optional[str] = None,
    orchestration_id: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    suites = await store.list_test_suites(
        session_id=session_id,
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        limit=limit,
    )
    return {"suites": suites}


@router.get("/test-suites/{suite_id}")
async def get_quality_test_suite(suite_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    suite = await store.get_test_suite(suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="test suite not found")
    return suite


@router.post("/test-suites/{suite_id}/run")
async def run_quality_test_suite(suite_id: str, request: Request, body: RunQualitySuiteRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    suite = await store.get_test_suite(suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="test suite not found")
    effective_orchestration_id = str(
        body.orchestration_id
        or suite.get("orchestration_id")
        or ""
    ).strip()
    if bool(body.wait_for_terminal) and effective_orchestration_id:
        await _wait_for_orchestration_terminal(
            request,
            orchestration_id=effective_orchestration_id,
            poll_interval_seconds=float(body.poll_interval_seconds or 1.0),
            max_wait_seconds=float(body.max_wait_seconds or 900.0),
        )
    context = await _resolve_context(
        request,
        assignment_id=body.assignment_id or suite.get("assignment_id"),
        run_id=body.run_id or suite.get("run_id"),
        orchestration_id=effective_orchestration_id or suite.get("orchestration_id"),
    )
    graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
    tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
    if not tasks:
        raise HTTPException(status_code=400, detail="no tasks found for this suite run context")
    run_record = await store.create_test_run(
        suite_id=suite_id,
        session_id=suite.get("session_id"),
        assignment_id=context.get("assignment_id"),
        run_id=context.get("run_id"),
        orchestration_id=context.get("orchestration_id"),
        status="running",
        score=0.0,
        result={"started_at": _now()},
        completed=False,
    )
    suite_payload = suite.get("suite") if isinstance(suite.get("suite"), dict) else {}
    evaluation = _evaluate_suite(suite_payload, tasks, graph)
    evaluation["context"] = {
        "assignment_id": context.get("assignment_id"),
        "run_id": context.get("run_id"),
        "orchestration_id": context.get("orchestration_id"),
    }
    final_run = await store.complete_test_run(
        run_record["id"],
        status=str(evaluation.get("status") or "failed"),
        score=float(evaluation.get("score") or 0.0),
        result=evaluation,
    )
    assert final_run is not None
    event = None
    if str(suite.get("session_id") or "").strip():
        event = await store.append_event(
            str(suite.get("session_id")),
            "action_trace",
            {
                "action": "run_quality_tests",
                "suite_id": suite_id,
                "test_run_id": final_run.get("id"),
                "status": final_run.get("status"),
                "score": final_run.get("score"),
                "operator_id": str(body.operator_id or request.headers.get("X-Nexus-Operator-ID") or "").strip() or None,
                "metadata": body.metadata,
            },
        )
    return {"suite": suite, "test_run": final_run, "event": event}


@router.get("/test-suites/{suite_id}/runs")
async def list_quality_test_runs(suite_id: str, request: Request, limit: int = 100) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    suite = await store.get_test_suite(suite_id)
    if suite is None:
        raise HTTPException(status_code=404, detail="test suite not found")
    return {"suite_id": suite_id, "runs": await store.list_test_runs(suite_id, limit=limit)}


@router.get("/test-runs/{run_id}")
async def get_quality_test_run(run_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    run = await store.get_test_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="test run not found")
    return run
