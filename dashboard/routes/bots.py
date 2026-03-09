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

from dashboard.connections_service import normalize_auth_payload, resolve_auth_payload
from dashboard.db import get_db
from dashboard.models import Bot, BotConnection, Connection, ProjectConnection, Task

logger = logging.getLogger(__name__)

bp = Blueprint("bots", __name__)


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
    if "launch_profile" in data:
        merged["launch_profile"] = data.get("launch_profile")
    return merged


def _bot_to_dict(b: Bot) -> dict[str, Any]:
    """Serialise a Bot ORM row to a plain dict."""
    routing_rules = json.loads(b.routing_rules) if b.routing_rules else {}
    return {
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
        "launch_profile": routing_rules.get("launch_profile") if isinstance(routing_rules, dict) else None,
    }


def _parse_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


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
                "config": _parse_json(row.config_json or "{}", {}),
                "auth": resolve_auth_payload(_parse_json(row.auth_json or "{}", {})),
                "schema_text": row.schema_text or "",
                "enabled": bool(row.enabled),
            }
        )
    return payloads


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
    db.query(BotConnection).filter(BotConnection.bot_ref == str(bot_ref)).delete()
    db.flush()
    for connection_id in existing_ids:
        _cleanup_orphaned_connection(db, connection_id)

    now = datetime.now(timezone.utc)
    for payload in connection_payloads:
        if not isinstance(payload, dict):
            continue
        row = Connection(
            name=str(payload.get("name") or "").strip() or "Imported Connection",
            kind=str(payload.get("kind") or "http").strip().lower() or "http",
            description=str(payload.get("description") or ""),
            config_json=json.dumps(payload.get("config") if isinstance(payload.get("config"), dict) else {}),
            auth_json=json.dumps(
                normalize_auth_payload(payload.get("auth") if isinstance(payload.get("auth"), dict) else {})
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


@bp.get("/bots")
@login_required
def bots_page() -> str:
    """Render the bots table page."""
    from dashboard.cp_client import get_cp_client

    cp_data = get_cp_client().list_bots()
    if cp_data is not None:
        return render_template("bots.html", bots=cp_data, error=None)

    flash(get_cp_client().unavailable_reason(), "warning")
    db = get_db()
    try:
        bots = db.query(Bot).order_by(Bot.priority).all()
        return render_template(
            "bots.html",
            bots=[_bot_to_dict(b) for b in bots],
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
    cp_tasks = cp.list_tasks()
    cp_runs = cp.list_bot_runs(bot_id) or []
    cp_artifacts = cp.list_bot_artifacts(bot_id, limit=300, include_content=False) or []
    cp_workers = cp.list_workers() or []
    cp_models = cp.list_models() or []
    cp_keys = cp.list_keys() or []

    if cp_bot is not None and cp_tasks is not None:
        tasks = [t for t in cp_tasks if str(t.get("bot_id")) == str(bot_id)]
        return render_template(
            "bot_detail.html",
            bot=cp_bot,
            tasks=tasks,
            runs=cp_runs,
            artifacts=cp_artifacts,
            workers=cp_workers,
            models=cp_models,
            api_keys=cp_keys,
            error=None,
        )

    db = get_db()
    try:
        # Fallback local bot IDs are integer PKs.
        if not str(bot_id).isdigit():
            return render_template("bot_detail.html", bot=None, tasks=[], error="Bot not found")
        bot = db.get(Bot, int(bot_id))
        if not bot:
            return render_template("bot_detail.html", bot=None, tasks=[], error="Bot not found")
        local_tasks = db.query(Task).filter_by(bot_id=bot.id).all()
        tasks = []
        for t in local_tasks:
            tasks.append(
                {
                    "id": t.id,
                    "bot_id": t.bot_id,
                    "status": t.status,
                    "payload": t.payload_as_dict(),
                    "result": json.loads(t.result) if t.result else None,
                    "error": json.loads(t.error) if t.error else None,
                    "created_at": t.created_at.isoformat() if t.created_at else "",
                    "updated_at": t.updated_at.isoformat() if t.updated_at else "",
                }
            )
        return render_template(
            "bot_detail.html",
            bot=_bot_to_dict(bot),
            tasks=tasks,
            runs=[],
            artifacts=[],
            workers=[],
            models=[],
            api_keys=[],
            error=None,
        )
    finally:
        db.close()


# ── API ────────────────────────────────────────────────────────────────────────

@bp.get("/api/bots")
@login_required
def api_list_bots():
    """List all bots as JSON."""
    db = get_db()
    try:
        bots = db.query(Bot).order_by(Bot.priority).all()
        return jsonify([_bot_to_dict(b) for b in bots])
    finally:
        db.close()


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
                "system_prompt": data.get("system_prompt"),
                "backends": data.get("backends", []),
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
        return jsonify({"error": "bot id already exists", "bot_id": bot_id}), 409

    import_payload = {
        "id": bot_id,
        "name": bot_name,
        "role": bot_payload.get("role", "") or "assistant",
        "priority": int(bot_payload.get("priority", 0) or 0),
        "enabled": bool(bot_payload.get("enabled", True)),
        "system_prompt": bot_payload.get("system_prompt"),
        "backends": bot_payload.get("backends", []),
        "routing_rules": _merge_routing_rules(bot_payload, existing=bot_payload.get("routing_rules")),
        "workflow": bot_payload.get("workflow"),
    }

    saved = cp.update_bot(bot_id, import_payload) if existing is not None else cp.create_bot(import_payload)
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
            return jsonify({"error": "control plane unavailable"}), 502
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
            return jsonify({"error": "delete failed"}), 502
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
    task = cp.create_task_full(
        bot_id=bot_id,
        payload=payload,
        metadata={
            "source": "bot_test",
            "project_id": data.get("project_id"),
            "conversation_id": data.get("conversation_id"),
            "priority": data.get("priority"),
        },
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
    from dashboard.bot_launch import normalize_launch_profile
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
