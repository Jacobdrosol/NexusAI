"""Bot-scoped external connections (HTTP/OpenAPI + DB) management routes."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.connections_service import (
    mask_auth_payload,
    normalize_auth_payload,
    parse_openapi_actions,
    resolve_auth_payload,
    test_database_connection,
    test_http_connection,
)
from dashboard.db import get_db
from dashboard.models import BotConnection, Connection

bp = Blueprint("connections", __name__)


def _parse_json(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return default


def _connection_to_dict(c: Connection, *, include_auth: bool = False) -> dict[str, Any]:
    cfg = _parse_json(c.config_json or "{}", {})
    auth = _parse_json(c.auth_json or "{}", {})
    return {
        "id": c.id,
        "name": c.name,
        "kind": c.kind,
        "description": c.description or "",
        "config": cfg if isinstance(cfg, dict) else {},
        "auth": resolve_auth_payload(auth) if include_auth else mask_auth_payload(auth if isinstance(auth, dict) else {}),
        "schema_text": c.schema_text or "",
        "actions": parse_openapi_actions(c.schema_text or "") if c.kind == "http" else [],
        "enabled": bool(c.enabled),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _bot_connections(db, bot_ref: str) -> list[dict[str, Any]]:
    links = db.query(BotConnection).filter(BotConnection.bot_ref == bot_ref).all()
    ids = [l.connection_id for l in links]
    if not ids:
        return []
    rows = db.query(Connection).filter(Connection.id.in_(ids)).order_by(Connection.name.asc()).all()
    return [_connection_to_dict(r) for r in rows]


@bp.get("/bots/<bot_id>/connections")
@login_required
def bot_connections_page(bot_id: str):
    db = get_db()
    try:
        return render_template("bot_connections.html", bot_id=str(bot_id), connections=_bot_connections(db, str(bot_id)))
    finally:
        db.close()


@bp.get("/api/bots/<bot_id>/connections")
@login_required
def list_bot_connections(bot_id: str):
    db = get_db()
    try:
        return jsonify(_bot_connections(db, str(bot_id)))
    finally:
        db.close()


@bp.post("/api/bots/<bot_id>/connections")
@login_required
def create_bot_connection(bot_id: str):
    body: dict[str, Any] = request.get_json(force=True) or {}
    name = str(body.get("name") or "").strip()
    kind = str(body.get("kind") or "http").strip().lower()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if kind not in {"http", "database"}:
        return jsonify({"error": "kind must be http or database"}), 400
    config = body.get("config") if isinstance(body.get("config"), dict) else {}
    auth_in = body.get("auth") if isinstance(body.get("auth"), dict) else {}
    schema_text = str(body.get("schema_text") or "")

    db = get_db()
    try:
        auth = normalize_auth_payload(auth_in)
        row = Connection(
            name=name,
            kind=kind,
            description=str(body.get("description") or ""),
            config_json=json.dumps(config),
            auth_json=json.dumps(auth),
            schema_text=schema_text,
            enabled=bool(body.get("enabled", True)),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        db.add(BotConnection(bot_ref=str(bot_id), connection_id=row.id, created_at=datetime.now(timezone.utc)))
        db.commit()
        return jsonify(_connection_to_dict(row)), 201
    finally:
        db.close()


@bp.put("/api/connections/<int:connection_id>")
@login_required
def update_connection(connection_id: int):
    body: dict[str, Any] = request.get_json(force=True) or {}
    db = get_db()
    try:
        row = db.get(Connection, connection_id)
        if not row:
            return jsonify({"error": "not found"}), 404
        if "name" in body:
            row.name = str(body.get("name") or "").strip() or row.name
        if "kind" in body:
            k = str(body.get("kind") or "").strip().lower()
            if k not in {"http", "database"}:
                return jsonify({"error": "kind must be http or database"}), 400
            row.kind = k
        if "description" in body:
            row.description = str(body.get("description") or "")
        if "enabled" in body:
            row.enabled = bool(body.get("enabled"))
        if "config" in body and isinstance(body.get("config"), dict):
            row.config_json = json.dumps(body["config"])
        if "schema_text" in body:
            row.schema_text = str(body.get("schema_text") or "")
        if "auth" in body and isinstance(body.get("auth"), dict):
            existing = _parse_json(row.auth_json or "{}", {})
            auth = normalize_auth_payload(body["auth"], existing=existing if isinstance(existing, dict) else {})
            row.auth_json = json.dumps(auth)
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(row)
        return jsonify(_connection_to_dict(row))
    finally:
        db.close()


@bp.delete("/api/connections/<int:connection_id>")
@login_required
def delete_connection(connection_id: int):
    db = get_db()
    try:
        row = db.get(Connection, connection_id)
        if not row:
            return jsonify({"error": "not found"}), 404
        db.query(BotConnection).filter(BotConnection.connection_id == connection_id).delete()
        db.delete(row)
        db.commit()
        return "", 204
    finally:
        db.close()


@bp.post("/api/bots/<bot_id>/connections/<int:connection_id>/attach")
@login_required
def attach_connection(bot_id: str, connection_id: int):
    db = get_db()
    try:
        exists = (
            db.query(BotConnection)
            .filter(BotConnection.bot_ref == str(bot_id), BotConnection.connection_id == connection_id)
            .first()
        )
        if not exists:
            db.add(BotConnection(bot_ref=str(bot_id), connection_id=connection_id, created_at=datetime.now(timezone.utc)))
            db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@bp.delete("/api/bots/<bot_id>/connections/<int:connection_id>/attach")
@login_required
def detach_connection(bot_id: str, connection_id: int):
    db = get_db()
    try:
        db.query(BotConnection).filter(
            BotConnection.bot_ref == str(bot_id), BotConnection.connection_id == connection_id
        ).delete()
        db.commit()
        return "", 204
    finally:
        db.close()


@bp.post("/api/connections/parse-openapi")
@login_required
def parse_openapi():
    body: dict[str, Any] = request.get_json(force=True) or {}
    schema_text = str(body.get("schema_text") or "")
    return jsonify({"actions": parse_openapi_actions(schema_text)})


@bp.get("/api/connections/<int:connection_id>/actions")
@login_required
def list_connection_actions(connection_id: int):
    db = get_db()
    try:
        row = db.get(Connection, connection_id)
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify({"actions": parse_openapi_actions(row.schema_text or "")})
    finally:
        db.close()


@bp.post("/api/connections/<int:connection_id>/test")
@login_required
def test_connection(connection_id: int):
    body: dict[str, Any] = request.get_json(force=True) or {}
    db = get_db()
    try:
        row = db.get(Connection, connection_id)
        if not row:
            return jsonify({"error": "not found"}), 404

        config = _parse_json(row.config_json or "{}", {})
        auth = resolve_auth_payload(_parse_json(row.auth_json or "{}", {}))

        if row.kind == "database":
            result = test_database_connection(config=config if isinstance(config, dict) else {}, payload=body)
        else:
            result = test_http_connection(
                config=config if isinstance(config, dict) else {},
                auth=auth if isinstance(auth, dict) else {},
                schema_text=row.schema_text or "",
                payload=body,
            )
        status = 200 if result.get("ok") else 400
        return jsonify(result), status
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        db.close()

