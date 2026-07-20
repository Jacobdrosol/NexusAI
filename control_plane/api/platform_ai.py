from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shared.exceptions import ProjectNotFoundError
from shared.models import CatalogModel, TaskMetadata


router = APIRouter(prefix="/v1/platform-ai", tags=["platform-ai"])
_PIPELINE_SESSION_CLAIM_LOCK = asyncio.Lock()

_QUALITY_FIELDS = {"summary", "quality_gates", "acceptance_criteria", "tests", "artifacts", "warnings", "errors"}
_CANONICAL_MODES = {"bot_tuner", "bot_creator", "pipeline_tuner", "pipeline_creator"}
_CANONICAL_STATUSES = {"ready", "running", "stopped"}
_TERMINAL_AUTONOMOUS_STATES = {
    "converged",
    "max_iterations_reached",
    "stopped",
    "refinement_launch_failed",
    "launch_failed",
}
_CLI_RUNTIME_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")


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


def _require_feature_flag(flag: str, *, action: str) -> None:
    if _env_enabled(flag):
        return
    raise HTTPException(status_code=403, detail=f"{action} is disabled ({flag} not enabled)")


def _normalize_project_id(value: Any) -> str:
    return str(value or "").strip()


def _raise_project_binding_error(*, reason_code: str, project_id: str, message: str) -> None:
    raise HTTPException(
        status_code=409,
        detail={
            "reason_code": reason_code,
            "message": message,
            "validation_errors": [
                {
                    "field_path": "project_id",
                    "message": message,
                    "invalid_value": project_id or None,
                }
            ],
        },
    )


async def _require_known_enabled_project(request: Request, project_id: str) -> None:
    """Keep Platform AI sessions and proposals inside a live project boundary."""
    safe_project_id = _normalize_project_id(project_id)
    if not safe_project_id:
        return
    try:
        project = await request.app.state.project_registry.get(safe_project_id)
    except ProjectNotFoundError:
        _raise_project_binding_error(
            reason_code="platform_ai_project_not_found",
            project_id=safe_project_id,
            message=f"Platform AI project '{safe_project_id}' does not exist.",
        )
    if not bool(project.enabled):
        _raise_project_binding_error(
            reason_code="platform_ai_project_disabled",
            project_id=safe_project_id,
            message=f"Platform AI project '{safe_project_id}' is disabled.",
        )


def _resolve_project_binding(*project_ids: str) -> str:
    """Return one explicit project binding or reject conflicting scope inputs."""
    values = {_normalize_project_id(value) for value in project_ids if _normalize_project_id(value)}
    if len(values) > 1:
        _raise_project_binding_error(
            reason_code="platform_ai_project_scope_mismatch",
            project_id=", ".join(sorted(values)),
            message="Platform AI project inputs must resolve to one project boundary.",
        )
    return next(iter(values), "")


@router.get("/capabilities")
async def get_platform_ai_capabilities() -> Dict[str, Any]:
    """Return non-secret Platform AI modes and operator-controlled safety flags."""
    cloud_context_policy = str(
        os.environ.get("NEXUSAI_CLOUD_CONTEXT_POLICY", "") or ""
    ).strip().lower()
    return {
        "session_modes": sorted(_CANONICAL_MODES),
        "cloud_context_policy": cloud_context_policy or "unset",
        "actions": {
            "privileged_mode": _env_enabled("NEXUS_PLATFORM_AI_PRIVILEGED_ENABLED"),
            "configuration_mutations": _env_enabled("NEXUS_PLATFORM_AI_CONFIGURATION_MUTATIONS_ENABLED"),
            "autonomous_pipeline_runs": _env_enabled("NEXUS_PLATFORM_AI_AUTONOMOUS_PIPELINES_ENABLED"),
            "project_repo_edits": _env_enabled("NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED"),
            "external_repo_edits": _env_enabled("NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_ENABLED"),
            "repository_edits": _env_enabled("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED"),
            "deployments": _env_enabled("NEXUS_PLATFORM_AI_DEPLOY_ENABLED"),
        },
    }


def _instruction_from_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return str(payload).strip()
    if isinstance(payload, dict):
        for key in ("instruction", "prompt", "message", "content"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return ""


def _bot_assignment_capabilities(bot: Any) -> Dict[str, Any]:
    capabilities = getattr(bot, "assignment_capabilities", None)
    if capabilities is None:
        return {}
    if isinstance(capabilities, dict):
        return dict(capabilities)
    if hasattr(capabilities, "model_dump"):
        return dict(capabilities.model_dump())
    return {}


def _bot_routing_rules(bot: Any) -> Dict[str, Any]:
    routing = getattr(bot, "routing_rules", None)
    return dict(routing) if isinstance(routing, dict) else {}


def _bot_is_pipeline_entry(bot: Any) -> bool:
    capabilities = _bot_assignment_capabilities(bot)
    if bool(capabilities.get("is_pipeline_entry")) or bool(capabilities.get("pipeline")) or bool(capabilities.get("is_project_manager")):
        return True
    launch_profile = _bot_routing_rules(bot).get("launch_profile")
    return isinstance(launch_profile, dict) and bool(launch_profile.get("is_pipeline"))


def _pipeline_name_for_bot(bot: Any) -> str:
    capabilities = _bot_assignment_capabilities(bot)
    routing = _bot_routing_rules(bot)
    launch_profile = routing.get("launch_profile") if isinstance(routing.get("launch_profile"), dict) else {}
    return str(
        capabilities.get("pipeline_name")
        or launch_profile.get("pipeline_name")
        or launch_profile.get("label")
        or getattr(bot, "name", None)
        or getattr(bot, "id", "")
    ).strip() or str(getattr(bot, "id", "pipeline")).strip()


def _pipeline_entry_payload(bot: Any) -> Dict[str, Any]:
    capabilities = _bot_assignment_capabilities(bot)
    routing = _bot_routing_rules(bot)
    launch_profile = routing.get("launch_profile") if isinstance(routing.get("launch_profile"), dict) else {}
    testing = routing.get("platform_ai_testing") if isinstance(routing.get("platform_ai_testing"), dict) else {}
    return {
        "pipeline_bot_id": str(getattr(bot, "id", "") or "").strip(),
        "name": _pipeline_name_for_bot(bot),
        "bot_name": str(getattr(bot, "name", "") or "").strip(),
        "enabled": bool(getattr(bot, "enabled", True)),
        "has_launch_profile": isinstance(launch_profile, dict) and bool(launch_profile),
        "pipeline": bool(capabilities.get("pipeline") or capabilities.get("is_pipeline_entry")),
        "pipeline_name": str(capabilities.get("pipeline_name") or "").strip() or None,
        "default_suite_id": str(testing.get("default_suite_id") or "").strip() or None,
    }


def _pipeline_tuner_reset_metadata(
    session: Dict[str, Any],
    *,
    for_new_target: bool,
) -> Dict[str, Any]:
    if str(session.get("mode") or "").strip().lower() != "pipeline_tuner":
        return {}
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    current_status = str(session.get("status") or "").strip().lower()
    autonomous_state = str(metadata.get("autonomous_state") or "").strip().lower()
    should_reset = for_new_target or current_status == "stopped" or autonomous_state in _TERMINAL_AUTONOMOUS_STATES
    if not should_reset:
        return {}
    return {
        "autonomous_iteration": 0,
        "autonomous_state": "observe",
        "autonomous_launch_state": None,
        "autonomous_launch_error": None,
        "autonomous_last_eval_signature": None,
        "autonomous_last_eval_status": None,
        "autonomous_last_eval_score": None,
        "autonomous_last_eval_run_id": None,
        "autonomous_last_eval_at": None,
        "autonomous_last_refine_signature": None,
        "autonomous_last_bot_refine_result": None,
        "autonomous_terminalized_at": None,
        "autonomous_terminal_reason": None,
    }


def _normalize_mode(value: Any) -> str:
    mode = str(value or "").strip().lower()
    if mode in _CANONICAL_MODES:
        return mode
    return ""


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in _CANONICAL_STATUSES:
        return status
    return ""


def _graph_from_bot(bot: Any) -> Dict[str, Any]:
    workflow = getattr(bot, "workflow", None)
    reference_graph = getattr(workflow, "reference_graph", None) if workflow is not None else None
    if reference_graph is None:
        bot_id = str(getattr(bot, "id", "") or "").strip()
        return {
            "nodes": [{"id": bot_id, "bot_id": bot_id, "title": _pipeline_name_for_bot(bot)}],
            "edges": [],
        }
    nodes: List[Dict[str, Any]] = []
    for node in getattr(reference_graph, "nodes", None) or []:
        bot_id = str(getattr(node, "bot_id", "") or "").strip()
        if not bot_id:
            continue
        nodes.append(
            {
                "id": bot_id,
                "bot_id": bot_id,
                "title": str(getattr(node, "title", "") or "").strip(),
                "stage_kind": str(getattr(node, "stage_kind", "") or "").strip() or None,
            }
        )
    edges: List[Dict[str, Any]] = []
    for edge in getattr(reference_graph, "edges", None) or []:
        source = str(getattr(edge, "source_bot_id", "") or "").strip()
        target = str(getattr(edge, "target_bot_id", "") or "").strip()
        if not source or not target:
            continue
        edges.append(
            {
                "source": source,
                "source_bot_id": source,
                "target": target,
                "target_bot_id": target,
                "route_kind": str(getattr(edge, "route_kind", "forward") or "forward"),
                "title": str(getattr(edge, "title", "") or "").strip() or None,
            }
        )
    return {"nodes": nodes, "edges": edges}


def _default_backend_config(
    provider: Optional[str],
    model: Optional[str],
    backend_type: Optional[str],
    credential_ref: Optional[str],
    params: Optional[Dict[str, Any]],
    vertex_project_id: Optional[str],
    vertex_location: Optional[str],
    worker_id: Optional[str],
) -> Dict[str, Any]:
    return {
        "provider": str(provider or "").strip() or None,
        "model": str(model or "").strip() or None,
        "backend_type": str(backend_type or "").strip() or None,
        "credential_ref": str(credential_ref or "").strip() or None,
        "params": dict(params or {}),
        "vertex_project_id": str(vertex_project_id or "").strip() or None,
        "vertex_location": str(vertex_location or "").strip() or None,
        "worker_id": str(worker_id or "").strip() or None,
    }


def _apply_cli_backend_profile(
    config: Dict[str, Any],
    *,
    cli_command_profile: Optional[str],
    cli_runtime_model: Optional[str],
) -> None:
    backend_type = str(config.get("backend_type") or "").strip().lower()
    profile = str(cli_command_profile or "").strip()
    runtime_model = str(cli_runtime_model or "").strip()
    if backend_type != "cli":
        config.pop("command", None)
        config.pop("cli_command_profile", None)
        config.pop("cli_runtime_model", None)
        return
    if str(config.get("provider") or "").strip().lower() != "cli" or str(config.get("model") or "").strip() != "claude":
        raise HTTPException(status_code=400, detail="CLI Platform AI sessions require provider 'cli' and model 'claude'.")
    if not str(config.get("worker_id") or "").strip():
        raise HTTPException(status_code=400, detail="CLI Platform AI sessions require worker_id.")
    if profile != "claude_ollama_json":
        raise HTTPException(status_code=400, detail="CLI Platform AI sessions require the approved Claude via Ollama JSON profile.")
    if not _CLI_RUNTIME_MODEL_PATTERN.fullmatch(runtime_model):
        raise HTTPException(status_code=400, detail="CLI runtime model must be a valid Ollama model name.")
    config["cli_command_profile"] = profile
    config["cli_runtime_model"] = runtime_model
    config["command"] = f"claude -p --model {runtime_model} --output-format json"


def _validate_backend_config(config: Dict[str, Any]) -> None:
    provider = str(config.get("provider") or "").strip().lower()
    if provider != "vertex":
        return
    credential_ref = str(config.get("credential_ref") or "").strip()
    if not credential_ref:
        raise HTTPException(status_code=400, detail="vertex sessions require credential_ref (service-account JSON key reference)")
    project_id = str(config.get("vertex_project_id") or "").strip()
    if project_id:
        if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "vertex_project_id must be a valid Google Cloud project ID "
                    "(6-30 chars, lowercase letters/digits/hyphens; not display name)."
                ),
            )


async def _ensure_backend_model_catalog_entry(request: Request, config: Dict[str, Any]) -> None:
    provider = str(config.get("provider") or "").strip().lower()
    model = str(config.get("model") or "").strip()
    if not provider or not model:
        return
    registry = getattr(request.app.state, "model_registry", None)
    if registry is None:
        return
    try:
        exists = await registry.exists(provider, model)
        if exists:
            return
        model_id = f"platform-ai-session:{provider}:{model}"
        await registry.register(
            CatalogModel(
                id=model_id,
                provider=provider,
                name=model,
                capabilities=["chat"],
                enabled=True,
                notes="Auto-registered from Platform AI session backend configuration.",
            )
        )
    except Exception:
        # Never block session creation/update on catalog maintenance.
        return

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
    start_running: bool = False
    assignment_id: Optional[str] = None
    run_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    seed_run_id: Optional[str] = None
    seed_orchestration_id: Optional[str] = None
    operator_id: Optional[str] = None
    privileged: bool = False
    project_id: Optional[str] = None
    pipeline_bot_id: Optional[str] = None
    pipeline_name: Optional[str] = None
    pipeline_name_seed: Optional[str] = None
    target_bot_id: Optional[str] = None
    bot_name_seed: Optional[str] = None
    reference_bot_ids: List[str] = Field(default_factory=list)
    reference_pipeline_ids: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    provider: Optional[str] = None
    model: Optional[str] = None
    backend_type: Optional[str] = None
    credential_ref: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    vertex_project_id: Optional[str] = None
    vertex_location: Optional[str] = None
    worker_id: Optional[str] = None
    cli_command_profile: Optional[str] = None
    cli_runtime_model: Optional[str] = None


class UpdatePlatformAISessionRequest(BaseModel):
    status: Optional[str] = None
    archived: Optional[bool] = None
    archived_by: Optional[str] = None
    assignment_id: Optional[str] = None
    run_id: Optional[str] = None
    orchestration_id: Optional[str] = None
    project_id: Optional[str] = None
    target_bot_id: Optional[str] = None
    pipeline_bot_id: Optional[str] = None
    pipeline_name: Optional[str] = None
    pipeline_name_seed: Optional[str] = None
    bot_name_seed: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    provider: Optional[str] = None
    model: Optional[str] = None
    backend_type: Optional[str] = None
    credential_ref: Optional[str] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    vertex_project_id: Optional[str] = None
    vertex_location: Optional[str] = None
    worker_id: Optional[str] = None
    cli_command_profile: Optional[str] = None
    cli_runtime_model: Optional[str] = None


class SessionMessageRequest(BaseModel):
    role: str = "operator"
    content: str
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


class DesignPipelineSuiteRequest(BaseModel):
    name: Optional[str] = None
    include_default_tests: bool = True
    suite_pass_threshold: float = 0.8
    quality_expectations: List[QualityExpectation] = Field(default_factory=list)
    set_default: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunPipelineSuiteRequest(BaseModel):
    suite_id: Optional[str] = None
    operator_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    wait_for_terminal: bool = True
    poll_interval_seconds: float = 1.0
    max_wait_seconds: float = 900.0


async def _find_or_create_pipeline_session(request: Request, *, pipeline_bot_id: str) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    sessions = await store.list_sessions(mode="pipeline_tuner", limit=500)
    for session in sessions:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        existing_pipeline_bot_id = str(
            metadata.get("pipeline_bot_id")
            or metadata.get("entry_bot_id")
            or ""
        ).strip()
        if existing_pipeline_bot_id == pipeline_bot_id:
            if str(session.get("status") or "").strip().lower() == "running" and str(metadata.get("source") or "").strip() in {"pipeline_test_modal", "pipeline_suite_api"}:
                ready = await store.update_session(
                    str(session.get("id") or ""),
                    status="ready",
                    metadata={"auto_managed": True},
                )
                if ready is not None:
                    return ready
            return session
    created = await store.create_session(
        mode="pipeline_tuner",
        metadata={"source": "pipeline_suite_api", "pipeline_bot_id": pipeline_bot_id, "auto_managed": True},
    )
    ready = await store.update_session(str(created.get("id") or ""), status="ready")
    return ready or created


def _session_pipeline_bot_id(session: Dict[str, Any]) -> str:
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    return str(metadata.get("pipeline_bot_id") or metadata.get("entry_bot_id") or "").strip()


async def _ensure_pipeline_not_already_claimed(
    request: Request,
    *,
    pipeline_bot_id: str,
) -> None:
    safe_pipeline_bot_id = str(pipeline_bot_id or "").strip()
    if not safe_pipeline_bot_id:
        return
    store = request.app.state.platform_ai_session_store
    sessions = await store.list_sessions(limit=2000, archived="active")
    for existing in sessions:
        if _session_pipeline_bot_id(existing) != safe_pipeline_bot_id:
            continue
        metadata = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        source = str(metadata.get("source") or "").strip()
        if bool(metadata.get("auto_managed")) or source in {"pipeline_test_modal", "pipeline_suite_api"}:
            continue
        existing_id = str(existing.get("id") or "").strip()
        existing_status = str(existing.get("status") or "").strip().lower() or "unknown"
        raise HTTPException(
            status_code=409,
            detail=(
                f"pipeline '{safe_pipeline_bot_id}' is already attached to session {existing_id} "
                f"({existing_status}); archive that session before creating another"
            ),
        )


async def _resolve_seed_binding(
    request: Request,
    *,
    seed_run_id: Optional[str],
    seed_orchestration_id: Optional[str],
    assignment_id: Optional[str],
    run_id: Optional[str],
    orchestration_id: Optional[str],
) -> Dict[str, Any]:
    run_store = getattr(request.app.state, "run_store", None)
    effective_assignment_id = str(assignment_id or "").strip() or None
    effective_run_id = str(run_id or "").strip() or None
    effective_orchestration_id = str(orchestration_id or "").strip() or None
    effective_project_id: Optional[str] = None
    effective_conversation_id: Optional[str] = None
    seed_run = None
    if run_store is not None:
        safe_seed_run = str(seed_run_id or "").strip()
        safe_seed_orch = str(seed_orchestration_id or "").strip()
        try:
            if safe_seed_run:
                seed_run = await run_store.get_run(safe_seed_run)
            elif safe_seed_orch:
                seed_run = await run_store.get_run_by_orchestration(safe_seed_orch)
            elif effective_run_id:
                seed_run = await run_store.get_run(effective_run_id)
            elif effective_orchestration_id:
                seed_run = await run_store.get_run_by_orchestration(effective_orchestration_id)
            elif effective_assignment_id:
                seed_run = await run_store.get_latest_run_for_assignment(effective_assignment_id)
        except Exception:
            seed_run = None
    if isinstance(seed_run, dict):
        effective_assignment_id = str(seed_run.get("assignment_id") or "").strip() or effective_assignment_id
        effective_run_id = str(seed_run.get("id") or "").strip() or effective_run_id
        effective_orchestration_id = str(seed_run.get("orchestration_id") or "").strip() or effective_orchestration_id
        effective_project_id = str(seed_run.get("project_id") or "").strip() or None
        effective_conversation_id = str(seed_run.get("conversation_id") or "").strip() or None
    seed_binding = None
    if isinstance(seed_run, dict):
        seed_meta = seed_run.get("metadata") if isinstance(seed_run.get("metadata"), dict) else {}
        seed_binding = {
            "seed_run_id": str(seed_run.get("id") or "").strip() or None,
            "seed_orchestration_id": str(seed_run.get("orchestration_id") or "").strip() or None,
            "seed_assignment_id": str(seed_run.get("assignment_id") or "").strip() or None,
            "seed_project_id": str(seed_run.get("project_id") or "").strip() or None,
            "seed_conversation_id": str(seed_run.get("conversation_id") or "").strip() or None,
            "instruction": str(seed_run.get("instruction") or "").strip() or None,
            "node_overrides": seed_run.get("node_overrides") if isinstance(seed_run.get("node_overrides"), dict) else {},
            "trigger_source": str(seed_meta.get("source") or "").strip() or None,
        }
    return {
        "assignment_id": effective_assignment_id,
        "run_id": effective_run_id,
        "orchestration_id": effective_orchestration_id,
        "project_id": effective_project_id,
        "conversation_id": effective_conversation_id,
        "seed_binding": seed_binding,
    }


@router.post("/sessions")
async def create_session(request: Request, body: CreatePlatformAISessionRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    operator_id = str(body.operator_id or request.headers.get("X-Nexus-Operator-ID") or "").strip()
    privileged = bool(body.privileged)
    if privileged and not _is_privileged_allowed(operator_id):
        raise HTTPException(status_code=403, detail="privileged Platform AI mode is disabled or operator is not allowlisted")
    mode = _normalize_mode(body.mode)
    if not mode:
        raise HTTPException(status_code=400, detail=f"mode is required and must be one of {sorted(_CANONICAL_MODES)}")

    pipeline_bot_id = str(body.pipeline_bot_id or "").strip()
    pipeline_name = str(body.pipeline_name or body.pipeline_name_seed or "").strip()
    project_id = _normalize_project_id(body.project_id)
    target_bot_id = str(body.target_bot_id or "").strip()
    bot_name_seed = str(body.bot_name_seed or "").strip()
    pipeline_name_seed = str(body.pipeline_name_seed or "").strip()
    backend_cfg = _default_backend_config(
        body.provider,
        body.model,
        body.backend_type,
        body.credential_ref,
        body.params,
        body.vertex_project_id,
        body.vertex_location,
        body.worker_id,
    )
    _apply_cli_backend_profile(
        backend_cfg,
        cli_command_profile=body.cli_command_profile,
        cli_runtime_model=body.cli_runtime_model,
    )
    _validate_backend_config(backend_cfg)
    await _ensure_backend_model_catalog_entry(request, backend_cfg)
    metadata = dict(body.metadata or {})
    metadata_project_id = _normalize_project_id(metadata.get("project_id"))
    if str(metadata.get("pipeline_bot_id") or "").strip() and not pipeline_bot_id:
        pipeline_bot_id = str(metadata.get("pipeline_bot_id") or "").strip()
    if str(metadata.get("pipeline_name") or "").strip() and not pipeline_name:
        pipeline_name = str(metadata.get("pipeline_name") or "").strip()
    if mode == "bot_tuner" and not target_bot_id:
        raise HTTPException(status_code=400, detail="bot_tuner sessions require target_bot_id")
    if mode == "bot_creator" and not bot_name_seed:
        raise HTTPException(status_code=400, detail="bot_creator sessions require bot_name_seed")
    if mode == "pipeline_tuner" and not pipeline_bot_id:
        raise HTTPException(status_code=400, detail="pipeline_tuner sessions require pipeline_bot_id")
    if mode == "pipeline_creator" and not pipeline_name_seed:
        raise HTTPException(status_code=400, detail="pipeline_creator sessions require pipeline_name_seed")

    mutation_policy_by_mode = {
        "bot_tuner": {"create": False, "update": True, "delete": False},
        "bot_creator": {"create": True, "update": True, "delete": False},
        "pipeline_tuner": {"create": True, "update": True, "delete": True},
        "pipeline_creator": {"create": True, "update": True, "delete": True},
    }

    seed_context = await _resolve_seed_binding(
        request,
        seed_run_id=body.seed_run_id,
        seed_orchestration_id=body.seed_orchestration_id,
        assignment_id=body.assignment_id,
        run_id=body.run_id,
        orchestration_id=body.orchestration_id,
    )
    project_id = _resolve_project_binding(
        project_id,
        metadata_project_id,
        _normalize_project_id(seed_context.get("project_id")),
    )
    await _require_known_enabled_project(request, project_id)
    if pipeline_bot_id:
        metadata["pipeline_bot_id"] = pipeline_bot_id
    if pipeline_name:
        metadata["pipeline_name"] = pipeline_name
    if pipeline_name_seed:
        metadata["pipeline_name_seed"] = pipeline_name_seed
    if project_id:
        metadata["project_id"] = project_id
    else:
        metadata.pop("project_id", None)
    if str(seed_context.get("conversation_id") or "").strip():
        metadata["conversation_id"] = str(seed_context.get("conversation_id") or "").strip()
    if target_bot_id:
        metadata["target_bot_id"] = target_bot_id
    if bot_name_seed:
        metadata["bot_name_seed"] = bot_name_seed
    if body.reference_bot_ids:
        metadata["reference_bot_ids"] = [str(item).strip() for item in body.reference_bot_ids if str(item).strip()]
    if body.reference_pipeline_ids:
        metadata["reference_pipeline_ids"] = [str(item).strip() for item in body.reference_pipeline_ids if str(item).strip()]
    metadata["mutation_policy"] = mutation_policy_by_mode.get(mode, {"create": False, "update": True, "delete": False})
    if isinstance(seed_context.get("seed_binding"), dict):
        metadata["seed_binding"] = seed_context.get("seed_binding")
    metadata["backend"] = backend_cfg
    metadata.setdefault("current_phase", "observe")
    initial_status = "running" if bool(body.start_running) else "ready"
    if pipeline_bot_id:
        async with _PIPELINE_SESSION_CLAIM_LOCK:
            await _ensure_pipeline_not_already_claimed(request, pipeline_bot_id=pipeline_bot_id)
            session = await store.create_session(
                mode=mode,
                status=initial_status,
                assignment_id=seed_context.get("assignment_id"),
                run_id=seed_context.get("run_id"),
                orchestration_id=seed_context.get("orchestration_id"),
                operator_id=operator_id or None,
                privileged=privileged,
                metadata=metadata,
            )
    else:
        session = await store.create_session(
            mode=mode,
            status=initial_status,
            assignment_id=seed_context.get("assignment_id"),
            run_id=seed_context.get("run_id"),
            orchestration_id=seed_context.get("orchestration_id"),
            operator_id=operator_id or None,
            privileged=privileged,
            metadata=metadata,
        )
    await store.append_event(
        session["id"],
        "action_trace",
        {"action": "session_backend_configured", "backend": backend_cfg, "seed_binding": seed_context.get("seed_binding")},
    )
    runtime = getattr(request.app.state, "platform_ai_runtime", None)
    if runtime is not None and str(session.get("status") or "").strip().lower() == "running":
        await runtime.ensure_session_loop(session["id"])
    return session


@router.get("/sessions")
async def list_sessions(
    request: Request,
    assignment_id: Optional[str] = None,
    orchestration_id: Optional[str] = None,
    mode: Optional[str] = None,
    archived: str = "active",
    limit: int = 100,
) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    sessions = await store.list_sessions(
        assignment_id=assignment_id,
        orchestration_id=orchestration_id,
        mode=mode,
        archived=archived,
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


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, request: Request) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    bundle = await store.export_session_bundle(session_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="session not found")
    session_obj = bundle.get("session") if isinstance(bundle.get("session"), dict) else {}
    pipeline_bot_id = _session_pipeline_bot_id(session_obj)
    if pipeline_bot_id:
        bot_registry = request.app.state.bot_registry
        try:
            pipeline_bot = await bot_registry.get(pipeline_bot_id)
            if hasattr(pipeline_bot, "model_dump"):
                bundle["pipeline_bot_config"] = pipeline_bot.model_dump()
            else:
                bundle["pipeline_bot_config"] = dict(pipeline_bot)
        except Exception:
            bundle["pipeline_bot_config"] = None
    return bundle


@router.patch("/sessions/{session_id}")
async def patch_session(session_id: str, request: Request, body: UpdatePlatformAISessionRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    metadata = dict(body.metadata or {})
    metadata_project_id = _normalize_project_id(metadata.get("project_id"))
    if body.project_id is not None:
        requested_project_id = _normalize_project_id(body.project_id)
        if metadata_project_id and metadata_project_id != requested_project_id:
            _resolve_project_binding(metadata_project_id, requested_project_id)
        metadata["project_id"] = requested_project_id or None
    elif "project_id" in metadata:
        metadata["project_id"] = metadata_project_id or None
    if body.target_bot_id is not None:
        metadata["target_bot_id"] = str(body.target_bot_id or "").strip() or None
    if body.pipeline_bot_id is not None:
        metadata["pipeline_bot_id"] = str(body.pipeline_bot_id or "").strip() or None
    if body.pipeline_name is not None:
        metadata["pipeline_name"] = str(body.pipeline_name or "").strip() or None
    if body.pipeline_name_seed is not None:
        metadata["pipeline_name_seed"] = str(body.pipeline_name_seed or "").strip() or None
    if body.bot_name_seed is not None:
        metadata["bot_name_seed"] = str(body.bot_name_seed or "").strip() or None
    status = _normalize_status(body.status) or None
    if body.status is not None and not status:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(_CANONICAL_STATUSES)}")
    current_status = str(session.get("status") or "").strip().lower()
    if status == "stopped" or (current_status == "stopped" and status and status != "stopped"):
        raise HTTPException(status_code=400, detail="stopped transitions are only allowed via session control actions")
    session_mode = str(session.get("mode") or "").strip().lower()
    existing_metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    merged_metadata = dict(existing_metadata)
    merged_metadata.update(metadata)
    await _require_known_enabled_project(request, _normalize_project_id(merged_metadata.get("project_id")))
    required_target_bot_id = str(merged_metadata.get("target_bot_id") or "").strip()
    required_pipeline_bot_id = str(merged_metadata.get("pipeline_bot_id") or "").strip()
    required_bot_name_seed = str(merged_metadata.get("bot_name_seed") or "").strip()
    required_pipeline_name_seed = str(merged_metadata.get("pipeline_name_seed") or "").strip()
    if session_mode == "bot_tuner" and not required_target_bot_id:
        raise HTTPException(status_code=400, detail="bot_tuner sessions require target_bot_id")
    if session_mode == "bot_creator" and not required_bot_name_seed:
        raise HTTPException(status_code=400, detail="bot_creator sessions require bot_name_seed")
    if session_mode == "pipeline_tuner" and not required_pipeline_bot_id:
        raise HTTPException(status_code=400, detail="pipeline_tuner sessions require pipeline_bot_id")
    if session_mode == "pipeline_creator" and not required_pipeline_name_seed:
        raise HTTPException(status_code=400, detail="pipeline_creator sessions require pipeline_name_seed")
    wants_backend_update = any(
        [
            body.provider is not None,
            body.model is not None,
            body.backend_type is not None,
            body.credential_ref is not None,
            bool(body.params),
            body.vertex_project_id is not None,
            body.vertex_location is not None,
            body.worker_id is not None,
            body.cli_command_profile is not None,
            body.cli_runtime_model is not None,
        ]
    )
    if wants_backend_update and current_status not in {"ready", "stopped"}:
        raise HTTPException(
            status_code=400,
            detail="session backend/model can only be changed when session status is ready or stopped",
        )
    if wants_backend_update:
        existing_meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        backend_cfg = existing_meta.get("backend") if isinstance(existing_meta.get("backend"), dict) else {}
        backend_cfg = dict(backend_cfg)
        if body.provider is not None:
            backend_cfg["provider"] = str(body.provider or "").strip() or None
        if body.model is not None:
            backend_cfg["model"] = str(body.model or "").strip() or None
        if body.backend_type is not None:
            backend_cfg["backend_type"] = str(body.backend_type or "").strip() or None
        if body.credential_ref is not None:
            backend_cfg["credential_ref"] = str(body.credential_ref or "").strip() or None
        if body.params:
            backend_cfg["params"] = dict(body.params)
        if body.vertex_project_id is not None:
            backend_cfg["vertex_project_id"] = str(body.vertex_project_id or "").strip() or None
        if body.vertex_location is not None:
            backend_cfg["vertex_location"] = str(body.vertex_location or "").strip() or None
        if body.worker_id is not None:
            backend_cfg["worker_id"] = str(body.worker_id or "").strip() or None
        current_profile = str(backend_cfg.get("cli_command_profile") or "").strip() or None
        current_runtime_model = str(backend_cfg.get("cli_runtime_model") or "").strip() or None
        _apply_cli_backend_profile(
            backend_cfg,
            cli_command_profile=body.cli_command_profile if body.cli_command_profile is not None else current_profile,
            cli_runtime_model=body.cli_runtime_model if body.cli_runtime_model is not None else current_runtime_model,
        )
        _validate_backend_config(backend_cfg)
        await _ensure_backend_model_catalog_entry(request, backend_cfg)
        metadata["backend"] = backend_cfg

    updated = await store.update_session(
        session_id,
        status=status,
        archived=body.archived,
        archived_by=(str(body.archived_by or "").strip() or None) if body.archived else None,
        assignment_id=body.assignment_id,
        run_id=body.run_id,
        orchestration_id=body.orchestration_id,
        metadata=metadata,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="session not found")
    runtime = getattr(request.app.state, "platform_ai_runtime", None)
    if runtime is not None and not bool(updated.get("archived")) and str(updated.get("status") or "").strip().lower() == "running":
        await runtime.ensure_session_loop(session_id)
    await store.append_event(
        session_id,
        "action_trace",
        {
            "action": "session_updated",
            "status": updated.get("status"),
            "metadata_keys": sorted((metadata or {}).keys()),
        },
    )
    return updated


@router.get("/sessions/{session_id}/events")
async def list_session_events(session_id: str, request: Request, limit: int = 200) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session_id, "events": await store.list_events(session_id, limit=limit)}


@router.get("/sessions/{session_id}/messages")
async def list_session_messages(session_id: str, request: Request, limit: int = 200) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": session_id, "messages": await store.list_messages(session_id, limit=limit)}


@router.post("/sessions/{session_id}/messages")
async def post_session_message(session_id: str, request: Request, body: SessionMessageRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    runtime = getattr(request.app.state, "platform_ai_runtime", None)
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    if bool(session.get("archived")):
        raise HTTPException(status_code=409, detail="session is archived; restore it before messaging")
    content = str(body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")
    role = str(body.role or "operator").strip().lower() or "operator"
    auto_start_on_message = _env_enabled("NEXUS_PLATFORM_AI_AUTO_START_ON_OPERATOR_MESSAGE")
    if auto_start_on_message and role == "operator" and str(session.get("status") or "").strip().lower() == "ready":
        session = await store.update_session(session_id, status="running") or session
        if runtime is not None:
            await runtime.ensure_session_loop(session_id)
    if runtime is not None:
        message = await runtime.post_message(session_id, role=role, content=content, metadata=body.metadata)
    else:
        message = await store.append_message(session_id, role=role, content=content, metadata=body.metadata)
    return {"session_id": session_id, "message": message}


@router.get("/sessions/{session_id}/messages/stream")
async def stream_session_messages(session_id: str, request: Request, since: Optional[str] = None) -> StreamingResponse:
    store = request.app.state.platform_ai_session_store
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    since_text = str(since or "").strip()

    async def _events() -> Any:
        seen: set[str] = set()
        started = time.monotonic()
        timeout_seconds = 120.0
        while True:
            current = await store.get_session(session_id)
            if current is None:
                yield "event: end\ndata: {\"reason\":\"session_not_found\"}\n\n"
                break
            rows = await store.list_messages(session_id, limit=300)
            for row in rows:
                msg_id = str(row.get("id") or "").strip()
                created_at = str(row.get("created_at") or "").strip()
                role = str(row.get("role") or "").strip().lower()
                if not msg_id or msg_id in seen or role != "assistant":
                    continue
                if since_text and created_at and created_at <= since_text:
                    seen.add(msg_id)
                    continue
                seen.add(msg_id)
                payload = json.dumps({"type": "assistant_message", "message": row}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            if str(current.get("status") or "").strip().lower() != "running":
                yield "event: end\ndata: {\"reason\":\"not_running\"}\n\n"
                break
            if time.monotonic() - started >= timeout_seconds:
                yield "event: end\ndata: {\"reason\":\"timeout\"}\n\n"
                break
            await asyncio.sleep(0.8)

    return StreamingResponse(_events(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/control")
async def control_session(session_id: str, request: Request, body: ControlPlatformAISessionRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    assignment_service = request.app.state.assignment_service
    runtime = getattr(request.app.state, "platform_ai_runtime", None)
    session = await store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    operator_id = str(body.operator_id or request.headers.get("X-Nexus-Operator-ID") or session.get("operator_id") or "").strip()
    action = str(body.action or "").strip().lower()
    privileged_requested = bool(body.privileged_action) or action in {"code_edit", "deploy", "hotfix", "external_repo_edit"}
    if privileged_requested and not _is_privileged_allowed(operator_id):
        raise HTTPException(status_code=403, detail="privileged control action denied")

    next_status: Optional[str] = None
    next_archived: Optional[bool] = None
    next_archived_by: Optional[str] = None
    result: Dict[str, Any] = {}
    control_metadata: Dict[str, Any] = dict(body.metadata or {})
    is_archived = bool(session.get("archived"))
    if is_archived and action not in {"restore", "unarchive"}:
        raise HTTPException(status_code=409, detail="session is archived; restore it before executing actions")

    if action in {"start", "resume", "continue"}:
        next_status = "running"
        result = {"status": "running"}
        control_metadata.update(_pipeline_tuner_reset_metadata(session, for_new_target=False))
        if runtime is not None:
            await runtime.ensure_session_loop(session_id)
    elif action in {"archive", "close"}:
        next_archived = True
        next_archived_by = operator_id or None
        next_status = "ready"
        result = {"status": "ready", "archived": True}
    elif action in {"restore", "unarchive"}:
        next_archived = False
        next_status = "ready"
        result = {"status": "ready", "archived": False}
    elif action in {"pause", "hold"}:
        next_status = "ready"
        result = {"status": "ready"}
    elif action in {"stop", "cancel"}:
        next_status = "stopped"
        result = {"status": "stopped"}
    elif action == "follow":
        result = {"status": session.get("status"), "follow": "attached"}
    elif action == "attach_assignment":
        assignment_id = str(body.assignment_id or "").strip()
        if not assignment_id:
            raise HTTPException(status_code=400, detail="attach_assignment requires assignment_id")
        context = await _resolve_context(request, assignment_id=assignment_id, run_id=None, orchestration_id=None)
        seed_context = await _resolve_seed_binding(
            request,
            seed_run_id=None,
            seed_orchestration_id=None,
            assignment_id=context.get("assignment_id"),
            run_id=context.get("run_id"),
            orchestration_id=context.get("orchestration_id"),
        )
        control_metadata.update(_pipeline_tuner_reset_metadata(session, for_new_target=True))
        if isinstance(seed_context.get("seed_binding"), dict):
            control_metadata["seed_binding"] = seed_context.get("seed_binding")
        if str(seed_context.get("project_id") or "").strip():
            control_metadata["project_id"] = str(seed_context.get("project_id") or "").strip()
        if str(seed_context.get("conversation_id") or "").strip():
            control_metadata["conversation_id"] = str(seed_context.get("conversation_id") or "").strip()
        session = await store.update_session(
            session_id,
            assignment_id=seed_context.get("assignment_id"),
            run_id=seed_context.get("run_id"),
            orchestration_id=seed_context.get("orchestration_id"),
            metadata=control_metadata,
        ) or session
        result = {
            "assignment_id": seed_context.get("assignment_id"),
            "run_id": seed_context.get("run_id"),
            "orchestration_id": seed_context.get("orchestration_id"),
            "project_id": seed_context.get("project_id"),
            "conversation_id": seed_context.get("conversation_id"),
        }
    elif action == "attach_orchestration":
        orch_id = str(body.orchestration_id or "").strip()
        if not orch_id:
            raise HTTPException(status_code=400, detail="attach_orchestration requires orchestration_id")
        context = await _resolve_context(request, assignment_id=None, run_id=None, orchestration_id=orch_id)
        seed_context = await _resolve_seed_binding(
            request,
            seed_run_id=None,
            seed_orchestration_id=None,
            assignment_id=context.get("assignment_id"),
            run_id=context.get("run_id"),
            orchestration_id=context.get("orchestration_id"),
        )
        control_metadata.update(_pipeline_tuner_reset_metadata(session, for_new_target=True))
        if isinstance(seed_context.get("seed_binding"), dict):
            control_metadata["seed_binding"] = seed_context.get("seed_binding")
        if str(seed_context.get("project_id") or "").strip():
            control_metadata["project_id"] = str(seed_context.get("project_id") or "").strip()
        if str(seed_context.get("conversation_id") or "").strip():
            control_metadata["conversation_id"] = str(seed_context.get("conversation_id") or "").strip()
        session = await store.update_session(
            session_id,
            assignment_id=seed_context.get("assignment_id"),
            run_id=seed_context.get("run_id"),
            orchestration_id=seed_context.get("orchestration_id"),
            metadata=control_metadata,
        ) or session
        result = {
            "assignment_id": seed_context.get("assignment_id"),
            "run_id": seed_context.get("run_id"),
            "orchestration_id": seed_context.get("orchestration_id"),
            "project_id": seed_context.get("project_id"),
            "conversation_id": seed_context.get("conversation_id"),
        }
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
    elif action in {"project_code_edit", "public_project_edit"}:
        _require_feature_flag("NEXUS_PLATFORM_AI_PROJECT_EDIT_ENABLED", action=action)
        if runtime is None:
            raise HTTPException(status_code=503, detail="platform ai runtime unavailable")
        result = await runtime.start_project_edit_run(
            session_id,
            requested_by=operator_id or "platform-ai",
            instruction=_instruction_from_payload(body.payload),
        )
        status_raw = str(result.get("status") or "").strip().lower()
        if status_raw in {"disabled", "denied"}:
            raise HTTPException(status_code=403, detail=str(result.get("detail") or "project code edit denied"))
        if status_raw == "error":
            raise HTTPException(status_code=400, detail=str(result.get("detail") or "project code edit error"))
    elif action in {"code_edit", "hotfix"}:
        _require_feature_flag("NEXUS_PLATFORM_AI_REPO_EDIT_ENABLED", action=action)
        if runtime is None:
            raise HTTPException(status_code=503, detail="platform ai runtime unavailable")
        result = await runtime.start_repo_edit_run(
            session_id,
            requested_by=operator_id or "platform-ai",
            instruction=_instruction_from_payload(body.payload),
            external=False,
        )
        status_raw = str(result.get("status") or "").strip().lower()
        if status_raw in {"disabled", "denied"}:
            raise HTTPException(status_code=403, detail=str(result.get("detail") or "repo edit denied"))
        if status_raw == "error":
            raise HTTPException(status_code=400, detail=str(result.get("detail") or "repo edit error"))
    elif action == "external_repo_edit":
        _require_feature_flag("NEXUS_PLATFORM_AI_EXTERNAL_REPO_EDIT_ENABLED", action=action)
        if runtime is None:
            raise HTTPException(status_code=503, detail="platform ai runtime unavailable")
        result = await runtime.start_repo_edit_run(
            session_id,
            requested_by=operator_id or "platform-ai",
            instruction=_instruction_from_payload(body.payload),
            external=True,
        )
        status_raw = str(result.get("status") or "").strip().lower()
        if status_raw in {"disabled", "denied"}:
            raise HTTPException(status_code=403, detail=str(result.get("detail") or "external repo edit denied"))
        if status_raw == "error":
            raise HTTPException(status_code=400, detail=str(result.get("detail") or "external repo edit error"))
    elif action == "deploy":
        _require_feature_flag("NEXUS_PLATFORM_AI_DEPLOY_ENABLED", action=action)
        if runtime is None:
            raise HTTPException(status_code=503, detail="platform ai runtime unavailable")
        result = await runtime.start_deploy_run(session_id, requested_by=operator_id or "platform-ai")
        status_raw = str(result.get("status") or "").strip().lower()
        if status_raw in {"disabled", "denied"}:
            raise HTTPException(status_code=403, detail=str(result.get("detail") or "deploy denied"))
        if status_raw == "error":
            raise HTTPException(status_code=400, detail=str(result.get("detail") or "deploy error"))
    else:
        raise HTTPException(status_code=400, detail=f"unsupported control action: {action}")

    if next_status is not None or next_archived is not None:
        session = await store.update_session(
            session_id,
            status=next_status,
            archived=next_archived,
            archived_by=next_archived_by,
            metadata=control_metadata,
        ) or session
        if runtime is not None and not bool(session.get("archived")) and str(session.get("status") or "").strip().lower() == "running":
            await runtime.ensure_session_loop(session_id)
    elif control_metadata and action not in {"attach_assignment", "attach_orchestration"}:
        session = await store.update_session(session_id, metadata=control_metadata) or session
    event = await store.append_event(
        session_id,
        "action_trace",
        {"action": action, "operator_id": operator_id, "privileged": privileged_requested, "result": result, "metadata": control_metadata},
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
    pipeline_bot_id: Optional[str] = None,
    assignment_id: Optional[str] = None,
    orchestration_id: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    suites = await store.list_test_suites(
        session_id=session_id,
        pipeline_bot_id=pipeline_bot_id,
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
        pipeline_bot_id=suite.get("pipeline_bot_id"),
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


@router.get("/pipelines")
async def list_pipeline_entries(request: Request) -> Dict[str, Any]:
    bot_registry = request.app.state.bot_registry
    bots = await bot_registry.list()
    pipelines = [_pipeline_entry_payload(bot) for bot in bots if _bot_is_pipeline_entry(bot)]
    pipelines.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("pipeline_bot_id") or "").lower()))
    return {"pipelines": pipelines}


@router.get("/pipelines/{pipeline_bot_id}/test-suites")
async def list_pipeline_test_suites(pipeline_bot_id: str, request: Request, limit: int = 200) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    bot_registry = request.app.state.bot_registry
    safe_bot_id = str(pipeline_bot_id or "").strip()
    if not safe_bot_id:
        raise HTTPException(status_code=400, detail="pipeline_bot_id is required")
    try:
        bot = await bot_registry.get(safe_bot_id)
    except Exception:
        raise HTTPException(status_code=404, detail="pipeline bot not found")
    if not _bot_is_pipeline_entry(bot):
        raise HTTPException(status_code=400, detail="bot is not marked as a pipeline entry")
    suites = await store.list_test_suites(pipeline_bot_id=safe_bot_id, limit=limit)
    routing = _bot_routing_rules(bot)
    testing = routing.get("platform_ai_testing") if isinstance(routing.get("platform_ai_testing"), dict) else {}
    default_suite_id = str(testing.get("default_suite_id") or "").strip() or None
    return {"pipeline": _pipeline_entry_payload(bot), "default_suite_id": default_suite_id, "suites": suites}


@router.post("/pipelines/{pipeline_bot_id}/test-suites/design")
async def design_pipeline_test_suite(pipeline_bot_id: str, request: Request, body: DesignPipelineSuiteRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    bot_registry = request.app.state.bot_registry
    safe_bot_id = str(pipeline_bot_id or "").strip()
    if not safe_bot_id:
        raise HTTPException(status_code=400, detail="pipeline_bot_id is required")
    try:
        bot = await bot_registry.get(safe_bot_id)
    except Exception:
        raise HTTPException(status_code=404, detail="pipeline bot not found")
    if not _bot_is_pipeline_entry(bot):
        raise HTTPException(status_code=400, detail="bot is not marked as a pipeline entry")

    session = await _find_or_create_pipeline_session(request, pipeline_bot_id=safe_bot_id)
    graph = _graph_from_bot(bot)
    suite_name = str(body.name or "").strip() or f"{_pipeline_name_for_bot(bot)} Quality Suite"
    existing_suites = await store.list_test_suites(pipeline_bot_id=safe_bot_id, limit=1000)
    max_version = 0
    for existing in existing_suites:
        metadata_obj = existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
        try:
            version = int(metadata_obj.get("suite_version") or 0)
        except Exception:
            version = 0
        if version > max_version:
            max_version = version
    next_version = max_version + 1
    suite_def = _build_suite_definition(
        suite_name=suite_name,
        graph=graph,
        include_default_tests=bool(body.include_default_tests),
        quality_expectations=body.quality_expectations,
        suite_pass_threshold=float(body.suite_pass_threshold),
    )
    suite_def["version"] = f"v{next_version}"
    suite_def["pipeline_bot_id"] = safe_bot_id
    metadata = dict(body.metadata or {})
    metadata["pipeline_bot_id"] = safe_bot_id
    metadata["suite_version"] = next_version
    suite = await store.create_test_suite(
        session_id=str(session.get("id") or ""),
        pipeline_bot_id=safe_bot_id,
        name=suite_name,
        suite=suite_def,
        metadata=metadata,
    )
    if body.set_default:
        routing = _bot_routing_rules(bot)
        testing = routing.get("platform_ai_testing") if isinstance(routing.get("platform_ai_testing"), dict) else {}
        testing["default_suite_id"] = str(suite.get("id") or "")
        routing["platform_ai_testing"] = testing
        updated = bot.model_copy(update={"routing_rules": routing})
        await bot_registry.update(safe_bot_id, updated)
    event = await store.append_event(
        str(session.get("id") or ""),
        "action_trace",
        {
            "action": "design_pipeline_quality_suite",
            "pipeline_bot_id": safe_bot_id,
            "suite_id": suite.get("id"),
            "suite_version": next_version,
            "set_default": bool(body.set_default),
        },
    )
    return {"pipeline": _pipeline_entry_payload(bot), "session": session, "suite": suite, "event": event}


@router.post("/pipelines/{pipeline_bot_id}/test-suites/run")
async def run_pipeline_test_suite(pipeline_bot_id: str, request: Request, body: RunPipelineSuiteRequest) -> Dict[str, Any]:
    store = request.app.state.platform_ai_session_store
    bot_registry = request.app.state.bot_registry
    task_manager = request.app.state.task_manager
    safe_bot_id = str(pipeline_bot_id or "").strip()
    if not safe_bot_id:
        raise HTTPException(status_code=400, detail="pipeline_bot_id is required")
    try:
        bot = await bot_registry.get(safe_bot_id)
    except Exception:
        raise HTTPException(status_code=404, detail="pipeline bot not found")
    if not _bot_is_pipeline_entry(bot):
        raise HTTPException(status_code=400, detail="bot is not marked as a pipeline entry")

    suites = await store.list_test_suites(pipeline_bot_id=safe_bot_id, limit=500)
    suite_id = str(body.suite_id or "").strip()
    if not suite_id:
        routing = _bot_routing_rules(bot)
        testing = routing.get("platform_ai_testing") if isinstance(routing.get("platform_ai_testing"), dict) else {}
        suite_id = str(testing.get("default_suite_id") or "").strip()
    suite = await store.get_test_suite(suite_id) if suite_id else None
    if suite is None:
        suite = suites[0] if suites else None
    if suite is None:
        raise HTTPException(status_code=400, detail="no stored test suite found for this pipeline; generate one first")
    if str(suite.get("pipeline_bot_id") or "").strip() not in {"", safe_bot_id}:
        raise HTTPException(status_code=400, detail="suite is not scoped to this pipeline")

    routing = _bot_routing_rules(bot)
    launch_profile = routing.get("launch_profile") if isinstance(routing.get("launch_profile"), dict) else {}
    launch_payload = launch_profile.get("payload") if isinstance(launch_profile.get("payload"), dict) else {}
    if not launch_payload:
        launch_payload = {"instruction": f"Run pipeline test for {safe_bot_id}"}
    orchestration_id = str(uuid.uuid4())
    task = await task_manager.create_task(
        bot_id=safe_bot_id,
        payload=launch_payload,
        metadata=TaskMetadata(
            source="platform_ai_pipeline_test",
            orchestration_id=orchestration_id,
            pipeline_name=_pipeline_name_for_bot(bot),
            pipeline_entry_bot_id=safe_bot_id,
        ),
    )
    if bool(body.wait_for_terminal):
        await _wait_for_orchestration_terminal(
            request,
            orchestration_id=orchestration_id,
            poll_interval_seconds=float(body.poll_interval_seconds or 1.0),
            max_wait_seconds=float(body.max_wait_seconds or 900.0),
        )

    context = await _resolve_context(request, assignment_id=None, run_id=None, orchestration_id=orchestration_id)
    graph = context.get("graph") if isinstance(context.get("graph"), dict) else {"nodes": [], "edges": []}
    tasks = context.get("tasks") if isinstance(context.get("tasks"), list) else []
    if not tasks:
        raise HTTPException(status_code=400, detail="pipeline test run produced no tasks to evaluate")
    run_record = await store.create_test_run(
        suite_id=str(suite.get("id") or ""),
        session_id=suite.get("session_id"),
        pipeline_bot_id=safe_bot_id,
        assignment_id=context.get("assignment_id"),
        run_id=context.get("run_id"),
        orchestration_id=orchestration_id,
        status="running",
        score=0.0,
        result={"started_at": _now(), "pipeline_bot_id": safe_bot_id},
    )
    suite_payload = suite.get("suite") if isinstance(suite.get("suite"), dict) else {}
    evaluation = _evaluate_suite(suite_payload, tasks, graph)
    evaluation["context"] = {
        "pipeline_bot_id": safe_bot_id,
        "orchestration_id": orchestration_id,
        "assignment_id": context.get("assignment_id"),
        "run_id": context.get("run_id"),
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
            str(suite.get("session_id") or ""),
            "action_trace",
            {
                "action": "run_pipeline_quality_suite",
                "pipeline_bot_id": safe_bot_id,
                "suite_id": suite.get("id"),
                "test_run_id": final_run.get("id"),
                "status": final_run.get("status"),
                "score": final_run.get("score"),
                "operator_id": str(body.operator_id or "").strip() or None,
                "metadata": body.metadata,
            },
        )
    return {
        "pipeline": _pipeline_entry_payload(bot),
        "suite": suite,
        "launched_task": task.model_dump(),
        "test_run": final_run,
        "event": event,
    }


# ---------------------------------------------------------------------------
# Pass 3: Session brief, durable actions, halt, and refresh-brief endpoints
# ---------------------------------------------------------------------------


class HaltSessionRequest(BaseModel):
    reason: str = Field(default="operator_requested", description="Machine-readable halt reason")
    operator_id: Optional[str] = Field(default=None)
    metadata: Optional[Dict[str, Any]] = Field(default=None)


class ApprovePatchRequest(BaseModel):
    operator_id: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)


class RefreshBriefRequest(BaseModel):
    operator_id: Optional[str] = Field(default=None)


@router.get("/sessions/{session_id}/brief")
async def get_session_brief(session_id: str, request: Request) -> Dict[str, Any]:
    """Return the compiled operator brief for a Platform AI session."""
    runtime = request.app.state.platform_ai_runtime
    safe_id = str(session_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="session_id required")
    brief = await runtime.get_session_brief(safe_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="session brief not found — POST a message to synthesize one")
    return {"session_id": safe_id, "brief": brief}


@router.get("/sessions/{session_id}/actions")
async def list_session_actions(
    session_id: str,
    request: Request,
    limit: int = 50,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """Return durable action records for a Platform AI session."""
    runtime = request.app.state.platform_ai_runtime
    safe_id = str(session_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="session_id required")
    actions = await runtime.list_session_actions(safe_id, limit=limit, status=status)
    return {"session_id": safe_id, "actions": actions, "count": len(actions)}


@router.get("/sessions/{session_id}/proposals")
async def list_session_proposals(
    session_id: str,
    request: Request,
    limit: int = 50,
) -> Dict[str, Any]:
    """Return persisted Platform AI proposals for individual operator review."""
    runtime = request.app.state.platform_ai_runtime
    safe_id = str(session_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="session_id required")
    proposals = await runtime.list_patch_proposals(safe_id, limit=limit)
    return {"session_id": safe_id, "proposals": proposals, "count": len(proposals)}


@router.post("/sessions/{session_id}/proposals/{proposal_id}/approve")
async def approve_session_proposal(
    session_id: str,
    proposal_id: str,
    request: Request,
    body: ApprovePatchRequest,
) -> Dict[str, Any]:
    """Apply one validated proposal after explicit operator approval."""
    runtime = request.app.state.platform_ai_runtime
    safe_session = str(session_id or "").strip()
    safe_proposal = str(proposal_id or "").strip()
    if not safe_session or not safe_proposal:
        raise HTTPException(status_code=400, detail="session_id and proposal_id required")
    result = await runtime.approve_patch_proposal(
        safe_session,
        safe_proposal,
        operator_id=str(body.operator_id or "").strip() or None,
        notes=str(body.notes or "").strip() or None,
    )
    status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else "error"
    if status == "error":
        detail = str((result or {}).get("detail") or "proposal approval failed")
        raise HTTPException(status_code=404 if detail == "proposal_not_found" else 409, detail=detail)
    if status == "blocked":
        raise HTTPException(status_code=409, detail=str(result.get("detail") or "proposal approval blocked"))
    return {"session_id": safe_session, "proposal_id": safe_proposal, "result": result}


@router.post("/sessions/{session_id}/proposals/{proposal_id}/preflight")
async def preflight_session_proposal(
    session_id: str,
    proposal_id: str,
    request: Request,
    body: ApprovePatchRequest,
) -> Dict[str, Any]:
    """Validate one pending proposal without registering, enabling, or dispatching it."""
    runtime = request.app.state.platform_ai_runtime
    safe_session = str(session_id or "").strip()
    safe_proposal = str(proposal_id or "").strip()
    if not safe_session or not safe_proposal:
        raise HTTPException(status_code=400, detail="session_id and proposal_id required")
    result = await runtime.preflight_patch_proposal(
        safe_session,
        safe_proposal,
        operator_id=str(body.operator_id or "").strip() or None,
    )
    status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else "error"
    if status == "error":
        detail = str((result or {}).get("detail") or "proposal preflight failed")
        raise HTTPException(status_code=404 if detail == "proposal_not_found" else 409, detail=detail)
    return {"session_id": safe_session, "proposal_id": safe_proposal, "result": result}


@router.post("/sessions/{session_id}/proposals/{proposal_id}/reject")
async def reject_session_proposal(
    session_id: str,
    proposal_id: str,
    request: Request,
    body: ApprovePatchRequest,
) -> Dict[str, Any]:
    """Reject one pending proposal without changing the target configuration."""
    runtime = request.app.state.platform_ai_runtime
    safe_session = str(session_id or "").strip()
    safe_proposal = str(proposal_id or "").strip()
    if not safe_session or not safe_proposal:
        raise HTTPException(status_code=400, detail="session_id and proposal_id required")
    result = await runtime.reject_patch_proposal(
        safe_session,
        safe_proposal,
        operator_id=str(body.operator_id or "").strip() or None,
        notes=str(body.notes or "").strip() or None,
    )
    status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else "error"
    if status == "error":
        detail = str((result or {}).get("detail") or "proposal rejection failed")
        raise HTTPException(status_code=404 if detail == "proposal_not_found" else 409, detail=detail)
    return {"session_id": safe_session, "proposal_id": safe_proposal, "result": result}


@router.post("/sessions/{session_id}/actions/{action_id}/approve")
async def approve_session_action(
    session_id: str,
    action_id: str,
    request: Request,
    body: ApprovePatchRequest,
) -> Dict[str, Any]:
    """Compatibility alias for the proposal approval endpoint."""
    runtime = request.app.state.platform_ai_runtime
    safe_session = str(session_id or "").strip()
    safe_action = str(action_id or "").strip()
    if not safe_session or not safe_action:
        raise HTTPException(status_code=400, detail="session_id and action_id required")
    result = await runtime.approve_patch_proposal(
        safe_session,
        safe_action,
        operator_id=str(body.operator_id or "").strip() or None,
        notes=str(body.notes or "").strip() or None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="action not found or not approvable")
    status = str(result.get("status") or "").strip().lower() if isinstance(result, dict) else "error"
    if status == "error":
        detail = str((result or {}).get("detail") or "proposal approval failed")
        raise HTTPException(status_code=404 if detail == "proposal_not_found" else 409, detail=detail)
    if status == "blocked":
        raise HTTPException(status_code=409, detail=str(result.get("detail") or "proposal approval blocked"))
    return {"session_id": safe_session, "proposal_id": safe_action, "result": result}


@router.post("/sessions/{session_id}/halt")
async def halt_session(session_id: str, request: Request, body: HaltSessionRequest) -> Dict[str, Any]:
    """Explicitly halt a Platform AI session with a reason."""
    runtime = request.app.state.platform_ai_runtime
    safe_id = str(session_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="session_id required")
    reason = str(body.reason or "operator_requested").strip() or "operator_requested"
    result = await runtime.halt_session(
        safe_id,
        reason=reason,
        operator_id=str(body.operator_id or "").strip() or None,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": safe_id, "halted": True, "reason": reason, "session": result}


@router.post("/sessions/{session_id}/refresh-brief")
async def refresh_session_brief(session_id: str, request: Request, body: RefreshBriefRequest) -> Dict[str, Any]:
    """Force a re-synthesis of the session brief from operator messages."""
    runtime = request.app.state.platform_ai_runtime
    safe_id = str(session_id or "").strip()
    if not safe_id:
        raise HTTPException(status_code=400, detail="session_id required")
    brief = await runtime.refresh_session_brief(
        safe_id,
        operator_id=str(body.operator_id or "").strip() or None,
    )
    if brief is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {"session_id": safe_id, "brief": brief, "refreshed_at": _now()}
