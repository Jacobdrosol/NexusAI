from __future__ import annotations

from collections import Counter
from typing import Any
import hashlib

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("pipelines", __name__)


def _cp_list_tasks_safe(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


def _cp_error_response(cp, fallback: str = "control plane unavailable"):
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    detail = ""
    status_code = None
    if isinstance(err, dict):
        detail = str(err.get("detail") or "").strip()
        raw_code = err.get("status_code")
        if isinstance(raw_code, int) and 400 <= raw_code <= 599:
            status_code = raw_code
    return jsonify({"error": detail or fallback}), (status_code or 502)


def _task_sort_key(task: dict[str, Any]) -> tuple[str, str]:
    return (str(task.get("created_at") or ""), str(task.get("updated_at") or ""))


def _pipeline_key(name: str, entry_bot_id: str, workflow_graph_id: str = "") -> str:
    graph_id = str(workflow_graph_id or "").strip()
    if graph_id:
        return f"graph:{graph_id}"
    raw = f"{str(entry_bot_id or '').strip().lower()}::{str(name or '').strip().lower()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"pipeline:{digest}"


def _status_summary(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(task.get("status") or "unknown") for task in tasks)
    return {
        "queued": counts.get("queued", 0),
        "blocked": counts.get("blocked", 0),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "retried": counts.get("retried", 0),
        "cancelled": counts.get("cancelled", 0),
    }


def _pipeline_status(tasks: list[dict[str, Any]]) -> str:
    summary = _status_summary(tasks)
    if summary["running"] or summary["queued"] or summary["blocked"]:
        return "running"
    if summary["failed"]:
        return "failed"
    if summary["cancelled"] and not summary["completed"] and not summary["retried"]:
        return "cancelled"
    if summary["completed"]:
        return "completed"
    if summary["retried"]:
        return "retried"
    return "unknown"


def _usage_totals(tasks: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for task in tasks:
        usage = task.get("usage")
        if not isinstance(usage, dict):
            usage = ((task.get("result") or {}).get("usage") if isinstance(task.get("result"), dict) else None) or {}
        for key in totals:
            try:
                totals[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                continue
    return totals


def _root_task(tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task in sorted(tasks, key=_task_sort_key):
        meta = task.get("metadata") or {}
        if str(meta.get("workflow_root_task_id") or "") == str(task.get("id") or ""):
            return task
    return sorted(tasks, key=_task_sort_key)[0] if tasks else None


def _pipeline_groups(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        meta = task.get("metadata") or {}
        orchestration_id = str(meta.get("orchestration_id") or "").strip()
        if not orchestration_id:
            continue
        groups.setdefault(orchestration_id, []).append(task)

    rows: list[dict[str, Any]] = []
    for orchestration_id, items in groups.items():
        root = _root_task(items)
        if not root:
            continue
        root_meta = root.get("metadata") or {}
        if str(root_meta.get("source") or "") != "saved_launch_pipeline" and not str(root_meta.get("pipeline_name") or "").strip():
            continue
        items_sorted = sorted(items, key=_task_sort_key)
        pipeline_name = str(root_meta.get("pipeline_name") or root.get("bot_id") or orchestration_id)
        entry_bot_id = str(root_meta.get("pipeline_entry_bot_id") or root.get("bot_id") or "")
        workflow_graph_id = str(root_meta.get("workflow_graph_id") or "").strip()
        rows.append(
            {
                "id": orchestration_id,
                "pipeline_key": _pipeline_key(pipeline_name, entry_bot_id, workflow_graph_id),
                "name": pipeline_name,
                "entry_bot_id": entry_bot_id,
                "root_task_id": str(root.get("id") or ""),
                "created_at": str(items_sorted[0].get("created_at") or ""),
                "updated_at": str(items_sorted[-1].get("updated_at") or items_sorted[-1].get("created_at") or ""),
                "task_count": len(items_sorted),
                "bot_count": len({str(task.get("bot_id") or "") for task in items_sorted}),
                "status": _pipeline_status(items_sorted),
                "status_summary": _status_summary(items_sorted),
                "usage": _usage_totals(items_sorted),
            }
        )
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("created_at") or "")), reverse=True)
    return rows


def _pipeline_detail(cp, orchestration_id: str) -> dict[str, Any] | None:
    tasks = _cp_list_tasks_safe(cp, orchestration_id=orchestration_id, limit=1000, include_content=False) or []
    if not tasks:
        return None
    tasks = sorted(tasks, key=_task_sort_key)
    root = _root_task(tasks)
    root_meta = (root or {}).get("metadata") or {}

    task_ids = {str(task.get("id") or "") for task in tasks}
    artifacts: list[dict[str, Any]] = []
    for bot_id in sorted({str(task.get("bot_id") or "") for task in tasks if str(task.get("bot_id") or "").strip()}):
        rows = cp.list_bot_artifacts(bot_id, limit=1000, include_content=False) or []
        artifacts.extend(row for row in rows if str(row.get("task_id") or "") in task_ids)
    artifacts.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("task_id") or "")), reverse=True)

    pipeline_name = str(root_meta.get("pipeline_name") or (root or {}).get("bot_id") or orchestration_id)
    entry_bot_id = str(root_meta.get("pipeline_entry_bot_id") or (root or {}).get("bot_id") or "")
    workflow_graph_id = str(root_meta.get("workflow_graph_id") or "").strip()
    return {
        "id": orchestration_id,
        "pipeline_key": _pipeline_key(pipeline_name, entry_bot_id, workflow_graph_id),
        "name": pipeline_name,
        "entry_bot_id": entry_bot_id,
        "root_task_id": str((root or {}).get("id") or ""),
        "status": _pipeline_status(tasks),
        "status_summary": _status_summary(tasks),
        "usage": _usage_totals(tasks),
        "tasks": tasks,
        "artifacts": artifacts,
    }


def _pipeline_catalog(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get("pipeline_key") or "").strip()
        if not key:
            continue
        grouped.setdefault(key, []).append(row)
    catalog: list[dict[str, Any]] = []
    for key, items in grouped.items():
        ordered = sorted(
            [item for item in items if isinstance(item, dict)],
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("created_at") or "")),
            reverse=True,
        )
        latest = ordered[0] if ordered else {}
        catalog.append(
            {
                "pipeline_key": key,
                "name": str(latest.get("name") or ""),
                "entry_bot_id": str(latest.get("entry_bot_id") or ""),
                "latest_orchestration_id": str(latest.get("id") or ""),
                "latest_status": str(latest.get("status") or ""),
                "run_count": len(ordered),
                "updated_at": str(latest.get("updated_at") or ""),
                "created_at": str(latest.get("created_at") or ""),
            }
        )
    catalog.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("created_at") or "")), reverse=True)
    return catalog


def _pipeline_inventory(cp: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cp_tasks = _cp_list_tasks_safe(cp, limit=1000, include_content=False)
    if cp_tasks is None:
        return [], []
    runs = _pipeline_groups(cp_tasks)
    catalog = _pipeline_catalog(runs)
    return runs, catalog


def _catalog_entry(catalog: list[dict[str, Any]], pipeline_key: str) -> dict[str, Any] | None:
    key = str(pipeline_key or "").strip()
    for row in catalog:
        if str(row.get("pipeline_key") or "").strip() == key:
            return row
    return None


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(
        [row for row in rows if isinstance(row, dict)],
        key=lambda row: (str(row.get("updated_at") or ""), str(row.get("created_at") or "")),
        reverse=True,
    )
    return ordered[0] if ordered else None


def _get_or_create_pipeline_test_session(
    cp: Any,
    *,
    pipeline_key: str,
    pipeline_name: str,
    entry_bot_id: str,
    orchestration_id: str,
) -> dict[str, Any] | None:
    listed = cp.list_platform_ai_sessions(mode="pipeline_tuner", limit=500)
    sessions = listed.get("sessions") if isinstance(listed, dict) and isinstance(listed.get("sessions"), list) else []
    matching = []
    for session in sessions:
        metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
        if str(metadata.get("pipeline_key") or "").strip() == str(pipeline_key or "").strip():
            matching.append(session)
    current = _latest_row(matching)
    if current is not None:
        return current
    created = cp.create_platform_ai_session(
        {
            "mode": "pipeline_tuner",
            "orchestration_id": orchestration_id,
            "metadata": {
                "source": "pipeline_test_modal",
                "pipeline_key": pipeline_key,
                "pipeline_name": pipeline_name,
                "entry_bot_id": entry_bot_id,
            },
        }
    )
    if not isinstance(created, dict):
        return None
    return created


@bp.get("/pipelines")
@login_required
def pipelines_page() -> str:
    cp = get_cp_client()
    runs, catalog = _pipeline_inventory(cp)
    if not runs and not catalog:
        return render_template(
            "pipelines.html",
            pipelines=[],
            pipeline_catalog=[],
            error=("Control plane unavailable" if _cp_list_tasks_safe(cp, limit=1, include_content=False) is None else None),
            active_page="pipelines",
        )
    return render_template(
        "pipelines.html",
        pipelines=runs,
        pipeline_catalog=catalog,
        error=None,
        active_page="pipelines",
    )


@bp.get("/pipelines/<orchestration_id>")
@login_required
def pipeline_detail_page(orchestration_id: str) -> str:
    cp = get_cp_client()
    detail = _pipeline_detail(cp, orchestration_id)
    if detail is None:
        return render_template("pipeline_detail.html", pipeline=None, error="Pipeline not found", active_page="pipelines")
    return render_template("pipeline_detail.html", pipeline=detail, error=None, active_page="pipelines")


@bp.get("/api/pipelines")
@login_required
def api_list_pipelines():
    cp = get_cp_client()
    cp_tasks = _cp_list_tasks_safe(cp, limit=1000, include_content=False)
    if cp_tasks is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(_pipeline_groups(cp_tasks))


@bp.get("/api/pipelines/<orchestration_id>")
@login_required
def api_get_pipeline(orchestration_id: str):
    cp = get_cp_client()
    detail = _pipeline_detail(cp, orchestration_id)
    if detail is None:
        return jsonify({"error": "pipeline not found"}), 404
    return jsonify(detail)


@bp.get("/api/pipelines/<orchestration_id>/tests")
@login_required
def api_list_pipeline_tests(orchestration_id: str):
    cp = get_cp_client()
    detail = _pipeline_detail(cp, orchestration_id)
    if detail is None:
        return jsonify({"error": "pipeline not found"}), 404
    session = _get_or_create_pipeline_test_session(
        cp,
        pipeline_key=str(detail.get("pipeline_key") or ""),
        pipeline_name=str(detail.get("name") or ""),
        entry_bot_id=str(detail.get("entry_bot_id") or ""),
        orchestration_id=orchestration_id,
    )
    if session is None:
        return _cp_error_response(cp, "unable to initialize pipeline testing session")
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        return jsonify({"error": "invalid pipeline test session"}), 502
    suites_resp = cp.list_platform_ai_quality_suites(session_id, limit=200)
    if suites_resp is None:
        return _cp_error_response(cp, "unable to list pipeline test suites")
    suites = suites_resp.get("suites") if isinstance(suites_resp.get("suites"), list) else []
    return jsonify({"session": session, "suites": suites, "pipeline": detail})


@bp.post("/api/pipelines/<orchestration_id>/tests/design")
@login_required
def api_design_pipeline_tests(orchestration_id: str):
    cp = get_cp_client()
    detail = _pipeline_detail(cp, orchestration_id)
    if detail is None:
        return jsonify({"error": "pipeline not found"}), 404
    session = _get_or_create_pipeline_test_session(
        cp,
        pipeline_key=str(detail.get("pipeline_key") or ""),
        pipeline_name=str(detail.get("name") or ""),
        entry_bot_id=str(detail.get("entry_bot_id") or ""),
        orchestration_id=orchestration_id,
    )
    if session is None:
        return _cp_error_response(cp, "unable to initialize pipeline testing session")
    session_id = str(session.get("id") or "").strip()
    if not session_id:
        return jsonify({"error": "invalid pipeline test session"}), 502
    data = request.get_json(silent=True) or {}
    quality_expectations = data.get("quality_expectations") if isinstance(data.get("quality_expectations"), list) else []
    designed = cp.design_platform_ai_quality_suite(
        session_id,
        {
            "name": str(data.get("name") or "Pipeline Stored Quality Suite").strip() or "Pipeline Stored Quality Suite",
            "orchestration_id": orchestration_id,
            "include_default_tests": bool(data.get("include_default_tests", True)),
            "suite_pass_threshold": float(data.get("suite_pass_threshold") or 0.8),
            "quality_expectations": quality_expectations,
            "metadata": {
                "source": "pipeline_test_modal",
                "pipeline_key": str(detail.get("pipeline_key") or ""),
                "pipeline_name": str(detail.get("name") or ""),
                "entry_bot_id": str(detail.get("entry_bot_id") or ""),
            },
        },
    )
    if designed is None:
        return _cp_error_response(cp, "unable to design pipeline test suite")
    return jsonify(designed)


@bp.post("/api/pipelines/<orchestration_id>/tests/run")
@login_required
def api_run_pipeline_tests(orchestration_id: str):
    cp = get_cp_client()
    data = request.get_json(silent=True) or {}
    suite_id = str(data.get("suite_id") or "").strip()
    if not suite_id:
        detail = _pipeline_detail(cp, orchestration_id)
        if detail is None:
            return jsonify({"error": "pipeline not found"}), 404
        session = _get_or_create_pipeline_test_session(
            cp,
            pipeline_key=str(detail.get("pipeline_key") or ""),
            pipeline_name=str(detail.get("name") or ""),
            entry_bot_id=str(detail.get("entry_bot_id") or ""),
            orchestration_id=orchestration_id,
        )
        if session is None:
            return _cp_error_response(cp, "unable to initialize pipeline testing session")
        session_id = str(session.get("id") or "").strip()
        suites_resp = cp.list_platform_ai_quality_suites(session_id, limit=200)
        suites = suites_resp.get("suites") if isinstance(suites_resp, dict) and isinstance(suites_resp.get("suites"), list) else []
        latest = _latest_row(suites)
        suite_id = str((latest or {}).get("id") or "").strip()
    if not suite_id:
        return jsonify({"error": "no stored test suite found for this pipeline; generate one first"}), 400
    run = cp.run_platform_ai_quality_suite(
        suite_id,
        {
            "orchestration_id": orchestration_id,
            "wait_for_terminal": True,
            "metadata": {"source": "pipeline_test_modal"},
        },
    )
    if run is None:
        return _cp_error_response(cp, "unable to run pipeline test suite")
    return jsonify(run)


@bp.get("/api/pipelines/<orchestration_id>/tests/runs")
@login_required
def api_list_pipeline_test_runs(orchestration_id: str):
    cp = get_cp_client()
    suite_id = str(request.args.get("suite_id") or "").strip()
    if not suite_id:
        detail = _pipeline_detail(cp, orchestration_id)
        if detail is None:
            return jsonify({"error": "pipeline not found"}), 404
        session = _get_or_create_pipeline_test_session(
            cp,
            pipeline_key=str(detail.get("pipeline_key") or ""),
            pipeline_name=str(detail.get("name") or ""),
            entry_bot_id=str(detail.get("entry_bot_id") or ""),
            orchestration_id=orchestration_id,
        )
        if session is None:
            return _cp_error_response(cp, "unable to initialize pipeline testing session")
        session_id = str(session.get("id") or "").strip()
        suites_resp = cp.list_platform_ai_quality_suites(session_id, limit=200)
        suites = suites_resp.get("suites") if isinstance(suites_resp, dict) and isinstance(suites_resp.get("suites"), list) else []
        latest = _latest_row(suites)
        suite_id = str((latest or {}).get("id") or "").strip()
    if not suite_id:
        return jsonify({"runs": []})
    runs = cp.list_platform_ai_quality_suite_runs(suite_id, limit=100)
    if runs is None:
        return _cp_error_response(cp, "unable to list pipeline test runs")
    return jsonify(runs)


@bp.get("/api/pipelines/tests/catalog")
@login_required
def api_pipeline_test_catalog():
    cp = get_cp_client()
    runs, catalog = _pipeline_inventory(cp)
    if not runs and not catalog and _cp_list_tasks_safe(cp, limit=1, include_content=False) is None:
        return _cp_error_response(cp, "control plane unavailable")
    return jsonify({"pipelines": catalog})


@bp.get("/api/pipelines/tests/target")
@login_required
def api_pipeline_target_tests():
    cp = get_cp_client()
    pipeline_key = str(request.args.get("pipeline_key") or "").strip()
    if not pipeline_key:
        return jsonify({"error": "pipeline_key is required"}), 400
    _, catalog = _pipeline_inventory(cp)
    pipeline = _catalog_entry(catalog, pipeline_key)
    if pipeline is None:
        return jsonify({"error": "pipeline not found"}), 404
    orchestration_id = str(pipeline.get("latest_orchestration_id") or "").strip()
    session = _get_or_create_pipeline_test_session(
        cp,
        pipeline_key=pipeline_key,
        pipeline_name=str(pipeline.get("name") or ""),
        entry_bot_id=str(pipeline.get("entry_bot_id") or ""),
        orchestration_id=orchestration_id,
    )
    if session is None:
        return _cp_error_response(cp, "unable to initialize pipeline testing session")
    session_id = str(session.get("id") or "").strip()
    suites_resp = cp.list_platform_ai_quality_suites(session_id, limit=200)
    if suites_resp is None:
        return _cp_error_response(cp, "unable to list pipeline test suites")
    suites = suites_resp.get("suites") if isinstance(suites_resp.get("suites"), list) else []
    return jsonify({"pipeline": pipeline, "session": session, "suites": suites})


@bp.post("/api/pipelines/tests/target/design")
@login_required
def api_pipeline_target_design_tests():
    cp = get_cp_client()
    data = request.get_json(silent=True) or {}
    pipeline_key = str(data.get("pipeline_key") or "").strip()
    if not pipeline_key:
        return jsonify({"error": "pipeline_key is required"}), 400
    _, catalog = _pipeline_inventory(cp)
    pipeline = _catalog_entry(catalog, pipeline_key)
    if pipeline is None:
        return jsonify({"error": "pipeline not found"}), 404
    orchestration_id = str(pipeline.get("latest_orchestration_id") or "").strip()
    session = _get_or_create_pipeline_test_session(
        cp,
        pipeline_key=pipeline_key,
        pipeline_name=str(pipeline.get("name") or ""),
        entry_bot_id=str(pipeline.get("entry_bot_id") or ""),
        orchestration_id=orchestration_id,
    )
    if session is None:
        return _cp_error_response(cp, "unable to initialize pipeline testing session")
    session_id = str(session.get("id") or "").strip()
    quality_expectations = data.get("quality_expectations") if isinstance(data.get("quality_expectations"), list) else []
    designed = cp.design_platform_ai_quality_suite(
        session_id,
        {
            "name": str(data.get("name") or "Pipeline Stored Quality Suite").strip() or "Pipeline Stored Quality Suite",
            "orchestration_id": orchestration_id,
            "include_default_tests": bool(data.get("include_default_tests", True)),
            "suite_pass_threshold": float(data.get("suite_pass_threshold") or 0.8),
            "quality_expectations": quality_expectations,
            "metadata": {
                "source": "pipeline_test_modal",
                "pipeline_key": pipeline_key,
                "pipeline_name": str(pipeline.get("name") or ""),
                "entry_bot_id": str(pipeline.get("entry_bot_id") or ""),
            },
        },
    )
    if designed is None:
        return _cp_error_response(cp, "unable to design pipeline test suite")
    return jsonify({"pipeline": pipeline, **designed})


@bp.post("/api/pipelines/tests/target/run")
@login_required
def api_pipeline_target_run_tests():
    cp = get_cp_client()
    data = request.get_json(silent=True) or {}
    pipeline_key = str(data.get("pipeline_key") or "").strip()
    suite_id = str(data.get("suite_id") or "").strip()
    if not pipeline_key:
        return jsonify({"error": "pipeline_key is required"}), 400
    _, catalog = _pipeline_inventory(cp)
    pipeline = _catalog_entry(catalog, pipeline_key)
    if pipeline is None:
        return jsonify({"error": "pipeline not found"}), 404
    orchestration_id = str(data.get("orchestration_id") or pipeline.get("latest_orchestration_id") or "").strip()
    if not suite_id:
        session = _get_or_create_pipeline_test_session(
            cp,
            pipeline_key=pipeline_key,
            pipeline_name=str(pipeline.get("name") or ""),
            entry_bot_id=str(pipeline.get("entry_bot_id") or ""),
            orchestration_id=orchestration_id,
        )
        if session is None:
            return _cp_error_response(cp, "unable to initialize pipeline testing session")
        session_id = str(session.get("id") or "").strip()
        suites_resp = cp.list_platform_ai_quality_suites(session_id, limit=200)
        suites = suites_resp.get("suites") if isinstance(suites_resp, dict) and isinstance(suites_resp.get("suites"), list) else []
        latest = _latest_row(suites)
        suite_id = str((latest or {}).get("id") or "").strip()
    if not suite_id:
        return jsonify({"error": "no stored test suite found for this pipeline; generate one first"}), 400
    run = cp.run_platform_ai_quality_suite(
        suite_id,
        {
            "orchestration_id": orchestration_id,
            "wait_for_terminal": True,
            "metadata": {"source": "pipeline_test_modal", "pipeline_key": pipeline_key},
        },
    )
    if run is None:
        return _cp_error_response(cp, "unable to run pipeline test suite")
    return jsonify({"pipeline": pipeline, **run})
