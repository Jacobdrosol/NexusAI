"""Bots blueprint — page + JSON API."""
from __future__ import annotations

import json
import logging
from typing import Any

from flask import Blueprint, flash, jsonify, render_template, request
from flask_login import login_required

from dashboard.db import get_db
from dashboard.models import Bot, Task

logger = logging.getLogger(__name__)

bp = Blueprint("bots", __name__)


def _bot_to_dict(b: Bot) -> dict[str, Any]:
    """Serialise a Bot ORM row to a plain dict."""
    return {
        "id": b.id,
        "name": b.name,
        "role": b.role,
        "priority": b.priority,
        "enabled": b.enabled,
        "backends": b.backends_as_list(),
        "routing_rules": json.loads(b.routing_rules) if b.routing_rules else {},
    }


@bp.get("/bots")
@login_required
def bots_page() -> str:
    """Render the bots table page."""
    from dashboard.cp_client import get_cp_client

    cp_data = get_cp_client().list_bots()
    if cp_data is not None:
        return render_template("bots.html", bots=cp_data, error=None)

    flash("Control plane unavailable — showing local data.", "warning")
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

    if cp_bot is not None and cp_tasks is not None:
        tasks = [t for t in cp_tasks if str(t.get("bot_id")) == str(bot_id)]
        return render_template("bot_detail.html", bot=cp_bot, tasks=tasks, error=None)

    db = get_db()
    try:
        # Fallback local bot IDs are integer PKs.
        if not str(bot_id).isdigit():
            return render_template("bot_detail.html", bot=None, tasks=[], error="Bot not found"), 404
        bot = db.get(Bot, int(bot_id))
        if not bot:
            return render_template("bot_detail.html", bot=None, tasks=[], error="Bot not found"), 404
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
        return render_template("bot_detail.html", bot=_bot_to_dict(bot), tasks=tasks, error=None)
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
    data: dict[str, Any] = request.get_json(force=True) or {}
    if not data.get("name"):
        return jsonify({"error": "name is required"}), 400
    db = get_db()
    try:
        bot = Bot(
            name=data["name"],
            role=data.get("role", ""),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            backends=json.dumps(data.get("backends", [])),
            routing_rules=json.dumps(data.get("routing_rules", {})),
        )
        db.add(bot)
        db.commit()
        db.refresh(bot)
        return jsonify(_bot_to_dict(bot)), 201
    finally:
        db.close()


@bp.get("/api/bots/<int:bot_id>")
@login_required
def api_get_bot(bot_id: int):
    """Get a single bot by ID."""
    db = get_db()
    try:
        bot = db.get(Bot, bot_id)
        if not bot:
            return jsonify({"error": "not found"}), 404
        return jsonify(_bot_to_dict(bot))
    finally:
        db.close()


@bp.put("/api/bots/<int:bot_id>")
@login_required
def api_update_bot(bot_id: int):
    """Update an existing bot."""
    db = get_db()
    try:
        bot = db.get(Bot, bot_id)
        if not bot:
            return jsonify({"error": "not found"}), 404
        data: dict[str, Any] = request.get_json(force=True) or {}
        for field in ("name", "role"):
            if field in data:
                setattr(bot, field, data[field])
        if "priority" in data:
            bot.priority = int(data["priority"])
        if "enabled" in data:
            bot.enabled = bool(data["enabled"])
        if "backends" in data:
            bot.backends = json.dumps(data["backends"])
        if "routing_rules" in data:
            bot.routing_rules = json.dumps(data["routing_rules"])
        db.commit()
        db.refresh(bot)
        return jsonify(_bot_to_dict(bot))
    finally:
        db.close()


@bp.delete("/api/bots/<int:bot_id>")
@login_required
def api_delete_bot(bot_id: int):
    """Delete a bot."""
    db = get_db()
    try:
        bot = db.get(Bot, bot_id)
        if not bot:
            return jsonify({"error": "not found"}), 404
        db.delete(bot)
        db.commit()
        return "", 204
    finally:
        db.close()
