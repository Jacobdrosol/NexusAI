"""Tests for scheduler API-key resolution behavior."""

from unittest.mock import AsyncMock, MagicMock, patch

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
    redacted = scheduler._apply_cloud_context_policy(backend, payload)
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
        scheduler._apply_cloud_context_policy(backend, payload)


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
