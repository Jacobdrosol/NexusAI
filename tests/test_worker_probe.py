import json

import httpx
import pytest

from control_plane.worker_probe import (
    WorkerProbeError,
    probe_worker,
    select_worker_inference_model,
    verify_worker_inference,
    worker_base_url,
)
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


def test_worker_base_url_allows_docker_service_names_with_underscores():
    assert worker_base_url(_worker(host="worker_agent")) == "http://worker_agent:8010"


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
                    "auth_required_cli_tools": ["codex"],
                    "unauthenticated_cli_tools": ["codex"],
                    "discarded_declared_tool_capabilities": 0,
                    },
                },
            )
        return httpx.Response(404)

    result = await probe_worker(_worker(), client_factory=_client_factory(handler))

    assert result["probe_status"] == "ready"
    assert result["dispatch_eligible"] is True
    assert result["capability_attestation"]["installed_cli_tools"] == ["python"]
    assert result["capability_attestation"]["unauthenticated_cli_tools"] == ["codex"]
    assert all(check["status"] != "fail" for check in result["checks"])


@pytest.mark.anyio
async def test_probe_worker_keeps_only_safe_browser_attestation_evidence():
    async def handler(request):
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "worker_id": "worker-1", "browser_ready": True},
            )
        return httpx.Response(
            200,
            json={
                "worker_id": "worker-1",
                "configured_capabilities": [
                    {"type": "llm", "provider": "ollama_cloud", "models": ["glm-5.2:cloud"]}
                ],
                "capability_attestation": {
                    "browser": {
                        "configured": True,
                        "ready": True,
                        "browser": "chromium",
                        "base_url": "https://private.example",
                        "user_data_dir": "/private/profile",
                    }
                },
            },
        )

    result = await probe_worker(_worker(), client_factory=_client_factory(handler))

    assert result["health"]["browser_ready"] is True
    assert result["capability_attestation"]["browser"] == {
        "configured": True,
        "ready": True,
        "reason": "",
        "browser": "chromium",
    }


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


def test_select_worker_inference_model_requires_an_unambiguous_declared_model():
    worker = _worker(
        capabilities=[
            Capability(type="llm", provider="ollama_cloud", models=["one", "two"]),
        ]
    )

    with pytest.raises(WorkerProbeError, match="multiple LLM models"):
        select_worker_inference_model(worker)
    assert select_worker_inference_model(worker, provider="ollama_cloud", model="two") == (
        "ollama_cloud",
        "two",
    )


@pytest.mark.anyio
async def test_verify_worker_inference_records_only_safe_completion_metadata():
    async def handler(request):
        assert request.url.path == "/infer"
        assert json.loads(request.content) == {
            "provider": "ollama_cloud",
            "model": "glm-5.2:cloud",
            "messages": [{"role": "user", "content": "Return exactly READY."}],
            "params": {"max_tokens": 16, "temperature": 0},
        }
        return httpx.Response(200, json={"output": "READY", "finish_reason": "stop"})

    result = await verify_worker_inference(_worker(), client_factory=_client_factory(handler))

    assert result["verification_status"] == "ready"
    assert result["output_length"] == 5
    assert result["finish_reason"] == "stop"
    assert "READY" not in str(result)


@pytest.mark.anyio
async def test_verify_worker_inference_rejects_empty_final_output():
    async def handler(request):
        return httpx.Response(200, json={"output": ""})

    result = await verify_worker_inference(_worker(), client_factory=_client_factory(handler))

    assert result["verification_status"] == "failed"
    assert result["detail"] == "inference response did not contain final output"


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
