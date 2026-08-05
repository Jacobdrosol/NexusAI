"""Workers blueprint — page + JSON API."""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import requests
from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Task, Worker

logger = logging.getLogger(__name__)

bp = Blueprint("workers", __name__)


def _cp_list_tasks_safe(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


def _worker_to_dict(w: Worker) -> dict[str, Any]:
    """Serialise a Worker ORM row to a plain dict."""
    return {
        "id": w.id,
        "name": w.name,
        "host": w.host,
        "port": w.port,
        "status": w.status,
        "enabled": w.enabled,
        "capabilities": w.capabilities_as_dict(),
        "metrics": w.metrics_as_dict(),
        "last_heartbeat_at": None,
    }


def _worker_base_url(worker: dict[str, Any]) -> str:
    host = str(worker.get("host") or "").strip()
    port = int(worker.get("port") or 0)
    if not host or not port:
        raise ValueError("worker host/port unavailable")
    return f"http://{host}:{port}"


def _worker_id_from_name(name: Any) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "worker"


def _worker_probe_view(probe: Any) -> dict[str, Any] | None:
    if not isinstance(probe, dict):
        return None
    status = str(probe.get("probe_status") or "unknown").strip().lower() or "unknown"
    if status == "unknown":
        return None
    failed_checks = [
        str(check.get("detail") or "").strip()
        for check in probe.get("checks") or []
        if isinstance(check, dict) and str(check.get("status") or "").strip().lower() == "fail"
    ]
    detail_parts = [item for item in failed_checks if item]
    raw_attestation = probe.get("capability_attestation")
    attestation = raw_attestation if isinstance(raw_attestation, dict) else {}
    unavailable_tools = [
        str(tool).strip()
        for tool in attestation.get("unavailable_cli_tools") or []
        if str(tool).strip()
    ]
    if unavailable_tools:
        detail_parts.append("CLI tools unavailable: " + ", ".join(unavailable_tools))
    unauthenticated_tools = [
        str(tool).strip()
        for tool in attestation.get("unauthenticated_cli_tools") or []
        if str(tool).strip()
    ]
    if unauthenticated_tools:
        detail_parts.append("CLI authentication required: " + ", ".join(unauthenticated_tools))
    browser = attestation.get("browser") if isinstance(attestation.get("browser"), dict) else {}
    if bool(browser.get("configured")) and not bool(browser.get("ready")):
        reason = str(browser.get("reason") or "").strip()
        detail_parts.append("Browser session unavailable" + (f": {reason}" if reason else ""))
        if status == "ready":
            status = "degraded"
    detail = " | ".join(detail_parts)
    if not detail:
        detail = "runtime and capability contract verified" if status == "ready" else "runtime probe requires attention"
    checked_at = str(probe.get("checked_at") or "").strip().replace("T", " ")[:19]
    provider_credentials = attestation.get("provider_credentials")
    provider_status: list[dict[str, Any]] = []
    if isinstance(provider_credentials, dict):
        for provider, configured in sorted(provider_credentials.items(), key=lambda item: str(item[0]).lower()):
            provider_name = str(provider or "").strip().lower()
            if provider_name:
                provider_status.append({"provider": provider_name, "configured": configured is True})

    def _attested_tools(key: str) -> list[str]:
        values = attestation.get(key)
        if not isinstance(values, list):
            return []
        return [str(value).strip() for value in values if str(value).strip()]

    runtime_evidence = {
        "provider_status": provider_status,
        "configured_cli_tools": _attested_tools("configured_cli_tools"),
        "installed_cli_tools": _attested_tools("installed_cli_tools"),
        "enabled_cli_tools": _attested_tools("enabled_cli_tools"),
        "unavailable_cli_tools": _attested_tools("unavailable_cli_tools"),
        "auth_required_cli_tools": _attested_tools("auth_required_cli_tools"),
        "unauthenticated_cli_tools": _attested_tools("unauthenticated_cli_tools"),
        "browser": {
            "configured": bool(browser.get("configured")),
            "ready": bool(browser.get("ready")),
            "name": str(browser.get("browser") or "").strip(),
            "reason": str(browser.get("reason") or "").strip(),
        },
    }
    return {
        "status": status,
        "detail": detail,
        "checked_at": checked_at,
        "runtime_evidence": runtime_evidence,
    }


def _worker_dependency_view(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    dependent_bots = []

    def _policy_list(policy: dict[str, Any], key: str) -> list[str]:
        values = policy.get(key)
        if not isinstance(values, list):
            return []
        result = []
        for value in values:
            label = str(value or "").strip()
            if label and label not in result:
                result.append(label)
        return result

    def _profile_list(profile: dict[str, Any], key: str) -> list[str]:
        values = profile.get(key)
        if isinstance(values, list):
            result = []
            for value in values:
                label = str(value or "").strip()
                if label and label not in result:
                    result.append(label)
            return result
        value = str(values or "").strip()
        return [value] if value else []

    for item in payload.get("dependent_bots") or []:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        routing = row.get("routing_rules") if isinstance(row.get("routing_rules"), dict) else {}
        worker_profile = routing.get("worker_profile") if isinstance(routing.get("worker_profile"), dict) else {}
        policy = row.get("execution_policy") if isinstance(row.get("execution_policy"), dict) else {}
        backend_routes = []
        for backend in row.get("backends") or []:
            if not isinstance(backend, dict):
                continue
            backend_type = str(backend.get("type") or "").strip()
            provider = str(backend.get("provider") or "").strip()
            model = str(backend.get("model") or "").strip()
            worker_id = str(backend.get("worker_id") or "").strip()
            route = provider or backend_type or "backend"
            if model:
                route += f" / {model}"
            if worker_id:
                route += f" on {worker_id}"
            if route and route not in backend_routes:
                backend_routes.append(route)
        site_account = str(
            worker_profile.get("site_account")
            or worker_profile.get("site_user")
            or worker_profile.get("site_username")
            or worker_profile.get("globeiq_user_email")
            or ""
        ).strip()
        row["worker_profile_view"] = {
            "can_edit": worker_profile.get("can_edit") if isinstance(worker_profile.get("can_edit"), bool) else None,
            "task_scope": str(worker_profile.get("task_scope") or "").strip(),
            "site_scope": str(worker_profile.get("site_scope") or worker_profile.get("site") or "").strip(),
            "site_account": site_account,
            "course_scope": _profile_list(worker_profile, "course_scope"),
            "lesson_scope": _profile_list(worker_profile, "lesson_scope"),
            "allowed_pages": _profile_list(worker_profile, "allowed_pages"),
            "cli_tools": _profile_list(worker_profile, "cli_tools"),
            "backend_routes": backend_routes,
        }
        row["action_policy_view"] = {
            "required_tools": _policy_list(policy, "required_worker_tools"),
            "connection_actions": _policy_list(policy, "connection_action_allowlist"),
            "connection_owner_approvals": _policy_list(policy, "connection_action_owner_approval_required"),
            "browser_actions": _policy_list(policy, "browser_action_allowlist"),
            "browser_owner_approvals": _policy_list(policy, "browser_action_owner_approval_required"),
        }
        dependent_bots.append(row)
    active_schedules = [item for item in payload.get("active_schedules") or [] if isinstance(item, dict)]
    return {
        "dependent_bots": dependent_bots,
        "active_schedules": active_schedules,
        "can_disable": bool(payload.get("can_disable", not any(bool(item.get("enabled")) for item in dependent_bots))),
        "can_delete": bool(payload.get("can_delete", not dependent_bots)),
    }


def _backend_route_labels(backends: Any, *, worker_id: str | None = None) -> list[str]:
    if not isinstance(backends, list):
        return []
    routes: list[str] = []
    target_worker_id = str(worker_id or "").strip()
    for backend in backends:
        if not isinstance(backend, dict):
            continue
        backend_worker_id = str(backend.get("worker_id") or "").strip()
        if target_worker_id and backend_worker_id != target_worker_id:
            continue
        backend_type = str(backend.get("type") or "").strip()
        provider = str(backend.get("provider") or "").strip()
        model = str(backend.get("model") or "").strip()
        route = provider or backend_type or "backend"
        if model:
            route += f" / {model}"
        if backend_worker_id:
            route += f" on {backend_worker_id}"
        if route and route not in routes:
            routes.append(route)
    return routes


def _worker_dependency_summary(worker_id: str, bots: Any) -> dict[str, Any]:
    if not isinstance(bots, list):
        bots = []
    enabled_count = 0
    disabled_count = 0
    backend_routes: list[str] = []
    dependent_bot_names: list[str] = []
    for bot in bots:
        if not isinstance(bot, dict):
            continue
        routes = _backend_route_labels(bot.get("backends"), worker_id=worker_id)
        if not routes:
            continue
        if bool(bot.get("enabled", True)):
            enabled_count += 1
        else:
            disabled_count += 1
        bot_name = str(bot.get("name") or bot.get("id") or "").strip()
        if bot_name and bot_name not in dependent_bot_names:
            dependent_bot_names.append(bot_name)
        for route in routes:
            if route not in backend_routes:
                backend_routes.append(route)
    return {
        "enabled_bot_count": enabled_count,
        "disabled_bot_count": disabled_count,
        "backend_routes": backend_routes,
        "bot_names": dependent_bot_names,
    }


def _control_plane_error(cp, fallback: str) -> tuple[dict[str, str], int]:
    error = cp.last_error()
    status = int(error.get("status_code") or 502)
    if status < 400 or status > 599:
        status = 502
    raw_detail = str(error.get("detail") or "").strip()
    detail = raw_detail
    try:
        parsed = json.loads(raw_detail)
        if isinstance(parsed, dict):
            payload = parsed.get("detail") if isinstance(parsed.get("detail"), dict) else parsed
            detail = str(payload.get("message") or payload.get("detail") or "").strip()
    except (TypeError, ValueError):
        pass
    return {"error": detail or fallback}, status


def _with_worker_probe_views(workers: list[dict[str, Any]], payload: Any, bots: Any = None) -> list[dict[str, Any]]:
    """Attach the latest persisted runtime evidence without performing fresh probes."""
    raw_probes = payload.get("probes") if isinstance(payload, dict) else []
    probe_by_id = {
        str(probe.get("worker_id") or "").strip(): probe
        for probe in raw_probes
        if isinstance(probe, dict) and str(probe.get("worker_id") or "").strip()
    }
    enriched: list[dict[str, Any]] = []
    for worker in workers:
        row = dict(worker)
        worker_id = str(row.get("id") or "").strip()
        row["probe"] = _worker_probe_view(probe_by_id.get(worker_id))
        row["dependency_summary"] = _worker_dependency_summary(worker_id, bots)
        enriched.append(row)
    return enriched


@bp.get("/workers")
@login_required
def workers_page() -> str:
    """Render the workers table page."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    cp_data = cp.list_workers()
    if cp_data is not None:
        bot_lister = getattr(cp, "list_bots", None)
        bots = bot_lister() if callable(bot_lister) else None
        return render_template(
            "workers.html",
            workers=_with_worker_probe_views(cp_data, cp.list_worker_probes(), bots),
            error=None,
        )

    flash(get_cp_client().unavailable_reason(), "warning")
    db = get_db()
    try:
        workers = db.query(Worker).all()
        return render_template(
            "workers.html",
            workers=[_worker_to_dict(w) for w in workers],
            error=None,
        )
    finally:
        db.close()


@bp.get("/workers/<worker_id>")
@login_required
def worker_detail_page(worker_id: str):
    """Render worker detail with capabilities, metrics, and basic actions."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    worker = cp.get_worker(worker_id)
    probe_getter = getattr(cp, "get_worker_probe", None)
    worker_probe = _worker_probe_view(probe_getter(worker_id) if callable(probe_getter) else None)
    dependency_getter = getattr(cp, "get_worker_dependencies", None)
    worker_dependencies = _worker_dependency_view(dependency_getter(worker_id) if callable(dependency_getter) else None)
    running_tasks = _cp_list_tasks_safe(cp, statuses=["running"], limit=200, include_content=False) or []
    running_tasks = [t for t in running_tasks if t.get("status") == "running"]
    if worker is not None:
        return render_template(
            "worker_detail.html",
            worker=worker,
            worker_probe=worker_probe,
            worker_dependencies=worker_dependencies,
            running_tasks=running_tasks,
            error=None,
        )

    flash(get_cp_client().unavailable_reason(), "warning")
    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return render_template(
                "worker_detail.html",
                worker=None,
                worker_probe=None,
                worker_dependencies=None,
                running_tasks=[],
                error="Worker not found",
            )
        local = db.get(Worker, int(worker_id))
        if not local:
            return render_template("worker_detail.html", worker=None, worker_probe=None, worker_dependencies=None, running_tasks=[], error="Worker not found")
        return render_template(
            "worker_detail.html",
            worker=_worker_to_dict(local),
            worker_probe=None,
            worker_dependencies=None,
            running_tasks=[],
            error=None,
        )
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/workers")
@login_required
def api_list_workers():
    """List all workers as JSON."""
    from dashboard.cp_client import get_cp_client

    cp_workers = get_cp_client().list_workers()
    if cp_workers is not None:
        return jsonify(cp_workers)
    db = get_db()
    try:
        workers = db.query(Worker).all()
        return jsonify([_worker_to_dict(w) for w in workers])
    finally:
        db.close()


@bp.post("/api/workers")
@login_required
def api_create_worker():
    """Create a new worker."""
    data: dict[str, Any] = request.get_json(force=True) or {}
    if not data.get("name") or not data.get("host"):
        return jsonify({"error": "name and host are required"}), 400
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    cp_workers = cp.list_workers()
    if cp_workers is not None:
        requested_id = str(data.get("id") or "").strip()
        worker_id = requested_id or _worker_id_from_name(data.get("name"))
        existing_ids = {str(worker.get("id") or "") for worker in cp_workers if isinstance(worker, dict)}
        if worker_id in existing_ids:
            worker_id = f"{worker_id}-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": worker_id,
            "name": str(data["name"]).strip(),
            "host": str(data["host"]).strip(),
            "port": int(data.get("port", 8001)),
            "status": "offline",
            "capabilities": data.get("capabilities") if isinstance(data.get("capabilities"), list) else [],
            "metrics": data.get("metrics") if isinstance(data.get("metrics"), dict) else {},
            "enabled": bool(data.get("enabled", True)),
        }
        created = cp.provision_worker(payload)
        if created is None:
            return jsonify({"error": "control plane unavailable"}), 502
        return jsonify(created), 201
    db = get_db()
    try:
        worker = Worker(
            name=data["name"],
            host=data["host"],
            port=int(data.get("port", 8001)),
            status=data.get("status", "offline"),
            capabilities=json.dumps(data.get("capabilities", [])),
            metrics=json.dumps(data.get("metrics", {})),
            enabled=bool(data.get("enabled", True)),
        )
        db.add(worker)
        db.commit()
        db.refresh(worker)
        return jsonify(_worker_to_dict(worker)), 201
    finally:
        db.close()


@bp.get("/api/workers/<worker_id>")
@login_required
def api_get_worker(worker_id: str):
    """Get a single worker by ID."""
    from dashboard.cp_client import get_cp_client
    cp_worker = get_cp_client().get_worker(worker_id)
    if cp_worker is not None:
        return jsonify(cp_worker)
    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        worker = db.get(Worker, worker_id)
        if not worker:
            return jsonify({"error": "not found"}), 404
        return jsonify(_worker_to_dict(worker))
    finally:
        db.close()


@bp.put("/api/workers/<worker_id>")
@login_required
def api_update_worker(worker_id: str):
    """Update an existing worker."""
    from dashboard.cp_client import get_cp_client
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp = get_cp_client()
    cp_worker = cp.get_worker(worker_id)
    if cp_worker is not None:
        merged = dict(cp_worker)
        merged.update(data)
        updated = cp.update_worker(worker_id, merged)
        if updated is None:
            body, status = _control_plane_error(cp, "worker update failed")
            return jsonify(body), status
        return jsonify(updated)

    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        worker_id_int = int(worker_id)
        worker = db.get(Worker, worker_id_int)
        if not worker:
            return jsonify({"error": "not found"}), 404
        for field in ("name", "host", "status"):
            if field in data:
                setattr(worker, field, data[field])
        if "port" in data:
            worker.port = int(data["port"])
        if "enabled" in data:
            worker.enabled = bool(data["enabled"])
        if "capabilities" in data:
            worker.capabilities = json.dumps(data["capabilities"])
        if "metrics" in data:
            worker.metrics = json.dumps(data["metrics"])
        db.commit()
        db.refresh(worker)
        return jsonify(_worker_to_dict(worker))
    finally:
        db.close()


@bp.delete("/api/workers/<worker_id>")
@login_required
def api_delete_worker(worker_id: str):
    """Delete a worker."""
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    cp_worker = cp.get_worker(worker_id)
    if cp_worker is not None:
        ok = cp.delete_worker(worker_id)
        if not ok:
            body, status = _control_plane_error(cp, "worker deletion failed")
            return jsonify(body), status
        return "", 204

    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        worker = db.get(Worker, int(worker_id))
        if not worker:
            return jsonify({"error": "not found"}), 404
        db.delete(worker)
        db.commit()
        return "", 204
    finally:
        db.close()


@bp.post("/api/workers/<worker_id>/ping")
@login_required
def api_ping_worker(worker_id: str):
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    resp = cp.heartbeat_worker(worker_id)
    if resp is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(resp)


@bp.post("/api/workers/<worker_id>/probe")
@login_required
def api_probe_worker(worker_id: str):
    """Proxy an operator-requested, non-mutating worker runtime probe."""
    from dashboard.cp_client import get_cp_client

    result = get_cp_client().probe_worker(worker_id)
    if result is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(result)


@bp.post("/api/workers/<worker_id>/verify-inference")
@login_required
def api_verify_worker_inference(worker_id: str):
    """Proxy a bounded, no-context LLM completion verification."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    result = cp.verify_worker_inference(worker_id, request.get_json(silent=True) or {})
    if result is None:
        err = cp.last_error()
        status = int(err.get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str(err.get("detail") or "inference verification failed")}), status
    return jsonify(result)


@bp.get("/api/workers/<worker_id>/live")
@login_required
def api_worker_live(worker_id: str):
    """Return worker details and a running-task snapshot for live UI polling."""
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    cp_worker = cp.get_worker(worker_id)
    if cp_worker is not None:
        running_tasks = _cp_list_tasks_safe(cp, statuses=["running"], limit=200, include_content=False) or []
        running_tasks = [t for t in running_tasks if t.get("status") == "running"]
        return jsonify({"worker": cp_worker, "running_tasks": running_tasks})

    db = get_db()
    try:
        if not str(worker_id).isdigit():
            return jsonify({"error": "not found"}), 404
        local = db.get(Worker, int(worker_id))
        if not local:
            return jsonify({"error": "not found"}), 404
        running = db.query(Task).filter(Task.status == "running").order_by(Task.updated_at.desc()).limit(20).all()
        running_tasks = []
        for t in running:
            running_tasks.append(
                {
                    "id": t.id,
                    "bot_id": t.bot_id,
                    "status": t.status,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else "",
                }
            )
        return jsonify({"worker": _worker_to_dict(local), "running_tasks": running_tasks})
    finally:
        db.close()


@bp.post("/api/workers/<worker_id>/models/pull")
@login_required
def api_worker_pull_model(worker_id: str):
    from dashboard.cp_client import get_cp_client

    data: dict[str, Any] = request.get_json(force=True) or {}
    model = str(data.get("model") or "").strip()
    provider = str(data.get("provider") or "ollama").strip().lower() or "ollama"
    if not model:
        return jsonify({"error": "model is required"}), 400

    cp = get_cp_client()
    worker = cp.get_worker(worker_id)
    if worker is None:
        return jsonify({"error": "worker lookup failed"}), 502

    try:
        base_url = _worker_base_url(worker)
        resp = requests.post(
            f"{base_url}/models/local/pull",
            json={"model": model, "provider": provider},
            timeout=600,
        )
        if resp.text:
            payload = resp.json()
        else:
            payload = {}
        if resp.status_code >= 400:
            return jsonify({"error": payload.get("detail") or payload.get("error") or "model pull failed"}), resp.status_code
        return jsonify(payload)
    except requests.RequestException as exc:
        logger.warning("Worker model pull failed for %s: %s", worker_id, exc)
        return jsonify({"error": str(exc)}), 502
