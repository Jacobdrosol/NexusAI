import httpx
import pytest

from worker_agent import main


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
