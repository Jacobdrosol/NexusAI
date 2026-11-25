from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from control_plane.api.chat import _messages_to_payload
from control_plane.scheduler.scheduler import Scheduler
from shared.models import BackendConfig


def _message(role: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, metadata=None)


def test_web_context_is_split_from_private_context():
    payload = _messages_to_payload(
        [_message("user", "best budget 3d scanner")],
        context_items=[
            "[web:example.com] Scanner guide\nURL: https://example.com/guide",
            "private repository snippet",
        ],
    )

    system_contents = [item["content"] for item in payload if item.get("role") == "system"]
    assert any(text.startswith("Current web context:\n[web:example.com]") for text in system_contents)
    assert any(text.startswith("Context:\nprivate repository snippet") for text in system_contents)


@pytest.mark.anyio
async def test_redact_policy_keeps_public_web_context(monkeypatch):
    scheduler = Scheduler(bot_registry=AsyncMock(), worker_registry=AsyncMock(), key_vault=AsyncMock())
    backend = BackendConfig(
        type="cloud_api",
        model="glm-5.3-flash:cloud",
        provider="ollama_cloud",
    )
    payload = [
        {"role": "system", "content": "Context:\nprivate repository snippet"},
        {"role": "system", "content": "Current web context:\n[web:example.com] guide"},
        {"role": "user", "content": "hello"},
    ]
    monkeypatch.setenv("NEXUSAI_CLOUD_CONTEXT_POLICY", "redact")

    result = await scheduler._apply_cloud_context_policy(backend, payload)

    assert result[0]["content"] == "Context:\n[REDACTED_BY_POLICY]"
    assert result[1]["content"].startswith("Current web context:\n")
