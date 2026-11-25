from unittest.mock import AsyncMock

import pytest

from control_plane.scheduler.scheduler import Scheduler
from control_plane.scheduler.web_research_tools import (
    WEB_TOOL_DEFINITIONS,
    _extract_text,
    _is_safe_public_url,
    execute_web_tool,
)
from shared.models import BackendConfig


def test_web_tool_definitions_expose_search_and_fetch():
    names = [tool["function"]["name"] for tool in WEB_TOOL_DEFINITIONS]

    assert names == ["web_search", "fetch_web_page"]


def test_extract_text_drops_scripts_and_keeps_readable_content():
    raw = (
        "<html><body><script>bad()</script><h1>Title</h1>"
        "<p>Hello world</p></body></html>"
    )

    text = _extract_text(raw)

    assert "Title" in text
    assert "Hello world" in text
    assert "bad()" not in text


@pytest.mark.anyio
async def test_private_and_internal_urls_are_blocked():
    assert await _is_safe_public_url("http://localhost:8000/health") is False  # sec-ok: http-url - negative SSRF test case
    assert await _is_safe_public_url("http://127.0.0.1/") is False  # sec-ok: http-url - negative SSRF test case
    assert await _is_safe_public_url("http://searxng:8080/") is False  # sec-ok: http-url - negative SSRF test case
    assert await _is_safe_public_url("http://169.254.169.254/latest/meta-data/") is False  # sec-ok: http-url - negative SSRF test case
    assert await _is_safe_public_url("ftp://example.com/file") is False


@pytest.mark.anyio
async def test_web_search_tool_returns_structured_results(monkeypatch):
    async def fake_search(query, *, limit=5):
        return [{"title": "Polycam", "url": "https://learn.poly.cam/x", "snippet": "guide"}]

    monkeypatch.setattr(
        "control_plane.scheduler.web_research_tools.search_web_results",
        fake_search,
    )

    raw = await execute_web_tool("web_search", {"query": "phone 3d scanning"})

    assert '"title": "Polycam"' in raw
    assert '"url": "https://learn.poly.cam/x"' in raw


@pytest.mark.anyio
async def test_web_research_loop_runs_model_requested_tools(monkeypatch):
    scheduler = Scheduler(
        bot_registry=AsyncMock(),
        worker_registry=AsyncMock(),
        key_vault=AsyncMock(),
    )
    calls = {"count": 0}

    async def fake_call(_backend, messages, *, tools=None, task=None):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "output": "",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "name": "web_search",
                        "arguments": {"query": "phone 3d scanning"},
                    }
                ],
                "usage": {},
            }
        return {
            "output": "Answer with https://learn.poly.cam/x",
            "tool_calls": [],
            "usage": {},
        }

    async def fake_execute(name, arguments):
        return '{"results":[{"title":"Polycam","url":"https://learn.poly.cam/x"}]}'

    monkeypatch.setattr(scheduler, "_call_backend_raw", fake_call)
    monkeypatch.setattr(
        "control_plane.scheduler.web_research_tools.execute_web_tool",
        fake_execute,
    )

    backend = BackendConfig(
        type="cloud_api",
        model="glm-5.3-flash:cloud",
        provider="ollama_cloud",
    )
    result = await scheduler._run_web_research_loop(
        backend,
        [{"role": "user", "content": "research phone 3d scanning"}],
    )

    assert result["output"].startswith("Answer with")
    assert result["tool_calls_executed"][0]["name"] == "web_search"
    assert calls["count"] == 2
