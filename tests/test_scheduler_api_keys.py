"""Tests for scheduler API-key resolution behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import pytest

from shared.models import BackendConfig, CatalogModel, Task, TaskMetadata


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
        "done_reason": "length",
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
    assert kwargs["json"]["think"] is False
    assert result["output"] == "ok"
    assert result["finish_reason"] == "length"


@pytest.mark.anyio
async def test_scheduler_openai_includes_finish_reason():
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "openai-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)

    backend = BackendConfig(type="cloud_api", model="gpt-4o-mini", provider="openai", api_key_ref="OPENAI_API_KEY")
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._call_openai(backend, payload)

    assert result["output"] == "ok"
    assert result["finish_reason"] == "length"


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
        params={"max_tokens": 768, "temperature": 0.3, "response_format": "json"},
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
    assert kwargs["json"]["think"] is False
    assert kwargs["json"]["options"]["num_predict"] == 768
    assert "max_tokens" not in kwargs["json"]["options"]
    assert "response_format" not in kwargs["json"]["options"]
    assert kwargs["json"]["options"]["temperature"] == 0.3
    assert kwargs["json"]["format"] == "json"


def test_scheduler_ollama_cloud_model_variants_include_cloud_alias():
    from control_plane.scheduler.scheduler import Scheduler

    variants = Scheduler._ollama_cloud_model_variants("qwen3.5:397b-cloud")
    assert variants == ["qwen3.5:397b-cloud", "qwen3.5:397b"]


@pytest.mark.anyio
async def test_scheduler_ollama_cloud_tries_alias_before_pull():
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = "ollama-secret"
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    backend = BackendConfig(
        type="cloud_api",
        model="qwen3.5:397b-cloud",
        provider="ollama_cloud",
        api_key_ref="OLLAMA_API_KEY",
    )
    payload = [{"role": "user", "content": "hello"}]

    def _response(status_code: int, body: dict):
        fake = MagicMock()
        fake.status_code = status_code
        fake.is_success = status_code < 400
        fake.json.return_value = body
        fake.text = str(body)
        if status_code >= 400:
            request = MagicMock()
            fake.raise_for_status.side_effect = httpx.HTTPStatusError(
                "request failed",
                request=request,
                response=fake,
            )
        else:
            fake.raise_for_status.return_value = None
        return fake

    async def _post(_url, headers=None, json=None):  # noqa: ANN001
        model_name = str((json or {}).get("model") or "")
        if model_name == "qwen3.5:397b-cloud":
            return _response(404, {"error": "model not found"})
        if model_name == "qwen3.5:397b":
            return _response(
                200,
                {
                    "message": {"content": "ok"},
                    "prompt_eval_count": 10,
                    "eval_count": 4,
                    "done_reason": "stop",
                },
            )
        return _response(404, {"error": "model not found"})

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.side_effect = _post

    pull_mock = AsyncMock()
    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        with patch.object(Scheduler, "_pull_ollama_cloud_model", pull_mock):
            result = await scheduler._call_ollama_cloud(backend, payload)

    assert result["output"] == "ok"
    assert result["resolved_model"] == "qwen3.5:397b"
    assert pull_mock.await_count == 0


@pytest.mark.anyio
async def test_scheduler_vertex_claude_uses_rawpredict_partner_endpoint():
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = (
        '{"project_id":"demo-project","client_email":"svc@example.com","private_key":"test-private-key"}'
    )
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    scheduler._vertex_access_token = AsyncMock(return_value="vertex-token")  # type: ignore[method-assign]

    backend = BackendConfig(
        type="cloud_api",
        model="claude-opus-4-6",
        provider="vertex",
        api_key_ref="VERTEX_SERVICE_ACCOUNT_JSON",
        params={"max_tokens": 128000, "temperature": 0.1, "num_ctx": 50000},
    )
    payload = [
        {"role": "system", "content": "be strict"},
        {"role": "user", "content": "hello"},
    ]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._call_vertex(backend, payload)

    args, kwargs = mock_client.post.call_args
    assert args[0].endswith(
        "/v1/projects/demo-project/locations/us-central1/publishers/anthropic/models/claude-opus-4-6:rawPredict"
    )
    assert kwargs["headers"]["Authorization"] == "Bearer vertex-token"
    assert kwargs["json"]["anthropic_version"] == "vertex-2023-10-16"
    assert kwargs["json"]["max_tokens"] == 128000
    assert kwargs["json"]["temperature"] == 0.1
    assert kwargs["json"]["system"] == "be strict"
    assert "generationConfig" not in kwargs["json"]
    assert "num_ctx" not in kwargs["json"]
    assert result["output"] == "ok"
    assert result["finish_reason"] == "end_turn"


@pytest.mark.anyio
async def test_scheduler_vertex_google_model_still_uses_generate_content():
    from control_plane.scheduler.scheduler import Scheduler

    key_vault = AsyncMock()
    key_vault.get_secret.return_value = (
        '{"project_id":"demo-project","client_email":"svc@example.com","private_key":"test-private-key"}'
    )
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=key_vault)
    scheduler._vertex_access_token = AsyncMock(return_value="vertex-token")  # type: ignore[method-assign]

    backend = BackendConfig(
        type="cloud_api",
        model="gemini-2.5-pro",
        provider="vertex",
        api_key_ref="VERTEX_SERVICE_ACCOUNT_JSON",
        params={"max_tokens": 4096, "temperature": 0.2},
    )
    payload = [{"role": "user", "content": "hello"}]

    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
    }

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    mock_client.post.return_value = fake_response

    with patch("control_plane.scheduler.scheduler.httpx.AsyncClient", return_value=mock_client):
        result = await scheduler._call_vertex(backend, payload)

    args, kwargs = mock_client.post.call_args
    assert args[0].endswith(
        "/v1/projects/demo-project/locations/us-central1/publishers/google/models/gemini-2.5-pro:generateContent"
    )
    assert "generationConfig" in kwargs["json"]
    assert kwargs["json"]["generationConfig"]["maxOutputTokens"] == 4096
    assert "max_tokens" not in kwargs["json"]["generationConfig"]
    assert result["output"] == "ok"
    assert result["finish_reason"] == "STOP"


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


@pytest.mark.anyio
async def test_scheduler_preferred_model_uses_enabled_catalog_model():
    from control_plane.scheduler.scheduler import _backend_with_preferred_model

    class _Registry:
        async def get(self, model_id):
            assert model_id == "ollama-cloud-gpt-oss-120b"
            return CatalogModel(
                id=model_id,
                name="gpt-oss:120b",
                provider="ollama_cloud",
                enabled=True,
            )

    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3.5:397b")
    task = Task(
        id="task-preferred-model",
        bot_id="bot-1",
        payload={"instruction": "chat"},
        metadata=TaskMetadata(source="chat", preferred_model_id="ollama-cloud-gpt-oss-120b"),
        status="queued",
        created_at="2026-08-05T00:00:00+00:00",
        updated_at="2026-08-05T00:00:00+00:00",
    )

    effective = await _backend_with_preferred_model(backend, task, _Registry())

    assert effective.provider == "ollama_cloud"
    assert effective.model == "gpt-oss:120b"


@pytest.mark.anyio
async def test_scheduler_preferred_model_rejects_provider_mismatch():
    from control_plane.scheduler.scheduler import _backend_with_preferred_model
    from shared.exceptions import BackendError

    class _Registry:
        async def get(self, _model_id):
            return CatalogModel(
                id="openai-gpt-5",
                name="gpt-5",
                provider="openai",
                enabled=True,
            )

    backend = BackendConfig(type="cloud_api", provider="ollama_cloud", model="qwen3.5:397b")
    task = Task(
        id="task-preferred-provider-mismatch",
        bot_id="bot-1",
        payload={"instruction": "chat"},
        metadata=TaskMetadata(source="chat", preferred_model_id="openai-gpt-5"),
        status="queued",
        created_at="2026-08-05T00:00:00+00:00",
        updated_at="2026-08-05T00:00:00+00:00",
    )

    with pytest.raises(BackendError, match="does not match backend provider"):
        await _backend_with_preferred_model(backend, task, _Registry())


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


def test_scheduler_retry_attempt_adds_default_max_tokens_when_backend_has_no_params(monkeypatch):
    from control_plane.scheduler import scheduler as scheduler_module
    from control_plane.scheduler.scheduler import _backend_with_retry_params
    from shared.models import Task, TaskMetadata

    monkeypatch.setattr(
        scheduler_module,
        "_settings_int",
        lambda name, default: 512 if name == "task_retry_max_tokens_increment" else 1024 if name == "task_retry_num_width_increment" else default,
    )

    backend = BackendConfig(
        type="cloud_api",
        model="gpt-test",
        provider="openai",
    )
    task = Task(
        id="task-3",
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
    assert effective.params.num_ctx is None


def test_scheduler_retry_attempt_adds_default_num_ctx_for_local_llm_when_missing(monkeypatch):
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
    )
    task = Task(
        id="task-4",
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
        with pytest.raises(BackendError, match="Ollama Cloud model 'qwen3.5:cloud' not found"):
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
