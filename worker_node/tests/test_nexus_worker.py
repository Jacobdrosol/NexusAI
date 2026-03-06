from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from nexus_worker.api import capabilities, health, infer, infer_stream, models
from nexus_worker.observability import install_observability


@pytest.fixture
def nx_worker_app():
    app = FastAPI()
    install_observability(app)
    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.include_router(models.router)
    app.include_router(infer.router)
    app.include_router(infer_stream.router)
    app.state.worker_config = {
        "id": "nx1",
        "name": "Nexus Worker",
        "ollama_host": "http://localhost:11434",
        "capabilities": [],
    }
    return app


@pytest.mark.anyio
async def test_nexus_worker_health(nx_worker_app):
    async with AsyncClient(transport=ASGITransport(app=nx_worker_app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.anyio
async def test_nexus_worker_infer_ollama(nx_worker_app):
    async with AsyncClient(transport=ASGITransport(app=nx_worker_app), base_url="http://test") as client:
        with patch(
            "nexus_worker.services.inference.ollama_backend.infer",
            new=AsyncMock(return_value={"output": "ok", "usage": {}}),
        ):
            resp = await client.post(
                "/infer",
                json={
                    "model": "llama3",
                    "provider": "ollama",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
    assert resp.status_code == 200
    assert resp.json()["output"] == "ok"


@pytest.mark.anyio
async def test_nexus_worker_cloud_context_policy_block(nx_worker_app, monkeypatch):
    monkeypatch.setenv("NEXUS_WORKER_CLOUD_CONTEXT_POLICY", "block")
    async with AsyncClient(transport=ASGITransport(app=nx_worker_app), base_url="http://test") as client:
        resp = await client.post(
            "/infer",
            json={
                "model": "gpt-4o-mini",
                "provider": "openai",
                "messages": [{"role": "system", "content": "Context:\nprivate"}],
            },
        )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_nexus_worker_metrics_endpoint(nx_worker_app):
    async with AsyncClient(transport=ASGITransport(app=nx_worker_app), base_url="http://test") as client:
        await client.get("/health")
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "nexus_worker_http_requests_total" in resp.text
