import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import BackendConfig, Bot, Capability, Task, Worker, WorkerMetrics


def test_backend_failure_message_includes_attempts():
    from control_plane.scheduler.scheduler import _backend_failure_message

    message = _backend_failure_message(
        "task-err",
        RuntimeError("timed out"),
        ["ollama_cloud/qwen3.5:397b-cloud: timed out"],
    )

    assert "All backends failed for task task-err: timed out." in message
    assert "Attempts: ollama_cloud/qwen3.5:397b-cloud: timed out." in message


def test_cloud_timeout_reads_env(monkeypatch):
    from control_plane.scheduler.scheduler import _cloud_timeout

    monkeypatch.setenv("NEXUSAI_CLOUD_API_TIMEOUT_SECONDS", "1800")

    assert _cloud_timeout() == 1800.0


def test_cloud_timeout_prefers_settings_manager(monkeypatch):
    from control_plane.scheduler import scheduler as scheduler_module

    class _FakeSettings:
        def get(self, key, default=None):
            assert key == "cloud_backend_timeout_seconds"
            return 2400

    monkeypatch.delenv("NEXUSAI_CLOUD_API_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(
        scheduler_module.SettingsManager,
        "instance",
        staticmethod(lambda: _FakeSettings()),
    )

    assert scheduler_module._cloud_timeout() == 2400.0


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


@pytest.mark.anyio
async def test_scheduler_injects_bot_system_prompt_into_payload():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-1",
        bot_id="course-outline",
        payload={"instruction": "build outline"},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert isinstance(result["payload"], list)
    assert result["payload"][0] == {"role": "system", "content": "Return only strict JSON."}
    assert result["payload"][1]["role"] == "user"
    assert '"instruction": "build outline"' in result["payload"][1]["content"]


@pytest.mark.anyio
async def test_scheduler_does_not_duplicate_existing_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-2",
        bot_id="course-outline",
        payload=[
            {"role": "system", "content": "Return only strict JSON."},
            {"role": "user", "content": "build outline"},
        ],
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"] == [
        {"role": "system", "content": "Return only strict JSON."},
        {"role": "user", "content": "build outline"},
    ]


@pytest.mark.anyio
async def test_scheduler_applies_bot_input_transform_before_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "instruction": "{{payload.instruction}}",
                    "course_brief": "{{payload.source_result.course_brief}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-3",
        bot_id="course-outline",
        payload={
            "instruction": "Build outline",
            "source_result": {
                "course_brief": {"topic": "AP World History", "subject": "History"}
            },
            "source_payload": {"noisy": True},
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"][0] == {"role": "system", "content": "Return only strict JSON."}
    transformed = json.loads(result["payload"][1]["content"])
    assert transformed == {
        "instruction": "Build outline",
        "course_brief": {"topic": "AP World History", "subject": "History"},
    }
