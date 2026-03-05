"""Vault dashboard page and proxy endpoints."""
from __future__ import annotations

from typing import Any

import requests
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
    namespaces = cp.list_vault_namespaces() or sorted(
        list({str(i.get("namespace", "global")) for i in items})
    )
    return render_template(
        "vault.html",
        items=items,
        namespaces=namespaces,
        namespace=namespace or "",
        results=[],
        error=None,
    )


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


@bp.post("/api/vault/upload")
@login_required
def api_vault_upload():
    source_mode = (request.form.get("source_mode") or "").strip().lower()
    title = (request.form.get("title") or "").strip()
    namespace = (request.form.get("namespace") or "global").strip() or "global"
    project_id = (request.form.get("project_id") or "").strip() or None
    source_ref = None
    content = ""

    if source_mode == "file":
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "file is required"}), 400
        source_ref = file.filename
        raw = file.read()
        try:
            content = raw.decode("utf-8")
        except Exception:
            content = raw.decode("latin-1", errors="ignore")
        if not title:
            title = file.filename
    elif source_mode == "url":
        url = (request.form.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url is required"}), 400
        source_ref = url
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            content = resp.text[:200000]
        except Exception as e:
            return jsonify({"error": f"url fetch failed: {e}"}), 400
        if not title:
            title = url
    elif source_mode in {"paste", "text", ""}:
        content = (request.form.get("content") or "").strip()
        if not title:
            title = "Pasted Content"
    else:
        return jsonify({"error": "invalid source_mode"}), 400

    if not title or not content:
        return jsonify({"error": "title and content are required"}), 400

    cp = get_cp_client()
    created = cp.ingest_vault_item(
        {
            "title": title,
            "content": content,
            "namespace": namespace,
            "project_id": project_id,
            "source_type": "file" if source_mode == "file" else ("url" if source_mode == "url" else "text"),
            "source_ref": source_ref,
        }
    )
    if created is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(created), 201


@bp.get("/api/vault/items/<item_id>/detail")
@login_required
def api_vault_item_detail(item_id: str):
    cp = get_cp_client()
    item = cp.get_vault_item(item_id)
    chunks = cp.list_vault_chunks(item_id)
    if item is None or chunks is None:
        return jsonify({"error": "control plane unavailable"}), 502
    preview = (item.get("content") or "")[:1200]
    return jsonify(
        {
            "item": item,
            "chunk_count": len(chunks),
            "preview": preview,
            "chunks": chunks[:10],
        }
    )


@bp.get("/api/vault/namespaces")
@login_required
def api_vault_namespaces():
    cp = get_cp_client()
    namespaces = cp.list_vault_namespaces()
    if namespaces is None:
        return jsonify({"error": "control plane unavailable"}), 502
    return jsonify(namespaces)


@bp.post("/api/vault/bulk-delete")
@login_required
def api_vault_bulk_delete():
    data: dict[str, Any] = request.get_json(force=True) or {}
    item_ids = data.get("item_ids") or []
    if not isinstance(item_ids, list) or not item_ids:
        return jsonify({"error": "item_ids list is required"}), 400
    cp = get_cp_client()
    failed: list[str] = []
    deleted = 0
    for item_id in item_ids:
        if not cp.delete_vault_item(str(item_id)):
            failed.append(str(item_id))
        else:
            deleted += 1
    if failed:
        return jsonify({"deleted": deleted, "failed": failed}), 207
    return jsonify({"deleted": deleted, "failed": []})
