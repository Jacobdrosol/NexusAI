"""Integration tests for control plane FastAPI routes."""
import pytest


@pytest.mark.anyio
async def test_health(cp_client):
    resp = await cp_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


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
