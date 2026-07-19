import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from worker_agent import main
from worker_agent.api import capabilities, health


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.anyio
async def test_register_with_control_plane_posts_the_declared_worker_config(monkeypatch):
    monkeypatch.setattr(main, "CONTROL_PLANE_URL", "http://control-plane")
    client = _FakeClient([httpx.Response(200, request=httpx.Request("POST", "http://control-plane/v1/workers"))])
    worker_config = {
        "id": "worker-1",
        "name": "Worker 1",
        "host": "worker-1",
        "port": 8001,
        "capabilities": [],
    }

    registered = await main._register_with_control_plane(worker_config, client)

    assert registered is True
    assert client.requests[0][0] == "http://control-plane/v1/workers"
    assert client.requests[0][1]["json"] == worker_config


@pytest.mark.anyio
async def test_register_with_control_plane_returns_false_for_an_error_response(monkeypatch):
    monkeypatch.setattr(main, "CONTROL_PLANE_URL", "http://control-plane")
    client = _FakeClient([httpx.Response(503, request=httpx.Request("POST", "http://control-plane/v1/workers"))])

    registered = await main._register_with_control_plane({"id": "worker-1"}, client)

    assert registered is False


@pytest.mark.anyio
async def test_worker_status_endpoints_use_the_capability_attestation_contract(monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(health.router)
    app.include_router(capabilities.router)
    app.state.worker_config = {
        "id": "worker-1",
        "capabilities": [{"type": "llm", "provider": "ollama", "models": ["llama3"]}],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        health_response = await client.get("/health")
        capabilities_response = await client.get("/capabilities")

    assert health_response.json()["enabled_cli_tools"] == []
    capabilities_payload = capabilities_response.json()
    assert capabilities_payload["configured_capabilities"] == app.state.worker_config["capabilities"]
    assert capabilities_payload["capability_attestation"]["enabled_cli_tools"] == []
    assert capabilities_payload["capability_attestation"]["provider_credentials"] == {}


@pytest.mark.anyio
async def test_worker_capability_attestation_reports_only_cloud_credential_readiness(monkeypatch):
    app = FastAPI()
    app.include_router(capabilities.router)
    app.state.worker_config = {
        "id": "worker-1",
        "capabilities": [
            {"type": "llm", "provider": "ollama_cloud", "models": ["glm-5.2:cloud"]}
        ],
    }

    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/capabilities")

    monkeypatch.setenv("OLLAMA_API_KEY", "test-key")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        configured = await client.get("/capabilities")

    assert missing.json()["capability_attestation"]["provider_credentials"] == {"ollama_cloud": False}
    assert configured.json()["capability_attestation"]["provider_credentials"] == {"ollama_cloud": True}
