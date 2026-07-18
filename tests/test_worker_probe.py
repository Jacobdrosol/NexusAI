import httpx
import pytest

from control_plane.worker_probe import probe_worker
from shared.models import Capability, Worker


def _worker(**overrides):
    values = {
        "id": "worker-1",
        "name": "Worker One",
        "host": "worker-1",
        "port": 8010,
        "status": "online",
        "enabled": True,
        "capabilities": [
            Capability(type="llm", provider="ollama_cloud", models=["glm-5.2:cloud"])
        ],
    }
    values.update(overrides)
    return Worker(**values)


def _client_factory(handler):
    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


@pytest.mark.anyio
async def test_probe_worker_reports_ready_for_matching_attested_runtime():
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "worker_id": "worker-1", "enabled_cli_tools": []},
            )
        if request.url.path == "/capabilities":
            return httpx.Response(
                200,
                json={
                    "worker_id": "worker-1",
                    "configured_capabilities": [
                        {"type": "llm", "provider": "ollama_cloud", "models": ["glm-5.2:cloud"]}
                    ],
                    "capability_attestation": {
                        "configured_cli_tools": [],
                        "installed_cli_tools": ["python"],
                        "enabled_cli_tools": [],
                        "unavailable_cli_tools": [],
                        "discarded_declared_tool_capabilities": 0,
                    },
                },
            )
        return httpx.Response(404)

    result = await probe_worker(_worker(), client_factory=_client_factory(handler))

    assert result["probe_status"] == "ready"
    assert result["dispatch_eligible"] is True
    assert result["capability_attestation"]["installed_cli_tools"] == ["python"]
    assert all(check["status"] != "fail" for check in result["checks"])


@pytest.mark.anyio
async def test_probe_worker_marks_capability_contract_mismatch_degraded():
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "worker_id": "worker-1"})
        return httpx.Response(
            200,
            json={
                "worker_id": "worker-1",
                "configured_capabilities": [],
                "capability_attestation": {"discarded_declared_tool_capabilities": "invalid"},
            },
        )

    result = await probe_worker(_worker(), client_factory=_client_factory(handler))

    assert result["probe_status"] == "degraded"
    contract = next(check for check in result["checks"] if check["name"] == "capability_contract")
    assert contract["status"] == "fail"
    assert "llm/ollama_cloud" in contract["detail"]
    assert result["capability_attestation"]["discarded_declared_tool_capabilities"] == 0


@pytest.mark.anyio
async def test_probe_worker_marks_missing_capability_endpoint_degraded():
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "worker_id": "worker-1"})
        return httpx.Response(404)

    result = await probe_worker(_worker(), client_factory=_client_factory(handler))

    assert result["probe_status"] == "degraded"
    assert result["checks"][-1] == {
        "name": "capability_report",
        "status": "fail",
        "detail": "capabilities endpoint returned HTTP 404",
    }


@pytest.mark.anyio
async def test_probe_worker_marks_unreachable_when_health_fails():
    async def handler(request):
        return httpx.Response(503)

    result = await probe_worker(_worker(), client_factory=_client_factory(handler))

    assert result["probe_status"] == "unreachable"
    assert result["checks"] == [
        {
            "name": "runtime_reachability",
            "status": "fail",
            "detail": "health endpoint returned HTTP 503",
        }
    ]


@pytest.mark.anyio
async def test_probe_worker_rejects_url_shaped_registered_host():
    result = await probe_worker(_worker(host="http://internal.example"))

    assert result["probe_status"] == "unreachable"
    assert result["checks"] == [
        {
            "name": "registered_address",
            "status": "fail",
            "detail": "registered worker host is invalid",
        }
    ]
