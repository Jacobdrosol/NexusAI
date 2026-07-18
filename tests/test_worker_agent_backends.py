"""Unit tests for worker agent backends and the /infer endpoint."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_httpx_response(json_data: dict, status_code: int = 200):
    """Return a mock httpx.Response with *json_data* as the body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# ollama_backend
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ollama_backend_infer():
    from worker_agent.backends import ollama_backend

    fake_response = _mock_httpx_response({
        "message": {"content": "Hello from Ollama"},
        "prompt_eval_count": 10,
        "eval_count": 5,
    })

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("worker_agent.backends.ollama_backend.httpx.AsyncClient", return_value=mock_client):
        result = await ollama_backend.infer(
            model="llama3",
            messages=[{"role": "user", "content": "hi"}],
            params={},
            host="http://localhost:11434",
        )

    assert result["output"] == "Hello from Ollama"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5


# ---------------------------------------------------------------------------
# ollama_cloud_backend
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ollama_cloud_backend_infer():
    from worker_agent.backends import ollama_cloud_backend

    fake_response = _mock_httpx_response(
        {
            "message": {"content": "Hello from Ollama Cloud"},
            "prompt_eval_count": 9,
            "eval_count": 4,
        }
    )
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch(
        "worker_agent.backends.ollama_cloud_backend.httpx.AsyncClient",
        return_value=mock_client,
    ):
        result = await ollama_cloud_backend.infer(
            model="glm-5.2:cloud",
            messages=[{"role": "user", "content": "hi"}],
            params={},
            api_key="test-key",
        )

    assert result["output"] == "Hello from Ollama Cloud"
    assert result["usage"]["prompt_tokens"] == 9
    assert mock_client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert mock_client.post.call_args.kwargs["json"]["think"] is False


# ---------------------------------------------------------------------------
# openai_backend
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_openai_backend_infer():
    from worker_agent.backends import openai_backend

    fake_response = _mock_httpx_response({
        "choices": [{"message": {"content": "Hello from OpenAI"}}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
    })

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("worker_agent.backends.openai_backend.httpx.AsyncClient", return_value=mock_client):
        result = await openai_backend.infer(
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            params={},
            api_key="test-key",
        )

    assert result["output"] == "Hello from OpenAI"
    assert result["usage"]["total_tokens"] == 12


# ---------------------------------------------------------------------------
# claude_backend
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_claude_backend_infer():
    from worker_agent.backends import claude_backend

    fake_response = _mock_httpx_response({
        "content": [{"text": "Hello from Claude"}],
        "usage": {"input_tokens": 6, "output_tokens": 3},
    })

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("worker_agent.backends.claude_backend.httpx.AsyncClient", return_value=mock_client):
        result = await claude_backend.infer(
            model="claude-3-5-sonnet",
            messages=[{"role": "user", "content": "hi"}],
            params={},
            api_key="test-key",
        )

    assert result["output"] == "Hello from Claude"
    assert result["usage"]["input_tokens"] == 6


# ---------------------------------------------------------------------------
# gemini_backend
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_gemini_backend_infer():
    from worker_agent.backends import gemini_backend

    fake_response = _mock_httpx_response({
        "candidates": [
            {"content": {"parts": [{"text": "Hello from Gemini"}]}}
        ],
        "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 3},
    })

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=fake_response)

    with patch("worker_agent.backends.gemini_backend.httpx.AsyncClient", return_value=mock_client):
        result = await gemini_backend.infer(
            model="gemini-1.5-pro",
            messages=[{"role": "user", "content": "hi"}],
            params={},
            api_key="test-key",
        )

    args, kwargs = mock_client.post.call_args
    assert "?key=" not in args[0]
    assert kwargs["headers"]["x-goog-api-key"] == "test-key"
    assert result["output"] == "Hello from Gemini"
    assert result["usage"]["promptTokenCount"] == 7


# ---------------------------------------------------------------------------
# cli_backend
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cli_backend_infer():
    from worker_agent.backends import cli_backend

    fake_proc = AsyncMock()
    fake_proc.communicate = AsyncMock(
        return_value=(b"hello world\n", b"")
    )
    fake_proc.returncode = 0

    with patch("asyncio.create_subprocess_shell", return_value=fake_proc) as mock_spawn:
        result = await cli_backend.infer(command="echo hello world", params={})

    mock_spawn.assert_called_once()
    assert result["output"] == "hello world\n"
    assert result["returncode"] == 0
    assert result["stderr"] == ""


# ---------------------------------------------------------------------------
# /infer endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_app():
    """Minimal FastAPI app with the infer router, no lifespan."""
    from fastapi import FastAPI
    from worker_agent.api import infer as infer_module
    from worker_agent.observability import install_observability

    app = FastAPI()
    install_observability(app)
    app.include_router(infer_module.router)
    app.state.worker_config = {
        "ollama_host": "http://localhost:11434",
        "capabilities": [
            {"type": "llm", "provider": "ollama", "models": ["llama3"]},
            {"type": "llm", "provider": "ollama_cloud", "models": ["glm-5.2:cloud"]},
        ],
    }
    return app


@pytest.mark.anyio
async def test_infer_endpoint_ollama(worker_app):
    async with AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        with patch(
            "worker_agent.backends.ollama_backend.infer",
            new=AsyncMock(return_value={"output": "ok", "usage": {}}),
        ):
            resp = await client.post(
                "/infer",
                json={
                    "model": "llama3",
                    "provider": "ollama",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
    assert resp.status_code == 200
    assert resp.json()["output"] == "ok"


@pytest.mark.anyio
async def test_infer_endpoint_unsupported_provider(worker_app):
    async with AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/infer",
            json={
                "model": "some-model",
                "provider": "unknown_provider",
                "messages": [],
            },
        )
    assert resp.status_code == 400


@pytest.mark.anyio
async def test_infer_endpoint_ollama_cloud(worker_app):
    async with AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        with patch(
            "worker_agent.backends.ollama_cloud_backend.infer",
            new=AsyncMock(return_value={"output": "cloud ok", "usage": {}}),
        ):
            response = await client.post(
                "/infer",
                json={
                    "model": "glm-5.2:cloud",
                    "provider": "ollama_cloud",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

    assert response.status_code == 200
    assert response.json()["output"] == "cloud ok"


@pytest.mark.anyio
async def test_infer_endpoint_cli(worker_app):
    async with AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/infer",
            json={
                "model": "echo hi",
                "provider": "cli",
                "messages": [],
                "command": "echo hi",
            },
        )
    assert resp.status_code == 403
    assert "disabled" in resp.json()["detail"]


@pytest.mark.anyio
async def test_infer_endpoint_rejects_undeclared_model(worker_app):
    async with AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/infer",
            json={
                "model": "not-declared",
                "provider": "ollama",
                "messages": [],
            },
        )
    assert resp.status_code == 403
    assert "not declared" in resp.json()["detail"]


@pytest.mark.anyio
async def test_worker_agent_metrics_endpoint(worker_app):
    async with AsyncClient(
        transport=ASGITransport(app=worker_app), base_url="http://test"
    ) as client:
        await client.post(
            "/infer",
            json={
                "model": "some-model",
                "provider": "unknown_provider",
                "messages": [],
            },
        )
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "nexus_worker_agent_http_requests_total" in resp.text
