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


def _require_admin() -> None:
    if not current_user.is_authenticated or current_user.role != "admin":
        abort(403)


def _load_work_overview() -> dict[str, Any]:
    cp = get_cp_client()
    active_tasks = _safe_call(
        cp.list_tasks,
        limit=OVERVIEW_ACTIVE_LIMIT,
        statuses=OVERVIEW_ACTIVE_STATUSES,
        include_content=False,
        timeout=1.5,
    ) or []
    recent_tasks = _safe_call(
        cp.list_tasks,
        limit=OVERVIEW_RECENT_LIMIT,
        include_content=False,
        timeout=1.5,
    ) or []
    tasks = _merge_task_rows(active_tasks, recent_tasks)
    projects = _safe_call(cp.list_projects, timeout=1.0) or []
    bots = _safe_call(cp.list_bots, timeout=1.0) or []
    workers = _safe_call(cp.list_workers, timeout=1.0) or []
    holds_payload = _safe_call(cp.list_work_dispatch_holds, timeout=1.0) or {}
    holds = holds_payload.get("holds") if isinstance(holds_payload, dict) else []
    overview = build_work_overview(tasks=tasks, projects=projects, bots=bots, workers=workers, holds=holds)
    overview["task_snapshot"] = {
        "active_limit": OVERVIEW_ACTIVE_LIMIT,
        "recent_limit": OVERVIEW_RECENT_LIMIT,
        "active_rows": len(active_tasks) if isinstance(active_tasks, list) else 0,
        "recent_rows": len(recent_tasks) if isinstance(recent_tasks, list) else 0,
        "merged_rows": len(tasks),
        "active_statuses": OVERVIEW_ACTIVE_STATUSES,
        "active_window_at_limit": isinstance(active_tasks, list) and len(active_tasks) >= OVERVIEW_ACTIVE_LIMIT,
        "recent_window_at_limit": isinstance(recent_tasks, list) and len(recent_tasks) >= OVERVIEW_RECENT_LIMIT,
    }
    task_usage = getattr(cp, "task_usage", None)
    overview["usage"] = _safe_call(task_usage, hours=24, limit_bots=25, timeout=1.5) if callable(task_usage) else None
    return overview


@bp.get("/work")
@login_required
def work_page() -> str:
    return render_template("work.html", overview=_load_work_overview(), error=None)


@bp.get("/api/work/overview")
@login_required
def api_work_overview():
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
    error = task.get("error") if isinstance(task.get("error"), dict) else None
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
        "error_code": str((error or {}).get("code") or ""),
        "error_message": str((error or {}).get("message") or ""),
    }


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


@bp.get("/api/work/lane")
@login_required
def api_work_lane():
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
