import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.models import BackendConfig, Bot, Capability, Task, TaskMetadata, Worker, WorkerMetrics


def test_backend_failure_message_includes_attempts():
    from control_plane.scheduler.scheduler import _backend_failure_message

    message = _backend_failure_message(
        "task-err",
        RuntimeError("timed out"),
        ["ollama_cloud/qwen3.5:397b-cloud: timed out"],
    )

    assert "All backends failed for task task-err: timed out." in message
    assert "Attempts: ollama_cloud/qwen3.5:397b-cloud: timed out." in message


def test_messages_for_ollama_preserve_tool_call_context():
    from control_plane.scheduler.scheduler import _messages_for_ollama

    normalized = _messages_for_ollama(
        [
            {"role": "user", "content": "Implement this feature."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc-1", "name": "search_files", "arguments": {"query": "ProgramSchedulerService"}},
                ],
            },
            {
                "role": "tool",
                "name": "search_files",
                "tool_call_id": "tc-1",
                "content": "--- acme.Server/Services/ProgramSchedulerService.cs ---",
            },
        ]
    )

    assert normalized[1]["role"] == "assistant"
    assert normalized[1]["tool_calls"][0]["function"]["name"] == "search_files"
    assert normalized[1]["tool_calls"][0]["function"]["arguments"] == {"query": "ProgramSchedulerService"}
    assert normalized[2]["role"] == "tool"
    assert normalized[2]["tool_call_id"] == "tc-1"
    assert normalized[2]["tool_name"] == "search_files"


def test_messages_for_openai_preserve_tool_call_context():
    from control_plane.scheduler.scheduler import _messages_for_openai

    normalized = _messages_for_openai(
        [
            {"role": "user", "content": "Implement this feature."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "tc-1", "name": "search_files", "arguments": {"query": "ProgramSchedulerService"}},
                ],
            },
            {
                "role": "tool",
                "name": "search_files",
                "tool_call_id": "tc-1",
                "content": "--- acme.Server/Services/ProgramSchedulerService.cs ---",
            },
        ]
    )

    assert normalized[1]["role"] == "assistant"
    assert normalized[1]["tool_calls"][0]["function"]["name"] == "search_files"
    assert json.loads(normalized[1]["tool_calls"][0]["function"]["arguments"]) == {"query": "ProgramSchedulerService"}
    assert normalized[2]["role"] == "tool"
    assert normalized[2]["tool_call_id"] == "tc-1"
    assert normalized[2]["name"] == "search_files"


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


def test_agent_workspace_context_reads_hidden_chat_marker(tmp_path):
    from control_plane.scheduler.scheduler import _agent_workspace_context

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)

    bot = Bot(
        id="inline-coder",
        name="Inline Coder",
        role="coder",
        backends=[],
        execution_policy={
            "repo_output_mode": "allow",
            "workspace_context_injection": True,
        },
    )
    task = Task(
        id="chat-1",
        bot_id=bot.id,
        payload=[
            {"role": "user", "content": "please code this"},
            {"role": "system", "content": "", "_workspace_root": str(workspace_root)},
        ],
        created_at="2026-04-08T00:00:00Z",
        updated_at="2026-04-08T00:00:00Z",
    )

    resolved_root, allow_writes = _agent_workspace_context(bot, task)
    assert resolved_root == workspace_root
    assert allow_writes is True


@pytest.mark.anyio
async def test_test_task_skips_cli_backend_and_disables_workspace_writes(tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir()
    bot = Bot(
        id="test-run-bot",
        name="Test Run Bot",
        role="coder",
        backends=[
            BackendConfig(type="cli", provider="cli", model="codex", worker_id="coder-worker"),
            BackendConfig(type="cloud_api", provider="ollama_cloud", model="glm-5.2:cloud"),
        ],
        execution_policy={
            "repo_output_mode": "allow",
            "workspace_context_injection": True,
        },
    )
    task = Task(
        id="safe-test-1",
        bot_id=bot.id,
        payload={"instruction": "Review the workspace", "_injected_workspace_root": str(workspace_root)},
        metadata=TaskMetadata(source="bot_test", execution_mode="test"),
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:00Z",
    )
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    agent_loop = AsyncMock(return_value={"output": "analysis only"})
    scheduler._run_agent_loop = agent_loop  # type: ignore[method-assign]

    result = await scheduler.schedule(task)

    assert result == {"output": "analysis only"}
    dispatched_backend = agent_loop.await_args.args[0]
    assert dispatched_backend.type == "cloud_api"
    assert agent_loop.await_args.kwargs["allow_writes"] is False
    assert "Execution mode is TEST" in str(agent_loop.await_args.args[1])


@pytest.mark.anyio
async def test_test_task_rejects_direct_cli_and_custom_dispatch():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    task = Task(
        id="safe-test-2",
        bot_id="bot",
        payload={"instruction": "test"},
        metadata=TaskMetadata(execution_mode="test"),
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:00Z",
    )
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    for backend in (
        BackendConfig(type="cli", provider="cli", model="codex", worker_id="worker"),
        BackendConfig(type="custom", provider="http_connection", model="connection"),
    ):
        with pytest.raises(BackendError, match="Test-mode tasks do not execute"):
            await scheduler._dispatch_backend(backend, {"instruction": "test"}, task=task)


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
async def test_scheduler_unpinned_local_backend_skips_busy_worker():
    from control_plane.scheduler.scheduler import Scheduler

    worker_a = Worker(
        id="w-a",
        name="Worker A",
        host="a.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0, load=10.0, gpu_utilization=[5.0]),
    )
    worker_b = Worker(
        id="w-b",
        name="Worker B",
        host="b.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=3, load=60.0, gpu_utilization=[50.0]),
    )
    worker_registry = AsyncMock()
    worker_registry.list.return_value = [worker_a, worker_b]
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)
    scheduler._inflight_by_worker["w-a"] = 1
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3")

    selected = await scheduler._resolve_worker_for_llm_backend(backend)
    assert selected.id == "w-b"


@pytest.mark.anyio
async def test_scheduler_pinned_local_backend_rejects_second_inflight_task():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="w-local",
        name="Worker Local",
        host="local.example",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0),
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)
    scheduler._inflight_by_worker["w-local"] = 1
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3", worker_id="w-local")

    with pytest.raises(BackendError, match="has no remaining task capacity"):
        await scheduler._resolve_worker_for_llm_backend(backend)


@pytest.mark.anyio
async def test_scheduler_browser_backend_rejects_second_inflight_task():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.example",
        port=8001,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
        metrics=WorkerMetrics(queue_depth=0),
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)
    scheduler._inflight_by_worker[worker.id] = 1
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
    )

    with pytest.raises(BackendError, match="has no remaining task capacity"):
        await scheduler._resolve_browser_worker(backend)


@pytest.mark.anyio
async def test_scheduler_rejects_worker_missing_declared_bot_tools():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.example",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
    )
    bot = Bot(
        id="browser-bot",
        name="Browser Bot",
        role="worker",
        backends=[BackendConfig(type="local_llm", provider="ollama", model="llama3", worker_id=worker.id)],
        execution_policy={"required_worker_tools": ["browser-ui"]},
    )
    task = Task(
        id="task-browser",
        bot_id=bot.id,
        payload={"instruction": "Open the browser"},
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:00Z",
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with pytest.raises(BackendError, match="missing required tool capabilities: browser-ui"):
        await scheduler._resolve_worker_for_llm_backend(bot.backends[0], task=task)

    worker.capabilities.append(Capability(type="tool", provider="cli", models=["browser-ui"]))
    selected = await scheduler._resolve_worker_for_llm_backend(bot.backends[0], task=task)

    assert selected.id == worker.id


@pytest.mark.anyio
async def test_scheduled_worker_dispatch_requires_recent_ready_probe():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="scheduled-worker",
        name="Scheduled Worker",
        host="scheduled.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
    )
    task = Task(
        id="scheduled-task",
        bot_id="scheduled-bot",
        payload={"instruction": "Run the scheduled report"},
        metadata=TaskMetadata(source="agent_schedule"),
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:00Z",
    )
    probe_store = type("ProbeStore", (), {"get": AsyncMock(return_value=None)})()
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        worker_probe_store=probe_store,
    )

    with pytest.raises(BackendError, match="recent ready probe"):
        await scheduler._require_fresh_autonomous_worker_probe(worker, task)


@pytest.mark.anyio
async def test_scheduled_worker_dispatch_accepts_recent_ready_probe():
    from datetime import datetime, timezone

    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="scheduled-worker",
        name="Scheduled Worker",
        host="scheduled.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
    )
    task = Task(
        id="scheduled-task",
        bot_id="scheduled-bot",
        payload={"instruction": "Run the scheduled report"},
        metadata=TaskMetadata(source="agent_schedule"),
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:00Z",
    )
    probe_store = type(
        "ProbeStore",
        (),
        {
            "get": AsyncMock(
                return_value={
                    "worker_id": worker.id,
                    "probe_status": "ready",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        },
    )()
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        worker_probe_store=probe_store,
    )

    await scheduler._require_fresh_autonomous_worker_probe(worker, task)


@pytest.mark.anyio
async def test_interactive_worker_dispatch_does_not_require_probe_store():
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="interactive-worker",
        name="Interactive Worker",
        host="interactive.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        status="online",
        enabled=True,
    )
    task = Task(
        id="interactive-task",
        bot_id="interactive-bot",
        payload={"instruction": "Answer this chat request"},
        metadata=TaskMetadata(source="chat"),
        created_at="2026-07-18T00:00:00Z",
        updated_at="2026-07-18T00:00:00Z",
    )
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    await scheduler._require_fresh_autonomous_worker_probe(worker, task)


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
async def test_scheduler_dispatch_uses_declared_worker_request_token(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="w-token",
        name="Worker Token",
        host="token.local",
        port=8001,
        capabilities=[Capability(type="llm", provider="ollama", models=["llama3"])],
        request_token_env="NEXUS_WORKER_REQUEST_TOKEN",
        status="online",
        enabled=True,
    )
    backend = BackendConfig(type="local_llm", provider="ollama", model="llama3", worker_id=worker.id)
    monkeypatch.setenv("NEXUS_WORKER_REQUEST_TOKEN", "node-token")

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"output": "ok"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())
    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_to_worker(
            worker,
            backend,
            [{"role": "user", "content": "hello"}],
        )

    assert result == {"output": "ok"}
    assert mock_client.post.await_args.kwargs["headers"] == {"X-Nexus-Worker-Token": "node-token"}


@pytest.mark.anyio
async def test_scheduler_dispatches_fixed_cli_command_to_worker():
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="w-cli",
        name="CLI Worker",
        host="cli.local",
        port=8001,
        capabilities=[Capability(type="tool", provider="cli", models=["claude"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="cli",
        provider="cli",
        model="claude",
        command="claude -p",
        worker_id="w-cli",
    )

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"output": "ok"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())
    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_to_worker(
            worker,
            backend,
            [{"role": "user", "content": "Review this change."}],
        )

    assert result == {"output": "ok"}
    request_body = mock_client.post.await_args.kwargs["json"]
    assert request_body["provider"] == "cli"
    assert request_body["command"] == "claude -p"


@pytest.mark.anyio
async def test_scheduler_dispatches_scoped_browser_inspection_with_worker_token(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"url": "https://app.example/admin/courses", "text": "Courses"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(
            backend,
            {"path": "/admin/courses", "text_limit": 500},
        )

    assert result["text"] == "Courses"
    assert mock_client.post.await_args.args[0] == "http://browser.local:8010/browser/inspect"
    assert mock_client.post.await_args.kwargs["json"] == {"path": "/admin/courses", "text_limit": 500}
    assert mock_client.post.await_args.kwargs["headers"] == {"X-Nexus-Worker-Token": "worker-token"}


@pytest.mark.anyio
async def test_scheduler_allows_browser_inspection_in_bot_path_allowlist(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="course-evidence",
        name="Course Evidence",
        role="browser-evidence-inspector",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_inspection_path_allowlist": ["/admin/courses/57/lessons"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-course-evidence",
        bot_id=bot.id,
        payload={"path": "/admin/courses/57/lessons"},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"url": "https://app.example/admin/courses/57/lessons", "text": "Lessons"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(backend, task.payload, task=task)

    assert result["text"] == "Lessons"
    assert mock_client.post.await_args.kwargs["json"] == {"path": "/admin/courses/57/lessons"}


@pytest.mark.anyio
async def test_scheduler_rejects_browser_inspection_outside_bot_path_allowlist(monkeypatch):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="course-evidence",
        name="Course Evidence",
        role="browser-evidence-inspector",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_inspection_path_allowlist": ["/admin/courses/57/lessons"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-course-evidence",
        bot_id=bot.id,
        payload={"path": "/admin/courses/57"},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with pytest.raises(BackendError, match="is not authorized to inspect path /admin/courses/57"):
        await scheduler._dispatch_backend(backend, task.payload, task=task)


@pytest.mark.anyio
async def test_scheduler_captures_pinned_worker_execution_provenance(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    task = Task(
        id="task-browser-provenance",
        bot_id="browser-bot",
        payload={"path": "/admin/courses"},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"url": "https://app.example/admin/courses", "text": "Courses"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        await scheduler._dispatch_backend(backend, task.payload, task=task)

    provenance = scheduler.consume_task_execution_provenance(task.id)
    assert provenance is not None
    assert provenance["backend_type"] == "browser"
    assert provenance["provider"] == "browser"
    assert provenance["model"] == "browser-ui"
    assert provenance["worker_id"] == worker.id
    assert scheduler.consume_task_execution_provenance(task.id) is None


@pytest.mark.anyio
async def test_scheduler_rejects_browser_backend_without_a_pinned_attested_worker():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())
    backend = BackendConfig(type="browser", provider="browser", model="browser-ui")

    with pytest.raises(BackendError, match="worker_id is required"):
        await scheduler._dispatch_backend(backend, {"path": "/admin/courses"})


@pytest.mark.anyio
async def test_scheduler_dispatches_only_allowlisted_documentation_writes(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="docs-writer-01",
        name="Docs Writer",
        host="docs-writer.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="documentation", models=["documentation-v1"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="documentation",
        provider="documentation",
        model="documentation-v1",
        worker_id=worker.id,
        api_key_ref="DOCUMENTATION_WORKER_TOKEN",
    )
    bot = Bot(
        id="docs-hub-writer",
        name="Docs Hub Writer",
        role="docs-hub-writer",
        execution_policy={
            "required_worker_tools": ["documentation-v1"],
            "documentation_action_allowlist": ["documentation.create"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-docs-write",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("DOCUMENTATION_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "action": "create",
        "path": "docs/Automation_Workforce/Docs_Dana/activity.md",
        "content_hash": "a" * 64,
        "bytes_written": 8,
    }
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(
            backend,
            {
                "action": "create",
                "path": "docs/Automation_Workforce/Docs_Dana/activity.md",
                "content": "# Report",
            },
            task=task,
        )

    assert result["action"] == "create"
    assert mock_client.post.await_args.args[0] == "http://docs-writer.local:8010/documentation/write"
    assert mock_client.post.await_args.kwargs["json"] == {
        "action": "create",
        "path": "docs/Automation_Workforce/Docs_Dana/activity.md",
        "content": "# Report",
    }


@pytest.mark.anyio
async def test_scheduler_rejects_documentation_action_not_in_bot_policy():
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="docs-writer-01",
        name="Docs Writer",
        host="docs-writer.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="documentation", models=["documentation-v1"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="documentation",
        provider="documentation",
        model="documentation-v1",
        worker_id=worker.id,
        api_key_ref="DOCUMENTATION_WORKER_TOKEN",
    )
    bot = Bot(
        id="docs-hub-writer",
        name="Docs Hub Writer",
        role="docs-hub-writer",
        execution_policy={"documentation_action_allowlist": ["documentation.create"]},
        backends=[backend],
    )
    task = Task(id="task-docs-save", bot_id=bot.id, payload={}, created_at="now", updated_at="now")
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with pytest.raises(BackendError, match="not authorized for documentation.save"):
        await scheduler._dispatch_backend(
            backend,
            {
                "action": "save",
                "path": "docs/Automation_Workforce/Docs_Dana/activity.md",
                "content": "# Report",
                "expectedContentHash": "a" * 64,
            },
            task=task,
        )


@pytest.mark.anyio
async def test_scheduler_dispatches_only_authorized_draft_test_builder_actions(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="assessment-configurator",
        name="Assessment Configurator",
        role="assessment-configurator",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_action_allowlist": ["test_builder.save_configuration"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-assessment-config",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"status": "Test configuration saved successfully"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "test_builder",
                "action": "save_configuration",
                "mode": "draft",
                "confirmation": "approved:test-builder:save_configuration",
                "course_id": 60,
                "lesson_id": 601,
                "pass_threshold_pct": 70,
                "allow_review": False,
                "banks": [{"name": "Lesson 1", "easy": 1}],
            },
            task=task,
        )

    assert result["status"] == "Test configuration saved successfully"
    assert mock_client.post.await_args.args[0] == "http://browser.local:8010/browser/test-builder"
    assert mock_client.post.await_args.kwargs["json"]["action"] == "save_configuration"
    assert "browser_action" not in mock_client.post.await_args.kwargs["json"]


@pytest.mark.anyio
async def test_scheduler_rejects_browser_publish_before_worker_dispatch(monkeypatch):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)

    with pytest.raises(BackendError, match="cannot publish"):
        await scheduler._dispatch_backend(
            backend,
            {"browser_action": "test_builder", "action": "publish", "mode": "draft"},
        )


@pytest.mark.anyio
async def test_scheduler_dispatches_only_authorized_existing_question_patch(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="question-patcher",
        name="Question Patcher",
        role="question-patcher",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_action_allowlist": ["question_bank.patch_existing"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-question-patch",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"status": "Question Bank patch saved and verified"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "question_bank",
                "action": "patch_existing",
                "confirmation": "approved:question-bank:patch_existing:42:7",
                "bank_id": 42,
                "question_id": 7,
                "expected": {"prompt": "What is 2 + 2?", "question_type": "MCQ"},
                "changes": {"prompt": "What is 3 + 1?"},
                "review_evidence": {
                    "reviewer_bot_id": "acme-question-bank-review-01-bot",
                    "review_task_id": "review-42-7",
                    "approved_patch": True,
                    "semantic_duplicate_risk": "materially_distinct_context",
                    "reviewed_question_ids": [7],
                    "shortage_detected": False,
                    "rationale": "The reviewer checked the target and comparable questions for duplication.",
                },
            },
            task=task,
        )

    assert result["status"] == "Question Bank patch saved and verified"
    assert mock_client.post.await_args.args[0] == "http://browser.local:8010/browser/question-bank"
    assert mock_client.post.await_args.kwargs["json"]["action"] == "patch_existing"
    assert "browser_action" not in mock_client.post.await_args.kwargs["json"]


@pytest.mark.anyio
async def test_scheduler_rejects_question_bank_create_before_worker_dispatch(monkeypatch):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=worker_registry)

    with pytest.raises(BackendError, match="Unsupported Question Bank action"):
        await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "question_bank",
                "action": "create_question",
                "bank_id": 42,
                "question_id": 7,
                "expected": {},
                "changes": {},
            },
        )


@pytest.mark.anyio
async def test_scheduler_dispatches_only_authorized_single_question_creation(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="question-adder",
        name="Question Adder",
        role="question-bank-shortage-adder",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_action_allowlist": ["question_bank.create_one"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-question-create",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"status": "Question Bank question created and verified"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)
    review_task_id = "review-42-shortage"

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "question_bank",
                "action": "create_one",
                "confirmation": f"approved:question-bank:create_one:42:{review_task_id}",
                "bank_id": 42,
                "candidate": {
                    "prompt": "A shopper has two apples and receives one more. How many apples are there?",
                    "question_type": "MCQ",
                    "difficulty": "easy",
                    "category": "Counting",
                    "is_active": True,
                    "options": ["2", "3", "4"],
                    "correct_option_index": 1,
                },
                "review_evidence": {
                    "reviewer_bot_id": "acme-question-bank-review-01-bot",
                    "review_task_id": review_task_id,
                    "approved_create": True,
                    "semantic_duplicate_risk": "materially_distinct_context",
                    "reviewed_question_ids": [7, 8, 9],
                    "existing_question_count": 3,
                    "minimum_required_count": 4,
                    "shortage_detected": True,
                    "rationale": "The reviewer inspected every existing question and found a verified shortage.",
                },
            },
            task=task,
        )

    assert result["status"] == "Question Bank question created and verified"
    assert mock_client.post.await_args.args[0] == "http://browser.local:8010/browser/question-bank-create"
    assert mock_client.post.await_args.kwargs["json"]["action"] == "create_one"
    assert "browser_action" not in mock_client.post.await_args.kwargs["json"]


@pytest.mark.anyio
async def test_scheduler_rejects_question_bank_creation_without_create_allowlist(monkeypatch):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="question-patcher",
        name="Question Patcher",
        role="question-patcher",
        execution_policy={"browser_action_allowlist": ["question_bank.patch_existing"]},
        backends=[backend],
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)
    task = Task(id="task-question-create", bot_id="question-patcher", payload={}, created_at="now", updated_at="now")

    with pytest.raises(BackendError, match="not authorized for question_bank.create_one"):
        await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "question_bank",
                "action": "create_one",
                "confirmation": "approved:question-bank:create_one:42:review-42-shortage",
                "bank_id": 42,
                "candidate": {},
                "review_evidence": {},
            },
            task=task,
        )


@pytest.mark.anyio
async def test_scheduler_dispatches_only_authorized_question_bank_evidence_export(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot = Bot(
        id="question-evidence-exporter",
        name="Question Evidence Exporter",
        role="question-bank-evidence-exporter",
        execution_policy={
            "required_worker_tools": ["browser-ui"],
            "browser_action_allowlist": ["question_bank.export_evidence"],
        },
        backends=[backend],
    )
    task = Task(
        id="task-question-evidence",
        bot_id=bot.id,
        payload={},
        created_at="now",
        updated_at="now",
    )
    monkeypatch.setenv("BROWSER_WORKER_TOKEN", "worker-token")
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"status": "Question Bank evidence exported from the UI"}
    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    bot_registry = AsyncMock()
    bot_registry.get.return_value = bot
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "question_bank",
                "action": "export_evidence",
                "bank_id": 42,
                "approvedReadOnlyActions": ["export json"],
            },
            task=task,
        )

    assert result["status"] == "Question Bank evidence exported from the UI"
    assert mock_client.post.await_args.args[0] == "http://browser.local:8010/browser/question-bank-export"
    assert mock_client.post.await_args.kwargs["json"] == {
        "action": "export_evidence",
        "bank_id": 42,
        "approvedReadOnlyActions": ["export json"],
    }


@pytest.mark.anyio
async def test_scheduler_rejects_question_bank_evidence_export_without_allowlist(monkeypatch):
    from control_plane.scheduler.scheduler import BackendError, Scheduler

    worker = Worker(
        id="browser-worker",
        name="Browser Worker",
        host="browser.local",
        port=8010,
        capabilities=[Capability(type="tool", provider="browser", models=["browser-ui"])],
        status="online",
        enabled=True,
    )
    backend = BackendConfig(
        type="browser",
        provider="browser",
        model="browser-ui",
        worker_id=worker.id,
        api_key_ref="BROWSER_WORKER_TOKEN",
    )
    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="question-patcher",
        name="Question Patcher",
        role="question-patcher",
        execution_policy={"browser_action_allowlist": ["question_bank.patch_existing"]},
        backends=[backend],
    )
    worker_registry = AsyncMock()
    worker_registry.get.return_value = worker
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=worker_registry)
    task = Task(
        id="task-question-evidence",
        bot_id="question-patcher",
        payload={},
        created_at="now",
        updated_at="now",
    )

    with pytest.raises(BackendError, match="not authorized for question_bank.export_evidence"):
        await scheduler._dispatch_backend(
            backend,
            {
                "browser_action": "question_bank",
                "action": "export_evidence",
                "bank_id": 42,
                "approvedReadOnlyActions": ["export json"],
            },
            task=task,
        )


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
    system_message = result["payload"][0]
    assert system_message["role"] == "system"
    assert system_message["content"].startswith("Return only strict JSON.")
    assert "Execution policy:" in system_message["content"]
    assert "validation-only or planning-only" in system_message["content"]
    assert result["payload"][1]["role"] == "user"
    assert '"instruction": "build outline"' in result["payload"][1]["content"]


@pytest.mark.anyio
async def test_run_agent_loop_forces_tool_followup_for_writable_runs(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder:480b")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_messages: list[list[dict[str, Any]]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        state["count"] += 1
        call_messages.append([dict(item) for item in messages])
        if state["count"] == 1:
            return {"output": "Please tell me the specific task.", "tool_calls": [], "usage": {}}
        if state["count"] == 2:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-1", "name": "list_tree", "arguments": {}}],
                "usage": {},
            }
        if state["count"] == 3:
            return {
                "output": "",
                "tool_calls": [
                    {"id": "tc-2", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}
                ],
                "usage": {},
            }
        return {"output": "Implemented changes.", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [{"type": "function", "function": {"name": "list_tree"}}, {"type": "function", "function": {"name": "write_file"}}],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=6,
    )

    assert result.get("output") == "Implemented changes."
    assert state["count"] == 4
    assert any(str(item.get("name") or "") == "list_tree" for item in (result.get("tool_calls_executed") or []))
    assert any(str(item.get("name") or "") == "write_file" for item in (result.get("tool_calls_executed") or []))
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("allow_writes") is True
    assert diagnostics.get("observed_write_tool_call") is True
    assert any(
        str(message.get("role") or "") == "system"
        and "Tool-use requirement (mandatory for this writable coding run)" in str(message.get("content") or "")
        for message in call_messages[1]
    )


@pytest.mark.anyio
async def test_run_agent_loop_forces_discovery_when_only_workspace_tree_was_used(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_messages: list[list[dict[str, Any]]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        state["count"] += 1
        call_messages.append([dict(item) for item in messages])
        if state["count"] == 1:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-1", "name": "workspace_tree", "arguments": {}}],
                "usage": {},
            }
        if state["count"] == 2:
            return {"output": "I need to inspect more files first.", "tool_calls": [], "usage": {}}
        if state["count"] == 3:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-2", "name": "read_file", "arguments": {"path": "demo.txt"}}],
                "usage": {},
            }
        if state["count"] == 4:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-3", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}],
                "usage": {},
            }
        return {"output": "Implemented changes.", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "workspace_tree"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "write_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=8,
    )

    assert result.get("output") == "Implemented changes."
    assert state["count"] == 5
    assert any(str(item.get("name") or "") == "workspace_tree" for item in (result.get("tool_calls_executed") or []))
    assert any(str(item.get("name") or "") == "read_file" for item in (result.get("tool_calls_executed") or []))
    assert any(str(item.get("name") or "") == "write_file" for item in (result.get("tool_calls_executed") or []))
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("observed_non_tree_tool_call") is True
    assert diagnostics.get("observed_write_tool_call") is True
    assert any(
        str(message.get("role") or "") == "system"
        and "Discovery requirement (mandatory for this writable coding run)" in str(message.get("content") or "")
        for message in call_messages[2]
    )


@pytest.mark.anyio
async def test_run_agent_loop_treats_list_directory_as_insufficient_discovery(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_messages: list[list[dict[str, Any]]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        state["count"] += 1
        call_messages.append([dict(item) for item in messages])
        if state["count"] == 1:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-1", "name": "workspace_tree", "arguments": {}}],
                "usage": {},
            }
        if state["count"] == 2:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-2", "name": "list_directory", "arguments": {"path": "."}}],
                "usage": {},
            }
        if state["count"] == 3:
            return {"output": "I need to inspect more files first.", "tool_calls": [], "usage": {}}
        if state["count"] == 4:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-3", "name": "read_file", "arguments": {"path": "demo.txt"}}],
                "usage": {},
            }
        if state["count"] == 5:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-4", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}],
                "usage": {},
            }
        return {"output": "Implemented changes.", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "workspace_tree"}},
            {"type": "function", "function": {"name": "list_directory"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "write_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=9,
    )

    assert result.get("output") == "Implemented changes."
    assert state["count"] == 6
    names = [str(item.get("name") or "") for item in (result.get("tool_calls_executed") or [])]
    assert "workspace_tree" in names
    assert "list_directory" in names
    assert "read_file" in names
    assert "write_file" in names
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("observed_non_tree_tool_call") is True
    assert diagnostics.get("observed_write_tool_call") is True
    assert diagnostics.get("forced_followups_used", 0) >= 1
    assert any(
        str(message.get("role") or "") == "system"
        and "Discovery requirement (mandatory for this writable coding run)" in str(message.get("content") or "")
        for message in call_messages[3]
    )


@pytest.mark.anyio
async def test_run_agent_loop_disables_navigation_tools_and_then_forces_write_tools(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_tools: list[list[str]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        tool_names = []
        for tool in (tools or []):
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") or {}
            tool_names.append(str(fn.get("name") or ""))
        call_tools.append(tool_names)
        state["count"] += 1
        if state["count"] == 1:
            return {"output": "", "tool_calls": [{"id": "tc-1", "name": "workspace_tree", "arguments": {}}], "usage": {}}
        if state["count"] == 2:
            return {"output": "", "tool_calls": [], "usage": {}}
        if state["count"] == 3:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-2", "name": "read_file", "arguments": {"path": "demo.txt"}}],
                "usage": {},
            }
        if state["count"] == 4:
            return {"output": "", "tool_calls": [], "usage": {}}
        if state["count"] == 5:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-3", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}],
                "usage": {},
            }
        return {"output": "done", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "workspace_tree"}},
            {"type": "function", "function": {"name": "list_directory"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "search_files"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "edit_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=8,
    )

    # Call 1 uses full tool set.
    assert "workspace_tree" in call_tools[0]
    assert "list_directory" in call_tools[0]
    # After discovery followup, navigation tools are removed.
    assert "workspace_tree" not in call_tools[2]
    assert "list_directory" not in call_tools[2]
    # After write followup, tool set is write-priority (read/search + write).
    assert set(call_tools[4]) == {"read_file", "search_files", "write_file", "edit_file"}
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("navigation_tools_disabled") is True
    assert diagnostics.get("write_tools_only") is True
    assert diagnostics.get("observed_write_tool_call") is True


@pytest.mark.anyio
async def test_run_agent_loop_escalates_to_write_only_after_discovery_budget(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_tools: list[list[str]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        tool_names = []
        for tool in (tools or []):
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") or {}
            tool_names.append(str(fn.get("name") or ""))
        call_tools.append(tool_names)
        state["count"] += 1
        if state["count"] <= 3:
            return {
                "output": "",
                "tool_calls": [{"id": f"tc-{state['count']}", "name": "search_files", "arguments": {"query": "Program"}}],
                "usage": {},
            }
        if state["count"] == 4:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-4", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}],
                "usage": {},
            }
        return {"output": "done", "tool_calls": [], "usage": {}}

    monkeypatch.setenv("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_WRITE", "2")
    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "workspace_tree"}},
            {"type": "function", "function": {"name": "list_directory"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "search_files"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "edit_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=6,
    )

    # Initial calls include discovery tools.
    assert "search_files" in call_tools[0]
    assert "search_files" in call_tools[1]
    # After discovery budget is hit, tool set is escalated to write-priority.
    assert set(call_tools[2]) == {"read_file", "search_files", "write_file", "edit_file"}
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("write_tools_only") is True
    assert diagnostics.get("proactive_write_escalations", 0) >= 1
    assert diagnostics.get("observed_write_tool_call") is True


@pytest.mark.anyio
async def test_run_agent_loop_escalates_to_strict_write_only_after_extended_discovery(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_tools: list[list[str]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        tool_names = []
        for tool in (tools or []):
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") or {}
            tool_names.append(str(fn.get("name") or ""))
        call_tools.append(tool_names)
        state["count"] += 1
        if state["count"] <= 3:
            return {
                "output": "",
                "tool_calls": [{"id": f"tc-{state['count']}", "name": "search_files", "arguments": {"query": "Program"}}],
                "usage": {},
            }
        if state["count"] == 4:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-4", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}],
                "usage": {},
            }
        return {"output": "done", "tool_calls": [], "usage": {}}

    monkeypatch.setenv("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_WRITE", "2")
    monkeypatch.setenv("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_STRICT_WRITE", "3")
    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "workspace_tree"}},
            {"type": "function", "function": {"name": "list_directory"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "search_files"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "edit_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=8,
    )

    assert set(call_tools[2]) == {"read_file", "search_files", "write_file", "edit_file"}
    assert set(call_tools[3]) == {"write_file", "edit_file"}
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("write_tools_only") is True
    assert diagnostics.get("strict_write_only") is True
    assert diagnostics.get("strict_write_escalations", 0) >= 1
    assert diagnostics.get("observed_write_tool_call") is True


@pytest.mark.anyio
async def test_run_agent_loop_rejects_non_write_calls_in_strict_mode(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    call_tools: list[list[str]] = []
    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        tool_names = []
        for tool in (tools or []):
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") or {}
            tool_names.append(str(fn.get("name") or ""))
        call_tools.append(tool_names)
        state["count"] += 1
        if state["count"] <= 2:
            return {
                "output": "",
                "tool_calls": [{"id": f"tc-{state['count']}", "name": "search_files", "arguments": {"query": "Program"}}],
                "usage": {},
            }
        if state["count"] == 3:
            # Simulates a model continuing to request a non-write tool even after strict escalation.
            return {
                "output": "",
                "tool_calls": [{"id": "tc-3", "name": "read_file", "arguments": {"path": "demo.txt"}}],
                "usage": {},
            }
        if state["count"] == 4:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-4", "name": "edit_file", "arguments": {"path": "demo.txt", "old_text": "a", "new_text": "b"}}],
                "usage": {},
            }
        return {"output": "done", "tool_calls": [], "usage": {}}

    monkeypatch.setenv("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_WRITE", "1")
    monkeypatch.setenv("NEXUSAI_AGENT_DISCOVERY_ITERATIONS_BEFORE_STRICT_WRITE", "2")
    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "workspace_tree"}},
            {"type": "function", "function": {"name": "list_directory"}},
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "search_files"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "edit_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=9,
    )

    assert set(call_tools[2]) == {"write_file", "edit_file"}
    executed = result.get("tool_calls_executed") or []
    assert any(bool(item.get("rejected_in_strict_write_mode")) for item in executed if isinstance(item, dict))
    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("strict_write_only") is True
    assert diagnostics.get("strict_mode_rejected_tool_calls", 0) >= 1
    assert diagnostics.get("observed_write_tool_call") is True


@pytest.mark.anyio
async def test_run_agent_loop_rejects_noop_edit_as_non_material_write(monkeypatch, tmp_path):
    from control_plane.scheduler.scheduler import Scheduler

    workspace_root = tmp_path / "repo"
    workspace_root.mkdir(parents=True, exist_ok=True)
    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3-coder-next")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock())

    state = {"count": 0}

    async def _fake_call_backend_raw(_backend, messages, *, tools=None, task=None):
        state["count"] += 1
        if state["count"] == 1:
            return {
                "output": "",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "name": "edit_file",
                        "arguments": {"path": "demo.txt", "old_text": "same", "new_text": "same"},
                    }
                ],
                "usage": {},
            }
        if state["count"] == 2:
            return {
                "output": "",
                "tool_calls": [{"id": "tc-2", "name": "write_file", "arguments": {"path": "demo.txt", "content": "ok"}}],
                "usage": {},
            }
        return {"output": "done", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(scheduler, "_call_backend_raw", _fake_call_backend_raw)
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.get_tool_definitions",
        lambda allow_writes=False: [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "search_files"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "edit_file"}},
        ],
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.parse_tool_call_arguments",
        lambda value: value if isinstance(value, dict) else {},
    )
    monkeypatch.setattr(
        "control_plane.scheduler.agent_workspace_tools.execute_tool",
        lambda name, args, root, allow_writes=False: f"tool={name}",
    )
    monkeypatch.setattr(
        "control_plane.chat.workspace_tools.normalize_workspace_root",
        lambda value: workspace_root,
    )

    result = await scheduler._run_agent_loop(
        backend=backend,
        prepared_payload=[{"role": "user", "content": "Can you code this?"}],
        workspace_root=workspace_root,
        allow_writes=True,
        max_iterations=5,
    )

    diagnostics = result.get("agent_loop_diagnostics") or {}
    assert diagnostics.get("no_op_write_tool_requests", 0) >= 1
    assert diagnostics.get("observed_write_tool_call") is True
    executed = result.get("tool_calls_executed") or []
    assert any(bool(item.get("rejected_no_op_edit")) for item in executed if isinstance(item, dict))


@pytest.mark.anyio
async def test_scheduler_injects_retry_guidance_into_payload():
    from control_plane.scheduler.scheduler import Scheduler
    from shared.models import TaskError, TaskMetadata

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="doc-writer",
        name="Doc Writer",
        role="writer",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-retry-1",
        bot_id="doc-writer",
        payload={"instruction": "fix docs"},
        status="queued",
        created_at="now",
        updated_at="now",
        metadata=TaskMetadata(retry_attempt=1, source="auto_retry"),
        error=TaskError(
            message=(
                "Documentation output contains broken internal markdown links in generated artifacts: "
                "docs/blocks/implementation-guide.md -> ../project-context-research.md."
            )
        ),
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert isinstance(result["payload"], list)
    system_prompt = result["payload"][0]["content"]
    assert "Retry guidance:" in system_prompt
    assert "Previous attempt failed with this error:" in system_prompt
    assert "broken internal markdown links" in system_prompt
    assert "resolve internal markdown links relative to the generated file path" in system_prompt


@pytest.mark.anyio
async def test_scheduler_retry_guidance_includes_available_docs_and_link_correction():
    from control_plane.scheduler.scheduler import Scheduler
    from shared.models import TaskError, TaskMetadata

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-coder",
        name="PM Coder",
        role="coder",
        system_prompt="Return only strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-retry-doc-links",
        bot_id="pm-coder",
        payload={
            "instruction": "create docs",
            "deliverables": [
                "docs/blocks/mathematics/math-blocks-roadmap.md",
                "docs/blocks/mathematics/block-catalog.md",
            ],
            "upstream_artifacts": [
                {"path": "docs/blocks/repo-research-summary.md", "content": "# Repo Summary"},
                {"path": "docs/blocks/project-context-research.md", "content": "# Project Context"},
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
        metadata=TaskMetadata(retry_attempt=1, source="auto_retry"),
        error=TaskError(
            message=(
                "Documentation output contains broken internal markdown links in generated artifacts: "
                "docs/blocks/mathematics/math-blocks-roadmap.md -> ../../repo-research-summary.md."
            )
        ),
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_prompt = result["payload"][0]["content"]
    assert "Available markdown docs for this branch and upstream context:" in system_prompt
    assert "docs/blocks/repo-research-summary.md" in system_prompt
    assert "Likely link corrections:" in system_prompt
    assert "replace `../../repo-research-summary.md` with `../repo-research-summary.md`" in system_prompt


@pytest.mark.anyio
async def test_scheduler_injects_attached_connection_schema_into_model_prompt(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    monkeypatch.setattr(
        "control_plane.scheduler.scheduler._load_attached_connection_rows",
        lambda _bot_id: [
            {
                "id": 7,
                "name": "platform-schema",
                "kind": "http",
                "description": "Lesson block schema",
                "config": {"base_url": "https://example.test"},
                "schema_text": json.dumps(
                    {
                        "lesson_blocks": [
                            {
                                "variant": "paragraph",
                                "html": "<p>Example paragraph</p>",
                                "options": {"textAlign": "left"},
                            },
                            {
                                "code": "console.log('Hello, World!');",
                                "language": "javascript",
                                "theme": "dark",
                                "showLineNumbers": True,
                            },
                        ]
                    }
                ),
                "enabled": True,
            }
        ],
    )

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-lesson-writer",
        name="Course Lesson Writer",
        role="writer",
        system_prompt="Write lesson blocks as strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-ctx-1",
        bot_id="course-lesson-writer",
        payload={"instruction": "Write lesson 1"},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Attached connection schemas:" in system_message
    assert "platform-schema" in system_message
    assert '"variant": "paragraph"' in system_message
    assert '"showLineNumbers": true' in system_message


@pytest.mark.anyio
async def test_scheduler_fetches_dynamic_connection_context_from_payload_items(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-blocks-api",
                                "kind": "http",
                                "description": "Remote block schema API",
                                "config_json": json.dumps({"base_url": "https://example.test"}),
                                "auth_json": json.dumps({"type": "api_key", "name": "X-acme-BLOCKS-KEY", "api_key": "enc:ignored"}),
                                "schema_text": "openapi: 3.1.0",
                                "enabled": True,
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    def fake_http_connection_test(*, config, auth, schema_text, payload):
        assert auth["name"] == "X-acme-BLOCKS-KEY"
        block_type = str(payload.get("path") or "").split("/")[-1]
        return {
            "ok": True,
            "status": 200,
            "method": "GET",
            "url": f"https://example.test{payload.get('path')}",
            "body_preview": json.dumps(
                {
                    "blockType": block_type,
                    "schema": {"required": ["html"], "properties": {"html": {"type": "string"}}},
                    "example": {"variant": block_type, "html": "<p>Example</p>"},
                }
            ),
        }

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())
    monkeypatch.setattr("shared.connection_secrets.resolve_auth_payload", lambda payload: {"type": "api_key", "name": "X-acme-BLOCKS-KEY", "api_key": "live-key"})
    monkeypatch.setattr("shared.connection_runtime.test_http_connection", fake_http_connection_test)

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-lesson-writer",
        name="Course Lesson Writer",
        role="writer",
        system_prompt="Write lesson blocks as strict JSON.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "connection_context": {
                "enabled": True,
                "fetch_connection_name": "platform-blocks-api",
                "for_each_field": "generation_settings.allowed_lesson_blocks",
                "fetch_actions": [
                    {
                        "method": "GET",
                        "path": "/api/blocks/{{item}}",
                        "query_params": {"includeExample": "true"},
                    }
                ],
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-fetch-ctx-1",
        bot_id="course-lesson-writer",
        payload={"generation_settings": {"allowed_lesson_blocks": ["paragraph", "code"]}},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Dynamic connection fetch results:" in system_message
    assert "Fetch: /api/blocks/paragraph [paragraph]" in system_message
    assert '"blockType": "paragraph"' in system_message
    assert '"blockType": "code"' in system_message


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

    assert result["payload"][0]["role"] == "system"
    assert result["payload"][0]["content"].startswith("Return only strict JSON.")
    assert result["payload"][0]["content"].count("Return only strict JSON.") == 1
    assert "Execution policy:" in result["payload"][0]["content"]
    assert result["payload"][1] == {"role": "user", "content": "build outline"}


@pytest.mark.anyio
async def test_scheduler_keeps_custom_backend_payload_unwrapped_when_connection_context_exists(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-api",
                                "kind": "http",
                                "description": "Importer connection",
                                "config_json": json.dumps({"base_url": "https://example.test"}),
                                "schema_text": "openapi: 3.1.0",
                                "enabled": True,
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt="Do not wrap payloads.",
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-custom-ctx",
        bot_id="course-importer",
        payload={"connection_actions": [{"operation_id": "createCourse"}]},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"] == {"connection_actions": [{"operation_id": "createCourse"}]}


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

    assert result["payload"][0]["role"] == "system"
    assert result["payload"][0]["content"].startswith("Return only strict JSON.")
    assert "Execution policy:" in result["payload"][0]["content"]
    transformed = json.loads(result["payload"][1]["content"])
    assert transformed == {
        "instruction": "Build outline",
        "course_brief": {"topic": "AP World History", "subject": "History"},
    }


@pytest.mark.anyio
async def test_scheduler_input_transform_supports_coalesce_paths():
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
                    "course_brief": "{{coalesce:payload.source_result.course_brief,payload.source_payload.source_result.course_brief}}",
                    "generation_settings": "{{coalesce:payload.source_result.generation_settings,payload.source_payload.source_result.generation_settings}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-4",
        bot_id="course-outline",
        payload={
            "source_payload": {
                "source_result": {
                    "course_brief": {"topic": "AP World History"},
                    "generation_settings": {"generate_documentation": True},
                }
            }
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    transformed = json.loads(result["payload"][1]["content"])
    assert transformed == {
        "course_brief": {"topic": "AP World History"},
        "generation_settings": {"generate_documentation": True},
    }


@pytest.mark.anyio
async def test_scheduler_input_transform_can_render_nested_templates():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "connection_actions": "{{render:payload.generation_settings.platform_import_actions}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-render",
        bot_id="course-importer",
        payload={
            "import_package": {
                "course_package": {
                    "course_shell": {
                        "title": "World History Survey",
                    }
                }
            },
            "generation_settings": {
                "platform_import_actions": [
                    {
                        "operation_id": "createCourse",
                        "body_json": {
                            "title": "{{payload.import_package.course_package.course_shell.title}}",
                        },
                    }
                ]
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert isinstance(result["payload"], list)
    assert result["payload"][0]["role"] == "system"
    assert "Execution policy:" in result["payload"][0]["content"]
    assert "repo file artifacts" in result["payload"][0]["content"]
    transformed = json.loads(result["payload"][1]["content"])
    assert transformed == {
        "connection_actions": [
            {
                "operation_id": "createCourse",
                "body_json": {"title": "World History Survey"},
            }
        ]
    }


@pytest.mark.anyio
async def test_scheduler_input_transform_supports_literal_fallbacks_and_list_index_paths():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "create_badge": "{{coalesce:payload.source_payload.generation_settings.badge_settings.enabled,true}}",
                    "course_title": "{{coalesce:payload.source_result.course_package.course_shell.title,payload.source_result.course_package.units.0.approved_unit_package.unit_package.title,'Generated Course'}}",
                    "first_unit_title": "{{payload.source_result.course_package.units.0.approved_unit_package.unit_package.title}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-literals",
        bot_id="course-importer",
        payload={
            "source_payload": {"generation_settings": None},
            "source_result": {
                "course_package": {
                    "course_shell": {"title": None},
                    "units": [
                        {
                            "approved_unit_package": {
                                "unit_package": {
                                    "title": "The Global Tapestry (c. 1200-1450)",
                                }
                            }
                        }
                    ],
                }
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"]["create_badge"] is True
    assert result["payload"]["course_title"] == "The Global Tapestry (c. 1200-1450)"
    assert result["payload"]["first_unit_title"] == "The Global Tapestry (c. 1200-1450)"


@pytest.mark.anyio
async def test_scheduler_input_transform_supports_camelize_for_nested_payloads():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
        routing_rules={
            "input_transform": {
                "enabled": True,
                "template": {
                    "coursePackage": "{{json:camelize:payload.source_result.approved_package.course_package}}",
                    "badgeSpec": "{{json:camelize:payload.source_result.approved_package.badge_spec}}",
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-camelize",
        bot_id="course-importer",
        payload={
            "source_result": {
                "approved_package": {
                    "course_package": {
                        "course_shell": {"title": "World History Survey"},
                        "units": [
                            {
                                "unit_number": 1,
                                "unit_question_bank": {"question_count": 20},
                                "lessons": [{"lesson_number": 1, "title": "Lesson 1"}],
                            }
                        ],
                    },
                    "badge_spec": {"image_prompt": "Create a crest"},
                }
            }
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    assert result["payload"]["coursePackage"]["courseShell"]["title"] == "World History Survey"
    assert result["payload"]["coursePackage"]["units"][0]["unitNumber"] == 1
    assert result["payload"]["coursePackage"]["units"][0]["unitQuestionBank"]["questionCount"] == 20
    assert result["payload"]["coursePackage"]["units"][0]["lessons"][0]["lessonNumber"] == 1
    assert result["payload"]["badgeSpec"]["imagePrompt"] == "Create a crest"


@pytest.mark.anyio
async def test_scheduler_custom_http_connection_backend_executes_actions(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-api",
                                "kind": "http",
                                "config_json": json.dumps({"base_url": "https://api.example.test"}),
                                "auth_json": json.dumps({"type": "api_key", "api_key": "enc:ignored"}),
                                "schema_text": json.dumps(
                                    {
                                        "openapi": "3.1.0",
                                        "paths": {
                                            "/courses": {
                                                "post": {
                                                    "operationId": "createCourse",
                                                }
                                            }
                                        },
                                    }
                                ),
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())
    monkeypatch.setattr("shared.connection_secrets.resolve_auth_payload", lambda payload: {"type": "api_key", "api_key": "live-key"})
    monkeypatch.setattr(
        "shared.connection_runtime.test_http_connection",
        lambda **kwargs: {
            "ok": True,
            "status": 201,
            "method": "POST",
            "url": "https://api.example.test/courses",
            "body_preview": "{\"id\": 42}",
        },
    )

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-http",
        bot_id="course-importer",
        payload={
            "connection": {"name": "platform-api"},
            "connection_actions": [
                {
                    "operation_id": "createCourse",
                    "body_json": {"title": "World History Survey"},
                }
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    result = await scheduler.schedule(task)

    assert result["import_status"] == "success"
    assert result["connection_name"] == "platform-api"
    assert result["completed_actions"] == ["createCourse"]
    assert result["failed_actions"] == []
    assert result["action_results"][0]["status"] == 201


@pytest.mark.anyio
async def test_scheduler_custom_http_connection_404_import_includes_endpoint_hint(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from dashboard.models import BotConnection as DashboardBotConnection
    from dashboard.models import Connection as DashboardConnection

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return list(self._rows)

    class FakeSession:
        def query(self, model):
            if model is DashboardBotConnection:
                return FakeQuery([type("Link", (), {"connection_id": 7})()])
            if model is DashboardConnection:
                return FakeQuery(
                    [
                        type(
                            "Conn",
                            (),
                            {
                                "id": 7,
                                "name": "platform-api",
                                "kind": "http",
                                "config_json": json.dumps({"base_url": "https://api.example.test"}),
                                "auth_json": json.dumps({"type": "api_key", "api_key": "enc:ignored"}),
                                "schema_text": json.dumps(
                                    {
                                        "openapi": "3.1.0",
                                        "paths": {
                                            "/api/agent/import/course-package": {
                                                "post": {
                                                    "operationId": "importCoursePackage",
                                                }
                                            }
                                        },
                                    }
                                ),
                            },
                        )()
                    ]
                )
            raise AssertionError(f"Unexpected model queried: {model}")

        def close(self):
            return None

    monkeypatch.setattr("dashboard.db.get_db", lambda: FakeSession())
    monkeypatch.setattr("shared.connection_secrets.resolve_auth_payload", lambda payload: {"type": "api_key", "api_key": "live-key"})
    monkeypatch.setattr(
        "shared.connection_runtime.test_http_connection",
        lambda **kwargs: {
            "ok": False,
            "status": 404,
            "method": "POST",
            "url": "https://api.example.test/api/agent/import/course-package",
            "body_preview": "{\"title\":\"Not Found\",\"status\":404}",
        },
    )

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-importer",
        name="Course Importer",
        role="importer",
        system_prompt=None,
        backends=[BackendConfig(type="custom", provider="http_connection", model="attached-http")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-http-404",
        bot_id="course-importer",
        payload={
            "connection": {"name": "platform-api"},
            "connection_actions": [
                {
                    "operation_id": "importCoursePackage",
                    "body_json": {"coursePackage": {"courseShell": {"title": "World History Survey"}}},
                }
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    result = await scheduler.schedule(task)

    assert result["import_status"] == "failed"
    assert result["failed_actions"] == ["importCoursePackage"]
    assert "Endpoint /api/agent/import/course-package is not available on the target server." in result["errors"][0]


@pytest.mark.anyio
async def test_scheduler_appends_output_contract_guidance_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="course-outline",
        name="Course Outline",
        role="planner",
        system_prompt="Build the course outline.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        routing_rules={
            "output_contract": {
                "enabled": True,
                "mode": "model_output",
                "format": "json_object",
                "required_fields": ["course_shell", "course_structure"],
                "non_empty_fields": ["course_structure.units"],
                "fallback_mode": "disabled",
                "description": "Return a structured outline only.",
                "example_output": {
                    "course_shell": {"title": "Example"},
                    "course_structure": {"units": []},
                },
            }
        },
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-5",
        bot_id="course-outline",
        payload={"instruction": "Build outline"},
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Build the course outline." in system_message
    assert "Output contract:" in system_message
    assert "Required top-level fields: course_shell, course_structure." in system_message
    assert "Fields that must be populated: course_structure.units." in system_message
    assert "Missing or empty required fields will fail the run." in system_message
    assert "\"course_shell\"" in system_message


@pytest.mark.anyio
async def test_scheduler_appends_docs_only_assignment_scope_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-engineer",
        name="PM Engineer",
        role="engineer",
        system_prompt="Plan the implementation workstreams.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-docs-only",
        bot_id="pm-engineer",
        payload={
            "instruction": "Combine the joined research outputs into a plan.",
            "assignment_request": "Build documentation only in docs/blocks for the mathematics blocks.",
            "assignment_scope": {
                "docs_only": True,
                "conversation_brief": (
                    "Prior user intent 1: Focus on algebra, trigonometry, statistics, calculus, and multivariable calculus.\n"
                    "Prior user intent 2: Build as much as possible in house and do not rely on the Desmos API."
                ),
                "conversation_transcript": (
                    "user: Help me plan the mathematics blocks from algebra through multivariable calculus.\n"
                    "assistant: Here is a roadmap.\n"
                    "user: Build as much as possible in house and do not rely on the Desmos API."
                ),
                "conversation_message_count": 3,
                "conversation_transcript_strategy": "full",
                "requested_output_paths": ["docs/blocks"],
                "prefer_in_house": True,
                "avoid_external_apis": True,
                "prefer_client_side_execution": True,
                "minimize_server_load": True,
                "minimize_bandwidth": True,
                "requested_outcome_style": "roadmap",
                "focus_topics": ["algebra", "trigonometry", "statistics"],
                "requested_artifact_hints": ["roadmap", "guide"],
                "constraint_hints": ["Prefer in-house or locally owned solutions."],
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Plan the implementation workstreams." in system_message
    assert "Assignment scope:" in system_message
    assert "documentation-only run" in system_message.lower()
    assert "docs/blocks" in system_message
    assert "Do not interpret documentation-only as an empty plan." in system_message
    assert "implementation_workstreams" in system_message
    assert "Conversation brief from earlier user messages" in system_message
    assert "Conversation transcript (3 prior message(s), full):" in system_message
    assert "Requested artifact shapes: roadmap, guide" in system_message
    assert "Do not rely on external product APIs" in system_message
    assert "Interpreted scope constraints:" in system_message
    assert "Requested output shape: a roadmap" in system_message
    assert "only cross-link to markdown docs that actually exist" in system_message
    assert "Every downstream stage must validate its output against the original assignment scope" in system_message


def test_prepare_payload_for_backend_reduces_oversized_join_payload_deterministically():
    from control_plane.scheduler.scheduler import _prepare_payload_for_backend

    bot = Bot(
        id="pm-engineer",
        name="PM Engineer",
        role="engineer",
        system_prompt="Plan the implementation workstreams.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    backend = bot.backends[0]
    long_research = (
        "# Research Summary\n\n"
        "Keep the implementation in house and avoid the Desmos API.\n\n"
        + ("Evidence line about docs/blocks and research synthesis.\n" * 120)
    )
    payload = {
        "title": "Engineer join",
        "instruction": "Synthesize the joined research outputs into implementation workstreams.",
        "assignment_request": "Build documentation only in docs/blocks for the mathematics blocks and avoid external APIs.",
        "join_count": 3,
        "assignment_scope": {
            "request_text": "Build documentation only in docs/blocks for the mathematics blocks and avoid external APIs.",
            "conversation_brief": "Prior user intent 1: Build as much as possible in house.",
            "conversation_transcript": "\n".join(
                [
                    "user: Help me plan the mathematics blocks from algebra through multivariable calculus.",
                    "assistant: I will outline the research threads.",
                    "user: Build as much as possible in house and avoid the Desmos API.",
                ]
                + [
                    f"assistant: Filler planning context {index} " + ("detail " * 80)
                    for index in range(80)
                ]
            ),
            "conversation_transcript_strategy": "full",
            "focus_topics": ["algebra", "multivariable calculus", "desmos"],
        },
        "research_payloads": [
            {
                "source_task_id": f"research-{index}",
                "title": f"Research branch {index}",
                "instruction": "Inspect the repository and identify documentation requirements.",
                "deliverables": [f"docs/blocks/research-{index}.md"],
                "source_result": {
                    "status": "complete",
                    "findings": [f"Finding {index}: keep the stack in house."],
                    "evidence": [long_research],
                    "artifacts": [
                        {
                            "path": f"docs/blocks/research-{index}.md",
                            "content": long_research,
                        }
                    ],
                    "handoff_notes": "Use these findings for the pm-engineer synthesis.",
                },
            }
            for index in range(3)
        ],
        "join_results": [
            {
                "status": "complete",
                "findings": [f"Finding {index}: avoid the Desmos API."],
                "evidence": [long_research],
                "artifacts": [
                    {
                        "path": f"docs/blocks/research-{index}.md",
                        "content": long_research,
                    }
                ],
            }
            for index in range(3)
        ],
        "upstream_artifacts": [
            {
                "path": f"docs/blocks/research-{index}.md",
                "content": long_research,
            }
            for index in range(3)
        ],
    }

    prepared = _prepare_payload_for_backend(bot, backend, payload)

    assert isinstance(prepared, list)
    reduced_payload = json.loads(prepared[1]["content"])
    assert reduced_payload["join_count"] == 3
    assert len(reduced_payload["research_payloads"]) == 3
    assert len(reduced_payload["join_results"]) == 3
    assert len(reduced_payload["upstream_artifacts"]) == 3
    assert reduced_payload["context_reduction"]["applied"] is True
    assert "assignment_scope.conversation_transcript" in reduced_payload["context_reduction"]["reduced_fields"]
    assert "upstream_artifacts" in reduced_payload["context_reduction"]["reduced_fields"]
    assert "research_payloads" in reduced_payload["context_reduction"]["reduced_fields"]
    assert reduced_payload["assignment_scope"]["conversation_transcript_strategy"] == "context_reduced_excerpt"
    assert "multivariable calculus" in reduced_payload["assignment_scope"]["conversation_transcript"].lower()
    assert "desmos" in reduced_payload["assignment_scope"]["conversation_transcript"].lower()
    assert all(item.get("content_truncated_for_context") is True for item in reduced_payload["upstream_artifacts"])
    assert len(prepared[1]["content"]) < len(json.dumps(payload, ensure_ascii=False))


def test_prepare_payload_for_backend_preserves_small_non_join_payload():
    from control_plane.scheduler.scheduler import _prepare_payload_for_backend

    bot = Bot(
        id="pm-engineer",
        name="PM Engineer",
        role="engineer",
        system_prompt="Plan the implementation workstreams.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    backend = bot.backends[0]
    payload = {
        "instruction": "Summarize the assignment.",
        "assignment_request": "Write a short plan.",
        "assignment_scope": {
            "conversation_transcript": "user: Write a short plan.\nassistant: I can do that.",
            "conversation_transcript_strategy": "full",
        },
        "upstream_artifacts": [{"path": "docs/summary.md", "content": "# Summary"}],
    }

    prepared = _prepare_payload_for_backend(bot, backend, payload)

    assert isinstance(prepared, list)
    parsed_payload = json.loads(prepared[1]["content"])
    assert parsed_payload == payload


def test_prepare_payload_for_browser_backend_preserves_inspection_request():
    from control_plane.scheduler.scheduler import _prepare_payload_for_backend

    bot = Bot(
        id="browser-inspector",
        name="Browser Inspector",
        role="browser-inspector",
        system_prompt="Inspect only the configured page.",
        backends=[
            BackendConfig(
                type="browser",
                provider="browser",
                model="browser-ui",
                worker_id="browser-worker",
                api_key_ref="BROWSER_TOKEN",
            )
        ],
    )
    payload = {"path": "/admin/dashboard", "text_limit": 200, "element_limit": 5}

    prepared = _prepare_payload_for_backend(bot, bot.backends[0], payload)

    assert prepared == payload


def test_prepare_payload_for_scheduled_browser_backend_removes_only_scheduler_envelope():
    from control_plane.scheduler.scheduler import _prepare_payload_for_backend

    bot = Bot(
        id="browser-inspector",
        name="Browser Inspector",
        role="browser-inspector",
        backends=[
            BackendConfig(
                type="browser",
                provider="browser",
                model="browser-ui",
                worker_id="browser-worker",
                api_key_ref="BROWSER_TOKEN",
            )
        ],
    )
    payload = {
        "path": "/admin/dashboard",
        "text_limit": 200,
        "element_limit": 5,
        "instruction": "Inspect without mutation.",
        "source": "agent_schedule",
        "schedule_id": "schedule-1",
        "project_id": None,
        "node_overrides": {},
    }
    task = Task(
        id="scheduled-browser-task",
        bot_id=bot.id,
        payload=payload,
        metadata=TaskMetadata(source="agent_schedule"),
        created_at="2026-07-19T00:00:00+00:00",
        updated_at="2026-07-19T00:00:00+00:00",
    )

    prepared = _prepare_payload_for_backend(bot, bot.backends[0], payload, task=task)

    assert prepared == {"path": "/admin/dashboard", "text_limit": 200, "element_limit": 5}


def test_prepare_payload_for_retried_scheduled_browser_backend_removes_scheduler_envelope():
    from control_plane.scheduler.scheduler import _prepare_payload_for_backend

    bot = Bot(
        id="browser-inspector",
        name="Browser Inspector",
        role="browser-inspector",
        backends=[
            BackendConfig(
                type="browser",
                provider="browser",
                model="browser-ui",
                worker_id="browser-worker",
                api_key_ref="BROWSER_TOKEN",
            )
        ],
    )
    payload = {
        "path": "/admin/dashboard",
        "text_limit": 200,
        "element_limit": 5,
        "instruction": "Inspect without mutation.",
        "source": "agent_schedule",
        "schedule_id": "schedule-1",
        "project_id": None,
        "node_overrides": {},
    }
    task = Task(
        id="retried-scheduled-browser-task",
        bot_id=bot.id,
        payload=payload,
        metadata=TaskMetadata(source="auto_retry", retry_attempt=1),
        created_at="2026-07-19T00:00:00+00:00",
        updated_at="2026-07-19T00:00:00+00:00",
    )

    prepared = _prepare_payload_for_backend(bot, bot.backends[0], payload, task=task)

    assert prepared == {"path": "/admin/dashboard", "text_limit": 200, "element_limit": 5}


@pytest.mark.anyio
async def test_scheduler_appends_docs_only_upstream_artifact_guidance_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-tester",
        name="PM Tester",
        role="tester",
        system_prompt="Validate the workstream deterministically.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-docs-only-tester",
        bot_id="pm-tester",
        payload={
            "instruction": "Validate the documentation workstream.",
            "role_hint": "tester",
            "assignment_request": "Build documentation only in docs/blocks for the mathematics blocks.",
            "assignment_scope": {
                "docs_only": True,
                "requested_output_paths": ["docs/blocks"],
            },
            "upstream_artifacts": [
                {
                    "path": "docs/blocks/arithmetic.md",
                    "content": "# Arithmetic",
                }
            ],
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "upstream_artifacts" in system_message
    assert "live repo snapshot does not yet contain the proposed markdown files" in system_message
    assert "always return the repo-change contract JSON wrapper" in system_message
    assert "explicitly verify internal markdown links" in system_message
    assert "Do not invent sibling folders, placeholder doc names, or guessed markdown paths" in system_message
    assert "prefer the strongest upstream tester evidence over later skip/not_applicable review signals" in system_message


@pytest.mark.anyio
async def test_scheduler_appends_explicit_stage_exclusion_guidance_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-final-qc",
        name="PM Final QC",
        role="final-qc",
        system_prompt="Perform the final evidence-backed QC pass.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-final-qc-skip-ui",
        bot_id="pm-final-qc",
        payload={
            "instruction": "Verify the AI project grading feature branch.",
            "assignment_request": (
                "Implement the AI project grading feature. "
                "Skip the UI tester for now because Playwright is not configured yet, "
                "but still run the database engineer and all other tests."
            ),
            "assignment_scope": {
                "ui_test_mode": "build_only",
            },
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "UI validation mode: build_only." in system_message
    assert "Do not skip the pm-ui-tester stage." in system_message
    assert "Final QC must treat build_only UI validation as the intended validation mode" in system_message


@pytest.mark.anyio
async def test_scheduler_appends_database_stage_contract_guidance_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-database-engineer",
        name="PM Database Engineer",
        role="dba-sql",
        system_prompt="Produce the database migration.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-db-stage",
        bot_id="pm-database-engineer",
        payload={
            "instruction": "Create the canonical migration script.",
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "Database stage contract:" in system_message
    assert "If the outcome is pass/completed, return exactly one canonical SQL migration script artifact" in system_message
    assert "DELETE, DROP, TRUNCATE, and destructive ALTER TABLE" in system_message


@pytest.mark.anyio
async def test_scheduler_appends_repo_output_deny_policy_to_system_prompt():
    from control_plane.scheduler.scheduler import Scheduler

    bot_registry = AsyncMock()
    bot_registry.get.return_value = Bot(
        id="pm-docs-validator",
        name="PM Docs Validator",
        role="tester",
        system_prompt="Validate the workstream deterministically.",
        backends=[BackendConfig(type="cloud_api", provider="openai", model="gpt-4o-mini")],
        execution_policy={"repo_output_mode": "deny"},
    )
    scheduler = Scheduler(bot_registry=bot_registry, worker_registry=AsyncMock())
    task = Task(
        id="task-policy-deny",
        bot_id="pm-docs-validator",
        payload={
            "instruction": "Validate the documentation branch.",
            "step_kind": "test_execution",
            "deliverables": ["docs/blocks/coordinate-plane.md"],
            "role_hint": "tester",
        },
        status="queued",
        created_at="now",
        updated_at="now",
    )

    async def fake_dispatch(backend, payload, task=None):
        return {"payload": payload}

    scheduler._dispatch_backend = fake_dispatch  # type: ignore[method-assign]
    result = await scheduler.schedule(task)

    system_message = result["payload"][0]["content"]
    assert "execution_policy.repo_output_mode=deny" in system_message
    assert "Do not create, modify, or return repo file artifacts" in system_message
    assert "Treat any repo-style deliverables as read-only validation or planning targets only." in system_message
