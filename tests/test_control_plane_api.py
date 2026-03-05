"""Integration tests for control plane FastAPI routes."""
import hashlib
import hmac
import json

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.anyio
async def test_health(cp_client):
    resp = await cp_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_control_plane_optional_api_token_auth(cp_app):
    cp_app.state.control_plane_api_token = "test-token"
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        health = await client.get("/health")
        assert health.status_code == 200

        unauthorized = await client.get("/v1/workers")
        assert unauthorized.status_code == 401

        authorized_header = await client.get("/v1/workers", headers={"X-Nexus-API-Key": "test-token"})
        assert authorized_header.status_code == 200

        authorized_bearer = await client.get(
            "/v1/workers",
            headers={"Authorization": "Bearer test-token"},
        )
        assert authorized_bearer.status_code == 200


@pytest.mark.anyio
async def test_chat_message_rate_limit_guard(cp_client, monkeypatch):
    monkeypatch.setenv("CP_RATE_LIMIT_CHAT_MESSAGES_COUNT", "1")
    monkeypatch.setenv("CP_RATE_LIMIT_CHAT_MESSAGES_WINDOW_SECONDS", "60")

    create_resp = await cp_client.post("/v1/chat/conversations", json={"title": "Rate Limit"})
    conversation_id = create_resp.json()["id"]
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-rate",
            "name": "Rate Bot",
            "role": "assistant",
            "backends": [],
            "enabled": True,
        },
    )

    first = await cp_client.post(
        f"/v1/chat/conversations/{conversation_id}/messages",
        json={"content": "hello", "bot_id": "bot-rate"},
    )
    assert first.status_code in (200, 500)

    second = await cp_client.post(
        f"/v1/chat/conversations/{conversation_id}/messages",
        json={"content": "again", "bot_id": "bot-rate"},
    )
    assert second.status_code == 429


@pytest.mark.anyio
async def test_chat_message_body_size_guard(cp_client, monkeypatch):
    monkeypatch.setenv("CP_MAX_BODY_BYTES_CHAT_MESSAGES", "60")
    create_resp = await cp_client.post("/v1/chat/conversations", json={"title": "Body Size"})
    conversation_id = create_resp.json()["id"]
    await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-size",
            "name": "Size Bot",
            "role": "assistant",
            "backends": [],
            "enabled": True,
        },
    )
    payload = {"content": "x" * 200, "bot_id": "bot-size"}
    resp = await cp_client.post(
        f"/v1/chat/conversations/{conversation_id}/messages",
        json=payload,
    )
    assert resp.status_code == 413


@pytest.mark.anyio
async def test_list_workers_empty(cp_client):
    resp = await cp_client.get("/v1/workers")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_register_worker(cp_client):
    worker = {
        "id": "w1",
        "name": "Test Worker",
        "host": "localhost",
        "port": 8001,
        "status": "offline",
        "capabilities": [],
        "metrics": {},
        "enabled": True,
    }
    resp = await cp_client.post("/v1/workers", json=worker)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "w1"
    assert data["status"] == "online"


@pytest.mark.anyio
async def test_get_worker_not_found(cp_client):
    resp = await cp_client.get("/v1/workers/nonexistent")
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_worker_heartbeat(cp_client):
    worker = {"id": "w1", "name": "W1", "host": "h1", "port": 8001, "status": "offline", "capabilities": [], "metrics": {}, "enabled": True}
    await cp_client.post("/v1/workers", json=worker)
    resp = await cp_client.post("/v1/workers/w1/heartbeat", json={})
    assert resp.status_code == 200


@pytest.mark.anyio
async def test_update_worker(cp_client):
    worker = {"id": "w1", "name": "W1", "host": "h1", "port": 8001, "status": "offline", "capabilities": [], "metrics": {}, "enabled": True}
    await cp_client.post("/v1/workers", json=worker)
    update = {"id": "w1", "name": "W1 Renamed", "host": "h1", "port": 8001, "status": "online", "capabilities": [], "metrics": {}, "enabled": False}
    resp = await cp_client.put("/v1/workers/w1", json=update)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


@pytest.mark.anyio
async def test_list_bots_empty(cp_client):
    resp = await cp_client.get("/v1/bots")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_create_bot(cp_client):
    bot = {"id": "bot1", "name": "Bot 1", "role": "test", "priority": 0, "enabled": True, "backends": []}
    resp = await cp_client.post("/v1/bots", json=bot)
    assert resp.status_code == 200
    assert resp.json()["id"] == "bot1"


@pytest.mark.anyio
async def test_list_tasks_empty(cp_client):
    resp = await cp_client.get("/v1/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.anyio
async def test_create_project(cp_client):
    project = {"id": "p1", "name": "Project 1", "mode": "isolated"}
    resp = await cp_client.post("/v1/projects", json=project)
    assert resp.status_code == 200
    assert resp.json()["id"] == "p1"


@pytest.mark.anyio
async def test_add_project_bridge(cp_client):
    await cp_client.post("/v1/projects", json={"id": "p1", "name": "One", "mode": "bridged"})
    await cp_client.post("/v1/projects", json={"id": "p2", "name": "Two", "mode": "bridged"})

    resp = await cp_client.post("/v1/projects/p1/bridges/p2")
    assert resp.status_code == 200

    p1 = (await cp_client.get("/v1/projects/p1")).json()
    p2 = (await cp_client.get("/v1/projects/p2")).json()
    assert "p2" in p1["bridge_project_ids"]
    assert "p1" in p2["bridge_project_ids"]


@pytest.mark.anyio
async def test_add_project_bridge_rejects_isolated_mode(cp_client):
    await cp_client.post("/v1/projects", json={"id": "p1", "name": "One", "mode": "isolated"})
    await cp_client.post("/v1/projects", json={"id": "p2", "name": "Two", "mode": "bridged"})

    resp = await cp_client.post("/v1/projects/p1/bridges/p2")
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_create_and_get_api_key_metadata(cp_client):
    resp = await cp_client.post(
        "/v1/keys",
        json={"name": "openai-dev", "provider": "openai", "value": "sk-test"},
    )
    assert resp.status_code == 200

    meta = await cp_client.get("/v1/keys/openai-dev")
    assert meta.status_code == 200
    data = meta.json()
    assert data["name"] == "openai-dev"
    assert data["provider"] == "openai"
    assert "value" not in data


@pytest.mark.anyio
async def test_delete_api_key(cp_client):
    await cp_client.post(
        "/v1/keys",
        json={"name": "gemini-dev", "provider": "gemini", "value": "gk-test"},
    )
    delete_resp = await cp_client.delete("/v1/keys/gemini-dev")
    assert delete_resp.status_code == 200

    get_resp = await cp_client.get("/v1/keys/gemini-dev")
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_create_and_get_catalog_model(cp_client):
    model = {
        "id": "openai-gpt-4o-mini",
        "name": "gpt-4o-mini",
        "provider": "openai",
        "context_window": 128000,
        "capabilities": ["chat"],
        "input_cost_per_1k": 0.00015,
        "output_cost_per_1k": 0.0006,
        "notes": "fast baseline model",
        "enabled": True,
    }
    resp = await cp_client.post("/v1/models", json=model)
    assert resp.status_code == 200
    assert resp.json()["id"] == "openai-gpt-4o-mini"

    get_resp = await cp_client.get("/v1/models/openai-gpt-4o-mini")
    assert get_resp.status_code == 200
    assert get_resp.json()["provider"] == "openai"


@pytest.mark.anyio
async def test_delete_catalog_model(cp_client):
    await cp_client.post(
        "/v1/models",
        json={"id": "gemini-2-flash", "name": "gemini-2.0-flash", "provider": "gemini"},
    )
    delete_resp = await cp_client.delete("/v1/models/gemini-2-flash")
    assert delete_resp.status_code == 200

    get_resp = await cp_client.get("/v1/models/gemini-2-flash")
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_vault_ingest_and_search(cp_client):
    ingest_resp = await cp_client.post(
        "/v1/vault/items",
        json={
            "title": "Design Notes",
            "content": "NexusAI uses a control plane and worker nodes.",
            "namespace": "global",
        },
    )
    assert ingest_resp.status_code == 200
    item_id = ingest_resp.json()["id"]

    chunks_resp = await cp_client.get(f"/v1/vault/items/{item_id}/chunks")
    assert chunks_resp.status_code == 200
    assert len(chunks_resp.json()) >= 1

    search_resp = await cp_client.post(
        "/v1/vault/search",
        json={"query": "control plane", "limit": 3},
    )
    assert search_resp.status_code == 200
    assert len(search_resp.json()) >= 1


@pytest.mark.anyio
async def test_vault_context_endpoint(cp_client):
    await cp_client.post(
        "/v1/vault/items",
        json={"title": "JWT", "content": "JWT refresh token flow and auth middleware."},
    )
    context_resp = await cp_client.post(
        "/v1/vault/context",
        json={"query": "auth token", "limit": 2},
    )
    assert context_resp.status_code == 200
    data = context_resp.json()
    assert "contexts" in data
    assert data["context_count"] >= 1


@pytest.mark.anyio
async def test_vault_delete_item_and_list_namespaces(cp_client):
    item_a = (
        await cp_client.post(
            "/v1/vault/items",
            json={"title": "A", "content": "alpha", "namespace": "alpha"},
        )
    ).json()
    await cp_client.post(
        "/v1/vault/items",
        json={"title": "B", "content": "beta", "namespace": "beta"},
    )

    ns_resp = await cp_client.get("/v1/vault/namespaces")
    assert ns_resp.status_code == 200
    namespaces = ns_resp.json()
    assert "alpha" in namespaces
    assert "beta" in namespaces

    del_resp = await cp_client.delete(f"/v1/vault/items/{item_a['id']}")
    assert del_resp.status_code == 200

    get_resp = await cp_client.get(f"/v1/vault/items/{item_a['id']}")
    assert get_resp.status_code == 404


@pytest.mark.anyio
async def test_list_tasks_filtered_by_orchestration_id(cp_client):
    await cp_client.post(
        "/v1/tasks",
        json={
            "bot_id": "bot-a",
            "payload": {"instruction": "a"},
            "metadata": {"source": "chat_assign", "orchestration_id": "orch-1"},
        },
    )
    await cp_client.post(
        "/v1/tasks",
        json={
            "bot_id": "bot-b",
            "payload": {"instruction": "b"},
            "metadata": {"source": "chat_assign", "orchestration_id": "orch-2"},
        },
    )

    resp = await cp_client.get("/v1/tasks?orchestration_id=orch-1")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    assert all((r.get("metadata") or {}).get("orchestration_id") == "orch-1" for r in rows)


@pytest.mark.anyio
async def test_project_github_pat_connect_status_disconnect(cp_client):
    create_resp = await cp_client.post(
        "/v1/projects",
        json={"id": "gh-proj", "name": "GitHub Project", "mode": "isolated"},
    )
    assert create_resp.status_code == 200

    connect_resp = await cp_client.post(
        "/v1/projects/gh-proj/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )
    assert connect_resp.status_code == 200
    assert connect_resp.json()["status"] == "connected"

    status_resp = await cp_client.get("/v1/projects/gh-proj/github/status")
    assert status_resp.status_code == 200
    status = status_resp.json()
    assert status["connected"] is True
    assert status["repo_full_name"] == "owner/repo"

    disconnect_resp = await cp_client.delete("/v1/projects/gh-proj/github/pat")
    assert disconnect_resp.status_code == 200

    status_after = await cp_client.get("/v1/projects/gh-proj/github/status")
    assert status_after.status_code == 200
    assert status_after.json()["connected"] is False


@pytest.mark.anyio
async def test_project_github_webhook_ingestion_and_list(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-hook", "name": "GitHub Hook Project", "mode": "isolated"},
    )
    set_secret = await cp_client.post(
        "/v1/projects/gh-hook/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    assert set_secret.status_code == 200

    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {"number": 42},
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    ingest = await cp_client.post(
        "/v1/projects/gh-hook/github/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "pull_request",
            "X-GitHub-Delivery": "delivery-1",
        },
    )
    assert ingest.status_code == 200
    assert ingest.json()["status"] == "accepted"

    events = await cp_client.get("/v1/projects/gh-hook/github/webhook/events")
    assert events.status_code == 200
    rows = events.json()["events"]
    assert len(rows) >= 1
    assert rows[0]["event_type"] == "pull_request"
    assert rows[0]["action"] == "opened"


@pytest.mark.anyio
async def test_project_github_webhook_rejects_bad_signature(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-hook-bad", "name": "GitHub Hook Bad", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-hook-bad/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    ingest = await cp_client.post(
        "/v1/projects/gh-hook-bad/github/webhook",
        json={"repository": {"full_name": "owner/repo"}},
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Event": "push",
        },
    )
    assert ingest.status_code == 401


@pytest.mark.anyio
async def test_project_github_context_sync_ingests_vault_items(cp_client, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-sync", "name": "GitHub Sync", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-sync/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    async def _fake_fetch(token, repo_full_name, branch, max_files):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "files": [
                {"path": "README.md", "content": "# test", "size": 6, "sha": "abc"},
                {"path": "src/app.py", "content": "print('ok')", "size": 11, "sha": "def"},
            ][:max_files],
        }

    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch)

    sync_resp = await cp_client.post(
        "/v1/projects/gh-sync/github/context/sync",
        json={"max_files": 10},
    )
    assert sync_resp.status_code == 200
    body = sync_resp.json()
    assert body["ingested_count"] == 2

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-sync&limit=20")
    assert items_resp.status_code == 200
    assert len(items_resp.json()) >= 2


@pytest.mark.anyio
async def test_project_github_pr_review_workflow_creates_task(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-pr", "name": "GitHub PR", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-pr/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )
    await cp_client.post(
        "/v1/projects/gh-pr/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    cfg = await cp_client.post(
        "/v1/projects/gh-pr/github/pr-review/config",
        json={"enabled": True, "bot_id": "bot-reviewer"},
    )
    assert cfg.status_code == 200

    payload = {
        "action": "opened",
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 7,
            "title": "Add auth",
            "body": "Please review",
            "html_url": "https://github.com/owner/repo/pull/7",
            "base": {"ref": "main"},
            "head": {"ref": "feature/auth"},
        },
    }
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    ingest = await cp_client.post(
        "/v1/projects/gh-pr/github/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "pull_request",
        },
    )
    assert ingest.status_code == 200
    review_task_id = ingest.json().get("review_task_id")
    assert review_task_id

    tasks = await cp_client.get("/v1/tasks")
    assert tasks.status_code == 200
    rows = tasks.json()
    assert any((r.get("payload") or {}).get("source") == "github_pr_review" for r in rows)


@pytest.mark.anyio
async def test_audit_events_record_privileged_actions(cp_client):
    upsert = await cp_client.post(
        "/v1/keys",
        json={"name": "audit-key", "provider": "openai", "value": "sk-test"},
    )
    assert upsert.status_code == 200

    create_model = await cp_client.post(
        "/v1/models",
        json={"id": "audit-model", "name": "audit-model", "provider": "openai"},
    )
    assert create_model.status_code == 200

    events = await cp_client.get("/v1/audit/events?limit=20")
    assert events.status_code == 200
    rows = events.json()
    actions = {r.get("action") for r in rows}
    assert "keys.upsert" in actions
    assert "models.create" in actions
