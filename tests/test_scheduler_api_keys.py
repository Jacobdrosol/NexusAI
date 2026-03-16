"""Tests for scheduler API-key resolution behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import pytest

from shared.models import BackendConfig


@pytest.mark.anyio
async def test_scheduler_prefers_key_vault_secret():
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "vault-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)

    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai", api_key_ref="openai-dev")
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        await scheduler._call_openai(backend, payload)

    _, kwargs = mock_client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer vault-secret"


@pytest.mark.anyio
async def test_scheduler_falls_back_to_env_when_key_not_in_vault(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.side_effect = Exception("not found")
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    monkeypatch.setenv("OPENAI_DEV", "env-secret")

    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai", api_key_ref="OPENAI_DEV")
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {"choices": [{"message": {"content": "ok"}}], "usage": {}}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        await scheduler._call_openai(backend, payload)

    _, kwargs = mock_client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer env-secret"


@pytest.mark.anyio
async def test_scheduler_cloud_context_policy_redact(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=AsyncMock())
    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai", api_key_ref="OPENAI_API_KEY")
    payload = [
        {"role": "system", "content": "Context:\nSensitive notes"},
        {"role": "user", "content": "hello"},
    ]
    monkeypatch.setenv("NEXUSAI_CLOUD_CONTEXT_POLICY", "redact")
    redacted = await scheduler._apply_cloud_context_policy(backend, payload)
    assert redacted[0]["content"] == "Context:\n[REDACTED_BY_POLICY]"


@pytest.mark.anyio
async def test_scheduler_cloud_context_policy_block(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler
    from shared.exceptions import BackendError

    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=AsyncMock())
    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai", api_key_ref="OPENAI_API_KEY")
    payload = [{"role": "system", "content": "Context:\nSensitive notes"}]
    monkeypatch.setenv("NEXUSAI_CLOUD_CONTEXT_POLICY", "block")
    with pytest.raises(BackendError):
        await scheduler._apply_cloud_context_policy(backend, payload)


@pytest.mark.anyio
async def test_scheduler_gemini_uses_header_api_key_not_query(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "gemini-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)

    backend = BackendConfig(
        type="cloud_api",
        model="gemini-1.5-pro",
        provider="gemini",
        api_key_ref="GEMINI_API_KEY",
    )
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
        "usageMetadata": {},
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        await scheduler._call_gemini(backend, payload)

    args, kwargs = mock_client.post.call_args
    assert "?key=" not in args[0]
    assert kwargs["headers"]["x-goog-api-key"] == "gemini-secret"


@pytest.mark.anyio
async def test_scheduler_ollama_cloud_uses_bearer_key_and_chat_endpoint(monkeypatch):
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "ollama-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    backend = BackendConfig(
        type="cloud_api",
        model="llama3.2",
        provider="ollama_cloud",
        api_key_ref="OLLAMA_API_KEY",
    )
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "message": {"content": "ok"},
        "prompt_eval_count": 3,
        "eval_count": 5,
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._call_ollama_cloud(backend, payload)

    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://ollama.com/api/chat"
    assert kwargs["headers"]["Authorization"] == "Bearer ollama-secret"
    assert result["output"] == "ok"


@pytest.mark.anyio
async def test_scheduler_ollama_cloud_maps_max_tokens_to_num_predict():
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "ollama-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    backend = BackendConfig(
        type="cloud_api",
        model="qwen3.5:cloud",
        provider="ollama_cloud",
        api_key_ref="OLLAMA_API_KEY",
        params={"max_tokens": 768, "temperature": 0.3},
    )
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "message": {"content": "ok"},
        "prompt_eval_count": 3,
        "eval_count": 5,
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        await scheduler._call_ollama_cloud(backend, payload)

    _, kwargs = mock_client.post.call_args
    assert kwargs["json"]["options"]["num_predict"] == 768
    assert "max_tokens" not in kwargs["json"]["options"]
    assert kwargs["json"]["options"]["temperature"] == 0.3


def test_scheduler_retry_attempt_increases_max_tokens_and_num_width(monkeypatch):
    from control_plane.scheduler import scheduler as scheduler_module
    from control_plane.scheduler.scheduler import _backend_with_retry_params
    from shared.models import Task, TaskMetadata

    monkeypatch.setattr(
        scheduler_module,
        "_settings_int",
        lambda name, default: 256 if name == "task_retry_max_tokens_increment" else 32 if name == "task_retry_num_width_increment" else default,
    )

    backend = BackendConfig(
        type="local_llm",
        model="llama3.2",
        provider="ollama",
        params={"max_tokens": 1024, "num_width": 128, "temperature": 0.2},
    )
    task = Task(
        id="task-1",
        bot_id="bot-1",
        payload={"instruction": "retry"},
        metadata=TaskMetadata(retry_attempt=2),
        status="queued",
        created_at="2026-03-16T00:00:00+00:00",
        updated_at="2026-03-16T00:00:00+00:00",
    )

    effective = _backend_with_retry_params(backend, task)

    assert effective.params is not None
    assert effective.params.max_tokens == 1536
    assert effective.params.num_width == 192
    assert effective.params.temperature == 0.2


def test_scheduler_retry_attempt_falls_back_to_num_ctx_when_num_width_missing(monkeypatch):
    from control_plane.scheduler import scheduler as scheduler_module
    from control_plane.scheduler.scheduler import _backend_with_retry_params
    from shared.models import Task, TaskMetadata

    monkeypatch.setattr(
        scheduler_module,
        "_settings_int",
        lambda name, default: 512 if name == "task_retry_max_tokens_increment" else 1024 if name == "task_retry_num_width_increment" else default,
    )

    backend = BackendConfig(
        type="local_llm",
        model="llama3.2",
        provider="ollama",
        params={"max_tokens": 1024, "num_ctx": 8192},
    )
    task = Task(
        id="task-2",
        bot_id="bot-1",
        payload={"instruction": "retry"},
        metadata=TaskMetadata(retry_attempt=1),
        status="queued",
        created_at="2026-03-16T00:00:00+00:00",
        updated_at="2026-03-16T00:00:00+00:00",
    )

    effective = _backend_with_retry_params(backend, task)

    assert effective.params is not None
    assert effective.params.max_tokens == 1536
    assert effective.params.num_ctx == 9216


@pytest.mark.anyio
async def test_scheduler_worker_timeout_disables_read_deadline():
    from control_plane.scheduler.scheduler import _worker_timeout

    timeout = _worker_timeout()
    assert timeout.connect == 10.0
    assert timeout.read is None
    assert timeout.write == 120.0


@pytest.mark.anyio
async def test_scheduler_ollama_cloud_surfaces_provider_error_detail():
    from control_plane.scheduler.scheduler import Scheduler
    from shared.exceptions import BackendError

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "ollama-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    backend = BackendConfig(
        type="cloud_api",
        model="qwen3.5:cloud",
        provider="ollama_cloud",
        api_key_ref="OLLAMA_API_KEY",
    )
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.status_code = 404
    fake_response.json.return_value = {"error": "model not found"}
    fake_response.text = '{"error":"model not found"}'
    request = MagicMock()
    fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "not found",
        request=request,
        response=fake_response,
    )

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(BackendError, match="Ollama Cloud request failed \\(404\\): model not found"):
            await scheduler._call_ollama_cloud(backend, payload)


@pytest.mark.anyio
async def test_scheduler_project_cloud_policy_provider_redact_disallows_bot_allow():
    from control_plane.scheduler.scheduler import Scheduler
    from shared.models import Task, TaskMetadata

    project_registry = AsyncMock()
    project_registry.get.return_value = MagicMock(
        settings_overrides={
            "cloud_context_policy": {
                "provider_policies": {"openai": "redact"},
                "bot_overrides": {"bot-1": {"openai": "allow"}},
            }
        }
    )
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        key_vault=AsyncMock(),
        project_registry=project_registry,
    )
    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai")
    task = Task(
        id="t1",
        bot_id="bot-1",
        payload=[],
        metadata=TaskMetadata(project_id="proj-1"),
        status="running",
        created_at="now",
        updated_at="now",
    )
    policy = await scheduler._resolve_cloud_context_policy(backend=backend, task=task)
    assert policy == "redact"


@pytest.mark.anyio
async def test_scheduler_project_cloud_policy_provider_block_wins():
    from control_plane.scheduler.scheduler import Scheduler
    from shared.models import Task, TaskMetadata

    project_registry = AsyncMock()
    project_registry.get.return_value = MagicMock(
        settings_overrides={
            "cloud_context_policy": {
                "provider_policies": {"openai": "block"},
                "bot_overrides": {"bot-1": {"openai": "redact"}},
            }
        }
    )
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        key_vault=AsyncMock(),
        project_registry=project_registry,
    )
    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai")
    task = Task(
        id="t1",
        bot_id="bot-1",
        payload=[],
        metadata=TaskMetadata(project_id="proj-1"),
        status="running",
        created_at="now",
        updated_at="now",
    )
    policy = await scheduler._resolve_cloud_context_policy(backend=backend, task=task)
    assert policy == "block"
