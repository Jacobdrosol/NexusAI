"""Integration tests for control plane FastAPI routes."""
import asyncio
import hashlib
import hmac
import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi.testclient import TestClient


@pytest.mark.anyio
async def test_health(cp_client):
    resp = await cp_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_metrics_endpoint_exposes_prometheus_text(cp_client):
    await cp_client.get("/health")
    resp = await cp_client.get("/metrics")
    assert resp.status_code == 200
    text = resp.text
    assert "nexus_control_plane_http_requests_total" in text
    assert "nexus_control_plane_http_request_duration_seconds_bucket" in text


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


def test_create_app_does_not_seed_workers_from_config_by_default(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    workers_dir = config_dir / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "seeded-worker.yaml").write_text(
        "\n".join(
            [
                'id: "seeded-worker"',
                'name: "Seeded Worker"',
                'host: "127.0.0.1"',
                "port: 8001",
                'status: "offline"',
                "capabilities: []",
                "enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "bots").mkdir(parents=True)
    (config_dir / "nexus_config.yaml").write_text(
        "\n".join(
            [
                "control_plane:",
                "  host: 0.0.0.0",
                "  port: 8000",
                f"  workers_config_dir: {workers_dir.as_posix()}",
                f"  bots_config_dir: {(config_dir / 'bots').as_posix()}",
                "  seed_bots_from_config: false",
                "  heartbeat_timeout_seconds: 30",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_CONFIG_PATH", str(config_dir / "nexus_config.yaml"))

    import control_plane.main as main_module
    importlib.reload(main_module)

    create_app = main_module.create_app
    app = create_app()
    with TestClient(app):
        workers = asyncio.run(app.state.worker_registry.list())
        assert workers == []
    monkeypatch.delenv("NEXUS_CONFIG_PATH", raising=False)


def test_create_app_can_seed_workers_from_config_when_enabled(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    workers_dir = config_dir / "workers"
    workers_dir.mkdir(parents=True)
    (workers_dir / "seeded-worker.yaml").write_text(
        "\n".join(
            [
                'id: "seeded-worker"',
                'name: "Seeded Worker"',
                'host: "127.0.0.1"',
                "port: 8001",
                'status: "offline"',
                "capabilities: []",
                "enabled: true",
            ]
        ),
        encoding="utf-8",
    )
    (config_dir / "bots").mkdir(parents=True)
    (config_dir / "nexus_config.yaml").write_text(
        "\n".join(
            [
                "control_plane:",
                "  host: 0.0.0.0",
                "  port: 8000",
                f"  workers_config_dir: {workers_dir.as_posix()}",
                f"  bots_config_dir: {(config_dir / 'bots').as_posix()}",
                "  seed_workers_from_config: true",
                "  seed_bots_from_config: false",
                "  heartbeat_timeout_seconds: 30",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_CONFIG_PATH", str(config_dir / "nexus_config.yaml"))

    import control_plane.main as main_module
    importlib.reload(main_module)

    create_app = main_module.create_app
    app = create_app()
    with TestClient(app):
        workers = asyncio.run(app.state.worker_registry.list())
        assert len(workers) == 1
        assert workers[0].id == "seeded-worker"
    monkeypatch.delenv("NEXUS_CONFIG_PATH", raising=False)


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
async def test_external_bot_trigger_creates_task_with_auth_and_payload_field(cp_client):
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext",
            "name": "External Trigger Bot",
            "role": "assistant",
            "enabled": True,
            "backends": [],
            "routing_rules": {
                "external_trigger": {
                    "enabled": True,
                    "require_auth": True,
                    "auth_header": "X-Nexus-Trigger-Token",
                    "auth_token": "topsecret",
                    "payload_field": "event.data",
                    "allow_metadata": True,
                    "source": "webhook",
                }
            },
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext/trigger",
        json={
            "event": {"data": {"instruction": "continue", "course_id": "course-1"}},
            "metadata": {"project_id": "proj-1", "priority": 3},
        },
        headers={"X-Nexus-Trigger-Token": "topsecret"},
    )
    assert trigger.status_code == 200
    body = trigger.json()
    assert body["bot_id"] == "bot-ext"
    assert body["payload"] == {"instruction": "continue", "course_id": "course-1"}
    assert (body.get("metadata") or {}).get("source") == "webhook"
    assert (body.get("metadata") or {}).get("project_id") == "proj-1"
    assert (body.get("metadata") or {}).get("priority") == 3


@pytest.mark.anyio
async def test_external_bot_trigger_rejects_when_disabled(cp_client):
    create = await cp_client.post(
        "/v1/bots",
        json={
            "id": "bot-ext-disabled",
            "name": "External Trigger Disabled",
            "role": "assistant",
            "enabled": True,
            "backends": [],
            "routing_rules": {"external_trigger": {"enabled": False}},
        },
    )
    assert create.status_code == 200

    trigger = await cp_client.post(
        "/v1/bots/bot-ext-disabled/trigger",
        json={"payload": {"instruction": "ignored"}},
    )
    assert trigger.status_code == 403


@pytest.mark.anyio
async def test_external_bot_trigger_bypasses_global_cp_token_when_bot_auth_is_valid(cp_app):
    cp_app.state.control_plane_api_token = "global-cp-token"
    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create = await client.post(
            "/v1/bots",
            json={
                "id": "bot-ext-auth",
                "name": "External Trigger Auth",
                "role": "assistant",
                "enabled": True,
                "backends": [],
                "routing_rules": {
                    "external_trigger": {
                        "enabled": True,
                        "require_auth": True,
                        "auth_header": "X-External-Token",
                        "auth_token": "external-secret",
                    }
                },
            },
            headers={"X-Nexus-API-Key": "global-cp-token"},
        )
        assert create.status_code == 200

        trigger = await client.post(
            "/v1/bots/bot-ext-auth/trigger",
            json={"payload": {"instruction": "run"}},
            headers={"X-External-Token": "external-secret"},
        )
        assert trigger.status_code == 200
        assert trigger.json()["bot_id"] == "bot-ext-auth"


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
async def test_vault_upsert_reuses_existing_source_ref(cp_client):
    first = await cp_client.post(
        "/v1/vault/items/upsert",
        json={
            "title": "Doc",
            "content": "v1",
            "namespace": "project:test:data",
            "project_id": "test",
            "source_ref": "project-data://test/docs/readme.md",
        },
    )
    assert first.status_code == 200

    second = await cp_client.post(
        "/v1/vault/items/upsert",
        json={
            "title": "Doc",
            "content": "v2",
            "namespace": "project:test:data",
            "project_id": "test",
            "source_ref": "project-data://test/docs/readme.md",
        },
    )
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    items_resp = await cp_client.get("/v1/vault/items?project_id=test&limit=10")
    assert items_resp.status_code == 200
    items = items_resp.json()
    assert len(items) == 1
    assert items[0]["content"] == "v2"


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
async def test_bot_runs_and_artifacts_endpoints_expose_task_history(cp_client):
    await cp_client.post(
        "/v1/bots",
        json={"id": "bot-history", "name": "Bot History", "role": "assistant", "backends": []},
    )
    await cp_client.post(
        "/v1/tasks",
        json={"bot_id": "bot-history", "payload": {"instruction": "hello"}},
    )

    for _ in range(30):
        runs = await cp_client.get("/v1/bots/bot-history/runs")
        if runs.status_code == 200 and runs.json():
            first = runs.json()[0]
            if first["status"] in {"completed", "failed"}:
                break
        await asyncio.sleep(0.1)

    runs = await cp_client.get("/v1/bots/bot-history/runs")
    assert runs.status_code == 200
    run_rows = runs.json()
    assert len(run_rows) >= 1
    assert run_rows[0]["task_id"]

    artifacts = await cp_client.get("/v1/bots/bot-history/artifacts")
    assert artifacts.status_code == 200
    artifact_rows = artifacts.json()
    labels = {row["label"] for row in artifact_rows}
    assert "Task Payload" in labels


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
async def test_project_cloud_context_policy_update_and_get(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-policy", "name": "Policy Project", "mode": "isolated"},
    )

    update = await cp_client.put(
        "/v1/projects/p-policy/cloud-context-policy",
        json={
            "provider_policies": {"openai": "redact", "claude": "allow", "gemini": "block"},
            "bot_overrides": {
                "bot-a": {"openai": "block", "claude": "allow"},
            },
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["provider_policies"]["openai"] == "redact"
    assert body["bot_overrides"]["bot-a"]["openai"] == "block"

    get_resp = await cp_client.get("/v1/projects/p-policy/cloud-context-policy")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["provider_policies"]["gemini"] == "block"


@pytest.mark.anyio
async def test_project_cloud_context_policy_rejects_invalid_bot_allow_under_redact(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-policy-bad", "name": "Policy Project Bad", "mode": "isolated"},
    )
    resp = await cp_client.put(
        "/v1/projects/p-policy-bad/cloud-context-policy",
        json={
            "provider_policies": {"openai": "redact"},
            "bot_overrides": {"bot-a": {"openai": "allow"}},
        },
    )
    assert resp.status_code == 400
    assert "not allowed" in (resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_chat_tool_access_update_and_get(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-chat-tools", "name": "Project Chat Tools", "mode": "isolated"},
    )

    update = await cp_client.put(
        "/v1/projects/p-chat-tools/chat-tool-access",
        json={
            "enabled": True,
            "filesystem": True,
            "repo_search": True,
            "workspace_root": "C:\\repo\\workspace",
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["enabled"] is True
    assert body["filesystem"] is True
    assert body["repo_search"] is True
    assert body["workspace_root"] == "C:\\repo\\workspace"

    get_resp = await cp_client.get("/v1/projects/p-chat-tools/chat-tool-access")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["enabled"] is True
    assert got["filesystem"] is True
    assert got["repo_search"] is True
    assert got["workspace_root"] == "C:\\repo\\workspace"


@pytest.mark.anyio
async def test_project_chat_tool_access_rejects_too_long_workspace_root(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-chat-tools-bad", "name": "Project Chat Tools Bad", "mode": "isolated"},
    )
    root = "x" * 2000
    update = await cp_client.put(
        "/v1/projects/p-chat-tools-bad/chat-tool-access",
        json={
            "enabled": True,
            "filesystem": True,
            "repo_search": True,
            "workspace_root": root,
        },
    )
    assert update.status_code == 400
    assert "workspace_root" in (update.json().get("detail") or "")


@pytest.mark.anyio
async def test_project_repo_workspace_update_and_get(cp_client, tmp_path):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-workspace", "name": "Repo Workspace Project", "mode": "isolated"},
    )
    root = tmp_path / "workspace"
    update = await cp_client.put(
        "/v1/projects/p-repo-workspace/repo/workspace",
        json={
            "enabled": True,
            "root_path": str(root),
            "clone_url": "https://github.com/example/repo.git",
            "default_branch": "main",
            "allow_push": True,
            "allow_command_execution": True,
        },
    )
    assert update.status_code == 200
    body = update.json()
    assert body["enabled"] is True
    assert body["root_path"] == str(root.resolve())
    assert body["clone_url"] == "https://github.com/example/repo.git"
    assert body["default_branch"] == "main"
    assert body["allow_push"] is True
    assert body["allow_command_execution"] is True

    get_resp = await cp_client.get("/v1/projects/p-repo-workspace/repo/workspace")
    assert get_resp.status_code == 200
    got = get_resp.json()
    assert got["enabled"] is True
    assert got["root_path"] == str(root.resolve())
    assert got["allow_push"] is True
    assert got["allow_command_execution"] is True


@pytest.mark.anyio
async def test_project_repo_workspace_rejects_relative_root_path(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-workspace-bad", "name": "Repo Workspace Bad", "mode": "isolated"},
    )
    update = await cp_client.put(
        "/v1/projects/p-repo-workspace-bad/repo/workspace",
        json={
            "enabled": True,
            "root_path": "relative/path",
        },
    )
    assert update.status_code == 400
    assert "absolute path" in (update.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_repo_workspace_run_command_requires_policy(cp_client, tmp_path):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-run-policy", "name": "Repo Run Policy", "mode": "isolated"},
    )
    root = tmp_path / "run-policy"
    root.mkdir(parents=True, exist_ok=True)
    update = await cp_client.put(
        "/v1/projects/p-repo-run-policy/repo/workspace",
        json={
            "enabled": True,
            "root_path": str(root),
            "allow_command_execution": False,
        },
    )
    assert update.status_code == 200

    run_resp = await cp_client.post(
        "/v1/projects/p-repo-run-policy/repo/workspace/run",
        json={"command": ["py", "-m", "pytest", "-q"]},
    )
    assert run_resp.status_code == 403
    assert "disabled" in (run_resp.json().get("detail") or "").lower()


@pytest.mark.anyio
async def test_project_repo_workspace_run_command_executes_allowed_command(cp_client, tmp_path, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-run", "name": "Repo Run", "mode": "isolated"},
    )
    root = tmp_path / "run-workspace"
    root.mkdir(parents=True, exist_ok=True)
    update = await cp_client.put(
        "/v1/projects/p-repo-run/repo/workspace",
        json={
            "enabled": True,
            "root_path": str(root),
            "allow_command_execution": True,
        },
    )
    assert update.status_code == 200

    captured = {}

    async def _fake_run(args, *, cwd, timeout_seconds=None, env_overrides=None):
        captured["args"] = args
        captured["cwd"] = str(cwd)
        captured["timeout_seconds"] = timeout_seconds
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "tests passed",
            "stderr": "",
            "command": args,
            "timeout_seconds": timeout_seconds or 120,
        }

    monkeypatch.setattr("control_plane.api.projects._run_repo_command", _fake_run)

    run_resp = await cp_client.post(
        "/v1/projects/p-repo-run/repo/workspace/run",
        json={"command": ["py", "-m", "pytest", "-q"], "timeout_seconds": 90},
    )
    assert run_resp.status_code == 200
    body = run_resp.json()
    assert body["status"] == "ok"
    assert body["result"]["ok"] is True
    assert captured["args"] == ["py", "-m", "pytest", "-q"]
    assert captured["cwd"] == str(root.resolve())
    assert captured["timeout_seconds"] == 90


@pytest.mark.anyio
async def test_project_repo_workspace_push_requires_allow_push(cp_client, tmp_path):
    await cp_client.post(
        "/v1/projects",
        json={"id": "p-repo-push", "name": "Repo Push", "mode": "isolated"},
    )
    root = tmp_path / "push-workspace"
    root.mkdir(parents=True, exist_ok=True)
    update = await cp_client.put(
        "/v1/projects/p-repo-push/repo/workspace",
        json={
            "enabled": True,
            "root_path": str(root),
            "allow_push": False,
        },
    )
    assert update.status_code == 200

    push_resp = await cp_client.post(
        "/v1/projects/p-repo-push/repo/workspace/push",
        json={"remote": "origin", "branch": "main"},
    )
    assert push_resp.status_code == 403
    assert "disabled" in (push_resp.json().get("detail") or "").lower()


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
            "X-GitHub-Delivery": "delivery-bad-sig",
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

    async def _fake_fetch(token, repo_full_name, branch):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "files": [
                {"path": "README.md", "content": "# test", "size": 6, "sha": "abc"},
                {"path": "src/app.py", "content": "print('ok')", "size": 11, "sha": "def"},
            ],
        }

    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch)

    sync_resp = await cp_client.post(
        "/v1/projects/gh-sync/github/context/sync",
        json={"sync_mode": "full"},
    )
    assert sync_resp.status_code == 200
    for _ in range(30):
        status_resp = await cp_client.get("/v1/projects/gh-sync/github/context/sync")
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert body["status"] == "completed"
    assert body["ingested_count"] == 2

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-sync&limit=20")
    assert items_resp.status_code == 200
    assert len(items_resp.json()) >= 2


@pytest.mark.anyio
async def test_project_github_context_sync_can_ingest_commits_prs_and_issues(cp_client, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-full-sync", "name": "GitHub Full Sync", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-full-sync/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    async def _fake_fetch_files(token, repo_full_name, branch):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "files": [
                {"path": "README.md", "content": "# test", "size": 6, "sha": "abc"},
            ],
        }

    async def _fake_fetch_commits(token, repo_full_name, branch, since=None):
        return {
            "repo_full_name": repo_full_name,
            "branch": branch or "main",
            "commits": [
                {"sha": "deadbeef", "html_url": "https://example/commit/deadbeef", "message": "Initial import", "author_name": "Jake", "authored_at": "2026-03-07T00:00:00Z"},
            ],
        }

    async def _fake_fetch_pulls(token, repo_full_name, include_conversations, updated_after=None):
        return [
            {
                "number": 12,
                "title": "Add orchestration",
                "body": "Implements chained bots",
                "state": "open",
                "draft": False,
                "html_url": "https://example/pull/12",
                "user": "octocat",
                "created_at": "2026-03-06T00:00:00Z",
                "updated_at": "2026-03-07T00:00:00Z",
                "merged_at": None,
                "base_ref": "main",
                "head_ref": "feature/orchestration",
                "issue_comments": [{"user": "reviewer", "created_at": "2026-03-07T01:00:00Z", "body": "Looks good"}],
                "review_comments": [{"user": "reviewer", "created_at": "2026-03-07T02:00:00Z", "path": "bot.py", "body": "Tighten this"}],
            }
        ]

    async def _fake_fetch_issues(token, repo_full_name, include_conversations, updated_after=None):
        return [
            {
                "number": 8,
                "title": "Ingestion backlog",
                "body": "Need docs and vectors",
                "state": "open",
                "html_url": "https://example/issues/8",
                "user": "octocat",
                "created_at": "2026-03-05T00:00:00Z",
                "updated_at": "2026-03-07T00:00:00Z",
                "comments": [{"user": "teammate", "created_at": "2026-03-07T03:00:00Z", "body": "Agreed"}],
            }
        ]

    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch_files)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_commits", _fake_fetch_commits)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_pull_requests", _fake_fetch_pulls)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_issues", _fake_fetch_issues)

    sync_resp = await cp_client.post(
        "/v1/projects/gh-full-sync/github/context/sync",
        json={
            "sync_mode": "full",
        },
    )
    assert sync_resp.status_code == 200
    for _ in range(30):
        status_resp = await cp_client.get("/v1/projects/gh-full-sync/github/context/sync")
        assert status_resp.status_code == 200
        body = status_resp.json()
        if body.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert body["status"] == "completed"
    assert body["ingested_count"] == 4
    assert body["counts"]["files"] == 1
    assert body["counts"]["commits"] == 1
    assert body["counts"]["pull_requests"] == 1
    assert body["counts"]["issues"] == 1
    assert body["counts"]["conversations"] == 3

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-full-sync&limit=20")
    assert items_resp.status_code == 200
    assert len(items_resp.json()) >= 4


@pytest.mark.anyio
async def test_project_github_context_update_ingests_only_newer_items(cp_client, monkeypatch):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-update", "name": "GitHub Update", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-update/github/pat",
        json={
            "token": "ghp_example_token_for_tests_only",
            "repo_full_name": "owner/repo",
            "validate": False,
        },
    )

    file_versions = [
        [{"path": "README.md", "content": "# v1", "size": 4, "sha": "sha-1"}],
        [{"path": "README.md", "content": "# v2", "size": 4, "sha": "sha-2"}],
    ]
    commit_versions = [
        [{"sha": "aaa", "html_url": "https://example/commit/aaa", "message": "one", "author_name": "Jake", "authored_at": "2026-03-07T00:00:00Z"}],
        [{"sha": "bbb", "html_url": "https://example/commit/bbb", "message": "two", "author_name": "Jake", "authored_at": "2026-03-08T00:00:00Z"}],
    ]
    pr_versions = [
        [{"number": 1, "title": "One", "body": "body", "state": "open", "draft": False, "html_url": "https://example/pull/1", "user": "octocat", "created_at": "2026-03-07T00:00:00Z", "updated_at": "2026-03-07T00:00:00Z", "merged_at": None, "base_ref": "main", "head_ref": "feat/one", "issue_comments": [], "review_comments": []}],
        [{"number": 2, "title": "Two", "body": "body", "state": "open", "draft": False, "html_url": "https://example/pull/2", "user": "octocat", "created_at": "2026-03-08T00:00:00Z", "updated_at": "2026-03-08T00:00:00Z", "merged_at": None, "base_ref": "main", "head_ref": "feat/two", "issue_comments": [], "review_comments": []}],
    ]
    issue_versions = [
        [{"number": 8, "title": "Old", "body": "body", "state": "open", "html_url": "https://example/issues/8", "user": "octocat", "created_at": "2026-03-07T00:00:00Z", "updated_at": "2026-03-07T00:00:00Z", "comments": []}],
        [{"number": 9, "title": "New", "body": "body", "state": "open", "html_url": "https://example/issues/9", "user": "octocat", "created_at": "2026-03-08T00:00:00Z", "updated_at": "2026-03-08T00:00:00Z", "comments": []}],
    ]
    state = {"index": 0}

    async def _fake_fetch_files(token, repo_full_name, branch):
        return {"repo_full_name": repo_full_name, "branch": branch or "main", "files": file_versions[state["index"]]}

    async def _fake_fetch_commits(token, repo_full_name, branch, since=None):
        return {"repo_full_name": repo_full_name, "branch": branch or "main", "commits": commit_versions[state["index"]]}

    async def _fake_fetch_pulls(token, repo_full_name, include_conversations, updated_after=None):
        return pr_versions[state["index"]]

    async def _fake_fetch_issues(token, repo_full_name, include_conversations, updated_after=None):
        return issue_versions[state["index"]]

    monkeypatch.setattr("control_plane.api.projects._fetch_repo_context_files", _fake_fetch_files)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_commits", _fake_fetch_commits)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_pull_requests", _fake_fetch_pulls)
    monkeypatch.setattr("control_plane.api.projects._fetch_repo_issues", _fake_fetch_issues)

    first = await cp_client.post("/v1/projects/gh-update/github/context/sync", json={"sync_mode": "full"})
    assert first.status_code == 200
    for _ in range(30):
        first_status = await cp_client.get("/v1/projects/gh-update/github/context/sync")
        assert first_status.status_code == 200
        first_body = first_status.json()
        if first_body.get("status") == "completed":
            break
        await asyncio.sleep(0.1)
    assert first_body["ingested_count"] == 4

    state["index"] = 1
    second = await cp_client.post("/v1/projects/gh-update/github/context/sync", json={"sync_mode": "update"})
    assert second.status_code == 200
    for _ in range(30):
        second_status = await cp_client.get("/v1/projects/gh-update/github/context/sync")
        assert second_status.status_code == 200
        second_body = second_status.json()
        if second_body.get("status") == "completed" and second_body.get("sync_mode") == "update":
            break
        await asyncio.sleep(0.1)
    assert second_body["ingested_count"] == 4
    assert second_body["sync_mode"] == "update"

    items_resp = await cp_client.get("/v1/vault/items?project_id=gh-update&limit=20")
    assert items_resp.status_code == 200
    items = items_resp.json()
    titles = {item["title"] for item in items}
    assert "owner/repo:commit:bbb" in titles
    assert "owner/repo:pr:2" in titles
    assert "owner/repo:issue:9" in titles


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
            "X-GitHub-Delivery": "delivery-pr-1",
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
async def test_project_github_webhook_rejects_duplicate_delivery_id(cp_client):
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-dup", "name": "GitHub Dup", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-dup/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    payload = {"repository": {"full_name": "owner/repo"}}
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={sig}",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-dup-1",
    }
    first = await cp_client.post("/v1/projects/gh-dup/github/webhook", content=raw, headers=headers)
    assert first.status_code == 200
    second = await cp_client.post("/v1/projects/gh-dup/github/webhook", content=raw, headers=headers)
    assert second.status_code == 409


@pytest.mark.anyio
async def test_project_github_webhook_rejects_old_date_header(cp_client, monkeypatch):
    monkeypatch.setenv("NEXUSAI_GITHUB_WEBHOOK_MAX_SKEW_SECONDS", "1")
    await cp_client.post(
        "/v1/projects",
        json={"id": "gh-date", "name": "GitHub Date", "mode": "isolated"},
    )
    await cp_client.post(
        "/v1/projects/gh-date/github/webhook/secret",
        json={"secret": "topsecret"},
    )
    payload = {"repository": {"full_name": "owner/repo"}}
    raw = json.dumps(payload).encode("utf-8")
    sig = hmac.new(b"topsecret", raw, hashlib.sha256).hexdigest()
    old_date = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=10))
    ingest = await cp_client.post(
        "/v1/projects/gh-date/github/webhook",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-date-1",
            "Date": old_date,
        },
    )
    assert ingest.status_code == 401


@pytest.mark.anyio
async def test_project_github_webhook_events_self_heal_legacy_payload_json_schema(cp_app, tmp_path):
    legacy_db = tmp_path / "legacy_webhooks.db"
    conn = sqlite3.connect(legacy_db)
    conn.execute(
        """
        CREATE TABLE github_webhook_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            delivery_id TEXT,
            event_type TEXT NOT NULL,
            action TEXT,
            repository_full_name TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO github_webhook_events
            (project_id, delivery_id, event_type, action, repository_full_name, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "gh-legacy",
            "delivery-legacy",
            "push",
            "synchronize",
            "owner/repo",
            json.dumps({"hello": "world"}),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    from control_plane.github.webhook_store import GitHubWebhookStore

    cp_app.state.github_webhook_store = GitHubWebhookStore(db_path=str(legacy_db))

    async with AsyncClient(transport=ASGITransport(app=cp_app), base_url="http://test") as client:
        create_resp = await client.post(
            "/v1/projects",
            json={"id": "gh-legacy", "name": "Legacy GH", "mode": "isolated"},
        )
        assert create_resp.status_code == 200

        events = await client.get("/v1/projects/gh-legacy/github/webhook/events")
        assert events.status_code == 200
        rows = events.json()["events"]
        assert len(rows) == 1
        assert rows[0]["event_type"] == "push"
        assert rows[0]["payload"] == {"hello": "world"}


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
