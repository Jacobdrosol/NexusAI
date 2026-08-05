"""Bots blueprint — page + JSON API."""
from __future__ import annotations

import io
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, flash, jsonify, render_template, request, send_file
from flask_login import login_required

from dashboard.bot_chat_profiles import bot_chat_profile, bot_chat_tool_access, with_bot_chat_profiles
from dashboard.bot_tooling_status import build_bot_tooling_status
from dashboard.connections_service import (
    mask_auth_payload,
    mask_connection_config,
    normalize_auth_payload,
    normalize_connection_config,
)
from dashboard.db import get_db
from dashboard.models import Bot, BotConnection, Connection, ProjectConnection, Task

logger = logging.getLogger(__name__)

bp = Blueprint("bots", __name__)


def _cp_list_tasks_safe(cp, **kwargs):
    try:
        return cp.list_tasks(**kwargs)
    except TypeError:
        return cp.list_tasks()


def _slugify_bot_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "bot"


def _merge_routing_rules(data: dict[str, Any], existing: Any = None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(existing, str) and existing.strip():
        try:
            merged = json.loads(existing)
        except json.JSONDecodeError:
            merged = {}
    elif isinstance(existing, dict):
        merged = dict(existing)

    if isinstance(data.get("routing_rules"), dict):
        merged.update(data["routing_rules"])
    if "workflow" in data:
        merged["workflow"] = data.get("workflow")
    if "input_contract" in data:
        merged["input_contract"] = data.get("input_contract")
    if "input_transform" in data:
        merged["input_transform"] = data.get("input_transform")
    if "output_contract" in data:
        merged["output_contract"] = data.get("output_contract")
    if "connection_context" in data:
        merged["connection_context"] = data.get("connection_context")
    if "launch_profile" in data:
        merged["launch_profile"] = data.get("launch_profile")
    if "external_trigger" in data:
        merged["external_trigger"] = data.get("external_trigger")
    if "chat_profile" in data:
        merged["chat_profile"] = data.get("chat_profile")
    return merged


def _bot_chat_tool_access(bot: dict[str, Any]) -> dict[str, Any]:
    return bot_chat_tool_access(bot)


def _bot_chat_profile(bot: dict[str, Any]) -> dict[str, Any]:
    return bot_chat_profile(bot)


def _with_bot_chat_profiles(bots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return with_bot_chat_profiles(bots)


def _bot_to_dict(b: Bot) -> dict[str, Any]:
    """Serialise a Bot ORM row to a plain dict."""
    routing_rules = json.loads(b.routing_rules) if b.routing_rules else {}
    payload = {
        "id": b.id,
        "name": b.name,
        "role": b.role,
        "priority": b.priority,
        "enabled": b.enabled,
        "backends": b.backends_as_list(),
        "routing_rules": routing_rules,
        "workflow": routing_rules.get("workflow") if isinstance(routing_rules, dict) else None,
        "input_contract": routing_rules.get("input_contract") if isinstance(routing_rules, dict) else None,
        "input_transform": routing_rules.get("input_transform") if isinstance(routing_rules, dict) else None,
        "output_contract": routing_rules.get("output_contract") if isinstance(routing_rules, dict) else None,
        "connection_context": routing_rules.get("connection_context") if isinstance(routing_rules, dict) else None,
        "launch_profile": routing_rules.get("launch_profile") if isinstance(routing_rules, dict) else None,
        "external_trigger": routing_rules.get("external_trigger") if isinstance(routing_rules, dict) else None,
        "assignment_capabilities": None,
        "execution_policy": None,
    }
    payload["chat_profile"] = _bot_chat_profile(payload)
    return payload


def _parse_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


def _safe_backend_ref_label(value: Any) -> tuple[str, bool]:
    label = str(value or "").strip()
    if not label:
        return "", False
    lowered = label.lower()
    suspicious_prefixes = ("sk-", "xoxb-", "xoxp-", "ghp_", "github_pat_", "ya29.", "eyj")
    if any(lowered.startswith(prefix) for prefix in suspicious_prefixes):
        return "[redacted raw credential]", True
    if len(label) > 96:
        return label[:93] + "...", False
    return label, False


def _sanitize_bot_for_detail(bot: dict[str, Any]) -> dict[str, Any]:
    payload = dict(bot or {})
    sanitized_backends = []
    for backend in payload.get("backends") or []:
        if not isinstance(backend, dict):
            continue
        row = dict(backend)
        display_ref, raw_detected = _safe_backend_ref_label(row.get("api_key_ref"))
        row["api_key_ref_display"] = display_ref
        row["api_key_ref_raw_detected"] = raw_detected
        if raw_detected:
            row["api_key_ref"] = ""
        sanitized_backends.append(row)
    payload["backends"] = sanitized_backends
    return payload


def _cp_error_payload(cp, fallback: str) -> tuple[dict[str, Any], int]:
    """Normalize a control-plane error without losing safe readiness details."""
    err = cp.last_error() if hasattr(cp, "last_error") else {}
    raw_detail = err.get("detail") if isinstance(err, dict) else None
    try:
        parsed = json.loads(raw_detail) if isinstance(raw_detail, str) else raw_detail
    except (TypeError, json.JSONDecodeError):
        parsed = raw_detail
    detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
    detail = detail if isinstance(detail, dict) else {}
    message = str(detail.get("message") or raw_detail or fallback)
    status = int((err or {}).get("status_code") or 502) if isinstance(err, dict) else 502
    if status < 400 or status > 599:
        status = 502

    payload: dict[str, Any] = {"error": message}
    for key in ("reason_code", "readiness", "validation_errors", "dependencies"):
        if key in detail:
            payload[key] = detail[key]
    return payload, status


def _bot_test_preflight_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Return safe audit metadata for a one-off bot test run."""
    return {
        "tooling_state": row.get("state"),
        "blocking_category": row.get("blocking_category") or None,
        "recommended_action": row.get("recommended_action") or None,
        "required_tools": row.get("required_tools") or [],
        "connection_actions": row.get("connection_actions") or [],
        "browser_actions": row.get("browser_actions") or [],
        "credential_refs": row.get("credential_refs") or [],
        "missing_credential_refs": row.get("missing_credential_refs") or [],
        "raw_credential_ref_detected": bool(row.get("raw_credential_ref_detected")),
        "worker_ids": row.get("worker_ids") or [],
    }


def _bot_test_preflight(cp, bot_id: str) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Check whether a bot can safely run a proof task before queueing one."""
    get_bot = getattr(cp, "get_bot", None)
    if not callable(get_bot):
        return None, None
    bot = get_bot(bot_id)
    if bot is None:
        return None, ({"error": "bot not found"}, 404)

    readiness_getter = getattr(cp, "get_bot_readiness", None)
    workers_getter = getattr(cp, "list_workers", None)
    probes_getter = getattr(cp, "list_worker_probes", None)
    keys_getter = getattr(cp, "list_keys", None)
    readiness = readiness_getter(bot_id) if callable(readiness_getter) else None
    workers = workers_getter() if callable(workers_getter) else []
    probes = probes_getter() if callable(probes_getter) else None
    api_keys = keys_getter() if callable(keys_getter) else None
    tooling_status = build_bot_tooling_status(
        bots=[bot],
        readiness_payload={"readiness": [readiness]} if isinstance(readiness, dict) else None,
        workers=workers if isinstance(workers, list) else [],
        worker_probes_payload=probes if isinstance(probes, dict) else None,
        api_keys=api_keys if isinstance(api_keys, list) else None,
    )
    row = (tooling_status.get("rows") or [{}])[0]
    summary = _bot_test_preflight_summary(row)
    state = str(row.get("state") or "").strip().lower()
    if state in {"blocked", "disabled"}:
        return summary, (
            {
                "error": "bot test run blocked by tooling readiness",
                "tooling": summary,
                "blocking_messages": row.get("blocking_messages") or row.get("disabled_activation_messages") or [],
            },
            409,
        )
    return summary, None


def _bot_connections_payload(db, bot_ref: str) -> list[dict[str, Any]]:
    links = db.query(BotConnection).filter(BotConnection.bot_ref == str(bot_ref)).all()
    ids = [link.connection_id for link in links]
    if not ids:
        return []
    rows = db.query(Connection).filter(Connection.id.in_(ids)).order_by(Connection.name.asc()).all()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payloads.append(
            {
                "name": row.name,
                "kind": row.kind,
                "description": row.description or "",
                "config": mask_connection_config(_parse_json(row.config_json or "{}", {})),
                "auth": mask_auth_payload(_parse_json(row.auth_json or "{}", {})),
                "schema_text": row.schema_text or "",
                "enabled": bool(row.enabled),
            }
        )
    return payloads


def _connection_identity(name: Any, kind: Any) -> tuple[str, str]:
    return (str(name or "").strip().lower(), str(kind or "http").strip().lower())


def _cleanup_orphaned_connection(db, connection_id: int) -> None:
    has_bot_refs = db.query(BotConnection).filter(BotConnection.connection_id == connection_id).first()
    has_project_refs = db.query(ProjectConnection).filter(ProjectConnection.connection_id == connection_id).first()
    if has_bot_refs or has_project_refs:
        return
    row = db.get(Connection, connection_id)
    if row is not None:
        db.delete(row)


def _replace_bot_connections(db, bot_ref: str, connection_payloads: list[dict[str, Any]]) -> None:
    existing_links = db.query(BotConnection).filter(BotConnection.bot_ref == str(bot_ref)).all()
    existing_ids = [link.connection_id for link in existing_links]
    existing_rows = db.query(Connection).filter(Connection.id.in_(existing_ids)).all() if existing_ids else []
    existing_by_identity = {
        _connection_identity(row.name, row.kind): (
            _parse_json(row.config_json or "{}", {}),
            _parse_json(row.auth_json or "{}", {}),
        )
        for row in existing_rows
    }
    db.query(BotConnection).filter(BotConnection.bot_ref == str(bot_ref)).delete()
    db.flush()
    for connection_id in existing_ids:
        _cleanup_orphaned_connection(db, connection_id)

    now = datetime.now(timezone.utc)
    for payload in connection_payloads:
        if not isinstance(payload, dict):
            continue
        name = str(payload.get("name") or "").strip() or "Imported Connection"
        kind = str(payload.get("kind") or "http").strip().lower() or "http"
        existing_config, existing_auth = existing_by_identity.get(
            _connection_identity(name, kind), ({}, {})
        )
        row = Connection(
            name=name,
            kind=kind,
            description=str(payload.get("description") or ""),
            config_json=json.dumps(
                normalize_connection_config(
                    payload.get("config") if isinstance(payload.get("config"), dict) else {},
                    existing=existing_config if isinstance(existing_config, dict) else {},
                )
            ),
            auth_json=json.dumps(
                normalize_auth_payload(
                    payload.get("auth") if isinstance(payload.get("auth"), dict) else {},
                    existing=existing_auth if isinstance(existing_auth, dict) else {},
                )
            ),
            schema_text=str(payload.get("schema_text") or ""),
            enabled=bool(payload.get("enabled", True)),
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        db.flush()
        db.add(BotConnection(bot_ref=str(bot_ref), connection_id=row.id, created_at=now))


def _export_bundle(bot_payload: dict[str, Any], connections: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "nexusai.bot-export.v1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "bot": bot_payload,
        "connections": connections,
    }


def _cp_catalog_items(cp, method_name: str) -> list[dict[str, Any]]:
    method = getattr(cp, method_name, None)
    if not callable(method):
        return []
    try:
        result = method()
    except Exception:
        logger.exception("Unable to load bot creation catalog %s", method_name)
        return []
    return result if isinstance(result, list) else []


def _cp_warning(cp: Any, source: str) -> dict[str, Any]:
    reason = "Control plane request failed."
    detail = ""
    status_code = None
    last_error = getattr(cp, "last_error", None)
    if callable(last_error):
        try:
            error = last_error()
        except Exception:
            error = None
        if isinstance(error, dict):
            reason = str(error.get("error") or reason)
            detail = str(error.get("detail") or "")
            status_code = error.get("status_code")
    warning: dict[str, Any] = {"source": source, "reason": reason}
    if detail:
        warning["detail"] = detail
    if status_code is not None:
        warning["status_code"] = status_code
    return warning


def _optional_cp_payload(cp: Any, source: str, fn: Any, *args: Any, **kwargs: Any) -> tuple[Any, dict[str, Any] | None]:
    if not callable(fn):
        return None, {"source": source, "reason": "Control plane method unavailable."}
    try:
        result = fn(*args, **kwargs)
    except TypeError:
        try:
            result = fn(*args)
        except Exception:
            return None, _cp_warning(cp, source)
    except Exception:
        return None, _cp_warning(cp, source)
    if result is None:
        return None, _cp_warning(cp, source)
    return result, None


def _attach_tooling_data_warnings(status: dict[str, Any], warnings: list[dict[str, Any]]) -> dict[str, Any]:
    status["data_degraded"] = bool(warnings)
    status["data_warnings"] = warnings
    return status


def _bot_readiness_view(readiness: Any) -> dict[str, Any] | None:
    if not isinstance(readiness, dict):
        return None
    state = str(readiness.get("state") or "").strip().lower()
    failures = [
        str(check.get("message") or "").strip()
        for check in readiness.get("checks") or []
        if isinstance(check, dict) and str(check.get("status") or "").strip().lower() == "failed"
    ]
    ready = bool(readiness.get("ready"))
    if state not in {"ready", "blocked", "disabled"}:
        state = "ready" if ready else "blocked"
    if state == "disabled":
        detail = "This bot is disabled and will not receive dispatch."
    elif ready and failures:
        detail = f"Fallback dispatch is available; {len(failures)} backend check(s) are unavailable."
    elif ready:
        detail = "At least one declared backend is ready for dispatch."
    else:
        detail = failures[0] if failures else "No declared backend is ready for dispatch."
    return {"ready": ready, "state": state, "detail": detail, "failed": len(failures)}


def _bot_dependency_view(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    schedules = [item for item in payload.get("schedule_references") or [] if isinstance(item, dict)]
    workflows = [item for item in payload.get("workflow_references") or [] if isinstance(item, dict)]
    return {
        "schedule_references": schedules,
        "workflow_references": workflows,
        "can_disable": bool(payload.get("can_disable", not schedules and not workflows)),
        "can_delete": bool(payload.get("can_delete", not schedules and not workflows)),
    }


def _backend_route_labels(bot: dict[str, Any], *, limit: int = 4) -> list[str]:
    labels: list[str] = []
    for backend in bot.get("backends") or []:
        if not isinstance(backend, dict):
            continue
        backend_type = str(backend.get("type") or "").strip()
        provider = str(backend.get("provider") or "").strip()
        model = str(backend.get("model") or "").strip()
        worker_id = str(backend.get("worker_id") or "").strip()
        label = provider or backend_type or "backend"
        if model:
            label = f"{label} / {model}"
        if worker_id:
            label = f"{label} on {worker_id}"
        if label not in labels:
            labels.append(label)
        if len(labels) >= max(1, int(limit or 4)):
            break
    return labels


def _bot_detail_operating_summary(
    bot: dict[str, Any],
    readiness: Any,
    dependencies: dict[str, Any] | None,
) -> dict[str, Any]:
    schedules = (dependencies or {}).get("schedule_references") or []
    active_schedules = [
        schedule
        for schedule in schedules
        if isinstance(schedule, dict) and str(schedule.get("status") or "").strip().lower() == "active"
    ]
    paused_schedules = [
        schedule
        for schedule in schedules
        if isinstance(schedule, dict) and str(schedule.get("status") or "").strip().lower() == "paused"
    ]
    chat_profile = bot.get("chat_profile") if isinstance(bot.get("chat_profile"), dict) else _bot_chat_profile(bot)
    chat_tools = _bot_chat_tool_access(bot)
    tool_modes = [
        label
        for key, label in (("filesystem", "filesystem"), ("repo_search", "repo search"))
        if bool(chat_tools.get(key, False))
    ]
    readiness_view = _bot_readiness_view(readiness)
    readiness_state = readiness_view["state"] if readiness_view else "unknown"

    if not bool(bot.get("enabled", True)):
        dispatch_state = "disabled"
        next_action = "Enable the bot before assigning work."
    elif readiness_state == "blocked":
        dispatch_state = "blocked"
        next_action = str((readiness_view or {}).get("detail") or "Resolve readiness blockers before dispatch.")
    elif active_schedules:
        dispatch_state = "scheduled"
        next_action = f"{len(active_schedules)} active recurring schedule(s)."
    elif paused_schedules:
        dispatch_state = "paused"
        next_action = f"{len(paused_schedules)} paused schedule(s); resume only after confirming scope."
    else:
        dispatch_state = "manual"
        next_action = "No active recurring schedule; dispatch manually or add a scoped schedule."

    return {
        "dispatch_state": dispatch_state,
        "next_action": next_action,
        "active_schedule_count": len(active_schedules),
        "paused_schedule_count": len(paused_schedules),
        "readiness_state": readiness_state,
        "chat_profile_label": chat_profile.get("label") or chat_profile.get("mode") or "Chat",
        "backend_route_labels": _backend_route_labels(bot),
        "backend_route_count": len([backend for backend in bot.get("backends") or [] if isinstance(backend, dict)]),
        "chat_tool_label": (
            ", ".join(tool_modes)
            if bool(chat_tools.get("enabled", False)) and tool_modes
            else ("enabled" if bool(chat_tools.get("enabled", False)) else "off")
        ),
        "memory_enabled": bool(bot.get("memory_profiles_enabled", False)),
    }


def _bot_schedule_mode(schedule_payload: Any) -> dict[str, dict[str, Any]]:
    schedules = schedule_payload.get("schedules") if isinstance(schedule_payload, dict) else []
    schedule_refs: dict[str, list[dict[str, Any]]] = {}
    for schedule in schedules if isinstance(schedules, list) else []:
        if not isinstance(schedule, dict):
            continue
        for field in ("target_bot_id", "assignment_pm_bot_id"):
            bot_id = str(schedule.get(field) or "").strip()
            if bot_id:
                schedule_refs.setdefault(bot_id, []).append(schedule)

    modes: dict[str, dict[str, Any]] = {}
    for bot_id, references in schedule_refs.items():
        active_count = sum(
            1 for schedule in references if str(schedule.get("status") or "").strip().lower() == "active"
        )
        paused_count = sum(
            1 for schedule in references if str(schedule.get("status") or "").strip().lower() == "paused"
        )
        if active_count:
            modes[bot_id] = {
                "state": "scheduled",
                "detail": f"{active_count} active recurring schedule(s).",
                "active_schedule_count": active_count,
                "paused_schedule_count": paused_count,
            }
        elif paused_count:
            modes[bot_id] = {
                "state": "paused",
                "detail": f"{paused_count} paused schedule(s); no recurring dispatch is active.",
                "active_schedule_count": 0,
                "paused_schedule_count": paused_count,
            }
    return modes


def _with_bot_operating_mode(
    bots: list[dict[str, Any]],
    readiness_payload: Any,
    schedule_payload: Any,
) -> list[dict[str, Any]]:
    raw_readiness = readiness_payload.get("readiness") if isinstance(readiness_payload, dict) else []
    readiness_by_id = {
        str(item.get("bot_id") or "").strip(): item
        for item in raw_readiness
        if isinstance(item, dict) and str(item.get("bot_id") or "").strip()
    }
    schedule_mode_by_bot_id = _bot_schedule_mode(schedule_payload)
    enriched: list[dict[str, Any]] = []
    for bot in bots:
        row = dict(bot)
        bot_id = str(row.get("id") or "").strip()
        row["readiness"] = _bot_readiness_view(readiness_by_id.get(bot_id))
        if not bool(row.get("enabled")):
            row["operating_mode"] = {
                "state": "disabled",
                "detail": "This bot is disabled and cannot receive dispatch.",
                "active_schedule_count": 0,
                "paused_schedule_count": 0,
            }
        else:
            row["operating_mode"] = schedule_mode_by_bot_id.get(
                bot_id,
                {
                    "state": "manual",
                    "detail": "No recurring schedule is active; this bot runs only through an explicit task or workflow trigger.",
                    "active_schedule_count": 0,
                    "paused_schedule_count": 0,
                },
            )
        enriched.append(row)
    return enriched


@bp.get("/bots")
@login_required
def bots_page() -> str:
    """Render the bots table page."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    cp_data = cp.list_bots()
    if cp_data is not None:
        warnings: list[dict[str, Any]] = []
        readiness_payload, warning = _optional_cp_payload(cp, "bot readiness", getattr(cp, "list_bot_readiness", None))
        if warning:
            warnings.append(warning)
        schedule_payload, warning = _optional_cp_payload(cp, "schedules", getattr(cp, "list_schedules", None), limit=200)
        if warning:
            warnings.append(warning)
        workers_payload, warning = _optional_cp_payload(cp, "workers", getattr(cp, "list_workers", None))
        if warning:
            warnings.append(warning)
        workers = workers_payload if isinstance(workers_payload, list) else []
        worker_probes, warning = _optional_cp_payload(cp, "worker probes", getattr(cp, "list_worker_probes", None))
        if warning:
            warnings.append(warning)
        api_keys_payload, warning = _optional_cp_payload(cp, "API keys", getattr(cp, "list_keys", None))
        if warning:
            warnings.append(warning)
        api_keys = api_keys_payload if isinstance(api_keys_payload, list) else None
        tooling_status = build_bot_tooling_status(
            bots=cp_data,
            readiness_payload=readiness_payload if isinstance(readiness_payload, dict) else None,
            workers=workers,
            worker_probes_payload=worker_probes if isinstance(worker_probes, dict) else None,
            api_keys=api_keys,
        )
        _attach_tooling_data_warnings(tooling_status, warnings)
        return render_template(
            "bots.html",
            bots=_with_bot_chat_profiles(_with_bot_operating_mode(cp_data, readiness_payload, schedule_payload)),
            tooling_status=tooling_status,
            workers=workers,
            models=_cp_catalog_items(cp, "list_models"),
            api_keys=api_keys or [],
            projects=_cp_catalog_items(cp, "list_projects"),
            error=None,
        )

    flash(get_cp_client().unavailable_reason(), "warning")
    db = get_db()
    try:
        bots = db.query(Bot).order_by(Bot.priority).all()
        bot_rows = [_bot_to_dict(b) for b in bots]
        return render_template(
            "bots.html",
            bots=bot_rows,
            tooling_status=build_bot_tooling_status(
                bots=bot_rows,
                readiness_payload=None,
                workers=[],
                worker_probes_payload=None,
                api_keys=[],
            ),
            workers=[],
            models=[],
            api_keys=[],
            projects=[],
            error=None,
        )
    finally:
        db.close()


@bp.get("/bots/<bot_id>")
@login_required
def bot_detail_page(bot_id: str):
    """Render a bot detail page with backend chain and task board columns."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    cp_bot = cp.get_bot(bot_id)
    readiness_getter = getattr(cp, "get_bot_readiness", None)
    cp_readiness = readiness_getter(bot_id) if callable(readiness_getter) else None
    dependency_getter = getattr(cp, "get_bot_dependencies", None)
    cp_dependencies = _bot_dependency_view(dependency_getter(bot_id) if callable(dependency_getter) else None)
    cp_tasks = _cp_list_tasks_safe(cp, bot_id=bot_id, limit=300, include_content=False)
    cp_runs = cp.list_bot_runs(bot_id) or []
    cp_artifacts = cp.list_bot_artifacts(bot_id, limit=300, include_content=False) or []
    cp_workers = cp.list_workers() or []
    worker_probes_getter = getattr(cp, "list_worker_probes", None)
    cp_worker_probes = worker_probes_getter() if callable(worker_probes_getter) else None
    cp_models = cp.list_models() or []
    cp_keys_payload = cp.list_keys()
    cp_keys = cp_keys_payload if isinstance(cp_keys_payload, list) else []

    if cp_bot is not None:
        tasks = [t for t in (cp_tasks or []) if str(t.get("bot_id")) == str(bot_id)]
        raw_bot_payload = _with_bot_chat_profiles([cp_bot])[0]
        bot_payload = _sanitize_bot_for_detail(raw_bot_payload)
        tooling_status = build_bot_tooling_status(
            bots=[raw_bot_payload],
            readiness_payload={"readiness": [cp_readiness]} if isinstance(cp_readiness, dict) else None,
            workers=cp_workers,
            worker_probes_payload=cp_worker_probes if isinstance(cp_worker_probes, dict) else None,
            api_keys=cp_keys_payload if isinstance(cp_keys_payload, list) else None,
        )
        tooling_row = (tooling_status.get("rows") or [{}])[0]
        return render_template(
            "bot_detail.html",
            bot=bot_payload,
            tasks=tasks,
            runs=cp_runs,
            artifacts=cp_artifacts,
            workers=cp_workers,
            models=cp_models,
            api_keys=cp_keys,
            readiness=cp_readiness,
            tooling_row=tooling_row,
            bot_dependencies=cp_dependencies,
            operating_summary=_bot_detail_operating_summary(raw_bot_payload, cp_readiness, cp_dependencies),
            error=None,
        )

    db = get_db()
    try:
        # Fallback local bot IDs are integer PKs.
        if not str(bot_id).isdigit():
            return render_template("bot_detail.html", bot=None, tasks=[], bot_dependencies=None, error="Bot not found")
        bot = db.get(Bot, int(bot_id))
        if not bot:
            return render_template("bot_detail.html", bot=None, tasks=[], bot_dependencies=None, error="Bot not found")
        local_tasks = db.query(Task).filter_by(bot_id=bot.id).all()
        tasks = []
        for t in local_tasks:
            tasks.append(
                {
                    "id": t.id,
                    "bot_id": t.bot_id,
                    "status": t.status,
                    "has_payload": bool(t.payload),
                    "has_result": bool(t.result),
                    "has_error": bool(t.error),
                    "created_at": t.created_at.isoformat() if t.created_at else "",
                    "updated_at": t.updated_at.isoformat() if t.updated_at else "",
                }
            )
        bot_payload = _bot_to_dict(bot)
        safe_bot_payload = _sanitize_bot_for_detail(bot_payload)
        return render_template(
            "bot_detail.html",
            bot=safe_bot_payload,
            tasks=tasks,
            runs=[],
            artifacts=[],
            workers=[],
            models=[],
            api_keys=[],
            readiness=None,
            tooling_row=(build_bot_tooling_status(
                bots=[bot_payload],
                readiness_payload=None,
                workers=[],
                worker_probes_payload=None,
                api_keys=[],
            ).get("rows") or [{}])[0],
            bot_dependencies=None,
            operating_summary=_bot_detail_operating_summary(bot_payload, None, None),
            error=None,
        )
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/bots")
@login_required
def api_list_bots():
    """List all bots as JSON."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    cp_bots = cp.list_bots()
    if cp_bots is not None:
        return jsonify(cp_bots)

    db = get_db()
    try:
        bots = db.query(Bot).order_by(Bot.priority).all()
        return jsonify([_bot_to_dict(b) for b in bots])
    finally:
        db.close()


@bp.get("/api/bots/tooling-status")
@login_required
def api_bot_tooling_status():
    """Return a bounded operator summary of bot readiness and required tools."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    bots = cp.list_bots()
    if bots is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "control plane unavailable")}), status

    readiness_getter = getattr(cp, "list_bot_readiness", None)
    warnings: list[dict[str, Any]] = []
    readiness_payload, warning = _optional_cp_payload(cp, "bot readiness", readiness_getter)
    if warning:
        warnings.append(warning)
    workers_payload, warning = _optional_cp_payload(cp, "workers", getattr(cp, "list_workers", None))
    if warning:
        warnings.append(warning)
    workers = workers_payload if isinstance(workers_payload, list) else []
    worker_probes, warning = _optional_cp_payload(cp, "worker probes", getattr(cp, "list_worker_probes", None))
    if warning:
        warnings.append(warning)
    api_keys, warning = _optional_cp_payload(cp, "API keys", getattr(cp, "list_keys", None))
    if warning:
        warnings.append(warning)
    return jsonify(
        _attach_tooling_data_warnings(
            build_bot_tooling_status(
                bots=bots,
                readiness_payload=readiness_payload if isinstance(readiness_payload, dict) else None,
                workers=workers,
                worker_probes_payload=worker_probes if isinstance(worker_probes, dict) else None,
                api_keys=api_keys if isinstance(api_keys, list) else None,
            ),
            warnings,
        )
    )


@bp.post("/api/bots")
@login_required
def api_create_bot():
    """Create a new bot."""
    from dashboard.cp_client import get_cp_client

    data: dict[str, Any] = request.get_json(force=True) or {}
    if not data.get("name"):
        return jsonify({"error": "name is required"}), 400
    cp = get_cp_client()
    cp_bots = cp.list_bots()
    if cp_bots is not None:
        requested_id = str(data.get("id") or "").strip()
        bot_id = requested_id or _slugify_bot_id(str(data["name"]))
        existing_ids = {str(b.get("id")) for b in cp_bots if isinstance(b, dict)}
        if bot_id in existing_ids and not requested_id:
            base = bot_id
            suffix = 2
            while f"{base}-{suffix}" in existing_ids:
                suffix += 1
            bot_id = f"{base}-{suffix}"
        created = cp.create_bot(
            {
                "id": bot_id,
                "name": data["name"],
                "role": data.get("role", "") or "assistant",
                "priority": int(data.get("priority", 0)),
                "enabled": bool(data.get("enabled", True)),
                "memory_profiles_enabled": bool(data.get("memory_profiles_enabled", False)),
                "system_prompt": data.get("system_prompt"),
                "backends": data.get("backends", []),
                "assignment_capabilities": data.get("assignment_capabilities"),
                "execution_policy": data.get("execution_policy"),
                "routing_rules": _merge_routing_rules(data),
                "workflow": data.get("workflow"),
            }
        )
        if created is None:
            err = cp.last_error()
            detail = str((err or {}).get("detail") or "create failed")
            status = int((err or {}).get("status_code") or 502)
            if status < 400 or status > 599:
                status = 502
            return jsonify({"error": detail}), status
        return jsonify(created), 201
    db = get_db()
    try:
        bot = Bot(
            name=data["name"],
            role=data.get("role", ""),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            backends=json.dumps(data.get("backends", [])),
            routing_rules=json.dumps(_merge_routing_rules(data)),
        )
        db.add(bot)
        db.commit()
        db.refresh(bot)
        return jsonify(_bot_to_dict(bot)), 201
    finally:
        db.close()


@bp.get("/api/bot-blueprints")
@login_required
def api_list_bot_blueprints():
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    data = cp.list_bot_blueprints()
    if data is None:
        err = cp.last_error()
        detail = str((err or {}).get("detail") or "failed to load specialist catalog")
        status = int((err or {}).get("status_code") or 502)
        return jsonify({"error": detail}), status if 400 <= status <= 599 else 502
    return jsonify(data)


@bp.post("/api/bot-blueprints/preview")
@login_required
def api_preview_bot_blueprint():
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    data = cp.preview_bot_blueprint(request.get_json(silent=True) or {})
    if data is None:
        err = cp.last_error()
        detail = str((err or {}).get("detail") or "failed to preview specialist bot")
        status = int((err or {}).get("status_code") or 502)
        return jsonify({"error": detail}), status if 400 <= status <= 599 else 502
    return jsonify(data)


@bp.post("/api/bot-blueprints/preflight")
@login_required
def api_preflight_bot_blueprint():
    """Build and validate a specialist before it can be enabled or registered."""
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    preview = cp.preview_bot_blueprint(request.get_json(silent=True) or {})
    if preview is None or not isinstance(preview.get("bot"), dict):
        error, status = _cp_error_payload(cp, "failed to generate specialist configuration")
        return jsonify(error), status

    preflight = cp.preflight_bot_blueprint(preview["bot"])
    if preflight is None:
        error, status = _cp_error_payload(cp, "specialist preflight failed")
        return jsonify(error), status
    return jsonify({"bot": preview["bot"], "preflight": preflight})


@bp.post("/api/bot-blueprints/create")
@login_required
def api_create_bot_blueprint():
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    data = cp.create_bot_blueprint(request.get_json(silent=True) or {})
    if data is None:
        error, status = _cp_error_payload(cp, "failed to create specialist bot")
        return jsonify(error), status
    return jsonify(data), 201


@bp.get("/api/bots/<bot_id>")
@login_required
def api_get_bot(bot_id: str):
    """Get a single bot by ID."""
    from dashboard.cp_client import get_cp_client
    cp_bot = get_cp_client().get_bot(bot_id)
    if cp_bot is not None:
        return jsonify(cp_bot)
    db = get_db()
    try:
        if not str(bot_id).isdigit():
            return jsonify({"error": "not found"}), 404
        bot = db.get(Bot, int(bot_id))
        if not bot:
            return jsonify({"error": "not found"}), 404
        return jsonify(_bot_to_dict(bot))
    finally:
        db.close()


@bp.get("/api/bots/<bot_id>/export")
@login_required
def api_export_bot(bot_id: str):
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    bot_payload = cp.get_bot(bot_id)
    if bot_payload is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "bot export requires control plane access")}), status

    db = get_db()
    try:
        bundle = _export_bundle(bot_payload, _bot_connections_payload(db, str(bot_id)))
    finally:
        db.close()

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(bot_payload.get("id") or bot_id)).strip("._") or "bot"
    return send_file(
        io.BytesIO(json.dumps(bundle, indent=2, sort_keys=True).encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"{safe_name}.bot.json",
    )


@bp.post("/api/bots/import")
@login_required
def api_import_bot():
    from dashboard.cp_client import get_cp_client

    body: dict[str, Any] = request.get_json(force=True) or {}
    bundle = body.get("bundle") if isinstance(body.get("bundle"), dict) else body
    bot_payload = bundle.get("bot") if isinstance(bundle, dict) and isinstance(bundle.get("bot"), dict) else None
    if bot_payload is None:
        return jsonify({"error": "import bundle must include a bot object"}), 400

    bot_id = str(bot_payload.get("id") or "").strip()
    bot_name = str(bot_payload.get("name") or "").strip()
    if not bot_id or not bot_name:
        return jsonify({"error": "imported bot must include id and name"}), 400

    overwrite = bool(body.get("overwrite", False))
    cp = get_cp_client()
    existing = cp.get_bot(bot_id)
    if existing is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status not in {404} and (status < 400 or status > 599):
            status = 502
        if status not in {404}:
            return jsonify({"error": str((err or {}).get("detail") or "bot import requires control plane access")}), status

    if existing is not None and not overwrite:
        return jsonify({"error": "bot id already exists", "reason_code": "bot_id_conflict", "bot_id": bot_id}), 409

    import_payload = {
        "id": bot_id,
        "name": bot_name,
        "role": bot_payload.get("role", "") or "assistant",
        "project_id": bot_payload.get("project_id"),
        "priority": int(bot_payload.get("priority", 0) or 0),
        "enabled": bool(bot_payload.get("enabled", True)),
        "system_prompt": bot_payload.get("system_prompt"),
        "backends": bot_payload.get("backends", []),
        "context_access": bot_payload.get("context_access"),
        "assignment_capabilities": bot_payload.get("assignment_capabilities"),
        "execution_policy": bot_payload.get("execution_policy"),
        "routing_rules": _merge_routing_rules(bot_payload, existing=bot_payload.get("routing_rules")),
        "workflow": bot_payload.get("workflow"),
    }

    preflight = cp.preflight_bot(import_payload)
    if preflight is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "bot import preflight failed")}), status
    if bool(import_payload["enabled"]) and not bool(preflight.get("ready_to_enable")):
        return jsonify(
            {
                "error": f"Bot '{bot_id}' is not ready to enable. Import it disabled or correct the reported worker/backend blockers.",
                "reason_code": "bot_not_ready",
                "readiness": preflight.get("readiness"),
            }
        ), 409

    if existing is not None:
        saved = cp.update_bot(bot_id, import_payload)
    else:
        saved = cp.create_bot(import_payload)
    if saved is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "import failed")}), status

    db = get_db()
    try:
        connections = bundle.get("connections") if isinstance(bundle.get("connections"), list) else []
        _replace_bot_connections(db, str(bot_id), connections)
        db.commit()
    finally:
        db.close()

    return jsonify(
        {
            "ok": True,
            "bot": saved,
            "overwritten": existing is not None,
            "connection_count": len(bundle.get("connections") if isinstance(bundle.get("connections"), list) else []),
        }
    )


@bp.put("/api/bots/<bot_id>")
@login_required
def api_update_bot(bot_id: str):
    """Update an existing bot."""
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    data: dict[str, Any] = request.get_json(force=True) or {}
    cp_bot = cp.get_bot(bot_id)
    if cp_bot is not None:
        merged = dict(cp_bot)
        merged.update(data)
        updated = cp.update_bot(bot_id, merged)
        if updated is None:
            err = cp.last_error()
            status = int(err.get("status_code") or 502)
            if status < 400 or status > 599:
                status = 502
            raw_detail = err.get("detail") or "control plane update failed"
            try:
                detail = json.loads(raw_detail) if isinstance(raw_detail, str) else raw_detail
            except (TypeError, json.JSONDecodeError):
                detail = raw_detail
            message = detail.get("detail", {}).get("message") if isinstance(detail, dict) else None
            if not message and isinstance(detail, dict):
                message = detail.get("message")
            return jsonify({"error": message or str(raw_detail), "detail": detail}), status
        return jsonify(updated)

    db = get_db()
    try:
        if not str(bot_id).isdigit():
            return jsonify({"error": "not found"}), 404
        bot = db.get(Bot, int(bot_id))
        if not bot:
            return jsonify({"error": "not found"}), 404
        for field in ("name", "role"):
            if field in data:
                setattr(bot, field, data[field])
        if "priority" in data:
            bot.priority = int(data["priority"])
        if "enabled" in data:
            bot.enabled = bool(data["enabled"])
        if "memory_profiles_enabled" in data and hasattr(bot, "memory_profiles_enabled"):
            bot.memory_profiles_enabled = bool(data["memory_profiles_enabled"])
        if "backends" in data:
            bot.backends = json.dumps(data["backends"])
        if "routing_rules" in data or "workflow" in data:
            bot.routing_rules = json.dumps(_merge_routing_rules(data, existing=bot.routing_rules))
        db.commit()
        db.refresh(bot)
        return jsonify(_bot_to_dict(bot))
    finally:
        db.close()


@bp.delete("/api/bots/<bot_id>")
@login_required
def api_delete_bot(bot_id: str):
    """Delete a bot."""
    from dashboard.cp_client import get_cp_client
    cp = get_cp_client()
    cp_bot = cp.get_bot(bot_id)
    if cp_bot is not None:
        ok = cp.delete_bot(bot_id)
        if not ok:
            error, status = _cp_error_payload(cp, "bot deletion failed")
            return jsonify(error), status
        return "", 204

    db = get_db()
    try:
        if not str(bot_id).isdigit():
            return jsonify({"error": "not found"}), 404
        bot = db.get(Bot, int(bot_id))
        if not bot:
            return jsonify({"error": "not found"}), 404
        db.delete(bot)
        db.commit()
        return "", 204
    finally:
        db.close()


@bp.post("/api/bots/<bot_id>/test-run")
@login_required
def api_test_run_bot(bot_id: str):
    from dashboard.cp_client import get_cp_client

    data: dict[str, Any] = request.get_json(force=True) or {}
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return jsonify({"error": "payload object is required"}), 400

    cp = get_cp_client()
    preflight, preflight_error = _bot_test_preflight(cp, bot_id)
    if preflight_error:
        body, status = preflight_error
        return jsonify(body), status
    metadata = {
        "source": "bot_test",
        "execution_mode": "test",
        "project_id": data.get("project_id"),
        "conversation_id": data.get("conversation_id"),
        "priority": data.get("priority"),
    }
    if preflight:
        metadata["tooling_preflight"] = preflight
    task = cp.create_task_full(
        bot_id=bot_id,
        payload=payload,
        metadata=metadata,
    )
    if task is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "control plane unavailable")}), status
    return jsonify(task), 201


@bp.post("/api/bots/<bot_id>/launch")
@login_required
def api_launch_bot(bot_id: str):
    from dashboard.bot_launch import normalize_launch_payload, normalize_launch_profile
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    bot = cp.get_bot(bot_id)
    if bot is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "bot not found")}), status

    launch_profile = normalize_launch_profile(bot)
    if launch_profile is None:
        return jsonify({"error": "bot does not have a saved launch profile"}), 400

    data: dict[str, Any] = request.get_json(silent=True) or {}
    payload = data.get("payload")
    if payload is None:
        payload = launch_profile["payload"]
    if not isinstance(payload, dict):
        return jsonify({"error": "launch payload must be a JSON object"}), 400
    payload = normalize_launch_payload(bot, payload)

    metadata = {
        "source": "saved_launch_pipeline" if launch_profile.get("is_pipeline") else "saved_launch",
        "project_id": data.get("project_id", launch_profile.get("project_id")),
        "priority": data.get("priority", launch_profile.get("priority")),
    }
    orchestration_id = None
    if launch_profile.get("is_pipeline"):
        orchestration_id = str(uuid.uuid4())
        metadata["orchestration_id"] = orchestration_id
        metadata["pipeline_name"] = str(launch_profile.get("pipeline_name") or launch_profile.get("label") or bot.get("name") or bot_id).strip()
        metadata["pipeline_entry_bot_id"] = str(bot_id)
        concurrency_limit = launch_profile.get("concurrency_limit")
        if concurrency_limit is not None:
            metadata["orchestration_concurrency_limit"] = concurrency_limit
    task = cp.create_task_full(
        bot_id=bot_id,
        payload=payload,
        metadata=metadata,
    )
    if task is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "launch failed")}), status
    response_body = dict(task)
    if orchestration_id:
        response_body["pipeline_id"] = orchestration_id
        response_body["pipeline_name"] = metadata.get("pipeline_name")
    return jsonify(response_body), 201


@bp.get("/api/bots/<bot_id>/artifacts/<artifact_id>")
@login_required
def api_get_bot_artifact(bot_id: str, artifact_id: str):
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    artifact = cp.get_bot_artifact(bot_id, artifact_id)
    if artifact is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "artifact not found")}), status
    return jsonify(artifact)


@bp.get("/api/bots/<bot_id>/artifacts/<artifact_id>/download")
@login_required
def api_download_bot_artifact(bot_id: str, artifact_id: str):
    from dashboard.cp_client import get_cp_client

    cp = get_cp_client()
    artifact = cp.get_bot_artifact(bot_id, artifact_id)
    if artifact is None:
        err = cp.last_error()
        status = int((err or {}).get("status_code") or 502)
        if status < 400 or status > 599:
            status = 502
        return jsonify({"error": str((err or {}).get("detail") or "artifact not found")}), status

    content = artifact.get("content")
    if content is None:
        content = ""
    filename_label = str(artifact.get("label") or artifact_id).strip() or artifact_id
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename_label).strip("._") or "artifact"
    ext = ".json" if str(artifact.get("kind") or "") in {"payload", "result", "error"} else ".txt"
    return send_file(
        io.BytesIO(str(content).encode("utf-8")),
        mimetype="text/plain; charset=utf-8",
        as_attachment=True,
        download_name=f"{safe_name}{ext}",
    )
