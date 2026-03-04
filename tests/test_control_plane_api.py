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
