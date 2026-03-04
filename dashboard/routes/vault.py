"""Vault dashboard page and proxy endpoints."""
from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from dashboard.cp_client import get_cp_client

bp = Blueprint("vault", __name__)


@bp.get("/vault")
@login_required
def vault_page() -> str:
    cp = get_cp_client()
    namespace = request.args.get("namespace")
    items = cp.list_vault_items(namespace=namespace, limit=100) or []
    return render_template("vault.html", items=items, namespace=namespace or "", results=[], error=None)


@bp.post("/api/vault/ingest")
@login_required
def api_vault_ingest():
    data: dict[str, Any] = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400
    cp = get_cp_client()
    created = cp.ingest_vault_item(
        {
            "title": title,
            "content": content,
            "namespace": (data.get("namespace") or "global").strip() or "global",
            "project_id": data.get("project_id"),
            "source_type": data.get("source_type", "text"),
            "source_ref": data.get("source_ref"),
            "metadata": data.get("metadata"),
        }
    )
    if created is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(created), 201


@bp.post("/api/vault/search")
@login_required
def api_vault_search():
    data: dict[str, Any] = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    cp = get_cp_client()
    results = cp.search_vault(
        {
            "query": query,
            "namespace": data.get("namespace"),
            "project_id": data.get("project_id"),
            "limit": int(data.get("limit", 5)),
        }
    )
    if results is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(results)
