"""Bots blueprint — page + JSON API."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Bot, Task

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
    cp_artifacts = cp.list_bot_artifacts(bot_id, limit=150) or []
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
