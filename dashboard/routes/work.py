"""Work overview blueprint for project and manager operational visibility."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from dashboard.cp_client import get_cp_client
from dashboard.work_overview import build_work_overview, manager_id_for_task, project_id_for_task

bp = Blueprint("work", __name__)

STOPPABLE_WORK_STATUSES = {"queued", "blocked", "running"}
LANE_DETAIL_STATUSES = {"queued", "blocked", "running", "failed", "retried"}
OVERVIEW_ACTIVE_STATUSES = sorted(LANE_DETAIL_STATUSES)
OVERVIEW_RECENT_LIMIT = 250
OVERVIEW_ACTIVE_LIMIT = 1000


def _empty_usage_summary() -> dict[str, Any]:
    return {
        "totals": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tasks_with_usage": 0,
            "tasks_without_usage": 0,
        },
        "by_project": [],
        "by_manager": [],
        "by_provider_model": [],
    }


def _safe_count(container: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(container.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _attach_attention_summary(overview: dict[str, Any]) -> None:
    totals = overview.get("totals") if isinstance(overview.get("totals"), dict) else {}
    workers = overview.get("workers") if isinstance(overview.get("workers"), dict) else {}
    metadata_health = overview.get("metadata_health") if isinstance(overview.get("metadata_health"), dict) else {}
    route_evidence = overview.get("route_evidence") if isinstance(overview.get("route_evidence"), dict) else {}
    usage = overview.get("usage") if isinstance(overview.get("usage"), dict) else {}
    usage_totals = usage.get("totals") if isinstance(usage.get("totals"), dict) else {}

    problem_tasks = _safe_count(totals, "problem")
    stale_work = _safe_count(totals, "stale_active") + _safe_count(totals, "stale_waiting")
    metadata_gaps = _safe_count(metadata_health, "missing_project_count") + _safe_count(metadata_health, "inferred_manager_count")
    route_gaps = _safe_count(route_evidence, "missing_active_problem_count")
    worker_issues = _safe_count(workers, "issue_count")
    usage_gaps = _safe_count(usage_totals, "tasks_without_usage")
    total = problem_tasks + stale_work + metadata_gaps + route_gaps + worker_issues + usage_gaps
    overview["attention"] = {
        "total": total,
        "problem_tasks": problem_tasks,
        "stale_work": stale_work,
        "metadata_gaps": metadata_gaps,
        "route_gaps": route_gaps,
        "worker_issues": worker_issues,
        "usage_gaps": usage_gaps,
        "level": "critical" if problem_tasks or stale_work or worker_issues else ("warning" if total else "ready"),
    }


def _merge_task_rows(*groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    anonymous_index = 0
    for group in groups:
        for task in group or []:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or "").strip()
            if not task_id:
                task_id = f"anonymous:{anonymous_index}"
                anonymous_index += 1
            merged[task_id] = task
    return list(merged.values())


def _safe_call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        fallback_kwargs = {key: value for key, value in kwargs.items() if key != "timeout"}
        try:
            return fn(*args, **fallback_kwargs)
        except TypeError:
            try:
                return fn(*args)
            except Exception:
                return None
        except Exception:
            return None
    except Exception:
        return None


def _control_plane_warning(cp: Any, source: str) -> dict[str, Any]:
    reason = "Control plane request failed."
    detail = ""
    status_code = None

    unavailable_reason = getattr(cp, "unavailable_reason", None)
    if callable(unavailable_reason):
        try:
            reason = str(unavailable_reason() or reason)
        except Exception:
            reason = "Control plane request failed."

    last_error = getattr(cp, "last_error", None)
    if callable(last_error):
        try:
            error = last_error()
        except Exception:
            error = None
        if isinstance(error, dict):
            detail = str(error.get("detail") or error.get("error") or "")
            status_code = error.get("status_code")

    warning: dict[str, Any] = {"source": source, "reason": reason}
    if detail:
        warning["detail"] = detail
    if status_code is not None:
        warning["status_code"] = status_code
    return warning


def _safe_cp_call(cp: Any, source: str, fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, dict[str, Any] | None]:
    result = _safe_call(fn, *args, **kwargs)
    if result is None:
        return None, _control_plane_warning(cp, source)
    return result, None


def _require_admin() -> None:
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


def _load_work_overview() -> dict[str, Any]:
    cp = get_cp_client()
    warnings: list[dict[str, Any]] = []
    active_tasks_result, warning = _safe_cp_call(
        cp,
        "active/problem task summaries",
        cp.list_tasks,
        limit=OVERVIEW_ACTIVE_LIMIT,
        statuses=OVERVIEW_ACTIVE_STATUSES,
        include_content=False,
        timeout=1.5,
    )
    if warning:
        warnings.append(warning)
    active_tasks = active_tasks_result if isinstance(active_tasks_result, list) else []

    recent_tasks_result, warning = _safe_cp_call(
        cp,
        "recent task summaries",
        cp.list_tasks,
        limit=OVERVIEW_RECENT_LIMIT,
        include_content=False,
        timeout=1.5,
    )
    if warning:
        warnings.append(warning)
    recent_tasks = recent_tasks_result if isinstance(recent_tasks_result, list) else []

    tasks = _merge_task_rows(active_tasks, recent_tasks)
    projects_result, warning = _safe_cp_call(cp, "projects", cp.list_projects, timeout=1.0)
    if warning:
        warnings.append(warning)
    projects = projects_result if isinstance(projects_result, list) else []

    bots_result, warning = _safe_cp_call(cp, "bots", cp.list_bots, timeout=1.0)
    if warning:
        warnings.append(warning)
    bots = bots_result if isinstance(bots_result, list) else []

    workers_result, warning = _safe_cp_call(cp, "workers", cp.list_workers, timeout=1.0)
    if warning:
        warnings.append(warning)
    workers = workers_result if isinstance(workers_result, list) else []

    holds_payload, warning = _safe_cp_call(cp, "dispatch holds", cp.list_work_dispatch_holds, timeout=1.0)
    if warning:
        warnings.append(warning)
    holds_payload = holds_payload if isinstance(holds_payload, dict) else {}
    holds = holds_payload.get("holds") if isinstance(holds_payload, dict) else []
    overview = build_work_overview(tasks=tasks, projects=projects, bots=bots, workers=workers, holds=holds)
    overview["data_degraded"] = bool(warnings)
    overview["data_warnings"] = warnings
    overview["task_snapshot"] = {
        "active_limit": OVERVIEW_ACTIVE_LIMIT,
        "recent_limit": OVERVIEW_RECENT_LIMIT,
        "active_rows": len(active_tasks) if isinstance(active_tasks, list) else 0,
        "recent_rows": len(recent_tasks) if isinstance(recent_tasks, list) else 0,
        "merged_rows": len(tasks),
        "active_statuses": OVERVIEW_ACTIVE_STATUSES,
        "active_unavailable": active_tasks_result is None,
        "recent_unavailable": recent_tasks_result is None,
        "active_window_at_limit": isinstance(active_tasks, list) and len(active_tasks) >= OVERVIEW_ACTIVE_LIMIT,
        "recent_window_at_limit": isinstance(recent_tasks, list) and len(recent_tasks) >= OVERVIEW_RECENT_LIMIT,
    }
    task_usage = getattr(cp, "task_usage", None)
    if callable(task_usage):
        usage, warning = _safe_cp_call(cp, "token usage", task_usage, hours=24, limit_bots=25, timeout=1.5)
        overview["usage"] = usage if isinstance(usage, dict) else _empty_usage_summary()
        if warning:
            warnings.append(warning)
            overview["data_degraded"] = True
            overview["data_warnings"] = warnings
    else:
        overview["usage"] = _empty_usage_summary()
    _attach_attention_summary(overview)
    return overview


@bp.get("/work")
@login_required
def work_page() -> str:
    _require_admin()
    return render_template("work.html", overview=_load_work_overview(), error=None)


@bp.get("/api/work/overview")
@login_required
def api_work_overview():
    _require_admin()
    return jsonify(_load_work_overview())


def _stoppable_work_matches(
    task: dict[str, Any],
    *,
    project_id: str,
    manager_id: str,
) -> bool:
    status = str(task.get("status") or "").strip().lower()
    if status not in STOPPABLE_WORK_STATUSES:
        return False
    if project_id_for_task(task) != project_id:
        return False
    return not manager_id or manager_id_for_task(task) == manager_id


def _lane_work_matches(
    task: dict[str, Any],
    *,
    project_id: str,
    manager_id: str,
) -> bool:
    status = str(task.get("status") or "").strip().lower()
    if status not in LANE_DETAIL_STATUSES:
        return False
    if project_id_for_task(task) != project_id:
        return False
    return not manager_id or manager_id_for_task(task) == manager_id


def _compact_lane_task(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    error_summary = task.get("error_summary") if isinstance(task.get("error_summary"), dict) else {}
    error = task.get("error") if isinstance(task.get("error"), dict) else {}
    return {
        "id": str(task.get("id") or ""),
        "bot_id": str(task.get("bot_id") or ""),
        "status": str(task.get("status") or ""),
        "project_id": project_id_for_task(task),
        "manager_id": manager_id_for_task(task),
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or ""),
        "orchestration_id": str(metadata.get("orchestration_id") or ""),
        "step_id": str(metadata.get("step_id") or ""),
        "source": str(metadata.get("source") or ""),
        "has_error": bool(task.get("has_error")),
        "error_type": str(error_summary.get("type") or task.get("error_type") or error.get("type") or ""),
        "error_code": str(error_summary.get("code") or error.get("code") or error.get("error_code") or ""),
        "error_message": str(error_summary.get("message") or error.get("message") or error.get("detail") or ""),
    }


def _orchestration_task_matches(task: dict[str, Any], orchestration_id: str) -> bool:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return str(metadata.get("orchestration_id") or "").strip() == orchestration_id


@bp.post("/api/work/stop")
@login_required
def api_stop_work():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    project_id = str(body.get("project_id") or "").strip()
    manager_id = str(body.get("manager_id") or "").strip()
    reason = str(body.get("reason") or "operator_stopped_from_work_overview").strip()
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    if not reason:
        return jsonify({"error": "reason is required."}), 400

    try:
        max_tasks = min(max(int(body.get("max_tasks", 50)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "max_tasks must be an integer between 1 and 200."}), 400
    dry_run = bool(body.get("dry_run", False))

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        limit=1000,
        statuses=sorted(STOPPABLE_WORK_STATUSES),
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503

    matches = [
        task
        for task in tasks
        if isinstance(task, dict)
        and _stoppable_work_matches(task, project_id=project_id, manager_id=manager_id)
    ]
    selected = matches[:max_tasks]
    selected_rows = [
        {
            "id": str(task.get("id") or ""),
            "bot_id": str(task.get("bot_id") or ""),
            "status": str(task.get("status") or ""),
            "project_id": project_id_for_task(task),
            "manager_id": manager_id_for_task(task),
        }
        for task in selected
    ]

    if dry_run:
        return jsonify(
            {
                "status": "dry_run",
                "project_id": project_id,
                "manager_id": manager_id,
                "matched_task_count": len(matches),
                "selected_task_count": len(selected_rows),
                "truncated": len(matches) > len(selected_rows),
                "tasks": selected_rows,
            }
        )

    cancelled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for task in selected:
        task_id = str(task.get("id") or "").strip()
        if not task_id:
            continue
        result = _safe_call(cp.cancel_task, task_id, reason=reason)
        if isinstance(result, dict):
            cancelled.append({"id": task_id, "status": str(result.get("status") or "cancelled")})
        else:
            failed.append({"id": task_id, "error": "cancel failed"})

    return jsonify(
        {
            "status": "ok" if not failed else "partial",
            "project_id": project_id,
            "manager_id": manager_id,
            "reason": reason,
            "matched_task_count": len(matches),
            "selected_task_count": len(selected_rows),
            "cancelled_task_count": len(cancelled),
            "failed_task_count": len(failed),
            "truncated": len(matches) > len(selected_rows),
            "cancelled": cancelled,
            "failed": failed,
        }
    ), (200 if not failed else 207)


@bp.get("/api/work/orchestration")
@login_required
def api_work_orchestration():
    _require_admin()
    orchestration_id = str(request.args.get("orchestration_id") or "").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required."}), 400
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer between 1 and 200."}), 400

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        orchestration_id=orchestration_id,
        limit=1000,
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503

    matching_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and _orchestration_task_matches(task, orchestration_id)
    ]
    matching_tasks.sort(key=lambda task: str(task.get("updated_at") or task.get("created_at") or ""), reverse=True)
    counts: dict[str, int] = {}
    for task in matching_tasks:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1

    return jsonify(
        {
            "orchestration_id": orchestration_id,
            "count": len(matching_tasks),
            "counts": counts,
            "stoppable_count": sum(
                1
                for task in matching_tasks
                if str(task.get("status") or "").strip().lower() in STOPPABLE_WORK_STATUSES
            ),
            "tasks": [_compact_lane_task(task) for task in matching_tasks[:limit]],
            "truncated": len(matching_tasks) > limit,
        }
    )


@bp.post("/api/work/orchestration/stop")
@login_required
def api_stop_orchestration_work():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    orchestration_id = str(body.get("orchestration_id") or "").strip()
    reason = str(body.get("reason") or "operator_stopped_orchestration_from_work_overview").strip()
    if not orchestration_id:
        return jsonify({"error": "orchestration_id is required."}), 400
    if not reason:
        return jsonify({"error": "reason is required."}), 400
    dry_run = bool(body.get("dry_run", False))

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        orchestration_id=orchestration_id,
        limit=1000,
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503
    matching_tasks = [
        task
        for task in tasks
        if isinstance(task, dict) and _orchestration_task_matches(task, orchestration_id)
    ]
    status_counts: dict[str, int] = {}
    cancellable_tasks: list[dict[str, Any]] = []
    for task in matching_tasks:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in STOPPABLE_WORK_STATUSES:
            cancellable_tasks.append(task)

    preview = {
        "orchestration_id": orchestration_id,
        "task_count": len(matching_tasks),
        "cancellable_task_count": len(cancellable_tasks),
        "status_counts": status_counts,
        "tasks": [
            {
                "id": str(task.get("id") or ""),
                "bot_id": str(task.get("bot_id") or ""),
                "status": str(task.get("status") or ""),
                "project_id": project_id_for_task(task),
                "manager_id": manager_id_for_task(task),
            }
            for task in cancellable_tasks[:50]
        ],
        "truncated": len(cancellable_tasks) > 50,
    }
    if dry_run:
        return jsonify({"status": "dry_run", **preview})

    result = _safe_call(cp.cancel_orchestration, orchestration_id, reason=reason)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 503
    return jsonify({"status": "ok", "preview": preview, "result": result})


@bp.get("/api/work/lane")
@login_required
def api_work_lane():
    _require_admin()
    project_id = str(request.args.get("project_id") or "").strip()
    manager_id = str(request.args.get("manager_id") or "").strip()
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer between 1 and 200."}), 400

    cp = get_cp_client()
    tasks = _safe_call(
        cp.list_tasks,
        limit=1000,
        statuses=sorted(LANE_DETAIL_STATUSES),
        include_content=False,
        timeout=2.0,
    )
    if tasks is None:
        return jsonify({"error": "control plane unavailable"}), 503
    holds_payload = _safe_call(cp.list_work_dispatch_holds, timeout=1.0) or {}
    holds = holds_payload.get("holds") if isinstance(holds_payload, dict) else []
    hold = None
    if isinstance(holds, list):
        for row in holds:
            if not isinstance(row, dict):
                continue
            if str(row.get("project_id") or "").strip() != project_id:
                continue
            hold_manager = str(row.get("manager_id") or "").strip()
            if (manager_id and hold_manager == manager_id) or (not manager_id and not hold_manager):
                hold = row
                break

    matches = [
        task
        for task in tasks
        if isinstance(task, dict)
        and _lane_work_matches(task, project_id=project_id, manager_id=manager_id)
    ]
    matches.sort(key=lambda task: str(task.get("updated_at") or task.get("created_at") or ""), reverse=True)
    counts: dict[str, int] = {}
    for task in matches:
        status = str(task.get("status") or "unknown").strip().lower() or "unknown"
        counts[status] = counts.get(status, 0) + 1

    return jsonify(
        {
            "project_id": project_id,
            "manager_id": manager_id,
            "count": len(matches),
            "counts": counts,
            "stoppable_count": sum(
                1
                for task in matches
                if str(task.get("status") or "").strip().lower() in STOPPABLE_WORK_STATUSES
            ),
            "hold": hold,
            "tasks": [_compact_lane_task(task) for task in matches[:limit]],
            "truncated": len(matches) > limit,
        }
    )


@bp.post("/api/work/hold")
@login_required
def api_hold_work():
    _require_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    action = str(body.get("action") or "hold").strip().lower()
    project_id = str(body.get("project_id") or "").strip()
    manager_id = str(body.get("manager_id") or "").strip()
    reason = str(body.get("reason") or "operator_hold_from_work_overview").strip()
    if action not in {"hold", "release"}:
        return jsonify({"error": "action must be hold or release."}), 400
    if not project_id:
        return jsonify({"error": "project_id is required."}), 400
    if action == "hold" and not reason:
        return jsonify({"error": "reason is required."}), 400

    cp = get_cp_client()
    operator_id = str(getattr(current_user, "email", "") or "operator").strip() or "operator"
    if action == "release":
        result = _safe_call(
            cp.release_work_dispatch_hold,
            project_id=project_id,
            manager_id=manager_id,
            operator_id=operator_id,
        )
    else:
        result = _safe_call(
            cp.set_work_dispatch_hold,
            project_id=project_id,
            manager_id=manager_id,
            reason=reason,
            operator_id=operator_id,
        )
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 503
    return jsonify(result)
