from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import BackendConfig, Capability, Worker, WorkerMetrics


@pytest.mark.anyio
async def test_scheduler_unpinned_backend_prefers_lower_weight_worker():
    from control_plane.scheduler.scheduler import Scheduler

    worker_a = Worker(
        id="w-a",
        name="Worker A",
        host="a.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=6, load=95.0, gpu_utilization=[90.0]),
    )
    worker_b = Worker(
        id="w-b",
        name="Worker B",
        host="b.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0, load=15.0, gpu_utilization=[10.0]),
    )
    worker_registry = AsyncMock()
    worker_registry.list.return_value = [worker_a, worker_b]
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3")

    selected = await scheduler._resolve_worker_for_llm_backend(backend)
    assert selected.id == "w-b"


@pytest.mark.anyio
async def test_scheduler_dispatch_tracks_latency_and_inflight():
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="w-lat",
        name="Worker Lat",
        host="lat.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0),
    )
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3", worker_id="w-lat")
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"output": "ok"}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())
    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        out = await scheduler._dispatch_to_worker(worker, backend, payload)

    assert out["output"] == "ok"
    runtime = scheduler.get_worker_runtime_metrics()
    assert "w-lat" in runtime
    assert runtime["w-lat"]["inflight"] == 0.0
    assert runtime["w-lat"]["latency_ema_ms"] > 0.0

